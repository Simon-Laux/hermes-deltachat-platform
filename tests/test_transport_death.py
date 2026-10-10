"""Regression tests for a dead deltachat-rpc-server no longer hanging callers.

Before the fix, IOTransport.call() did a bare threading.Event.wait() with no
timeout: once the RPC subprocess died, the writer loop stopped and every
subsequent call blocked forever. See the "vendored deltachat2" note in README.
"""

import logging
import queue
import threading
import time

import adapter  # noqa: F401  (inserts vendor/ onto sys.path)
from deltachat2.transport import IOTransport, JsonRpcError, _Result


class _FakeProc:
    """Minimal stand-in for the Popen the transport would normally own."""

    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


def _bare_transport(returncode=None):
    t = IOTransport.__new__(IOTransport)
    t.logger = logging.getLogger("test.transport")
    t.process = _FakeProc(returncode)
    t.id_iterator = iter(range(1, 1_000_000))
    t.pending_results = {}
    t.request_queue = queue.Queue()
    return t


def test_call_fast_fails_when_server_already_dead():
    t = _bare_transport(returncode=0)  # clean exit still counts as dead
    try:
        t.call("get_next_event")
        assert False, "expected JsonRpcError"
    except JsonRpcError as e:
        assert "disconnected" in str(e).lower()


def test_call_raises_when_server_dies_mid_wait():
    t = _bare_transport(returncode=None)  # alive at call time

    result = {}

    def run():
        try:
            t.call("get_next_event")
            result["outcome"] = "returned"
        except JsonRpcError:
            result["outcome"] = "raised"

    th = threading.Thread(target=run, daemon=True)  # never hang pytest on failure
    th.start()
    # Nothing drains request_queue, so the call is genuinely blocked in wait().
    time.sleep(0.2)
    assert th.is_alive(), "call should still be blocked while server is alive"

    t.process.returncode = -9  # server dies
    th.join(timeout=5)
    assert not th.is_alive(), "call must not hang after the server dies"
    assert result["outcome"] == "raised"
    assert t.pending_results == {}, "the abandoned request must be cleared"


def test_writer_loop_failure_wakes_pending_callers():
    t = _bare_transport(returncode=None)
    r = _Result()
    t.pending_results = {1: r}

    # Simulate the writer loop's finally path firing on BrokenPipeError.
    t._fail_all_pending()

    assert r._value["error"]["message"] == "RPC server disconnected"
    assert t.pending_results == {}


def test_reply_written_just_before_exit_is_not_reported_as_failure():
    t = _bare_transport(returncode=None)
    result = {}

    def run():
        result["value"] = t.call("send_msg")

    th = threading.Thread(target=run, daemon=True)
    th.start()
    time.sleep(0.2)
    # The server answered, then exited before the reader delivered the reply.
    t.process.returncode = 0
    time.sleep(1.1)  # past the slice in which call() notices the dead server
    t.pending_results.pop(1).set({"result": 42})
    th.join(timeout=5)
    assert not th.is_alive()
    assert result["value"] == 42


def test_dead_writer_thread_counts_as_dead_server():
    t = _bare_transport(returncode=None)
    t.writer_thread = threading.Thread(target=lambda: None)
    t.writer_thread.start()
    t.writer_thread.join()
    try:
        t.call("get_next_event")
        assert False, "expected JsonRpcError"
    except JsonRpcError as e:
        assert "disconnected" in str(e).lower()


def test_reader_tolerates_reply_for_abandoned_call():
    import io

    t = _bare_transport(returncode=None)
    survivor = _Result()
    t.pending_results = {2: survivor}
    # Reply 1 belongs to a call() that already gave up; reply 2 must still land.
    t.process.stdout = io.BytesIO(b'{"id": 1, "result": 1}\n{"id": 2, "result": 2}\n')
    t._reader_loop()
    assert survivor._value == {"id": 2, "result": 2}


def test_reader_survives_json_that_is_not_an_object():
    import io

    t = _bare_transport(returncode=None)
    survivor = _Result()
    t.pending_results = {1: survivor}
    # Valid JSON, but not an object: must be logged and skipped, not fatal.
    t.process.stdout = io.BytesIO(b'42\n[1, 2]\n"x"\nnull\n{"id": 1, "result": 1}\n')
    t._reader_loop()
    assert survivor._value == {"id": 1, "result": 1}


def test_reader_failure_retires_a_live_server():
    """A malformed line kills the reader; the process must not stay 'alive'."""
    import io
    from unittest.mock import MagicMock

    t = _bare_transport(returncode=None)
    t.closing = False
    t.process = MagicMock()
    t.process.stdout = io.BytesIO(b"not json\n")
    pending = _Result()
    t.pending_results = {1: pending}
    t._reader_loop()
    t.process.kill.assert_called_once()
    assert pending._value["error"]["message"] == "RPC server disconnected"


def test_bounded_call_times_out_and_forgets_the_request():
    t = _bare_transport(returncode=None)  # alive, but nothing ever answers
    start = time.monotonic()
    try:
        t._call("stop_io_for_all_accounts", (), timeout=0.3)
        assert False, "expected JsonRpcError"
    except JsonRpcError as e:
        assert "timed out" in str(e)
    assert time.monotonic() - start < 2
    assert t.pending_results == {}


# Real subprocesses standing in for deltachat-rpc-server in the close() tests.
_ANSWERS_THEN_EXITS_ON_EOF = (
    "import json, sys\n"
    "for line in sys.stdin:\n"
    "    req = json.loads(line)\n"
    "    print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': None}), flush=True)\n"
)
_WEDGED = "import time\nwhile True: time.sleep(1)\n"
# Stops reading stdin but stays alive, so the writer dies on EPIPE.
_CLOSES_STDIN_AND_HANGS = "import os, time\nos.close(0)\nwhile True: time.sleep(1)\n"

# Hard cap per close() test, so a regression fails instead of hanging CI
# (pytest-timeout isn't available in either test environment).
_CLOSE_DEADLINE = 15


def _start(code):
    import sys

    t = IOTransport(rpc_server=[sys.executable, "-c", code])
    t.start()
    return t


def _close_within_deadline(t, **close_kwargs):
    """Run t.close() in a thread; return its duration, failing past the deadline."""
    errors = []

    def run():
        try:
            t.close(**close_kwargs)
        except BaseException as e:  # noqa: BLE001  (surfaced by the assert below)
            errors.append(e)

    closer = threading.Thread(target=run, daemon=True)
    start = time.monotonic()
    closer.start()
    closer.join(_CLOSE_DEADLINE)
    elapsed = time.monotonic() - start
    try:
        assert not closer.is_alive(), f"close() still running after {_CLOSE_DEADLINE}s"
        assert not errors, f"close() raised {errors[0]!r}"
    except AssertionError:
        # The transport's reader thread isn't a daemon: a child left alive
        # would keep the whole pytest run from exiting.
        t.process.kill()
        raise
    return elapsed


def test_close_lets_a_healthy_server_exit_on_eof():
    t = _start(_ANSWERS_THEN_EXITS_ON_EOF)
    elapsed = _close_within_deadline(t, timeout=5)
    assert t.process.returncode == 0
    assert elapsed < 4
    assert not t.reader_thread.is_alive() and not t.writer_thread.is_alive()


def test_close_kills_a_wedged_server_without_sigterm():
    """After stdin EOF, SIGTERM hits the same cancel token in rpc-server, so close() goes straight to SIGKILL."""
    t = _start(_WEDGED)
    elapsed = _close_within_deadline(t, timeout=0.5, stop_io_timeout=0.5)
    assert t.process.returncode == -9
    assert elapsed < 5
    assert not t.reader_thread.is_alive() and not t.writer_thread.is_alive()


def test_close_survives_a_broken_stdin_pipe():
    """A writer that died on EPIPE leaves data buffered; closing stdin then
    raises BrokenPipeError, which used to escape close() and skip the kill."""
    t = _start(_CLOSES_STDIN_AND_HANGS)
    time.sleep(0.5)  # let the child close its stdin
    t.request_queue.put({"jsonrpc": "2.0", "method": "x", "params": [], "id": 0})
    t.writer_thread.join(5)
    assert not t.writer_thread.is_alive(), "writer should have died on EPIPE"

    _close_within_deadline(t, timeout=0.5, stop_io_timeout=0.5)

    assert t.process.returncode == -9
