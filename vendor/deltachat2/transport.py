"""JSON-RPC transports to communicate with Delta Chat core."""

import itertools
import json
import logging
import os
import subprocess
import sys
from abc import ABC, abstractmethod
from queue import Queue
from threading import Event, Thread
from typing import Any, Iterator, Optional

from ._utils import to_attrdict


class JsonRpcError(Exception):
    """An error occurred in your request to the JSON-RPC API."""


class RpcTransport(ABC):
    """Delta Chat RPC client's transport."""

    @abstractmethod
    def call(self, method: str, *args) -> Any:
        """Request the RPC server to call a function and return its return value if any."""


# Local divergence from upstream deltachat2: see the "vendored deltachat2" note
# in README.md. This file carries patches (dead-server handling here, to_attrdict
# on results, close() guards) that a naive re-vendor would silently drop.

_DISCONNECTED_ERROR = {"error": {"code": -1, "message": "RPC server disconnected"}}


class _Result(Event):
    def __init__(self) -> None:
        self._value: Any = None
        super().__init__()

    def set(self, value: Any) -> None:  # noqa
        self._value = value
        super().set()

    def wait(self, timeout: Optional[float] = None) -> bool:  # noqa
        """Block until a value is set; return True if set, False on timeout.

        Read the value from ``._value`` after a True return.
        """
        return super().wait(timeout)


class IOTransport:
    """Delta Chat RPC transport over IO using external deltachat-rpc-server program."""

    def __init__(self, accounts_dir: Optional[str] = None, rpc_server: str = "deltachat-rpc-server", **kwargs):
        """The given arguments will be passed to subprocess.Popen()"""
        self.logger = logging.getLogger("deltachat2.IOTransport")
        self._rpc_server = rpc_server
        if accounts_dir:
            kwargs["env"] = {
                **kwargs.get("env", os.environ),
                "DC_ACCOUNTS_PATH": str(accounts_dir),
            }

        self._kwargs = kwargs
        self.process: subprocess.Popen
        self.id_iterator: Iterator[int]
        # Map from request ID to the result.
        self.pending_results: dict[int, _Result]
        self.request_queue: Queue
        self.closing: bool
        self.reader_thread: Thread
        self.writer_thread: Thread

    def start(self) -> None:
        """Start the RPC server process."""
        if sys.version_info >= (3, 11):
            # Prevent subprocess from capturing SIGINT.
            kwargs = {"process_group": 0, **self._kwargs}
        else:
            # `process_group` is not supported before Python 3.11.
            kwargs = {"preexec_fn": os.setpgrp, **self._kwargs}  # noqa: PLW1509
        self.process = subprocess.Popen(  # pylint:disable=consider-using-with
            self._rpc_server,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            **kwargs,
        )
        self.id_iterator = itertools.count(start=1)
        self.pending_results = {}
        self.request_queue = Queue()
        self.closing = False
        self.reader_thread = Thread(target=self._reader_loop)
        self.reader_thread.start()
        self.writer_thread = Thread(target=self._writer_loop)
        self.writer_thread.start()

    def close(self) -> None:
        """Terminate RPC server process and wait until the reader loop finishes."""
        if not hasattr(self, "process"):
            return  # start() was never called or failed before process was created
        self.closing = True
        try:
            self.call("stop_io_for_all_accounts")
        except Exception:
            pass
        assert self.process.stdin
        self.process.stdin.close()
        self.reader_thread.join()
        self.request_queue.put(None)
        self.writer_thread.join()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()

    def _fail_all_pending(self) -> None:
        """Resolve every waiting caller with the disconnect error.

        Called from both the reader and writer loop when the RPC server dies, so
        no caller blocks forever on a request that will never be answered. The
        two callers may race on the dict, but both set the same error, so the
        outcome is identical either way.
        """
        for pending in list(self.pending_results.values()):
            pending.set(_DISCONNECTED_ERROR)
        self.pending_results.clear()

    def _server_dead(self) -> bool:
        """True once the RPC server subprocess has exited (any exit code)."""
        return hasattr(self, "process") and self.process.poll() is not None

    def _reader_loop(self) -> None:
        try:
            assert self.process.stdout
            while True:
                line = self.process.stdout.readline()
                if not line:  # EOF
                    break
                response = json.loads(line)
                if "id" in response:
                    self.pending_results.pop(response["id"]).set(response)
                else:
                    self.logger.warning("Got a response without ID: %s", response)
        except Exception:
            # Log an exception if the reader loop dies.
            self.logger.exception("Exception in the reader loop")
        finally:
            self._fail_all_pending()

    def _writer_loop(self) -> None:
        """Writer loop ensuring only a single thread writes requests."""
        try:
            assert self.process.stdin
            while True:
                request = self.request_queue.get()
                if not request:
                    break
                data = (json.dumps(request) + "\n").encode()
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except Exception:
            # Log an exception if the writer loop dies.
            self.logger.exception("Exception in the writer loop")
        finally:
            # A dead writer means no queued request will ever be sent; wake
            # every caller instead of letting them block forever.
            self._fail_all_pending()

    def call(self, method: str, *args) -> Any:
        """Request the RPC server to call a function and return its return value if any."""
        if self._server_dead():
            raise JsonRpcError(_DISCONNECTED_ERROR["error"])

        request_id = next(self.id_iterator)
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": args,
            "id": request_id,
        }
        # Log the request for debugging
        self.logger.debug(f"RPC request: method={method}, params={args}")

        result = self.pending_results[request_id] = _Result()
        self.request_queue.put(request)
        # Poll in short slices instead of an untimed wait: a legitimate slow
        # call (network round-trip) keeps waiting as long as the server is
        # alive, but a server that dies mid-call surfaces as an error within a
        # slice instead of hanging this thread forever.
        while not result.wait(timeout=1.0):
            if self._server_dead():
                self.pending_results.pop(request_id, None)
                raise JsonRpcError(_DISCONNECTED_ERROR["error"])
        response = result._value

        # Log the response for debugging
        self.logger.debug(f"RPC response for {method}: {response}")

        if "error" in response:
            raise JsonRpcError(response["error"])
        if "result" in response:
            return to_attrdict(response["result"])
        return None
