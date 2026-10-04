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

    th = threading.Thread(target=run)
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
