"""Delta Chat platform adapter for Hermes Gateway.

Integrates Delta Chat as a messaging platform using deltachat2 (direct JSON-RPC).
"""

import html
import os
import sys
import asyncio
import logging
from typing import Optional, Dict, Any

# Add vendor directory to sys.path so vendored deltachat2 can be imported
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_vendor_dir = os.path.join(_plugin_dir, "vendor")
if os.path.exists(_vendor_dir) and _vendor_dir not in sys.path:
    sys.path.insert(0, _vendor_dir)

from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from gateway.config import Platform, PlatformConfig

logger = logging.getLogger(__name__)

# Enable debug logging for RPC if requested
if os.getenv("DELTACHAT_DEBUG"):
    logging.getLogger("deltachat2").setLevel(logging.DEBUG)
    logging.getLogger("deltachat2.IOTransport").setLevel(logging.DEBUG)

# Minimum required Delta Chat core version
# Plugin will NOT connect with older versions
MIN_DC_VERSION = "2.51.0"

# Lazy import to avoid dependency issues if deltachat2 not installed
_DC2_AVAILABLE = None


def _check_dc2_available():
    """Check if deltachat2 is available."""
    global _DC2_AVAILABLE
    if _DC2_AVAILABLE is None:
        try:
            import deltachat2
            _DC2_AVAILABLE = True
            return True
        except ImportError:
            _DC2_AVAILABLE = False
    return _DC2_AVAILABLE


def _parse_version(version_str: str) -> tuple:
    """Parse version string into tuple of ints for comparison.

    Args:
        version_str: Version string like "2.51.0" or "2.51.0-dev"

    Returns:
        Tuple of (major, minor, patch) integers
    """
    try:
        # Remove any suffixes like -dev, -rc1, etc.
        base_version = version_str.split("-")[0]
        parts = base_version.split(".")
        # Pad with zeros if needed
        while len(parts) < 3:
            parts.append("0")
        return tuple(int(p) for p in parts[:3])
    except (ValueError, AttributeError):
        return (0, 0, 0)


async def _check_dc_version(rpc) -> bool:
    """Check Delta Chat core version and enforce minimum.

    Args:
        rpc: DeltaChat2 RPC client

    Returns:
        True if version is compatible, False if too old
    """
    try:
        # Get system info which includes version
        system_info = await rpc.get_system_info()
        dc_version_str = system_info.get("deltachat_version", "0.0.0")
        dc_version = _parse_version(dc_version_str)
        min_version = _parse_version(MIN_DC_VERSION)

        if dc_version < min_version:
            logger.error(
                f"Delta Chat version {dc_version_str} is too old. "
                f"This plugin requires {MIN_DC_VERSION} or higher. "
                f"Please update your Delta Chat installation."
            )
            return False
        elif dc_version > min_version:
            logger.warning(
                f"Delta Chat version {dc_version_str} is newer than "
                f"the minimum required ({MIN_DC_VERSION}). "
                f"The API may have changed and there may be errors."
            )

        return True

    except Exception as e:
        logger.warning(f"Could not check Delta Chat version: {e}")
        # Don't block connection for version check failures
        return True


class DeltaChatAdapter(BasePlatformAdapter):
    """Delta Chat platform adapter for Hermes Gateway.

    Uses deltachat2 for direct JSON-RPC access (not abstracted away).
    Each Hermes profile runs its own instance with its own DC_ACCOUNTS_PATH.
    """

    def __init__(self, config: PlatformConfig):
        """Initialize the adapter.

        Args:
            config: Hermes PlatformConfig for this profile
        """
        super().__init__(config, Platform("deltachat-platform"))
        self.rpc = None
        self._transport = None
        self.account_id: Optional[int] = None
        self._event_loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._dc_config_dir: Optional[str] = None

    def _get_dc_config_dir(self) -> str:
        """Get Delta Chat config directory path.

        Returns:
            Path to Delta Chat config directory (<HERMES_HOME>/deltachat-platform/)
        """
        if self._dc_config_dir is None:
            from gateway.config import get_hermes_home

            self._dc_config_dir = os.path.join(get_hermes_home(), "deltachat-platform")
            # Ensure directory exists
            os.makedirs(self._dc_config_dir, exist_ok=True)
        return self._dc_config_dir

    def _get_rpc_server_path(self) -> str:
        """Get deltachat-rpc-server binary path.

        Returns:
            Path to RPC server binary from config, env, or default.
        """
        # From config.extra
        if self.config.extra and self.config.extra.get("rpc_server"):
            return self.config.extra["rpc_server"]

        # From environment
        env_path = os.getenv("DELTACHAT_RPC_SERVER")
        if env_path:
            return env_path

        # Default - assume in PATH
        return "deltachat-rpc-server"

    async def connect(self) -> bool:
        """Connect to Delta Chat via RPC server.

        Starts the RPC server process, initializes the client,
        checks version, and begins listening for events.

        Returns:
            True if connection successful, False otherwise
        """
        if not _check_dc2_available():
            logger.error("deltachat2 is not installed. Run: pip install deltachat2")
            return False

        try:
            import deltachat2
        except ImportError as e:
            logger.error(f"Failed to import deltachat2: {e}")
            return False

        try:
            # Get config directory
            dc_accounts_path = self._get_dc_config_dir()
            logger.debug(f"Using DC accounts directory: {dc_accounts_path}")

            # Get RPC server path
            rpc_server_path = self._get_rpc_server_path()
            logger.debug(f"Using RPC server: {rpc_server_path}")

            # Initialize RPC client with deltachat2, passing accounts_dir to transport
            from deltachat2.transport import IOTransport

            os.environ["DC_ACCOUNTS_PATH"] = dc_accounts_path
            self._transport = IOTransport(accounts_dir=dc_accounts_path, rpc_server=rpc_server_path)
            self._transport.start()
            self.rpc = deltachat2.Rpc(self._transport)

            # Wait for RPC server to be ready
            await asyncio.sleep(1)

            # Check version - REJECT if too old
            if not await _check_dc_version(self.rpc):
                self._cleanup()
                return False

            # Get or create account - use first available
            accounts = await self.rpc.get_all_accounts()
            if accounts:
                self.account_id = accounts[0]["id"]
                logger.info(f"Using Delta Chat account: {self.account_id}")
            else:
                logger.error(
                    f"No Delta Chat accounts found in {dc_accounts_path}. "
                    "Run: python ~/.hermes/plugins/deltachat-platform/setup.py"
                )
                self._cleanup()
                return False

            # Start IO for the account to receive events
            self.rpc.start_io(self.account_id)
            logger.debug(f"Started IO for account {self.account_id}")

            # Start event listener
            self._running = True
            self._event_loop_task = asyncio.create_task(self._event_listener())

            self._mark_connected()

            # Log the bot's address for reference
            addr = await self.get_my_address()
            if addr:
                logger.info(f"Delta Chat connected successfully. Bot address: {addr}")
            else:
                logger.info("Delta Chat connected successfully")
            return True

        except Exception as e:
            logger.error(f"Delta Chat connection failed: {e}")
            self._cleanup()
            return False

    def _cleanup(self) -> None:
        """Clean up resources."""
        self._running = False
        if self._event_loop_task:
            self._event_loop_task.cancel()
            self._event_loop_task = None
        if self._transport:
            try:
                self._transport.close()
            except Exception as e:
                logger.warning(f"Error closing transport: {e}")
            self._transport = None
        self.rpc = None
        self.account_id = None

    async def disconnect(self) -> None:
        """Disconnect from Delta Chat."""
        self._cleanup()
        self._mark_disconnected()
        logger.info("Delta Chat disconnected")

    async def get_my_address(self) -> Optional[str]:
        """Get the Delta Chat account address or SecureJoin link.

        Returns:
            SecureJoin link (e.g., https://delta.chat/s?pk=...) or address (e.g., bot@server.org)
        """
        if not self.rpc or not self.account_id:
            return None

        try:
            # Try to get SecureJoin QR code content (which is the link)
            try:
                qr_content = await self.rpc.get_chat_securejoin_qr_code(
                    self.account_id,
                    None  # chat_id - None for account-level QR
                )
                if qr_content:
                    return qr_content
            except Exception:
                pass

            # Fallback: get account info which should include address
            info = await self.rpc.get_account_info(self.account_id)
            if info:
                # Try different field names for address
                address = info.get("address") or info.get("addr")
                if address:
                    return address
                # Construct from name and server
                name = info.get("name") or info.get("display_name", "")
                server = info.get("server", "")
                if name and server:
                    return f"{name}@{server}"

            # Final fallback: list accounts and find ours
            accounts = await self.rpc.get_all_accounts()
            for acc in accounts:
                if acc.get("id") == self.account_id:
                    name = acc.get("name", acc.get("display_name", ""))
                    server = acc.get("server", "")
                    if name and server:
                        return f"{name}@{server}"
        except Exception as e:
            logger.debug(f"Failed to get account address: {e}")

        return None

    def _format_html_message(self, text: str, max_lines: int = 40) -> tuple:
        """Format long messages with HTML for better readability in Delta Chat.

        If message is longer than max_lines, returns (text_part, html_part)
        where text_part is the first max_lines and html_part is the full
        message with proper styling. Otherwise returns (text, None).

        Args:
            text: The message text
            max_lines: Maximum lines before using HTML (default: 40)

        Returns:
            Tuple of (plain_text, html_text) - html_text is None if not needed
        """
        lines = text.split("\n")
        if len(lines) <= max_lines:
            return (text, None)

        # First max_lines as plain text
        text_part = "\n".join(lines[:max_lines])

        # Full message as HTML with nice formatting; escape to prevent injection
        escaped = html.escape(text).replace("\n", "<br>\n")
        html_part = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 16px;
    line-height: 1.5;
    color: #333;
    background-color: #fff;
    padding: 16px;
    max-width: 800px;
    margin: 0 auto;
}}
</style>
</head>
<body>
{escaped}
</body>
</html>"""

        return (text_part, html_part)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message to a Delta Chat chat.

        Args:
            chat_id: Delta Chat chat ID (string representation of integer)
            content: Message text to send
            reply_to: Message ID to reply to (optional)
            metadata: Additional metadata (optional)

        Returns:
            SendResult with success status and message ID
        """
        try:
            if not self.rpc or not self.account_id:
                return SendResult(
                    success=False,
                    error="Delta Chat not connected",
                )

            # Format long messages with HTML
            text_part, html_part = self._format_html_message(content)

            if html_part:
                # Send with HTML using send_msg RPC
                from deltachat2.types import MsgData, MessageViewtype

                msg_id = await self.rpc.send_msg(
                    self.account_id,
                    int(chat_id),
                    MsgData(text=text_part, html=html_part, viewtype=MessageViewtype.TEXT),
                )
            else:
                # Send as plain text using misc_send_text_message
                msg_id = await self.rpc.misc_send_text_message(
                    self.account_id,
                    int(chat_id),
                    content,
                )

            logger.debug(f"Sent message {msg_id} to chat {chat_id}")
            return SendResult(
                success=True,
                message_id=str(msg_id),
            )

        except Exception as e:
            logger.error(f"Error sending message to chat {chat_id}: {e}")
            return SendResult(
                success=False,
                error=str(e),
            )

    async def send_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
    ) -> SendResult:
        """Send a file (e.g., .xdc) to a Delta Chat chat.

        Args:
            chat_id: Delta Chat chat ID
            file_path: Path to file on disk
            caption: Optional caption for the file

        Returns:
            SendResult with success status and message ID
        """
        try:
            if not self.rpc or not self.account_id:
                return SendResult(
                    success=False,
                    error="Delta Chat not connected",
                )

            # Use send_msg with file attachment
            from deltachat2.types import MsgData

            msg_id = await self.rpc.send_msg(
                self.account_id,
                int(chat_id),
                MsgData(file=file_path, text=caption or ""),
            )

            logger.debug(f"Sent file {file_path} as message {msg_id} to chat {chat_id}")
            return SendResult(
                success=True,
                message_id=str(msg_id),
            )

        except Exception as e:
            logger.error(f"Error sending file {file_path} to chat {chat_id}: {e}")
            return SendResult(
                success=False,
                error=str(e),
            )

    async def send_location(
        self,
        chat_id: str,
        latitude: float,
        longitude: float,
        poi_name: str,
    ) -> SendResult:
        """Send a location/point of interest to a Delta Chat chat.

        Note: In Delta Chat, a single emoji character is displayed as that emoji
        on the map. A text message is displayed as a pin icon that can be clicked
        to view the message.

        Args:
            chat_id: Delta Chat chat ID
            latitude: Latitude in degrees
            longitude: Longitude in degrees
            poi_name: POI name or emoji (e.g., "☕" for coffee, "🏠" for home,
                     or "My favorite café" for a pin with text)

        Returns:
            SendResult with success status and message ID
        """
        try:
            if not self.rpc or not self.account_id:
                return SendResult(
                    success=False,
                    error="Delta Chat not connected",
                )

            from deltachat2.types import MsgData, MessageViewtype

            msg_id = await self.rpc.send_msg(
                self.account_id,
                int(chat_id),
                MsgData(text=poi_name, location=(longitude, latitude), viewtype=MessageViewtype.TEXT),
            )

            logger.debug(f"Sent location to chat {chat_id}")
            return SendResult(
                success=True,
                message_id=str(msg_id),
            )

        except Exception as e:
            logger.error(f"Error sending location to chat {chat_id}: {e}")
            return SendResult(
                success=False,
                error=str(e),
            )

    async def _event_listener(self) -> None:
        """Listen for Delta Chat events and forward to Hermes."""
        while self._running:
            try:
                if self.account_id:
                    event = await self.rpc.get_next_event(self.account_id)
                    await self._handle_dc_event(event)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Event listener error: {e}")
                await asyncio.sleep(1)

    async def _handle_dc_event(self, event: Dict[str, Any]) -> None:
        """Handle a Delta Chat event and convert to Hermes MessageEvent.

        Args:
            event: Delta Chat event dictionary
        """
        from deltachat2.types import EventType

        event_kind = event.get("kind")

        if event_kind == EventType.INCOMING_MSG:
            await self._handle_incoming_message(event)
        elif event_kind == EventType.MSG_DELIVERED:
            logger.debug(f"Message delivered: {event.get('msg_id')}")
        elif event_kind == EventType.MSG_FAILED:
            logger.warning(f"Message failed: {event.get('msg_id')}")
        else:
            logger.debug(f"Unhandled event type: {event_kind}")

    async def _handle_incoming_message(self, event: Dict[str, Any]) -> None:
        """Handle an incoming text message.

        Args:
            event: Delta Chat INCOMING_MSG event
        """
        try:
            chat_id = event.get("chat_id")
            msg_id = event.get("msg_id")

            if not chat_id or not msg_id:
                logger.warning(f"Invalid message event: {event}")
                return

            # Get message details via direct RPC
            msg = await self.rpc.get_message(
                self.account_id,
                int(msg_id),
            )
            if not msg:
                logger.warning(f"Could not retrieve message {msg_id}")
                return

            text = msg.get("text", "")
            if not text:
                # Handle non-text messages (files, images, audio, etc.)
                await self._handle_non_text_message(msg, chat_id, msg_id)
                return

            # Get chat info
            chat = await self.rpc.get_basic_chat_info(
                self.account_id,
                int(chat_id),
            )

            # Get sender info
            from_id = msg.get("from_id")
            if from_id:
                contact = await self.rpc.get_contact(
                    self.account_id,
                    int(from_id),
                )
                user_name = contact.get("name", f"Contact {from_id}")
                user_id = str(from_id)
            else:
                user_name = "Unknown"
                user_id = "unknown"

            # Determine chat type
            chat_type = "group" if chat.get("is_group", False) else "dm"
            chat_name = chat.get("name", f"Chat {chat_id}")

            # Build source
            source = self.build_source(
                chat_id=str(chat_id),
                chat_name=chat_name,
                chat_type=chat_type,
                user_id=user_id,
                user_name=user_name,
            )

            # Build and handle message event
            message_event = MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=str(msg_id),
                metadata={
                    "chat_id": str(chat_id),
                    "from_id": user_id,
                    "timestamp": msg.get("timestamp"),
                },
            )
            await self.handle_message(message_event)

        except Exception as e:
            logger.error(f"Error handling message event: {e}")

    async def _handle_non_text_message(
        self, msg: Dict, chat_id: str, msg_id: str
    ) -> None:
        """Handle non-text messages (files, images, audio, etc.).

        Args:
            msg: Delta Chat message dictionary
            chat_id: Chat ID (string representation)
            msg_id: Message ID (string representation)
        """
        msg_type = msg.get("msg_type", "").upper()
        filename = msg.get("file", "")

        # Audio/Voice message - Phase 3
        if msg_type in ("AUDIO", "VOICE"):
            logger.info(f"Audio message received: {filename}")
            # filename is a local filepath, ready to read
            # TODO: Phase 3 - transcribe and forward

        # File attachment (including .xdc)
        elif msg_type == "FILE" and filename:
            logger.info(f"File received: {filename}")
            # TODO: Phase 2 - handle .xdc files specially

        # Image
        elif msg_type == "IMAGE":
            logger.info(f"Image received: {filename}")

        else:
            logger.debug(f"Unhandled message type: {msg_type}, file: {filename}")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get metadata for a chat.

        Args:
            chat_id: Delta Chat chat ID

        Returns:
            Dictionary with chat info (name, type, etc.)
        """
        try:
            if self.rpc and self.account_id:
                chat = await self.rpc.get_basic_chat_info(
                    self.account_id,
                    int(chat_id),
                )
                return {
                    "name": chat.get("name", chat_id),
                    "type": "group" if chat.get("is_group") else "dm",
                }
        except Exception as e:
            logger.warning(f"Error getting chat info for {chat_id}: {e}")
        return {"name": chat_id, "type": "dm"}

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a message from a Delta Chat chat.

        Args:
            chat_id: Delta Chat chat ID
            message_id: Message ID to delete

        Returns:
            True if deletion successful, False otherwise
        """
        try:
            if self.rpc and self.account_id:
                await self.rpc.delete_messages(
                    self.account_id,
                    [int(message_id)],
                )
                logger.debug(f"Deleted message {message_id} from chat {chat_id}")
                return True
        except Exception as e:
            logger.error(f"Error deleting message {message_id} from chat {chat_id}: {e}")
            return False
        return False


def check_requirements() -> bool:
    """Check if deltachat2 and deltachat-rpc-server are available."""
    import shutil

    # Check Python package
    try:
        import deltachat2
    except ImportError:
        return False

    # Check binary
    rpc_server = os.getenv("DELTACHAT_RPC_SERVER", "deltachat-rpc-server")
    if shutil.which(rpc_server):
        return True

    return False


def validate_config(config) -> bool:
    """Validate platform configuration."""
    return check_requirements()


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Seed PlatformConfig from environment variables."""
    import shutil

    rpc_server = os.getenv("DELTACHAT_RPC_SERVER", "deltachat-rpc-server").strip()

    # Check if binary exists
    if not shutil.which(rpc_server):
        # Try without path
        if shutil.which("deltachat-rpc-server"):
            rpc_server = "deltachat-rpc-server"
        else:
            return None

    result = {"rpc_server": rpc_server}

    # Add home channel if set
    home_channel = os.getenv("DELTACHAT_HOME_CHANNEL")
    if home_channel:
        result["home_channel"] = {
            "chat_id": home_channel,
            "name": "Home",
        }

    return result


def register_platform(ctx):
    """Register Delta Chat platform adapter with Hermes."""
    ctx.register_platform(
        name="deltachat-platform",
        label="Delta Chat",
        adapter_factory=lambda cfg: DeltaChatAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["DELTACHAT_RPC_SERVER"],
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="DELTACHAT_HOME_CHANNEL",
        emoji="💬",
        platform_hint=(
            "You are chatting via Delta Chat. "
            "Delta Chat does NOT support markdown formatting or message editing. "
            "Messages longer than 40 lines will be automatically formatted with HTML "
            "for better readability with a 'Show full message' button. "
            "For very long content, consider sending as a document file instead. "
            "You can send webxdc mini apps for interactive responses, "
            "send/receive voice messages, videos, images, and delete messages. "
            "Location messages can be sent to share points of interest on a map."
        ),
        max_message_length=3200,
    )
