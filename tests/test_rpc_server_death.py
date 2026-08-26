"""What happens when the deltachat-rpc-server subprocess dies underneath us.

The vendored transport fails in a way retries cannot survive: on EOF its reader
thread resolves every in-flight call with "RPC server disconnected" and clears
the pending map, so the *next* call enqueues a request nobody will answer and
blocks forever (_Result.wait() is an untimed threading.Event.wait()). One error,
then permanent silence — with is_connected still True.

So the single error is the only window in which we can notice. These tests pin
that window shut.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from adapter import DeltaChatAdapter


@pytest.fixture
def adapter(platform_config):
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = AsyncMock()
    a._transport = MagicMock()
    a._mark_connected()
    return a


def _server(adapter, *, exit_code):
    """Point the adapter's transport at a live (None) or dead subprocess."""
    adapter._transport.process.poll.return_value = exit_code


class TestExitCodeProbe:
    def test_alive_server_reports_none(self, adapter):
        _server(adapter, exit_code=None)
        assert adapter._rpc_server_exit_code() is None

    def test_dead_server_reports_its_code(self, adapter):
        _server(adapter, exit_code=1)
        assert adapter._rpc_server_exit_code() == 1

    def test_exit_code_zero_still_counts_as_dead(self, adapter):
        """A clean exit is still an exit — poll() returning 0 is not 'alive'."""
        _server(adapter, exit_code=0)
        assert adapter._rpc_server_exit_code() == 0

    def test_unstarted_transport_is_not_dead(self, adapter):
        """IOTransport only binds .process in start(); absent != dead."""
        adapter._transport = object()  # no .process attribute
        assert adapter._rpc_server_exit_code() is None

    def test_no_transport_at_all_is_not_dead(self, adapter):
        adapter._transport = None
        assert adapter._rpc_server_exit_code() is None


class TestTransientErrorsStillRetry:
    @pytest.mark.asyncio
    async def test_live_server_keeps_polling(self, adapter):
        _server(adapter, exit_code=None)
        with patch("asyncio.sleep", AsyncMock()) as sleep:
            assert await adapter._handle_listener_error(OSError("blip")) is True
        sleep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_live_server_does_not_escalate(self, adapter):
        _server(adapter, exit_code=None)
        with patch("asyncio.sleep", AsyncMock()):
            await adapter._handle_listener_error(OSError("blip"))
        assert adapter.has_fatal_error is False

    @pytest.mark.asyncio
    async def test_loop_survives_a_transient_error(self, adapter):
        _server(adapter, exit_code=None)
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("blip")
            raise asyncio.CancelledError()

        adapter.rpc.get_next_event = AsyncMock(side_effect=flaky)
        with patch("asyncio.sleep", AsyncMock()):
            await adapter._event_listener()

        assert len(calls) == 2  # retried rather than bailing out


class TestDeadServerStopsAndEscalates:
    @pytest.mark.asyncio
    async def test_stops_polling(self, adapter):
        _server(adapter, exit_code=1)
        assert await adapter._handle_listener_error(OSError("disconnected")) is False

    @pytest.mark.asyncio
    async def test_marks_a_retryable_fatal_error(self, adapter):
        _server(adapter, exit_code=1)
        await adapter._handle_listener_error(OSError("disconnected"))

        assert adapter.fatal_error_code == "rpc_server_died"
        # Retryable: rebuilding the adapter respawns the RPC server, so this is
        # recoverable — the gateway must not write the platform off.
        assert adapter.fatal_error_retryable is True
        assert "1" in adapter.fatal_error_message

    @pytest.mark.asyncio
    async def test_notifies_the_gateway(self, adapter):
        _server(adapter, exit_code=1)
        handler = AsyncMock()
        adapter.set_fatal_error_handler(handler)

        await adapter._handle_listener_error(OSError("disconnected"))
        await asyncio.sleep(0)  # the notify runs as its own task

        handler.assert_awaited_once_with(adapter)

    @pytest.mark.asyncio
    async def test_listener_exits_instead_of_hanging(self, adapter):
        """The regression this PR exists for: one error, then never call again."""
        _server(adapter, exit_code=1)
        adapter.rpc.get_next_event = AsyncMock(side_effect=OSError("disconnected"))

        await asyncio.wait_for(adapter._event_listener(), timeout=1)

        # Exactly one poll: a second would be the call that blocks forever.
        assert adapter.rpc.get_next_event.await_count == 1

    @pytest.mark.asyncio
    async def test_does_not_escalate_during_a_deliberate_teardown(self, adapter):
        """Stopping the server ourselves must not look like a crash."""
        _server(adapter, exit_code=0)
        adapter._running = False  # as _cleanup() would have left it

        assert await adapter._handle_listener_error(OSError("closed")) is False
        assert adapter.has_fatal_error is False
