"""Lifecycle: how a dead event listener is reported, and how teardown behaves.

Hermes owns supervision — _handle_adapter_fatal_error drops the adapter and
_platform_reconnect_watcher rebuilds a fresh one with backoff. The adapter's
whole job is to say "I am done" through the fatal-error contract. These tests
pin that contract, and the teardown paths that used to skip it.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from adapter import DeltaChatAdapter


@pytest.fixture
def adapter(platform_config):
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = AsyncMock()
    return a


class TestListenerDeathEscalates:
    """Note: self._running is *also* the base class's is_connected, so the only
    way the while-loop condition goes false is a deliberate teardown. The
    realistic death paths are therefore an exception escaping the loop, or a
    cancellation that didn't come from disconnect()."""

    @pytest.mark.asyncio
    async def test_cancellation_while_connected_is_fatal_and_retryable(self, adapter):
        """Cancelled without going through disconnect() -> we are deaf."""
        adapter._mark_connected()
        adapter.rpc.get_next_event = AsyncMock(side_effect=asyncio.CancelledError())

        await adapter._event_listener()

        assert adapter.has_fatal_error is True
        assert adapter.fatal_error_code == "event_listener_stopped"
        # Retryable: the gateway should rebuild us, not give up on the platform.
        assert adapter.fatal_error_retryable is True

    @pytest.mark.asyncio
    async def test_gateway_handler_is_notified(self, adapter):
        adapter._mark_connected()
        handler = AsyncMock()
        adapter.set_fatal_error_handler(handler)
        adapter.rpc.get_next_event = AsyncMock(side_effect=asyncio.CancelledError())

        await adapter._event_listener()
        await asyncio.sleep(0)  # the notify is fired as its own task

        handler.assert_awaited_once_with(adapter)

    @pytest.mark.asyncio
    async def test_ordinary_errors_do_not_escalate(self, adapter):
        """A transient RPC error is retried in-loop, not reported as fatal."""
        adapter._mark_connected()
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise OSError("transient")
            adapter._running = False  # stop the loop so the test terminates
            raise asyncio.CancelledError()

        adapter.rpc.get_next_event = AsyncMock(side_effect=flaky)
        with patch("asyncio.sleep", AsyncMock()):
            await adapter._event_listener()

        assert len(calls) == 3
        # The loop condition went false, i.e. a deliberate stop -> no escalation.
        assert adapter.has_fatal_error is False

    @pytest.mark.asyncio
    async def test_deliberate_disconnect_does_not_escalate(self, adapter):
        """Tearing down on purpose must not look like a crash to the gateway."""
        adapter._mark_connected()
        adapter._cleanup()  # clears is_connected, as a real disconnect would

        await adapter._event_listener()

        assert adapter.has_fatal_error is False

    @pytest.mark.asyncio
    async def test_a_raising_listener_still_escalates(self, adapter):
        """The finally must fire even when the loop body blows up."""
        adapter._mark_connected()
        adapter.rpc.get_next_event = AsyncMock(side_effect=BaseException("boom"))

        with pytest.raises(BaseException, match="boom"):
            await adapter._event_listener()

        assert adapter.fatal_error_code == "event_listener_stopped"

    @pytest.mark.asyncio
    async def test_no_self_restart(self, adapter):
        """We must not resurrect the listener ourselves.

        An adapter-side supervisor races the gateway's reconnect watcher, which
        builds a *fresh* adapter — the orphaned old one keeps the RPC subprocess
        and accounts-dir lock alive and blocks its replacement.
        """
        adapter._mark_connected()
        adapter.rpc.get_next_event = AsyncMock(side_effect=asyncio.CancelledError())

        await adapter._event_listener()
        # Give any (unwanted) restart task a chance to run and poll again.
        for _ in range(5):
            await asyncio.sleep(0)

        assert adapter.rpc.get_next_event.await_count == 1
        assert adapter._event_loop_task is None


class TestCleanupReportsStatus:
    def test_cleanup_marks_disconnected(self, adapter):
        """A failed connect() used to leave a stale 'connected' status behind."""
        adapter._mark_connected()
        adapter._cleanup()

        assert adapter.is_connected is False
        assert adapter._disconnected is True

    def test_cleanup_does_not_downgrade_a_fatal_error(self, adapter):
        """'fatal'/'retrying' must survive the cleanup that follows it."""
        adapter._mark_connected()
        adapter._set_fatal_error("event_listener_stopped", "x", retryable=True)

        adapter._cleanup()

        assert adapter.has_fatal_error is True
        assert adapter._disconnected is False

    def test_cleanup_closes_the_transport(self, adapter):
        transport = MagicMock()
        adapter._transport = transport

        adapter._cleanup()

        transport.close.assert_called_once()
        assert adapter._transport is None
        assert adapter.rpc is None


class TestDisconnectIsResilient:
    @pytest.mark.asyncio
    async def test_teardown_failure_still_cleans_up(self, adapter):
        """A raising teardown used to skip _cleanup entirely, leaking the
        RPC subprocess and the accounts-dir lock."""
        adapter._mark_connected()
        transport = MagicMock()
        adapter._transport = transport
        adapter._call_manager = MagicMock()
        adapter._call_manager.teardown = AsyncMock(side_effect=Exception("nope"))

        await adapter.disconnect()

        transport.close.assert_called_once()
        assert adapter.rpc is None
        assert adapter.is_connected is False

    @pytest.mark.asyncio
    async def test_normal_disconnect_tears_down_the_call_manager(self, adapter):
        adapter._mark_connected()
        adapter._call_manager = MagicMock()
        adapter._call_manager.teardown = AsyncMock()

        await adapter.disconnect()

        adapter._call_manager is None
        assert adapter.is_connected is False


class TestListenerDoneCallback:
    def test_exception_is_retrieved_and_logged(self, adapter, caplog):
        """Otherwise asyncio only reports it whenever the GC gets around to it."""
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("listener blew up")

        with caplog.at_level("ERROR"):
            DeltaChatAdapter._on_listener_done(task)

        task.exception.assert_called_once()
        assert "listener blew up" in caplog.text

    def test_cancellation_is_not_an_error(self, adapter, caplog):
        task = MagicMock()
        task.cancelled.return_value = True

        with caplog.at_level("ERROR"):
            DeltaChatAdapter._on_listener_done(task)

        assert caplog.text == ""
        task.exception.assert_not_called()
