"""Pytest fixtures for Delta Chat plugin integration tests.

Provides mocks for Hermes gateway module that match the real API.
"""

import asyncio
import sys
import os
from unittest.mock import MagicMock, Mock, AsyncMock
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Mock Hermes gateway module BEFORE any imports that might need it
# ---------------------------------------------------------------------------


class MockMessageType(Enum):
    """Mock of gateway.platforms.base.MessageType."""

    TEXT = "text"
    FILE = "file"
    IMAGE = "image"
    AUDIO = "audio"
    VOICE = "voice"
    STICKER = "sticker"
    GIF = "gif"


@dataclass
class MockSendResult:
    """Mock of gateway.platforms.base.SendResult."""

    success: bool
    error: Optional[str] = None
    message_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class MockSource:
    """Mock of gateway.platforms.base.Source."""

    chat_id: str
    chat_name: str
    chat_type: str
    user_id: str
    user_name: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MockMessageEvent:
    """Mock of gateway.platforms.base.MessageEvent."""

    text: str
    message_type: MockMessageType
    source: MockSource
    message_id: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class MockPlatform(Enum):
    """Mock of gateway.config.Platform."""

    DELTACHAT = "deltachat-platform"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"
    WHATSAPP = "whatsapp"

    def __str__(self) -> str:
        return self.value


@dataclass
class MockPlatformConfig:
    """Mock of gateway.config.PlatformConfig."""

    name: str
    platform: MockPlatform
    extra: Optional[Dict[str, Any]] = None
    enabled: bool = True


class MockHermesConfig:
    """Mock of gateway.config module."""

    Platform = MockPlatform
    PlatformConfig = MockPlatformConfig

    @staticmethod
    def get_hermes_home() -> str:
        """Mock get_hermes_home - returns temp dir or HERMES_HOME env."""
        # Allow override via HERMES_HOME_TEST environment variable for testing
        test_home = os.environ.get("HERMES_HOME_TEST")
        if test_home:
            return test_home
        return os.environ.get("HERMES_HOME", "/tmp/hermes-test")


class MockBasePlatformAdapter:
    """Mock of gateway.platforms.base.BasePlatformAdapter."""

    def __init__(self, config: MockPlatformConfig, platform: MockPlatform):
        self.config = config
        self.platform = platform
        self._connected = False
        self._disconnected = False
        # Mirrors the real base: _running *is* is_connected, and the fatal-error
        # trio is what an adapter uses to hand a dead transport back to the
        # gateway's reconnect watcher.
        self._running = False
        self._fatal_error_code = None
        self._fatal_error_message = None
        self._fatal_error_retryable = True
        self._fatal_error_handler = None

    @property
    def is_connected(self) -> bool:
        return self._running

    @property
    def has_fatal_error(self) -> bool:
        return self._fatal_error_message is not None

    @property
    def fatal_error_code(self):
        return self._fatal_error_code

    @property
    def fatal_error_message(self):
        return self._fatal_error_message

    @property
    def fatal_error_retryable(self) -> bool:
        return self._fatal_error_retryable

    def set_fatal_error_handler(self, handler) -> None:
        self._fatal_error_handler = handler

    def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
        self._running = False
        self._fatal_error_code = code
        self._fatal_error_message = message
        self._fatal_error_retryable = retryable

    async def _notify_fatal_error(self) -> None:
        handler = self._fatal_error_handler
        if not handler:
            return
        result = handler(self)
        if asyncio.iscoroutine(result):
            await result

    def _mark_connected(self) -> None:
        """Mark adapter as connected."""
        self._connected = True
        self._running = True
        self._fatal_error_code = None
        self._fatal_error_message = None
        self._fatal_error_retryable = True

    def _mark_disconnected(self) -> None:
        """Mark adapter as disconnected."""
        self._running = False
        # The real base refuses to downgrade a recorded fatal error to a plain
        # "disconnected" status.
        if self.has_fatal_error:
            return
        self._disconnected = True

    def build_source(
        self,
        chat_id: str,
        chat_name: str,
        chat_type: str,
        user_id: str,
        user_name: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MockSource:
        """Build a source object for message events."""
        return MockSource(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
            metadata=metadata or {},
        )

    async def handle_message(self, event: MockMessageEvent) -> None:
        """Handle a message event (to be overridden by adapter)."""
        pass

    @staticmethod
    def extract_media(content: str):
        """Mock base extractor.

        The real base only picks up MEDIA_DELIVERY_EXTS (which excludes .xdc),
        so for the adapter's .xdc-focused overrides an empty base result with
        the content passed through unchanged is a faithful stand-in.
        """
        return [], content

    @staticmethod
    def extract_local_files(content: str):
        """Mock base local-file extractor (see extract_media note)."""
        return [], content

    @staticmethod
    def filter_media_delivery_paths(media_files, session_key: str = ""):
        """Mock base media filter — pass through unchanged."""
        return list(media_files or [])

    @staticmethod
    def filter_local_delivery_paths(file_paths, session_key: str = ""):
        """Mock base local filter — pass through unchanged."""
        return list(file_paths or [])

    @staticmethod
    def _mask_protected_spans(content: str) -> str:
        """Minimal stand-in for the real span masker.

        The real one also handles blockquotes and spares backtick-quoted
        MEDIA: paths; fenced and inline code is enough to pin the contract
        the adapter depends on — masking is offset-preserving, so match
        spans stay valid against the unmasked text.
        """
        import re

        chars = list(content)
        for pattern in (r"```[^\n]*\n.*?```", r"`[^`\n]+`"):
            for m in re.finditer(pattern, content, re.DOTALL):
                for i in range(m.start(), m.end()):
                    if chars[i] != "\n":
                        chars[i] = " "
        return "".join(chars)


class MockGatewayBase:
    """Mock of gateway.platforms.base module."""

    BasePlatformAdapter = MockBasePlatformAdapter
    SendResult = MockSendResult
    MessageEvent = MockMessageEvent
    MessageType = MockMessageType


# ---------------------------------------------------------------------------
# Install mocks into sys.modules
# ---------------------------------------------------------------------------

# Create mock gateway module hierarchy
gateway_module = MagicMock()
gateway_platforms = MagicMock()
gateway_platforms_base = MockGatewayBase()
gateway_config_module = MockHermesConfig()

sys.modules["gateway"] = gateway_module
sys.modules["gateway.platforms"] = gateway_platforms
sys.modules["gateway.platforms.base"] = gateway_platforms_base
sys.modules["gateway.config"] = gateway_config_module

# Set up the actual module references
gateway_module.platforms = gateway_platforms
gateway_module.platforms.base = gateway_platforms_base
gateway_module.config = gateway_config_module


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.fixture
def platform_config():
    """Create a mock PlatformConfig."""
    return MockPlatformConfig(
        name="deltachat-platform",
        platform=MockPlatform.DELTACHAT,
        extra={"rpc_server": "deltachat-rpc-server"},
        enabled=True,
    )


@pytest.fixture
def mock_platform_config():
    """Create a mock PlatformConfig for tests that need it."""
    return MockPlatformConfig(
        name="test",
        platform=MockPlatform.DELTACHAT,
        extra={"rpc_server": "deltachat-rpc-server"},
    )


@pytest.fixture
def mock_rpc():
    """Create a mock deltachat2.Rpc instance."""
    rpc = MagicMock()
    # Make call an async method that returns a coroutine
    rpc.call = AsyncMock()
    rpc.start = AsyncMock()
    rpc.close = Mock()
    return rpc
