"""Integration tests for Delta Chat adapter.

Tests the adapter with mocked Hermes gateway classes.
"""

import asyncio
import json
import os
import signal
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import pytest

# The conftest.py already installs the mocks, so we can import adapter now
import adapter
from adapter import (
    DeltaChatAdapter,
    _parse_version,
    _check_dc_version,
    _check_dc2_available,
    MIN_DC_VERSION,
)


class TestConfigDirectoryIntegration:
    """Test config directory integration with mocked Hermes."""

    def test_dc_config_dir_uses_hermes_home(self, platform_config):
        """Test that _get_dc_config_dir uses HERMES_HOME correctly."""
        adapter = DeltaChatAdapter(platform_config)
        config_dir = adapter._get_dc_config_dir()

        # MockHermesConfig.get_hermes_home returns a default path
        # The adapter should append "deltachat-platform" to it
        from tests.conftest import MockHermesConfig

        expected_home = MockHermesConfig.get_hermes_home()
        expected = os.path.join(expected_home, "deltachat-platform")
        assert config_dir == expected

    def test_dc_config_dir_creates_directory(self, platform_config, tmp_path):
        """Test that _get_dc_config_dir creates the directory if it doesn't exist."""
        # Set a custom HERMES_HOME for this test
        test_home = str(tmp_path / "hermes")
        import os

        os.environ["HERMES_HOME"] = test_home

        # Clear the cached config dir
        adapter = DeltaChatAdapter(platform_config)
        adapter._dc_config_dir = None

        config_dir = adapter._get_dc_config_dir()
        expected_dir = os.path.join(test_home, "deltachat-platform")

        assert os.path.exists(config_dir)
        assert os.path.isdir(config_dir)
        assert config_dir == expected_dir


class TestRPCServerPath:
    """Test RPC server path resolution."""

    def test_default_rpc_path(self, platform_config):
        """Test default RPC server path."""
        adapter = DeltaChatAdapter(platform_config)
        path = adapter._get_rpc_server_path()
        assert path == "deltachat-rpc-server"

    def test_rpc_path_from_config(self, platform_config):
        """Test RPC server path from config.extra."""
        platform_config.extra = {"rpc_server": "/custom/path/to/rpc"}
        adapter = DeltaChatAdapter(platform_config)
        path = adapter._get_rpc_server_path()
        assert path == "/custom/path/to/rpc"

    def test_rpc_path_from_env(self, platform_config, monkeypatch):
        """Test RPC server path from DELTACHAT_RPC_SERVER env."""
        monkeypatch.setenv("DELTACHAT_RPC_SERVER", "/env/path/to/rpc")
        platform_config.extra = {}  # Clear config
        adapter = DeltaChatAdapter(platform_config)
        path = adapter._get_rpc_server_path()
        assert path == "/env/path/to/rpc"

    def test_rpc_path_precedence_config_over_env(self, platform_config, monkeypatch):
        """Test that config.extra takes precedence over env."""
        monkeypatch.setenv("DELTACHAT_RPC_SERVER", "/env/path")
        platform_config.extra = {"rpc_server": "/config/path"}
        adapter = DeltaChatAdapter(platform_config)
        path = adapter._get_rpc_server_path()
        assert path == "/config/path"


class TestVersionCheckIntegration:
    """Test version check with mocked RPC."""

    @pytest.mark.asyncio
    async def test_version_compatible(self, mock_rpc):
        """Test version check with compatible version."""
        mock_rpc.get_system_info = AsyncMock(
            return_value={"deltachat_core_version": MIN_DC_VERSION}
        )
        result = await _check_dc_version(mock_rpc)
        assert result is True

    @pytest.mark.asyncio
    async def test_version_too_old(self, mock_rpc, caplog):
        """Test version check with too old version."""
        mock_rpc.get_system_info = AsyncMock(
            return_value={"deltachat_core_version": "1.0.0"}
        )
        with caplog.at_level("ERROR"):
            result = await _check_dc_version(mock_rpc)
        assert result is False
        assert "too old" in caplog.text

    @pytest.mark.asyncio
    async def test_version_newer_warns(self, mock_rpc, caplog):
        """Test version check with newer version warns but allows."""
        mock_rpc.get_system_info = AsyncMock(
            return_value={"deltachat_core_version": "3.0.0"}
        )
        with caplog.at_level("WARNING"):
            result = await _check_dc_version(mock_rpc)
        assert result is True
        assert "newer than" in caplog.text


class TestSendMessage:
    """Test message sending functionality."""

    @pytest.mark.asyncio
    async def test_send_text_message_success(self, platform_config, mock_rpc):
        """Test successful text message sending."""
        # Setup adapter with mocked state
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = False
        adapter._mark_connected = Mock()
        adapter._mark_disconnected = Mock()
        adapter.build_source = Mock()
        adapter.handle_message = AsyncMock()

        # Mock RPC send_msg to return message ID
        mock_rpc.send_msg = AsyncMock(return_value=123)

        from adapter import SendResult

        result = await adapter.send("789", "Hello World")

        assert result.success is True
        assert result.message_id == "123"
        mock_rpc.send_msg.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_not_connected(self, platform_config):
        """Test sending fails when not connected."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = None
        adapter.account_id = None

        from adapter import SendResult

        result = await adapter.send("789", "Hello")

        assert result.success is False
        assert "not connected" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_file_success(self, platform_config, mock_rpc):
        """Test successful file sending."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.send_msg = AsyncMock(return_value=456)

        from adapter import SendResult

        result = await adapter.send_file("789", "/path/to/file.xdc", "A file")

        assert result.success is True
        assert result.message_id == "456"
        mock_rpc.send_msg.assert_called_once()


class TestGetChatInfo:
    """Test chat info retrieval."""

    @pytest.mark.asyncio
    async def test_get_chat_info_success(self, platform_config, mock_rpc):
        """Test successful chat info retrieval."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_id": 789, "name": "Test Chat", "chat_type": "Single"}
        )

        result = await adapter.get_chat_info("789")

        assert result["name"] == "Test Chat"
        assert result["type"] == "dm"

    @pytest.mark.asyncio
    async def test_get_chat_info_group(self, platform_config, mock_rpc):
        """Test chat info for group chat."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_id": 789, "name": "Group Chat", "chat_type": "Group"}
        )

        result = await adapter.get_chat_info("789")

        assert result["name"] == "Group Chat"
        assert result["type"] == "group"

    @pytest.mark.asyncio
    async def test_get_chat_info_fallback(self, platform_config, mock_rpc):
        """Test chat info fallback on error."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_basic_chat_info = AsyncMock(side_effect=Exception("RPC error"))

        result = await adapter.get_chat_info("789")

        assert result["name"] == "789"
        assert result["type"] == "dm"


class TestEventHandling:
    """Test event handling logic."""

    @pytest.mark.asyncio
    async def test_handle_incoming_message_event(self, platform_config, mock_rpc):
        """Test handling of INCOMING_MSG event."""
        from deltachat2.types import EventType

        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        # Open DM policy so the test contact is not rejected for being unverified.
        adapter._dm_policy = "open"

        mock_rpc.get_message = AsyncMock(
            return_value={
                "msg_id": 123,
                "text": "Test message",
                "from_id": 456,
                "timestamp": 1234567890,
                "view_type": "Text",
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_id": 789, "name": "Test Chat", "chat_type": "Single"}
        )
        mock_rpc.get_contact = AsyncMock(
            return_value={
                "contact_id": 456,
                "name": "Test User",
                "address": "user@example.com",
            }
        )
        mock_rpc.markseen_msgs = AsyncMock(return_value=None)
        adapter._running = True
        adapter._mark_connected = Mock()
        adapter._mark_disconnected = Mock()
        adapter.handle_message = AsyncMock()

        # Process an incoming message event
        event = {"kind": EventType.INCOMING_MSG, "chat_id": 789, "msg_id": 123}
        await adapter._handle_dc_event(event)

        # Verify message was handled
        assert adapter.handle_message.called
        call_args = adapter.handle_message.call_args[0][0]
        assert "Test message" in call_args.text
        assert call_args.message_id == "123"

    @pytest.mark.asyncio
    async def test_handle_delivered_event(self, platform_config, mock_rpc, caplog):
        """Test handling of MSG_DELIVERED event."""
        from deltachat2.types import EventType

        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True

        with caplog.at_level("DEBUG"):
            event = {"kind": EventType.MSG_DELIVERED, "msg_id": 123}
            await adapter._handle_dc_event(event)

        assert "delivered" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_handle_failed_event_logs_chat_id_and_error(
        self, platform_config, mock_rpc, caplog
    ):
        """MSG_FAILED must surface chat_id and the real failure reason, not
        just a bare msg_id — that's the whole point of fetching get_message."""
        from deltachat2.types import EventType

        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True
        mock_rpc.get_message = AsyncMock(
            return_value={"error": "SMTP error: 550 mailbox unavailable"}
        )

        with caplog.at_level("WARNING"):
            event = {"kind": EventType.MSG_FAILED, "msg_id": 123, "chat_id": 13}
            await adapter._handle_dc_event(event)

        mock_rpc.get_message.assert_awaited_once_with(1, 123)
        assert "123" in caplog.text
        assert "13" in caplog.text
        assert "SMTP error: 550 mailbox unavailable" in caplog.text

    @pytest.mark.asyncio
    async def test_handle_failed_event_survives_get_message_error(
        self, platform_config, mock_rpc, caplog
    ):
        """If get_message itself fails, still log what we know instead of
        raising and losing the original MSG_FAILED warning entirely."""
        from deltachat2.types import EventType

        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True
        mock_rpc.get_message = AsyncMock(side_effect=RuntimeError("rpc down"))

        with caplog.at_level("WARNING"):
            event = {"kind": EventType.MSG_FAILED, "msg_id": 123, "chat_id": 13}
            await adapter._handle_dc_event(event)

        assert "123" in caplog.text
        assert "13" in caplog.text
        assert "unknown" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_handle_incoming_call_event(self, platform_config, mock_rpc, caplog):
        """Test handling of INCOMING_CALL event."""
        from deltachat2.types import EventType

        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True

        # Provide a mock call manager so the event is routed.
        adapter._call_manager = Mock()
        adapter._call_manager.handle_incoming_call = AsyncMock()

        with caplog.at_level("INFO"):
            event = {"kind": EventType.INCOMING_CALL}
            await adapter._handle_dc_event(event)

        assert (
            "call" in caplog.text.lower()
            or adapter._call_manager.handle_incoming_call.called
        )

    @pytest.mark.asyncio
    async def test_handle_unknown_event(self, platform_config, mock_rpc, caplog):
        """Test handling of unknown event type."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True

        with caplog.at_level("DEBUG"):
            event = {"kind": "UNKNOWN_EVENT"}
            await adapter._handle_dc_event(event)

        assert "unhandled" in caplog.text.lower()


class TestDC2Availability:
    """Test deltachat2 availability check."""

    def test_dc2_available_when_installed(self, monkeypatch):
        """Test _check_dc2_available returns True when deltachat2 is installed."""
        # Temporarily add deltachat2 to sys.modules
        import sys

        # Save original state
        original_modules = sys.modules.get("deltachat2")

        # Mock deltachat2 being available
        monkeypatch.setitem(sys.modules, "deltachat2", MagicMock())

        # Reset the cache
        import adapter

        adapter._DC2_AVAILABLE = None

        result = _check_dc2_available()
        assert result is True

        # Restore original state
        if original_modules is not None:
            sys.modules["deltachat2"] = original_modules
        elif "deltachat2" in sys.modules:
            del sys.modules["deltachat2"]

    def test_dc2_not_available_when_not_installed(self, monkeypatch):
        """Test _check_dc2_available returns False when deltachat2 is not installed."""
        import sys

        # Save original state
        original_modules = sys.modules.get("deltachat2")

        # Ensure deltachat2 is not in sys.modules
        if "deltachat2" in sys.modules:
            del sys.modules["deltachat2"]

        # Also prevent the import from working
        monkeypatch.setitem(sys.modules, "deltachat2", None)

        # Reset the cache
        import adapter

        adapter._DC2_AVAILABLE = None

        result = _check_dc2_available()
        assert result is False

        # Restore original state
        if original_modules is not None:
            sys.modules["deltachat2"] = original_modules


class TestPlainTextDelivery:
    """The outbound text path strips markdown and splits long replies into
    ordered short plain-text messages (no HTML)."""

    @pytest.mark.asyncio
    async def test_markdown_is_stripped_before_send(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        sent = []
        mock_rpc.send_msg = AsyncMock(
            side_effect=lambda acc, chat, data: sent.append(data.text) or 1
        )

        await adapter.send(
            "5",
            "# Title\n\nSome **bold** and a [link](https://x.test/a).\n" "* one\n* two",
        )

        body = "\n".join(sent)
        assert "#" not in body
        assert "**" not in body
        assert "Title" in body
        assert "bold" in body
        assert "link (https://x.test/a)" in body
        assert "- one" in body and "- two" in body

    @pytest.mark.asyncio
    async def test_long_reply_split_into_ordered_messages(
        self, platform_config, mock_rpc
    ):
        platform_config.extra = {"max_message_lines": 5}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        sent = []
        mock_rpc.send_msg = AsyncMock(
            side_effect=lambda acc, chat, data: sent.append(data.text) or len(sent)
        )

        lines = [f"line {i}" for i in range(23)]
        await adapter.send("5", "\n".join(lines))

        assert len(sent) > 1
        for part in sent:
            assert part.count("\n") + 1 <= 5
        # ordering + nothing dropped
        assert "\n".join(sent).split("\n") == lines

    @pytest.mark.asyncio
    async def test_short_reply_sent_as_single_message(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        sent = []
        mock_rpc.send_msg = AsyncMock(
            side_effect=lambda acc, chat, data: sent.append(data.text) or 1
        )

        await adapter.send("5", "just a quick reply")

        assert sent == ["just a quick reply"]

    @pytest.mark.asyncio
    async def test_only_first_chunk_carries_quote_reply(
        self, platform_config, mock_rpc
    ):
        platform_config.extra = {"max_message_lines": 3}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        quoted = []
        mock_rpc.send_msg = AsyncMock(
            side_effect=lambda acc, chat, data: quoted.append(data.quoted_message_id)
            or len(quoted)
        )

        await adapter.send("5", "\n".join(f"l{i}" for i in range(10)), reply_to="99")

        assert quoted[0] == 99
        assert all(q is None for q in quoted[1:])


class TestLocationSending:
    """Test location/POI message sending."""

    @pytest.mark.asyncio
    async def test_send_location_success(self, platform_config, mock_rpc):
        """Test successful location sending."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.send_msg = AsyncMock(return_value=123)

        from adapter import SendResult

        result = await adapter.send_location("789", 52.5200, 13.4050, "☕ Coffee")

        assert result.success is True
        assert result.message_id == "123"
        mock_rpc.send_msg.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_location_with_text_poi(self, platform_config, mock_rpc):
        """Test location sending with text POI (displays as pin)."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.send_msg = AsyncMock(return_value=456)

        from adapter import SendResult

        result = await adapter.send_location("789", 40.7128, -74.0060, "Coffee Shop")

        assert result.success is True
        assert result.message_id == "456"
        mock_rpc.send_msg.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_location_not_connected(self, platform_config):
        """Test location sending fails when not connected."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = None
        adapter.account_id = None

        from adapter import SendResult

        result = await adapter.send_location("789", 0, 0, "🏠")

        assert result.success is False
        assert "not connected" in result.error.lower()


class TestStatusAndStats:
    """Test get_status, stats counters, and lifecycle helpers."""

    def test_get_status_not_connected(self, platform_config):
        """Test status snapshot when adapter is not connected."""
        adapter = DeltaChatAdapter(platform_config)
        status = adapter.get_status()
        assert status["connected"] is False
        assert status["running"] is False
        assert status["account_addr"] is None
        assert status["crashes_last_60s"] == 0
        assert status["stats"] == {}

    def test_bump_stat(self, platform_config):
        """Test internal stats counter increments."""
        adapter = DeltaChatAdapter(platform_config)
        adapter._bump_stat("messages_sent")
        adapter._bump_stat("messages_sent", 2)
        adapter._bump_stat("messages_send_failed")
        assert adapter._stats == {"messages_sent": 3, "messages_send_failed": 1}

    @pytest.mark.asyncio
    async def test_get_status_connected(self, platform_config, mock_rpc):
        """Test status snapshot when adapter is connected."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True
        adapter._self_addr = "bot@example.com"
        adapter._event_loop_task = asyncio.create_task(asyncio.sleep(60))
        adapter._bump_stat("messages_sent")

        status = adapter.get_status()

        assert status["connected"] is True
        assert status["running"] is True
        assert status["account_addr"] == "bot@example.com"
        assert status["stats"]["messages_sent"] == 1
        adapter._event_loop_task.cancel()
        try:
            await adapter._event_loop_task
        except asyncio.CancelledError:
            pass


class TestSignalHandling:
    """Test graceful shutdown signal registration."""

    @pytest.mark.asyncio
    async def test_connect_registers_signal_handlers(
        self, platform_config, monkeypatch
    ):
        """Test that connect() registers SIGTERM/SIGINT handlers."""
        adapter = DeltaChatAdapter(platform_config)
        added = []

        def fake_add_signal_handler(sig, handler):
            added.append(sig)

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", fake_add_signal_handler)
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: True)

        adapter._loop = loop
        adapter._signal_handler()

        # We can't easily run full connect() without RPC; just verify the
        # handler can be registered via loop mock.
        loop.add_signal_handler(signal.SIGTERM, adapter._signal_handler)
        loop.add_signal_handler(signal.SIGINT, adapter._signal_handler)
        assert signal.SIGTERM in added
        assert signal.SIGINT in added

    @pytest.mark.asyncio
    async def test_disconnect_removes_signal_handlers(
        self, platform_config, monkeypatch
    ):
        """Test that disconnect() removes signal handlers."""
        adapter = DeltaChatAdapter(platform_config)
        removed = []

        def fake_remove_signal_handler(sig):
            removed.append(sig)
            return True

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "remove_signal_handler", fake_remove_signal_handler)

        adapter._loop = loop
        adapter._mark_disconnected = Mock()
        await adapter.disconnect()

        assert signal.SIGTERM in removed
        assert signal.SIGINT in removed


class TestListenerDeathEscalation:
    """A dead event listener is reported to the gateway, not restarted here.

    Hermes owns supervision: a retryable fatal error makes it drop this adapter
    and rebuild a fresh one. self._running *is* the base's is_connected, so the
    realistic death paths are an exception/cancellation escaping the loop, or
    the RPC subprocess exiting.
    """

    @pytest.fixture
    def adapter(self, platform_config):
        a = DeltaChatAdapter(platform_config)
        a.account_id = 1
        a.rpc = AsyncMock()
        a._transport = MagicMock()
        a._transport.process.poll.return_value = None  # alive by default
        return a

    @pytest.mark.asyncio
    async def test_transient_error_retries_and_does_not_escalate(self, adapter):
        adapter._mark_connected()
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise OSError("transient")
            adapter._running = False  # deliberate stop so the test terminates
            raise asyncio.CancelledError()

        adapter.rpc.get_next_event = AsyncMock(side_effect=flaky)
        with patch("asyncio.sleep", AsyncMock()):
            await adapter._event_listener()

        assert len(calls) == 3
        assert adapter.has_fatal_error is False
        assert adapter._stats.get("event_listener_errors") == 2

    @pytest.mark.asyncio
    async def test_dead_rpc_server_escalates_retryable(self, adapter):
        adapter._mark_connected()
        adapter._transport.process.poll.return_value = 1
        adapter.rpc.get_next_event = AsyncMock(side_effect=OSError("disconnected"))

        await asyncio.wait_for(adapter._event_listener(), timeout=1)

        # Exactly one poll: a second call is the one that would block forever.
        assert adapter.rpc.get_next_event.await_count == 1
        assert adapter.fatal_error_code == "rpc_server_died"
        assert adapter.fatal_error_retryable is True

    @pytest.mark.asyncio
    async def test_cancellation_while_connected_is_fatal(self, adapter):
        """Cancelled without going through disconnect() -> we are deaf."""
        adapter._mark_connected()
        adapter.rpc.get_next_event = AsyncMock(side_effect=asyncio.CancelledError())

        await adapter._event_listener()

        assert adapter.fatal_error_code == "event_listener_stopped"
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
    async def test_deliberate_disconnect_does_not_escalate(self, adapter):
        adapter._mark_connected()
        adapter._cleanup()  # clears is_connected, as a real disconnect would

        await adapter._event_listener()

        assert adapter.has_fatal_error is False

    @pytest.mark.asyncio
    async def test_no_self_restart(self, adapter):
        adapter._mark_connected()
        adapter.rpc.get_next_event = AsyncMock(side_effect=asyncio.CancelledError())

        await adapter._event_listener()
        for _ in range(5):
            await asyncio.sleep(0)  # give any unwanted restart task a chance

        assert adapter.rpc.get_next_event.await_count == 1


class TestListenerDoneCallback:
    def test_exception_is_retrieved_and_logged(self, caplog):
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("listener blew up")

        with caplog.at_level("ERROR"):
            DeltaChatAdapter._on_listener_done(task)

        task.exception.assert_called_once()
        assert "listener blew up" in caplog.text

    def test_cancellation_is_not_an_error(self, caplog):
        task = MagicMock()
        task.cancelled.return_value = True

        with caplog.at_level("ERROR"):
            DeltaChatAdapter._on_listener_done(task)

        task.exception.assert_not_called()


class TestMentions:
    """Test group mention detection and DELTACHAT_REQUIRE_MENTION."""

    @pytest.fixture
    def group_event(self):
        return {"kind": "IncomingMsg", "chat_id": 1, "msg_id": 10}

    @pytest.mark.asyncio
    async def test_require_mention_blocks_unmentioned_group_message(
        self, platform_config, mock_rpc, group_event
    ):
        platform_config.extra = {"require_mention": "true", "display_name": "Bot"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        adapter.send = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "hello there",
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "Test Group"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})

        await adapter._handle_incoming_message(group_event)

        adapter.handle_message.assert_not_called()
        adapter.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_require_mention_allows_mentioned_group_message(
        self, platform_config, mock_rpc, group_event
    ):
        platform_config.extra = {"require_mention": "true", "display_name": "Bot"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "@Bot hello there",
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "Test Group"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})

        await adapter._handle_incoming_message(group_event)

        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_require_mention_skips_commands(
        self, platform_config, mock_rpc, group_event
    ):
        platform_config.extra = {"require_mention": "true", "display_name": "Bot"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "/help",
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "Test Group"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})

        await adapter._handle_incoming_message(group_event)

        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_require_mention_applies_to_image_caption(
        self, platform_config, mock_rpc, group_event
    ):
        platform_config.extra = {"require_mention": "true", "display_name": "Bot"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "look at this",
                "view_type": "Image",
                "from_id": 11,
                "file": "/tmp/photo.jpg",
                "file_mime": "image/jpeg",
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "Test Group"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})
        adapter._resolve_blob_path = lambda x: x
        adapter._copy_to_hermes_cache = lambda src, kind: src

        await adapter._handle_incoming_message(group_event)

        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_require_mention_blocks_captionless_image(
        self, platform_config, mock_rpc, group_event
    ):
        platform_config.extra = {"require_mention": "true", "display_name": "Bot"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "",
                "view_type": "Image",
                "from_id": 11,
                "file": "/tmp/photo.jpg",
                "file_mime": "image/jpeg",
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "Test Group"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})
        adapter._resolve_blob_path = lambda x: x
        adapter._copy_to_hermes_cache = lambda src, kind: src

        await adapter._handle_incoming_message(group_event)

        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_bot_exchange_guard_applies_to_images(
        self, platform_config, mock_rpc, group_event
    ):
        platform_config.extra = {"max_bot_exchanges": 1, "display_name": "Bot"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        adapter._get_group_roster = AsyncMock(
            return_value=[{"name": "a"}, {"name": "b"}]
        )
        adapter._resolve_blob_path = lambda x: x
        adapter._copy_to_hermes_cache = lambda src, kind: src
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "",
                "view_type": "Image",
                "from_id": 11,
                "file": "/tmp/photo.jpg",
                "file_mime": "image/jpeg",
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "Test Group"}
        )
        contact = {"address": "x@example.com", "is_bot": True}
        mock_rpc.get_contact = AsyncMock(side_effect=lambda *_: contact)

        async def send(msg_id):
            await adapter._handle_incoming_message({**group_event, "msg_id": msg_id})

        await send(10)  # bot image 1: allowed
        await send(11)  # bot image 2: exceeds max_bot_exchanges=1
        assert adapter.handle_message.call_count == 1

        contact = {"address": "human@example.com", "is_bot": False}
        await send(12)  # human image resets the count and is processed
        contact = {"address": "x@example.com", "is_bot": True}
        await send(13)  # bot image counts as 1 again: allowed
        assert adapter.handle_message.call_count == 3

    @pytest.mark.asyncio
    async def test_reply_to_own_message_is_implicit_mention_for_image(
        self, platform_config, mock_rpc, group_event
    ):
        """The text path exempts a quote-reply to the bot's own message from

        mention-gating (see test_reply_to_own_message_is_implicit_mention);
        the image/file path went through a separate code path
        (_handle_non_text_message) that lacked this exemption entirely, so an
        image sent as a quote-reply to the bot in a mention-gated group (e.g.
        "here's a screenshot of what you just said") was silently dropped
        even though the equivalent text reply worked. Regression for OCEAN
        Support ignoring image replies."""
        platform_config.extra = {
            "require_mention": "true",
            "display_name": "Alice",
        }
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            side_effect=[
                {
                    "text": "here's what you just said",
                    "view_type": "Image",
                    "from_id": 11,
                    "file": "/tmp/photo.jpg",
                    "file_mime": "image/jpeg",
                    "quote": {
                        "kind": "WithMessage",
                        "text": "shall I proceed?",
                        "message_id": 99,
                        "author_display_name": "Me",
                    },
                },
                {"from_id": 1, "text": "shall I proceed?"},
            ]
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "OCEAN Support"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})
        adapter._resolve_blob_path = lambda x: x
        adapter._copy_to_hermes_cache = lambda src, kind: src

        await adapter._handle_incoming_message(group_event)

        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reply_to_own_message_is_implicit_mention(
        self, platform_config, mock_rpc, group_event
    ):
        """In a mention-gated chat, a quote-reply to the bot's own message
        (quoted from_id == DC_CONTACT_ID_SELF) is processed without an
        @mention — even though core reports the quote author_display_name as
        the localized "Me", not the configured display name."""
        platform_config.extra = {
            "require_mention": "true",
            "display_name": "Alice",
        }
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            side_effect=[
                {
                    "text": "yes do that",
                    "view_type": "Text",
                    "from_id": 11,
                    "file": None,
                    "quote": {
                        "kind": "WithMessage",
                        "text": "shall I proceed?",
                        "message_id": 99,
                        "author_display_name": "Me",
                    },
                },
                {"from_id": 1, "text": "shall I proceed?"},
            ]
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "OCEAN Support"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})

        await adapter._handle_incoming_message(group_event)

        adapter.handle_message.assert_awaited_once()
        event_arg = adapter.handle_message.await_args[0][0]
        # quoted-text splice uses the bot's real name, not the "Me" from core
        assert '[replying to Alice: "shall I proceed?"]' in event_arg.text

    @pytest.mark.asyncio
    async def test_reply_to_other_member_still_gated(
        self, platform_config, mock_rpc, group_event
    ):
        """A quote-reply to another member's message (quoted from_id != SELF)
        in a mention-gated chat is still dropped when there is no @mention."""
        platform_config.extra = {
            "require_mention": "true",
            "display_name": "Alice",
        }
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        adapter.send = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            side_effect=[
                {
                    "text": "agreed",
                    "view_type": "Text",
                    "from_id": 11,
                    "file": None,
                    "quote": {
                        "kind": "WithMessage",
                        "text": "let's ship it",
                        "message_id": 98,
                        "author_display_name": "Bob",
                    },
                },
                {"from_id": 12, "text": "let's ship it"},
            ]
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "OCEAN Support"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})

        await adapter._handle_incoming_message(group_event)

        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_reply_to_own_message_explicit_mention_still_works(
        self, platform_config, mock_rpc, group_event
    ):
        """Explicit @mention keeps working alongside the quote-reply path."""
        platform_config.extra = {
            "require_mention": "true",
            "display_name": "Alice",
        }
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "@Alice test",
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "OCEAN Support"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})

        await adapter._handle_incoming_message(group_event)

        adapter.handle_message.assert_awaited_once()


class TestLoopGuardChatScope:
    """The consecutive-reply and bot-exchange guards assume a group with a
    changing participant set ("no one else joining in" only means something
    when a third party *could* join). A DM has exactly one counterparty by
    definition, so from_id never changes turn to turn — applying the guard
    there would trip permanently after max_consecutive_replies messages with
    no way to recover. Guards must be scoped to group chats only.
    """

    async def _send_n_messages(self, adapter, chat_id, n, from_id=11):
        for msg_id in range(1, n + 1):
            await adapter._handle_incoming_message(
                {"kind": "IncomingMsg", "chat_id": chat_id, "msg_id": msg_id}
            )

    @pytest.mark.asyncio
    async def test_loop_guard_does_not_trip_in_dm(self, platform_config, mock_rpc):
        platform_config.extra = {"max_consecutive_replies": 2, "dm_policy": "open"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        adapter.send = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "hello there",
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Single", "name": "DM"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})

        await self._send_n_messages(adapter, chat_id=7, n=10)

        assert adapter.handle_message.await_count == 10
        adapter.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_loop_guard_still_trips_in_group(self, platform_config, mock_rpc):
        platform_config.extra = {"max_consecutive_replies": 2}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        adapter.send = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "hello there",
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "Test Group"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})
        # Two other members besides self — a real multi-party group, so the
        # guard's "someone else could chime in" assumption actually holds.
        mock_rpc.get_chat_contacts = AsyncMock(return_value=[1, 11, 12])

        await self._send_n_messages(adapter, chat_id=1, n=10)

        # max_consecutive_replies=2 lets the first 2 from the same sender
        # through; the 3rd trips the guard and it stays tripped (same
        # from_id every time in this test), so the rest are dropped.
        assert adapter.handle_message.await_count == 2

    @pytest.mark.asyncio
    async def test_loop_guard_trip_logs_warning_even_without_rejection_replies(
        self, platform_config, mock_rpc, caplog
    ):
        """A tripped guard must leave a trace even with send_rejection_replies=false,

        or a genuinely multi-member group whose other members just go quiet
        for a while trips permanently with zero evidence anywhere (#2)."""
        platform_config.extra = {
            "max_consecutive_replies": 2,
            "send_rejection_replies": False,
        }
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        adapter.send = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "hello there",
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "Test Group"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})
        mock_rpc.get_chat_contacts = AsyncMock(return_value=[1, 11, 12])

        with caplog.at_level("WARNING"):
            await self._send_n_messages(adapter, chat_id=1, n=5)

        adapter.send.assert_not_called()
        assert any("loop_guard tripped" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_loop_guard_does_not_trip_in_solo_member_group(
        self, platform_config, mock_rpc
    ):
        """A DC 'Group' with only one other member (e.g. a solo-topic group

        created for a single human) is structurally identical to a DM for
        loop-guard purposes: that one member is the only possible sender, so
        "someone else chiming in" can never happen and a trip would be
        permanent. Regression for the "Household" group going silent forever
        after ~20 messages from its one human member (#2).
        """
        platform_config.extra = {"max_consecutive_replies": 2}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        adapter.send = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "hello there",
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "name": "Household"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})
        # Only self (contact 1) and one human (contact 11) — roster (which
        # excludes self) has a single member.
        mock_rpc.get_chat_contacts = AsyncMock(return_value=[1, 11])

        await self._send_n_messages(adapter, chat_id=13, n=10)

        assert adapter.handle_message.await_count == 10
        adapter.send.assert_not_called()


class TestMetadata:
    """Test incoming/outgoing metadata enrichment."""

    @pytest.mark.asyncio
    async def test_message_event_has_metadata(self, platform_config, mock_rpc):
        platform_config.extra = {"dm_policy": "open"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.handle_message = AsyncMock()
        mock_rpc.get_message = AsyncMock(
            return_value={
                "text": "hello",
                "view_type": "Text",
                "from_id": 11,
                "file": None,
            }
        )
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Single", "name": "DM"}
        )
        mock_rpc.get_contact = AsyncMock(return_value={"address": "user@example.com"})

        await adapter._handle_incoming_message(
            {"kind": "IncomingMsg", "chat_id": 7, "msg_id": 42}
        )

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.metadata["chat_id"] == "7"
        assert event.metadata["message_id"] == "42"
        assert event.metadata["from_id"] == "11"
        assert event.metadata["is_group"] is False
        assert "dc_token" in event.metadata

    @pytest.mark.asyncio
    async def test_send_result_includes_metadata(self, platform_config, mock_rpc):
        from adapter import _chat_id_to_token

        _chat_id_to_token[7] = "abc123"
        try:
            adapter = DeltaChatAdapter(platform_config)
            adapter.rpc = mock_rpc
            adapter.account_id = 1
            adapter._running = False
            mock_rpc.send_msg = AsyncMock(return_value=99)

            result = await adapter.send("7", "Hello")

            assert result.success is True
            assert result.metadata["chat_id"] == "7"
            assert result.metadata["message_id"] == "99"
            assert result.metadata["dc_token"] == "abc123"
        finally:
            _chat_id_to_token.pop(7, None)


class TestUrlImageSending:
    """Test send_image_file() with image URLs."""

    def _mock_httpx_stream(self, content_type, content, content_length=None):
        from unittest.mock import AsyncMock, MagicMock

        resp = MagicMock()
        resp.headers = {"content-type": content_type}
        if content_length is not None:
            resp.headers["content-length"] = str(content_length)
        resp.raise_for_status = MagicMock()

        async def _aiter_bytes(**kwargs):
            yield content

        resp.aiter_bytes = _aiter_bytes

        stream_cm = MagicMock()
        stream_cm.__aenter__ = AsyncMock(return_value=resp)
        stream_cm.__aexit__ = AsyncMock(return_value=False)

        client = MagicMock()
        client.stream = MagicMock(return_value=stream_cm)
        client_cm = MagicMock()
        client_cm.__aenter__ = AsyncMock(return_value=client)
        client_cm.__aexit__ = AsyncMock(return_value=False)

        return client_cm

    @pytest.mark.asyncio
    async def test_send_image_file_with_url_success(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        mock_rpc.send_msg = AsyncMock(return_value=123)

        client_cm = self._mock_httpx_stream("image/png", b"pngdata", 7)
        with patch("httpx.AsyncClient", return_value=client_cm):
            result = await adapter.send_image_file("7", "https://example.com/photo.png")

        assert result.success is True
        assert result.message_id == "123"
        mock_rpc.send_msg.assert_awaited_once()
        args = mock_rpc.send_msg.await_args.args
        assert args[0] == 1
        assert args[1] == 7
        assert args[2].file.endswith(".png")

    @pytest.mark.asyncio
    async def test_send_image_file_with_url_rejects_non_image(
        self, platform_config, mock_rpc
    ):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        client_cm = self._mock_httpx_stream("text/plain", b"not an image", 12)
        with patch("httpx.AsyncClient", return_value=client_cm):
            result = await adapter.send_image_file("7", "https://example.com/file.txt")

        assert result.success is False
        assert "image" in result.error.lower()
        mock_rpc.send_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_image_file_with_url_size_limit(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        client_cm = self._mock_httpx_stream(
            "image/png", b"x", content_length=50 * 1024 * 1024
        )
        with patch("httpx.AsyncClient", return_value=client_cm):
            result = await adapter.send_image_file("7", "https://example.com/huge.png")

        assert result.success is False
        assert "25" in result.error or "limit" in result.error.lower()
        mock_rpc.send_msg.assert_not_called()


class TestGroupRoster:
    """Test group roster fetching and caching for _get_group_roster."""

    @pytest.mark.asyncio
    async def test_fetches_and_excludes_self(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_chat_contacts = AsyncMock(return_value=[1, 2, 3])
        mock_rpc.get_contact = AsyncMock(
            side_effect=lambda account_id, cid: {
                2: {"name": "Alice", "address": "alice@x.com"},
                3: {"name": "Holly", "address": "holly@x.com"},
            }[cid]
        )

        roster = await adapter._get_group_roster("13")

        assert mock_rpc.get_contact.await_count == 2
        assert {"name": "Alice", "address": "alice@x.com"} in roster
        assert {"name": "Holly", "address": "holly@x.com"} in roster
        assert all(r["address"] != "" or r["name"] for r in roster)

    @pytest.mark.asyncio
    async def test_caches_and_skips_rpc_on_second_call(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_chat_contacts = AsyncMock(return_value=[1, 2])
        mock_rpc.get_contact = AsyncMock(
            return_value={"name": "Alice", "address": "alice@x.com"}
        )

        await adapter._get_group_roster("13")
        await adapter._get_group_roster("13")

        mock_rpc.get_chat_contacts.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refetches_after_ttl_expires(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._ROSTER_CACHE_TTL = 0

        mock_rpc.get_chat_contacts = AsyncMock(return_value=[1, 2])
        mock_rpc.get_contact = AsyncMock(
            return_value={"name": "Alice", "address": "alice@x.com"}
        )

        await adapter._get_group_roster("13")
        await adapter._get_group_roster("13")

        assert mock_rpc.get_chat_contacts.await_count == 2

    @pytest.mark.asyncio
    async def test_rpc_failure_falls_back_to_stale_cache(
        self, platform_config, mock_rpc
    ):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_chat_contacts = AsyncMock(return_value=[1, 2])
        mock_rpc.get_contact = AsyncMock(
            return_value={"name": "Alice", "address": "alice@x.com"}
        )
        roster = await adapter._get_group_roster("13")

        mock_rpc.get_chat_contacts = AsyncMock(side_effect=RuntimeError("rpc down"))
        adapter._ROSTER_CACHE_TTL = 0
        fallback = await adapter._get_group_roster("13")

        assert fallback == roster

    @pytest.mark.asyncio
    async def test_rpc_failure_with_no_cache_returns_empty(
        self, platform_config, mock_rpc
    ):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_chat_contacts = AsyncMock(side_effect=RuntimeError("rpc down"))

        roster = await adapter._get_group_roster("13")

        assert roster == []


class TestMessageMetadataRoster:
    """Test that _message_metadata includes participants only for groups."""

    def test_group_metadata_includes_participants(self, platform_config):
        adapter = DeltaChatAdapter(platform_config)
        roster = [{"name": "Alice", "address": "alice@x.com"}]

        meta = adapter._message_metadata("13", "5", "2", True, "tok", roster)

        assert meta["participants"] == roster

    def test_dm_metadata_omits_participants(self, platform_config):
        adapter = DeltaChatAdapter(platform_config)
        roster = [{"name": "Alice", "address": "alice@x.com"}]

        meta = adapter._message_metadata("13", "5", "2", False, "tok", roster)

        assert "participants" not in meta

    def test_no_roster_omits_participants(self, platform_config):
        adapter = DeltaChatAdapter(platform_config)

        meta = adapter._message_metadata("13", "5", "2", True, "tok", None)

        assert "participants" not in meta


class TestRegisterPlatformAuthEnv:
    """Without these, gateway._is_user_authorized never learns our env var
    names and silently drops every sender regardless of our own
    dm_policy/group_policy config (see CHANGELOG 1.5.6)."""

    def test_declares_allowed_users_and_allow_all_env(self):
        mock_ctx = MagicMock()

        adapter.register_platform(mock_ctx)

        _, kwargs = mock_ctx.register_platform.call_args
        assert kwargs["allowed_users_env"] == "DELTACHAT_ALLOWED_USERS"
        assert kwargs["allow_all_env"] == "DELTACHAT_ALLOW_ALL_USERS"


def _get_registered_tool_handler(tool_name: str):
    mock_ctx = MagicMock()
    adapter.register_rpc_tools(mock_ctx)
    for call in mock_ctx.register_tool.call_args_list:
        if call.kwargs.get("name") == tool_name:
            return call.kwargs["handler"]
    raise AssertionError(f"{tool_name} was not registered")


class TestDcSendMessageAddress:
    """dc_send_message's 'address' param: cold-open a 1:1 chat with a contact,
    guarded to addresses already seen in a group roster this bot has fetched."""

    @pytest.mark.asyncio
    async def test_rejects_invalid_email(self, platform_config, mock_rpc):
        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter.rpc = mock_rpc
        real_adapter.account_id = 1
        adapter._active_adapter = real_adapter
        try:
            handler = _get_registered_tool_handler("dc_send_message")

            result = json.loads(
                await handler({"text": "hi", "address": "not-an-email"})
            )

            assert "error" in result
            assert "not a valid email" in result["error"]
        finally:
            adapter._active_adapter = None

    @pytest.mark.asyncio
    async def test_rejects_address_not_in_any_roster(self, platform_config, mock_rpc):
        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter.rpc = mock_rpc
        real_adapter.account_id = 1
        adapter._active_adapter = real_adapter
        try:
            handler = _get_registered_tool_handler("dc_send_message")

            result = json.loads(
                await handler({"text": "hi", "address": "stranger@example.com"})
            )

            assert "error" in result
            assert "roster" in result["error"]
            mock_rpc.lookup_contact_id_by_addr.assert_not_called()
        finally:
            adapter._active_adapter = None

    @pytest.mark.asyncio
    async def test_sends_to_address_known_from_roster(self, platform_config, mock_rpc):
        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter.rpc = mock_rpc
        real_adapter.account_id = 1
        real_adapter._roster_cache["13"] = (
            9999999999.0,
            [{"name": "Alice", "address": "alice@example.com"}],
        )
        adapter._active_adapter = real_adapter
        try:
            mock_rpc.lookup_contact_id_by_addr = AsyncMock(return_value=42)
            mock_rpc.create_chat_by_contact_id = AsyncMock(return_value=99)
            mock_rpc.send_msg = AsyncMock(return_value=123)
            handler = _get_registered_tool_handler("dc_send_message")

            result = json.loads(
                await handler({"text": "hi Alice", "address": "Alice@Example.com"})
            )

            assert result["success"] is True
            mock_rpc.lookup_contact_id_by_addr.assert_awaited_once_with(
                1, "alice@example.com"
            )
            mock_rpc.create_chat_by_contact_id.assert_awaited_once_with(1, 42)
            mock_rpc.create_contact.assert_not_called()
        finally:
            adapter._active_adapter = None

    @pytest.mark.asyncio
    async def test_creates_contact_when_lookup_returns_none(
        self, platform_config, mock_rpc
    ):
        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter.rpc = mock_rpc
        real_adapter.account_id = 1
        real_adapter._roster_cache["13"] = (
            9999999999.0,
            [{"name": "Alice", "address": "alice@example.com"}],
        )
        adapter._active_adapter = real_adapter
        try:
            mock_rpc.lookup_contact_id_by_addr = AsyncMock(return_value=None)
            mock_rpc.create_contact = AsyncMock(return_value=42)
            mock_rpc.create_chat_by_contact_id = AsyncMock(return_value=99)
            mock_rpc.send_msg = AsyncMock(return_value=123)
            handler = _get_registered_tool_handler("dc_send_message")

            result = json.loads(
                await handler({"text": "hi Alice", "address": "alice@example.com"})
            )

            assert result["success"] is True
            mock_rpc.create_contact.assert_awaited_once_with(
                1, "alice@example.com", None
            )
        finally:
            adapter._active_adapter = None

    @pytest.mark.asyncio
    async def test_chat_token_takes_precedence_over_address(
        self, platform_config, mock_rpc
    ):
        from adapter import _chat_token_to_id

        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter.rpc = mock_rpc
        real_adapter.account_id = 1
        adapter._active_adapter = real_adapter
        _chat_token_to_id["tok123"] = 7
        try:
            mock_rpc.send_msg = AsyncMock(return_value=123)
            handler = _get_registered_tool_handler("dc_send_message")

            result = json.loads(
                await handler(
                    {
                        "text": "hi",
                        "chat_token": "tok123",
                        "address": "stranger@example.com",
                    }
                )
            )

            assert result["success"] is True
            mock_rpc.lookup_contact_id_by_addr.assert_not_called()
        finally:
            adapter._active_adapter = None
            _chat_token_to_id.pop("tok123", None)


class TestIsAddressInKnownRosters:
    def test_true_when_address_in_a_cached_roster(self, platform_config):
        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter._roster_cache["13"] = (
            1.0,
            [{"name": "Alice", "address": "alice@example.com"}],
        )

        assert real_adapter._is_address_in_known_rosters("ALICE@EXAMPLE.COM") is True

    def test_false_when_no_roster_has_it(self, platform_config):
        real_adapter = adapter.DeltaChatAdapter(platform_config)
        real_adapter._roster_cache["13"] = (
            1.0,
            [{"name": "Alice", "address": "alice@example.com"}],
        )

        assert real_adapter._is_address_in_known_rosters("bob@example.com") is False

    def test_false_with_no_cached_rosters(self, platform_config):
        real_adapter = adapter.DeltaChatAdapter(platform_config)

        assert real_adapter._is_address_in_known_rosters("alice@example.com") is False
