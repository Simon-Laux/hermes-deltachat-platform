"""Integration tests for Delta Chat adapter.

Tests the adapter with mocked Hermes gateway classes.
"""

import asyncio
import os
import random
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import pytest

# The conftest.py already installs the mocks, so we can import adapter now
from adapter import (
    DeltaChatAdapter,
    _parse_version,
    _check_dc_version,
    _check_dc2_available,
    MIN_DC_VERSION,
    MAX_TESTED_DC_VERSION,
    _DC_TEXT_LIMIT,
    _dc_len,
    _dc_split,
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

    # _check_dc_version calls rpc.get_system_info() and reads
    # "deltachat_core_version" — mocking rpc.call or "deltachat_version"
    # instead just exercises the except branch.

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

    @pytest.mark.asyncio
    async def test_version_inside_tested_window_is_silent(self, mock_rpc, caplog):
        """A version between the minimum and the tested ceiling must not warn.

        The whole range is verified, so warning on it is a false alarm on every
        connect — and warnings that fire when nothing is wrong get filtered out
        by the people who would need to read the real one.
        """
        assert _parse_version(MAX_TESTED_DC_VERSION) > _parse_version(MIN_DC_VERSION)
        mock_rpc.get_system_info = AsyncMock(
            return_value={"deltachat_core_version": MAX_TESTED_DC_VERSION}
        )
        with caplog.at_level("WARNING"):
            result = await _check_dc_version(mock_rpc)
        assert result is True
        assert caplog.text == ""

    @pytest.mark.asyncio
    async def test_missing_version_key_refuses(self, mock_rpc):
        """A missing key defaults to 0.0.0, which is too old — fail closed."""
        mock_rpc.get_system_info = AsyncMock(return_value={})
        assert await _check_dc_version(mock_rpc) is False

    @pytest.mark.asyncio
    async def test_rpc_failure_refuses(self, mock_rpc, caplog):
        """A broken RPC transport must refuse, not fall through as compatible."""
        mock_rpc.get_system_info = AsyncMock(side_effect=RuntimeError("transport closed"))
        with caplog.at_level("ERROR"):
            result = await _check_dc_version(mock_rpc)
        assert result is False
        assert "Could not check Delta Chat version" in caplog.text


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

        mock_rpc.send_msg = AsyncMock(return_value=123)

        result = await adapter.send("789", "Hello World")

        assert result.success is True
        assert result.message_id == "123"

        # send_msg(account_id, chat_id, MsgData) — chat_id is coerced to int.
        account_id, chat_id, data = mock_rpc.send_msg.await_args.args
        assert (account_id, chat_id) == (1, 789)
        assert data.text == "Hello World"

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

        result = await adapter.send_file("789", "/path/to/file.xdc", "A file")

        assert result.success is True
        assert result.message_id == "456"

        # DC core auto-detects viewtype from the extension, so no viewtype is set.
        account_id, chat_id, data = mock_rpc.send_msg.await_args.args
        assert (account_id, chat_id) == (1, 789)
        assert data.file == "/path/to/file.xdc"
        assert data.text == "A file"

    @pytest.mark.asyncio
    async def test_send_video_uses_video_viewtype(self, platform_config, mock_rpc):
        """Called the way cron delivery calls it: keyword args only, no caption."""
        from deltachat2.types import MessageViewtype

        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        mock_rpc.send_msg = AsyncMock(return_value=321)

        result = await adapter.send_video(
            chat_id="789", metadata=None, video_path="/path/to/clip.mp4"
        )

        assert result.success is True
        assert result.message_id == "321"
        account_id, chat_id, data = mock_rpc.send_msg.await_args.args
        assert (account_id, chat_id) == (1, 789)
        assert data.file == "/path/to/clip.mp4"
        assert data.viewtype == MessageViewtype.VIDEO
        assert data.text == ""

    @pytest.mark.asyncio
    async def test_send_accepts_a_chat_token_as_target(self, platform_config, mock_rpc):
        """A cron job the agent wrote targets `deltachat-platform:<token>`."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        token_key = "ui.hermes.token_chat.79489f9c02ceb390"
        mock_rpc.get_config = AsyncMock(
            side_effect=lambda acc, key: "789" if key == token_key else None
        )
        mock_rpc.send_msg = AsyncMock(return_value=55)

        result = await adapter.send("79489f9c02ceb390", "from cron")
        assert result.success is True
        assert mock_rpc.send_msg.await_args.args[1] == 789

        result = await adapter.send_video(chat_id="79489f9c02ceb390", video_path="/p/clip.mp4")
        assert result.success is True
        assert mock_rpc.send_msg.await_args.args[1] == 789

    @pytest.mark.asyncio
    async def test_send_rejects_an_unknown_token(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        mock_rpc.get_config = AsyncMock(return_value=None)
        mock_rpc.send_msg = AsyncMock(return_value=55)

        result = await adapter.send("deadbeefdeadbeef", "hi")

        assert result.success is False
        assert "unknown Delta Chat chat id or token" in result.error
        mock_rpc.send_msg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_numeric_id_skips_the_token_lookup(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        mock_rpc.get_config = AsyncMock(return_value=None)
        mock_rpc.send_msg = AsyncMock(return_value=55)

        await adapter.send("789", "hi")

        mock_rpc.get_config.assert_not_awaited()
        assert mock_rpc.send_msg.await_args.args[1] == 789

    def _in_call(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        cm = Mock()
        cm.is_call_end_reply = lambda r: False
        cm.has_active_call = lambda chat_id: True
        cm.is_call_thread = lambda thread_id: thread_id == "call-1780"
        cm.consume_call_ack = Mock(return_value=False)
        cm.play_response = AsyncMock()
        adapter._call_manager = cm
        mock_rpc.send_msg = AsyncMock(return_value=55)
        return adapter, cm

    @pytest.mark.asyncio
    async def test_call_speaks_only_the_final_reply(self, platform_config, mock_rpc):
        """Status sends (memory notices, tool progress, busy acks) carry no
        `notify`; seen live being read aloud in calls."""
        adapter, cm = self._in_call(platform_config, mock_rpc)

        for status in ("💾 Memory updated", "⏳ Queued", "🔧 terminal: ls"):
            result = await adapter.send("19", status, metadata={"thread_id": "call-1780"})
            assert result.success is True
        cm.play_response.assert_not_called()
        cm.consume_call_ack.assert_not_called()   # a status line must not use up the ack drop
        mock_rpc.send_msg.assert_not_awaited()     # nor leak into the chat as text

        await adapter.send("19", "Here's a joke.",
                           metadata={"thread_id": "call-1780", "notify": True})
        await asyncio.sleep(0)
        cm.play_response.assert_called_once_with("19", "Here's a joke.")

    @pytest.mark.asyncio
    async def test_text_thread_during_a_call_still_sends(self, platform_config, mock_rpc):
        """The filter is call-only: a text-chat send during a call is delivered."""
        adapter, cm = self._in_call(platform_config, mock_rpc)

        await adapter.send("19", "💾 Memory updated", metadata={"thread_id": None})

        mock_rpc.send_msg.assert_awaited_once()
        cm.play_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_reply_to_a_call_end_note_is_suppressed(self, platform_config, mock_rpc):
        """Hermes anchors the reply on the note's synthetic id — seen live as
        `invalid literal for int() with base 10: 'callend-35422583'`."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._call_manager = Mock()
        adapter._call_manager.is_call_end_reply = lambda r: str(r).startswith("callend-")
        mock_rpc.send_msg = AsyncMock(return_value=55)

        result = await adapter.send("19", "Okay, call over.", reply_to="callend-35422583")

        assert result.success is True
        mock_rpc.send_msg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_numeric_reply_to_sends_unquoted(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        mock_rpc.send_msg = AsyncMock(return_value=55)

        result = await adapter.send("19", "hi", reply_to="synthetic-7")
        assert result.success is True
        assert mock_rpc.send_msg.await_args.args[2].quoted_message_id is None

        await adapter.send("19", "hi", reply_to="1756")
        assert mock_rpc.send_msg.await_args.args[2].quoted_message_id == 1756

    @pytest.mark.asyncio
    async def test_send_video_not_connected(self, platform_config):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = None
        adapter.account_id = None

        result = await adapter.send_video("789", "/path/to/clip.mp4")

        assert result.success is False
        assert "not connected" in result.error.lower()


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
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.get_message = AsyncMock(
            return_value={
                "id": 123,
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
            return_value={"id": 456, "display_name": "Test User", "is_key_contact": True}
        )
        adapter._running = True
        adapter._mark_connected = Mock()
        adapter._mark_disconnected = Mock()
        adapter.handle_message = AsyncMock()

        # Process an incoming message event
        event = {"kind": "IncomingMsg", "chat_id": 789, "msg_id": 123}
        await adapter._handle_dc_event(event)

        # Verify message was handled
        assert adapter.handle_message.called
        call_args = adapter.handle_message.call_args[0][0]
        # The adapter appends a "[dc:chat=<token>]" metadata line to every
        # inbound text, so the body is a prefix rather than the whole string.
        assert call_args.text.startswith("Test message")
        assert "[dc:chat=" in call_args.text
        assert call_args.message_id == "123"

    @pytest.mark.asyncio
    async def test_info_logs_omit_message_content(self, platform_config, mock_rpc, tmp_path, caplog):
        """Captions must not reach INFO+, which Hermes writes to disk by default."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        mock_rpc.get_message = AsyncMock(return_value={
            "id": 123, "text": "inbound-caption-secret", "from_id": 456,
            "view_type": "Voice", "file": str(tmp_path / "missing.ogg"),
            "file_mime": "audio/ogg",
        })
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_id": 789, "name": "Test Chat", "chat_type": "Single"}
        )
        mock_rpc.get_contact = AsyncMock(
            return_value={"id": 456, "display_name": "Test User", "is_key_contact": True}
        )
        mock_rpc.send_msg = AsyncMock(return_value=55)
        adapter._running = True
        adapter.handle_message = AsyncMock()
        voice = tmp_path / "reply.ogg"
        voice.write_bytes(b"ogg")

        # The mock MessageEvent knows no media_urls; the real one is not needed here.
        with patch("adapter.MessageEvent", MagicMock()), \
                caplog.at_level("INFO", logger="hermes_plugins.deltachat"):
            await adapter._handle_dc_event({"kind": "IncomingMsg", "chat_id": 789, "msg_id": 123})
            result = await adapter.send_voice("789", str(voice), caption="outbound-caption-secret")

        assert adapter.handle_message.called
        assert result.success
        assert "Non-text message" in caplog.text
        assert "inbound-caption-secret" not in caplog.text
        assert "outbound-caption-secret" not in caplog.text

    @pytest.mark.asyncio
    async def test_handle_delivered_event(self, platform_config, mock_rpc, caplog):
        """Test handling of MSG_DELIVERED event."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True

        with caplog.at_level("DEBUG"):
            event = {"kind": "MsgDelivered", "msg_id": 123}
            await adapter._handle_dc_event(event)

        assert "delivered" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_handle_failed_event_reports_the_reason(
        self, platform_config, mock_rpc, caplog
    ):
        """MSG_FAILED must surface DC's error text, not just a bare msg_id."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        mock_rpc.get_message = AsyncMock(return_value={"error": "SMTP: over quota"})

        with caplog.at_level("WARNING"):
            await adapter._handle_dc_event(
                {"kind": "MsgFailed", "msg_id": 123, "chat_id": 789}
            )

        assert "SMTP: over quota" in caplog.text
        assert "789" in caplog.text

    @pytest.mark.asyncio
    async def test_handle_failed_event_survives_a_broken_lookup(
        self, platform_config, mock_rpc, caplog
    ):
        """The reason lookup is a second RPC and may fail — still log the failure."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        mock_rpc.get_message = AsyncMock(side_effect=Exception("transport closed"))

        with caplog.at_level("WARNING"):
            await adapter._handle_dc_event(
                {"kind": "MsgFailed", "msg_id": 123, "chat_id": 789}
            )

        assert "unknown" in caplog.text
        assert "123" in caplog.text

    @pytest.mark.asyncio
    async def test_handle_incoming_call_event(self, platform_config, mock_rpc):
        """An IncomingCall event is delegated to the CallManager."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True
        adapter._call_manager = MagicMock()
        adapter._call_manager.handle_incoming_call = AsyncMock()

        event = {"kind": "IncomingCall", "chat_id": 789, "msg_id": 123}
        await adapter._handle_dc_event(event)
        # The handler is dispatched via create_task, so yield once to let it run.
        await asyncio.sleep(0)

        adapter._call_manager.handle_incoming_call.assert_awaited_once_with(event)

    @pytest.mark.asyncio
    async def test_handle_unknown_event(self, platform_config, mock_rpc, caplog):
        """Test handling of unknown event type."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True

        with caplog.at_level("DEBUG"):
            event = {"event_type": "UNKNOWN_EVENT"}
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


class TestLongMessages:
    """Long text is split into messages Delta Chat shows in full (no HTML part)."""

    @staticmethod
    def _core_truncates(text):
        """Port of core's truncate_by_lines(text, 38, 100) decision (src/tools.rs)."""
        lines = line_chars = 0
        for ch in text:
            if ch == "\n":
                line_chars, lines = 0, lines + 1
            else:
                line_chars += 1
                if line_chars > 100:
                    line_chars, lines = 1, lines + 1
            if lines == 38:
                return True
        return False

    def test_dc_len_matches_core(self):
        rnd = random.Random(0)
        for _ in range(2000):
            text = "".join(rnd.choice(["\n", "x" * rnd.randint(1, 250), " "])
                           for _ in range(rnd.randint(0, 60)))
            # measured as an edit's receiver sees it: core prepends "✏️"
            assert (_dc_len(text) > _DC_TEXT_LIMIT) == self._core_truncates("✏️" + text), repr(text)

    def test_split_keeps_text_and_fits(self):
        rnd = random.Random(1)
        for _ in range(300):
            text = "".join(rnd.choice(["\n", "word ", "x" * rnd.randint(1, 500)])
                           for _ in range(rnd.randint(0, 400)))
            pieces = _dc_split(text)
            assert "".join(pieces) == text
            assert all(_dc_len(p) <= _DC_TEXT_LIMIT for p in pieces)

    @pytest.mark.asyncio
    async def test_long_reply_is_several_messages(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc, adapter.account_id = mock_rpc, 1
        mock_rpc.send_msg = AsyncMock(side_effect=[11, 12, 13])
        text = "\n".join(f"Line {i}" for i in range(100))

        result = await adapter.send("789", text, reply_to="5")

        sent = [c.args[2] for c in mock_rpc.send_msg.await_args_list]
        assert len(sent) == 3
        assert "\n".join(d.text for d in sent) == text
        assert all(_dc_len(d.text) <= _DC_TEXT_LIMIT and not d.html for d in sent)
        assert [d.quoted_message_id for d in sent] == [5, None, None]
        assert result.success and result.message_id == "13"
        assert result.continuation_message_ids == ("11", "12")

    @pytest.mark.asyncio
    async def test_short_reply_is_one_message(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc, adapter.account_id = mock_rpc, 1
        mock_rpc.send_msg = AsyncMock(return_value=11)
        text = "\n".join(f"Line {i}" for i in range(38))   # the most core shows in full

        result = await adapter.send("789", text)

        assert mock_rpc.send_msg.await_args.args[2].text == text
        assert result.message_id == "11" and result.continuation_message_ids == ()

    @pytest.mark.asyncio
    async def test_partial_failure_is_reported_as_partial(self, platform_config, mock_rpc):
        # Hermes must not re-send the part that already arrived
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc, adapter.account_id = mock_rpc, 1
        mock_rpc.send_msg = AsyncMock(side_effect=[11, RuntimeError("relay down")])
        text = "\n".join(f"Line {i}" for i in range(60))

        result = await adapter.send("789", text)

        first = mock_rpc.send_msg.await_args_list[0].args[2].text
        assert not result.success and result.message_id == "11"
        assert result.raw_response["partial_overflow"] is True
        assert result.raw_response["last_message_id"] == "11"
        assert result.raw_response["delivered_prefix"].strip("\n") == first

    def test_split_is_fast_on_huge_text(self):
        # was ~50 s for 1 MB of short lines, blocking the gateway
        import time
        text = "short line\n" * 100_000
        start = time.monotonic()
        pieces = _dc_split(text)
        assert time.monotonic() - start < 5
        assert "".join(pieces) == text

    @pytest.mark.asyncio
    async def test_retry_after_partial_failure_sends_only_the_rest(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc, adapter.account_id = mock_rpc, 1
        mock_rpc.send_msg = AsyncMock(side_effect=[11, RuntimeError("relay down"), 12])
        text = "\n".join(f"Line {i}" for i in range(60))

        partial = await adapter.send("789", text)
        resumed = await adapter._resume_partial_send("789", partial, reply_to=None, metadata=None)

        sent = [c.args[2].text for c in mock_rpc.send_msg.await_args_list]
        assert resumed.success and resumed.message_id == "12"
        assert sent[0] + "\n" + sent[2] == text   # nothing twice, nothing lost

    def test_split_does_not_send_a_tiny_first_piece(self):
        # the only space is near the start: cut mid-word rather than send "Key:"
        pieces = _dc_split("Key: " + "x" * 5000)
        assert len(pieces[0]) > 1000

    def test_hermes_measures_like_core(self, platform_config):
        adapter = DeltaChatAdapter(platform_config)
        assert adapter.message_len_fn is _dc_len
        # Hermes keeps a streamed message under MAX - len(cursor) - 100
        assert adapter.MAX_MESSAGE_LENGTH - _dc_len(" ▉") - 100 < _DC_TEXT_LIMIT


class TestLocationSending:
    """Test location/POI message sending."""

    @pytest.mark.asyncio
    async def test_send_location_success(self, platform_config, mock_rpc):
        """Test successful location sending."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.send_msg = AsyncMock(return_value=123)

        result = await adapter.send_location("789", 52.5200, 13.4050, "☕ Coffee")

        assert result.success is True
        assert result.message_id == "123"

        # MsgData.location is (latitude, longitude), per GeoJSON convention.
        account_id, chat_id, data = mock_rpc.send_msg.await_args.args
        assert (account_id, chat_id) == (1, 789)
        assert data.text == "☕ Coffee"
        assert data.location == (52.5200, 13.4050)

    @pytest.mark.asyncio
    async def test_send_location_with_text_poi(self, platform_config, mock_rpc):
        """Test location sending with text POI (displays as pin)."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.send_msg = AsyncMock(return_value=456)

        result = await adapter.send_location("789", 40.7128, -74.0060, "Coffee Shop")

        assert result.success is True
        assert result.message_id == "456"

        account_id, chat_id, data = mock_rpc.send_msg.await_args.args
        assert (account_id, chat_id) == (1, 789)
        assert data.text == "Coffee Shop"
        assert data.location == (40.7128, -74.0060)

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
