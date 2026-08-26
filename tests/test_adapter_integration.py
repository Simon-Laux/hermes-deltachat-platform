"""Integration tests for Delta Chat adapter.

Tests the adapter with mocked Hermes gateway classes.
"""

import os
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
        mock_rpc.call = AsyncMock(return_value={"deltachat_version": MIN_DC_VERSION})
        result = await _check_dc_version(mock_rpc)
        assert result is True

    @pytest.mark.asyncio
    async def test_version_too_old(self, mock_rpc, caplog):
        """Test version check with too old version."""
        mock_rpc.call = AsyncMock(return_value={"deltachat_version": "1.0.0"})
        with caplog.at_level("ERROR"):
            result = await _check_dc_version(mock_rpc)
        assert result is False
        assert "too old" in caplog.text

    @pytest.mark.asyncio
    async def test_version_newer_warns(self, mock_rpc, caplog):
        """Test version check with newer version warns but allows."""
        mock_rpc.call = AsyncMock(return_value={"deltachat_version": "3.0.0"})
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

        # Mock RPC call to return message ID
        mock_rpc.call = AsyncMock(return_value=123)

        from adapter import SendResult

        result = await adapter.send("789", "Hello World")

        assert result.success is True
        assert result.message_id == "123"

        # Verify RPC was called with correct params
        mock_rpc.call.assert_called_with(
            "send_text_message",
            {"account_id": 1, "chat_id": 789, "message": "Hello World"},
        )

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

        mock_rpc.call = AsyncMock(return_value=456)

        from adapter import SendResult

        result = await adapter.send_file("789", "/path/to/file.xdc", "A file")

        assert result.success is True
        assert result.message_id == "456"

        mock_rpc.call.assert_called_with(
            "send_file",
            {
                "account_id": 1,
                "chat_id": 789,
                "file": "/path/to/file.xdc",
                "caption": "A file",
            },
        )


class TestGetChatInfo:
    """Test chat info retrieval."""

    @pytest.mark.asyncio
    async def test_get_chat_info_success(self, platform_config, mock_rpc):
        """Test successful chat info retrieval."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.call = AsyncMock(
            return_value={"chat_id": 789, "name": "Test Chat", "is_group": False}
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

        mock_rpc.call = AsyncMock(
            return_value={"chat_id": 789, "name": "Group Chat", "is_group": True}
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

        mock_rpc.call = AsyncMock(side_effect=Exception("RPC error"))

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

        # Mock RPC responses
        def mock_call(method, params=None):
            if method == "get_message":
                return {
                    "msg_id": 123,
                    "text": "Test message",
                    "from_id": 456,
                    "timestamp": 1234567890,
                    "msg_type": "TEXT",
                }
            elif method == "get_chat":
                return {"chat_id": 789, "name": "Test Chat", "is_group": False}
            elif method == "get_contact":
                return {"contact_id": 456, "name": "Test User"}
            return {}

        mock_rpc.call = AsyncMock(side_effect=mock_call)
        adapter._running = True
        adapter._mark_connected = Mock()
        adapter._mark_disconnected = Mock()
        adapter.handle_message = AsyncMock()

        # Process an incoming message event
        event = {"event_type": "INCOMING_MSG", "chat_id": 789, "msg_id": 123}
        await adapter._handle_dc_event(event)

        # Verify message was handled
        assert adapter.handle_message.called
        call_args = adapter.handle_message.call_args[0][0]
        assert call_args.text == "Test message"
        assert call_args.message_id == "123"

    @pytest.mark.asyncio
    async def test_handle_delivered_event(self, platform_config, mock_rpc, caplog):
        """Test handling of MSG_DELIVERED event."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True

        with caplog.at_level("DEBUG"):
            event = {"event_type": "MSG_DELIVERED", "msg_id": 123}
            await adapter._handle_dc_event(event)

        assert "delivered" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_handle_incoming_call_event(self, platform_config, mock_rpc, caplog):
        """Test handling of IncomingCall event."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1
        adapter._running = True

        with caplog.at_level("INFO"):
            event = {"event_type": "IncomingCall"}
            await adapter._handle_dc_event(event)

        assert "call" in caplog.text.lower()

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


class TestHTMLFormatting:
    """Test HTML message formatting for long messages."""

    def test_short_message_no_html(self, platform_config):
        """Test that short messages (< 40 lines) don't get HTML formatting."""
        adapter = DeltaChatAdapter(platform_config)
        short_text = "This is a short message."

        text_part, html_part = adapter._format_html_message(short_text)

        assert text_part == short_text
        assert html_part is None

    def test_long_message_with_html(self, platform_config):
        """Test that long messages (> 40 lines) get HTML formatting."""
        adapter = DeltaChatAdapter(platform_config)
        # Create a message with 50 lines
        long_text = "\n".join([f"Line {i}" for i in range(50)])

        text_part, html_part = adapter._format_html_message(long_text)

        # text_part should have first 40 lines
        assert text_part == "\n".join([f"Line {i}" for i in range(40)])
        # html_part should contain the full message
        assert html_part is not None
        assert "Line 40" in html_part
        assert "Line 49" in html_part
        # Check for HTML styling
        assert "sans-serif" in html_part
        assert "font-size: 16px" in html_part

    def test_exactly_40_lines_no_html(self, platform_config):
        """Test that exactly 40 lines doesn't trigger HTML formatting."""
        adapter = DeltaChatAdapter(platform_config)
        text_40_lines = "\n".join([f"Line {i}" for i in range(40)])

        text_part, html_part = adapter._format_html_message(text_40_lines)

        assert text_part == text_40_lines
        assert html_part is None

    def test_41_lines_with_html(self, platform_config):
        """Test that 41 lines triggers HTML formatting."""
        adapter = DeltaChatAdapter(platform_config)
        text_41_lines = "\n".join([f"Line {i}" for i in range(41)])

        text_part, html_part = adapter._format_html_message(text_41_lines)

        # text_part should have first 40 lines
        assert text_part == "\n".join([f"Line {i}" for i in range(40)])
        # html_part should exist
        assert html_part is not None


class TestLocationSending:
    """Test location/POI message sending."""

    @pytest.mark.asyncio
    async def test_send_location_success(self, platform_config, mock_rpc):
        """Test successful location sending."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.call = AsyncMock(return_value=123)

        from adapter import SendResult

        result = await adapter.send_location("789", 52.5200, 13.4050, "☕ Coffee")

        assert result.success is True
        assert result.message_id == "123"

        # Verify RPC was called with correct params
        mock_rpc.call.assert_called_with(
            "send_msg",
            {
                "account_id": 1,
                "chat_id": 789,
                "data": {
                    "text": "☕ Coffee",
                    "location": [13.4050, 52.5200],
                    "viewtype": "Text",
                },
            },
        )

    @pytest.mark.asyncio
    async def test_send_location_with_text_poi(self, platform_config, mock_rpc):
        """Test location sending with text POI (displays as pin)."""
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc = mock_rpc
        adapter.account_id = 1

        mock_rpc.call = AsyncMock(return_value=456)

        from adapter import SendResult

        result = await adapter.send_location("789", 40.7128, -74.0060, "Coffee Shop")

        assert result.success is True
        assert result.message_id == "456"

        # Verify text POI is sent
        mock_rpc.call.assert_called_with(
            "send_msg",
            {
                "account_id": 1,
                "chat_id": 789,
                "data": {
                    "text": "Coffee Shop",
                    "location": [-74.0060, 40.7128],
                    "viewtype": "Text",
                },
            },
        )

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


class TestFilterDeliveryPathsSessionKey:
    """Regression: Hermes core calls these on the adapter instance as
    self.filter_media_delivery_paths(media_files, session_key=session_key) /
    self.filter_local_delivery_paths(file_paths, session_key=session_key).
    Without a matching session_key parameter, every reply containing a
    MEDIA directive or local file path crashes with
    TypeError: ...filter_media_delivery_paths() got an unexpected keyword
    argument 'session_key'."""

    def test_filter_media_delivery_paths_accepts_session_key(self, platform_config):
        adapter = DeltaChatAdapter(platform_config)

        result = adapter.filter_media_delivery_paths(
            [("/home/user/app.xdc", False)], session_key="agent:main:deltachat:dm:12"
        )

        assert result == [("/home/user/app.xdc", False)]

    def test_filter_local_delivery_paths_accepts_session_key(self, platform_config):
        adapter = DeltaChatAdapter(platform_config)

        result = adapter.filter_local_delivery_paths(
            ["/home/user/report.pdf"], session_key="agent:main:deltachat:dm:12"
        )

        assert result == ["/home/user/report.pdf"]

    def test_session_key_not_forwarded_to_a_base_without_it(
        self, platform_config, monkeypatch
    ):
        """Hermes 0.15.1's base staticmethods take one positional argument.

        Forwarding session_key to those raises the same TypeError this class
        exists to prevent, so the override must forward it only when the
        installed base declares it.
        """
        from gateway.platforms.base import BasePlatformAdapter

        def legacy_media(media_files):
            return list(media_files or [])

        def legacy_local(file_paths):
            return list(file_paths or [])

        monkeypatch.setattr(
            BasePlatformAdapter, "filter_media_delivery_paths",
            staticmethod(legacy_media),
        )
        monkeypatch.setattr(
            BasePlatformAdapter, "filter_local_delivery_paths",
            staticmethod(legacy_local),
        )

        adapter = DeltaChatAdapter(platform_config)

        assert adapter.filter_media_delivery_paths(
            [("/home/user/app.xdc", False)], session_key="agent:main:deltachat:dm:12"
        ) == [("/home/user/app.xdc", False)]
        assert adapter.filter_local_delivery_paths(
            ["/home/user/report.pdf"], session_key="agent:main:deltachat:dm:12"
        ) == ["/home/user/report.pdf"]
