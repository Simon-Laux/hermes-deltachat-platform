"""Delta Chat platform adapter for Hermes Gateway.

Integrates Delta Chat as a messaging platform using deltachat2 (direct JSON-RPC).
"""

import functools
import html
import json
import os
import secrets
import sys
import asyncio
import logging
from typing import Optional, Dict, Any

# Add vendor directory to sys.path so vendored deltachat2 can be imported
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)
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

# Must use "hermes_plugins.*" prefix so records appear in gateway.log.
# __name__ resolves to "adapter" (standalone module), which only goes to agent.log.
logger = logging.getLogger("hermes_plugins.deltachat")

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
        # Remove any suffixes like -dev, -rc1, etc. and leading 'v'
        base_version = version_str.lstrip("v").split("-")[0]
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
        dc_version_str = system_info.get("deltachat_core_version", "0.0.0")
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


class _AsyncRpc:
    """Wraps synchronous deltachat2.Rpc so every call runs in a thread executor.

    deltachat2.Rpc.transport.call() blocks on a threading.Event until the
    RPC server responds.  Calling it directly from an async function would
    freeze the asyncio event loop.  This wrapper makes every attribute access
    return an async function that runs the underlying sync call in the default
    ThreadPoolExecutor, keeping the event loop free.
    """

    def __init__(self, rpc) -> None:
        object.__setattr__(self, "_rpc", rpc)

    def __getattr__(self, name: str):
        method = getattr(object.__getattribute__(self, "_rpc"), name)
        async def _async_call(*args):
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, method, *args)
        return _async_call


# Tracks the currently connected adapter instance; used by RPC tools.
_active_adapter = None

# Per-session opaque token ↔ real chat_id mapping.
# Tokens are generated once per unique chat_id using secrets.token_hex so they
# are unguessable and stable within a process lifetime.  They are injected into
# every incoming message text as "[dc:chat=<token>]" so the LLM always has the
# right token in its context without ever seeing the raw numeric id.
_chat_id_to_token: Dict[int, str] = {}
_chat_token_to_id: Dict[str, int] = {}

# Methods that mutate or destroy chat data — blocked from dc_safe_rpc_call.
_DESTRUCTIVE_METHODS = frozenset({
    "delete_chat",
    "delete_messages",
    "delete_messages_for_all",
    "remove_contact_from_chat",
    "remove_draft",
    "leave_group",
})

# Cached OpenRPC spec (fetched lazily on first use).
_spec_cache: Optional[dict] = None


async def _get_or_create_chat_token(rpc, account_id: int, chat_id: int) -> str:
    """Return a stable opaque token for *chat_id*.

    Checks memory cache first, then DC UI config (persists across restarts),
    creating and storing a new token if none exists yet.
    """
    if chat_id in _chat_id_to_token:
        return _chat_id_to_token[chat_id]

    dc_key = f"ui.hermes.chat_token.{chat_id}"
    try:
        existing = await rpc.get_config(account_id, dc_key)
    except Exception:
        existing = None

    if existing:
        token = existing
    else:
        token = secrets.token_hex(8)
        try:
            await rpc.set_config(account_id, dc_key, token)
            await rpc.set_config(account_id, f"ui.hermes.token_chat.{token}", str(chat_id))
        except Exception as e:
            logger.warning(f"Could not persist chat token to DC config: {e}")

    _chat_id_to_token[chat_id] = token
    _chat_token_to_id[token] = chat_id
    return token


async def _resolve_chat_token(rpc, account_id: int, token: str) -> Optional[int]:
    """Resolve an opaque token back to the real chat_id.

    Checks memory cache first, then DC UI config as a fallback for
    tokens issued in a previous session.
    """
    if token in _chat_token_to_id:
        return _chat_token_to_id[token]

    dc_key = f"ui.hermes.token_chat.{token}"
    try:
        chat_id_str = await rpc.get_config(account_id, dc_key)
    except Exception:
        chat_id_str = None

    if chat_id_str:
        chat_id = int(chat_id_str)
        _chat_token_to_id[token] = chat_id
        _chat_id_to_token[chat_id] = token
        return chat_id

    return None


async def _fetch_spec() -> dict:
    """Fetch and cache the OpenRPC spec from deltachat-rpc-server --openrpc."""
    global _spec_cache
    if _spec_cache is None:
        rpc_server = (
            _active_adapter._get_rpc_server_path()
            if _active_adapter is not None
            else os.getenv("DELTACHAT_RPC_SERVER", "deltachat-rpc-server")
        )
        proc = await asyncio.create_subprocess_exec(
            rpc_server, "--openrpc",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"deltachat-rpc-server --openrpc failed: {stderr.decode().strip()}")
        _spec_cache = json.loads(stdout.decode())
    return _spec_cache


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
        self._call_manager = None



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

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to Delta Chat via RPC server.

        Starts the RPC server process, initializes the client,
        checks version, and begins listening for events.

        Args:
            is_reconnect: True when reconnecting after a drop; ignored (DC
                RPC has no buffered update queue to preserve).

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
            self.rpc = _AsyncRpc(deltachat2.Rpc(self._transport))

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

            # Enable bot mode: auto-accept contact requests
            try:
                await self.rpc.set_config(self.account_id, "bot", "1")
                logger.debug("Bot mode enabled: contact requests will be auto-accepted")
            except Exception as e:
                logger.warning(f"Could not set bot config: {e}")

            # Start IO for the account to receive events
            await self.rpc.start_io(self.account_id)
            logger.debug(f"Started IO for account {self.account_id}")

            # Start event listener
            self._running = True
            self._event_loop_task = asyncio.create_task(self._event_listener())

            self._mark_connected()
            global _active_adapter
            _active_adapter = self

            from call_handler import CallManager
            self._call_manager = CallManager(self)

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
        global _active_adapter
        if _active_adapter is self:
            _active_adapter = None
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
        if self._call_manager:
            await self._call_manager.teardown()
            self._call_manager = None
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

        When a voice call is active for this chat the response is routed to
        TTS and played into the call instead of being sent as a DC message.
        """
        if self._call_manager and self._call_manager.has_active_call(chat_id):
            thread_id = (metadata or {}).get("thread_id")
            if self._call_manager.is_call_thread(thread_id):
                # Reply belongs to the call conversation — speak it into the call.
                # In shared-history mode the placing agent's "call connected" ack
                # also lands here (same session), so drop that one line.
                if self._call_manager.consume_call_ack(chat_id):
                    return SendResult(success=True, message_id=None)
                asyncio.create_task(self._call_manager.play_response(chat_id, content))
                return SendResult(success=True, message_id=None)
            # Reply from the text/chat thread while a call is active (e.g. the
            # agent's "calling you now" line in separate-thread mode, or a
            # concurrent DM) — deliver it as a normal Delta Chat message instead
            # of speaking it into the call. Falls through to the normal send path.
        # Suppress the AI's reply to the internal "call ended" note so we don't
        # text the user a stray message after a call.
        if self._call_manager and self._call_manager.consume_drop_response(chat_id):
            return SendResult(success=True, message_id=None)

        try:
            if not self.rpc or not self.account_id:
                return SendResult(
                    success=False,
                    error="Delta Chat not connected",
                )

            # Format long messages with HTML
            text_part, html_part = self._format_html_message(content)

            quoted_id = int(reply_to) if reply_to else None

            if html_part:
                from deltachat2.types import MsgData, MessageViewtype

                msg_id = await self.rpc.send_msg(
                    self.account_id,
                    int(chat_id),
                    MsgData(text=text_part, html=html_part, viewtype=MessageViewtype.TEXT, quoted_message_id=quoted_id),
                )
            else:
                from deltachat2.types import MsgData

                msg_id = await self.rpc.send_msg(
                    self.account_id,
                    int(chat_id),
                    MsgData(text=content, quoted_message_id=quoted_id),
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
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a file to a Delta Chat chat via send_msg.

        DC core auto-detects the viewtype from the extension — .xdc files
        are delivered as webxdc apps without any special handling here.
        """
        try:
            if not self.rpc or not self.account_id:
                return SendResult(success=False, error="Delta Chat not connected")

            from deltachat2.types import MsgData

            msg_id = await self.rpc.send_msg(
                self.account_id,
                int(chat_id),
                MsgData(file=file_path, text=caption or "", quoted_message_id=int(reply_to) if reply_to else None),
            )
            logger.debug(f"Sent file {file_path} as message {msg_id} to chat {chat_id}")
            return SendResult(success=True, message_id=str(msg_id))

        except Exception as e:
            logger.error(f"Error sending file {file_path} to chat {chat_id}: {e}")
            return SendResult(success=False, error=str(e))

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document/file attachment to a Delta Chat chat.

        Delegates to send_file; file_name is ignored because DC derives the
        display name from the blob path.  DC core auto-detects viewtype from
        the file extension (.xdc → webxdc, .pdf → document, etc.).
        """
        return await self.send_file(
            chat_id=chat_id,
            file_path=file_path,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send an image file to a Delta Chat chat.

        Args:
            chat_id: Delta Chat chat ID
            image_path: Path to image file on disk
            caption: Optional caption for the image
            reply_to: Optional message ID to reply to
            metadata: Optional metadata

        Returns:
            SendResult with success status and message ID
        """
        try:
            if not self.rpc or not self.account_id:
                return SendResult(success=False, error="Delta Chat not connected")

            from deltachat2.types import MsgData, MessageViewtype

            msg_id = await self.rpc.send_msg(
                self.account_id,
                int(chat_id),
                MsgData(
                    file=image_path,
                    text=caption or "",
                    viewtype=MessageViewtype.IMAGE,
                    quoted_message_id=int(reply_to) if reply_to else None,
                ),
            )
            logger.debug(f"Sent image {image_path} as message {msg_id} to chat {chat_id}")
            return SendResult(success=True, message_id=str(msg_id))
        except Exception as e:
            logger.error(f"Error sending image {image_path} to chat {chat_id}: {e}")
            return SendResult(success=False, error=str(e))

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a voice message to a Delta Chat chat.

        Delta Chat supports voice messages natively.

        Args:
            chat_id: Delta Chat chat ID
            audio_path: Path to audio file on disk
            caption: Optional caption for the voice message
            reply_to: Optional message ID to reply to
            metadata: Optional metadata

        Returns:
            SendResult with success status and message ID
        """
        import os
        logger.info(f"send_voice called: chat_id={chat_id}, audio_path={audio_path}, caption={caption[:50] if caption else None}")
        logger.debug(f"send_voice kwargs: {kwargs}")

        # Validate audio file exists and is accessible
        if not os.path.exists(audio_path):
            logger.error(f"send_voice: Audio file does not exist: {audio_path}")
            return SendResult(
                success=False,
                error=f"Audio file not found: {audio_path}",
            )
        if not os.path.isfile(audio_path):
            logger.error(f"send_voice: Path is not a file: {audio_path}")
            return SendResult(
                success=False,
                error=f"Path is not a file: {audio_path}",
            )
        file_size = os.path.getsize(audio_path)
        logger.info(f"send_voice: Audio file exists, size={file_size} bytes")

        # Delta Chat sends voice messages as files with VOICE viewtype
        from deltachat2.types import MsgData, MessageViewtype

        try:
            if not self.rpc or not self.account_id:
                logger.error("send_voice: Delta Chat not connected (rpc={}, account_id={})".format(
                    "None" if not self.rpc else "set",
                    "None" if not self.account_id else self.account_id
                ))
                return SendResult(
                    success=False,
                    error="Delta Chat not connected",
                )

            logger.debug(f"send_voice: Sending to account_id={self.account_id}, chat_id={chat_id}")
            msg_id = await self.rpc.send_msg(
                self.account_id,
                int(chat_id),
                MsgData(file=audio_path, text=caption or "", viewtype=MessageViewtype.VOICE),
            )

            logger.info(f"Sent voice message {msg_id} to chat {chat_id}, file={audio_path}, size={file_size}")
            return SendResult(
                success=True,
                message_id=str(msg_id),
            )

        except Exception as e:
            import traceback
            logger.error(f"Error in send_voice: {e}")
            logger.debug(f"send_voice exception traceback:\n{traceback.format_exc()}")
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

            from deltachat2.types import MsgData

            # location tuple is (latitude, longitude) per GeoJSON convention
            msg_id = await self.rpc.send_msg(
                self.account_id,
                int(chat_id),
                MsgData(text=poi_name, location=(latitude, longitude)),
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

    # ------------------------------------------------------------------
    # Container-to-host file path mapping
    # ------------------------------------------------------------------
    # The Docker LLM sandbox mounts /workspace inside the container to
    #   ~/.hermes/sandboxes/docker/default/workspace/   on the host.
    # When the agent writes output files to /workspace/ and emits MEDIA
    # directives or bare paths, Hermes's path validator runs on the HOST
    # and can't find container-local paths.  These overrides remap any
    # /workspace/<rel> path to the host sandbox path, copy the file to
    # the Hermes documents cache (a validated safe root), and return the
    # cache path so the base-class validator accepts it.
    #
    # The same pattern works for any output file type (.pdf, .html, .zip,
    # .xdc, etc.) — just write to /workspace/ in the container.
    # ------------------------------------------------------------------

    @staticmethod
    def _container_workspace_to_host(container_path: str) -> Optional[str]:
        """Map a /workspace/<rel> container path to its host-side sandbox path.

        Returns None when the path is not under /workspace/.
        """
        from pathlib import Path

        p = str(container_path)
        if not p.startswith("/workspace/"):
            return None
        rel = p[len("/workspace/"):]
        try:
            from tools.environments.base import get_sandbox_dir
            sandbox_workspace = get_sandbox_dir() / "docker" / "default" / "workspace"
        except ImportError:
            from gateway.config import get_hermes_home
            sandbox_workspace = Path(get_hermes_home()) / "sandboxes" / "docker" / "default" / "workspace"
        return str(sandbox_workspace / rel)

    def _copy_container_file_to_cache(self, container_path: str) -> Optional[str]:
        """Copy a /workspace/ container file to the Hermes docs cache.

        Returns the cache path on success, None if the file doesn't exist.
        Same pattern as _copy_to_hermes_cache for DC audio blobs.
        """
        import shutil
        from pathlib import Path
        from gateway.config import get_hermes_home

        host_path_str = self._container_workspace_to_host(container_path)
        if host_path_str is None:
            return None

        host_path = Path(host_path_str)
        if not host_path.is_file():
            logger.warning("Container output file not found on host: %s", host_path)
            return None

        docs_dir = Path(get_hermes_home()) / "cache" / "documents"
        docs_dir.mkdir(parents=True, exist_ok=True)
        dest = docs_dir / host_path.name
        shutil.copy2(str(host_path), str(dest))
        logger.info("Copied container output %s → %s", host_path.name, dest)
        return str(dest)

    def extract_media(self, content: str):
        """Extend base extract_media to also handle .xdc MEDIA tags.

        .xdc is not in Hermes's MEDIA_DELIVERY_EXTS so the base staticmethod
        misses it.  We catch those tags here so they flow through the normal
        filter_media_delivery_paths → send_document pipeline, exactly like
        Telegram handles any other document type.
        """
        import re
        from gateway.platforms.base import BasePlatformAdapter

        media_files, remaining = BasePlatformAdapter.extract_media(content)

        xdc_re = re.compile(
            r'[`"\']?MEDIA:\s*[`"\']?((?:~/|/)[\w./\- ]+\.xdc)[`"\']?',
            re.IGNORECASE,
        )
        for match in xdc_re.finditer(content):
            path = match.group(1).strip()
            if not any(p == path for p, _ in media_files):
                media_files.append((path, False))
            remaining = remaining.replace(match.group(0), "").strip()

        return media_files, remaining

    def extract_local_files(self, content: str):
        """Extend base to also pick up bare /workspace/*.xdc paths.

        The base staticmethod checks os.path.isfile() on the host — container
        paths like /workspace/app.xdc don't exist there, so we add them
        explicitly.  filter_local_delivery_paths then does the host mapping.
        """
        import re
        from gateway.platforms.base import BasePlatformAdapter

        files, remaining = BasePlatformAdapter.extract_local_files(content)

        xdc_re = re.compile(r'(?<![/:\w.])(/workspace/[\w./\-]+\.xdc)\b', re.IGNORECASE)
        for match in xdc_re.finditer(content):
            path = match.group(1)
            if path not in files:
                files.append(path)
                remaining = remaining.replace(match.group(0), "").strip()

        return files, remaining

    def filter_media_delivery_paths(self, media_files):
        """Remap /workspace/ container paths to host cache before validation."""
        from gateway.platforms.base import BasePlatformAdapter

        remapped = []
        for media_path, is_voice in media_files or []:
            p = str(media_path)
            if p.startswith("/workspace/"):
                cached = self._copy_container_file_to_cache(p)
                if cached:
                    remapped.append((cached, is_voice))
                    continue
                logger.warning("Could not resolve container path for delivery: %s", p)
            remapped.append((media_path, is_voice))
        return BasePlatformAdapter.filter_media_delivery_paths(remapped)

    def filter_local_delivery_paths(self, file_paths):
        """Remap /workspace/ container paths to host cache before validation."""
        from gateway.platforms.base import BasePlatformAdapter

        remapped = []
        for file_path in file_paths or []:
            p = str(file_path)
            if p.startswith("/workspace/"):
                cached = self._copy_container_file_to_cache(p)
                if cached:
                    remapped.append(cached)
                    continue
                logger.warning("Could not resolve container path for delivery: %s", p)
            else:
                remapped.append(file_path)
        return BasePlatformAdapter.filter_local_delivery_paths(remapped)

    async def _event_listener(self) -> None:
        """Listen for Delta Chat events and forward to Hermes."""
        while self._running:
            try:
                if self.account_id:
                    envelope = await self.rpc.get_next_event()
                    if envelope.get("context_id") == self.account_id:
                        await self._handle_dc_event(envelope.get("event", {}))
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
        elif event_kind == EventType.INCOMING_CALL:
            if self._call_manager:
                asyncio.create_task(self._call_manager.handle_incoming_call(event))
        elif event_kind == EventType.CALL_ENDED:
            if self._call_manager:
                asyncio.create_task(self._call_manager.handle_call_ended(event))
        elif event_kind == EventType.OUTGOING_CALL_ACCEPTED:
            if self._call_manager:
                asyncio.create_task(self._call_manager.handle_outgoing_call_accepted(event))
        elif event_kind == EventType.INCOMING_CALL_ACCEPTED:
            logger.info("Incoming call accepted msg_id=%s", event.get("msg_id"))
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

            # Send read receipt immediately
            try:
                await self.rpc.markseen_msgs(self.account_id, [int(msg_id)])
            except Exception as e:
                logger.debug(f"Could not mark message {msg_id} as seen: {e}")

            text = msg.get("text", "")
            view_type = msg.get("view_type", "")
            has_file = bool(msg.get("file") or msg.get("file_mime"))
            # Route to non-text handler when viewtype is non-text OR when the
            # message has a file attachment even if DC reported viewType=Text
            # (happens for image+caption combos or pending downloads).
            if not text or view_type not in ("Text", "", None) or has_file:
                logger.info(
                    "Non-text message: view_type=%r text=%r file=%r file_mime=%r msg_id=%s",
                    view_type, text[:80] if text else text,
                    msg.get("file"), msg.get("file_mime"), msg_id,
                )
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
                user_name = (contact.get("name") or contact.get("display_name")
                             or contact.get("name_and_addr") or f"Contact {from_id}")
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

            # Append chat token for dc_safe_rpc_call — skip on slash commands so
            # Hermes doesn't misparse the token as part of the command argument.
            if text.startswith("/"):
                text_with_token = text
            else:
                token = await _get_or_create_chat_token(self.rpc, self.account_id, int(chat_id))
                text_with_token = f"{text}\n[dc:chat={token}]"

            # Build and handle message event
            message_event = MessageEvent(
                text=text_with_token,
                message_type=MessageType.TEXT,
                source=source,
                message_id=str(msg_id),
            )
            await self.handle_message(message_event)

        except Exception as e:
            logger.error(f"Error handling message event: {e}")

    def _resolve_blob_path(self, filename: str) -> Optional[str]:
        """Resolve a DC file path to an accessible absolute path.

        The RPC returns whatever path DC core has internally, which may be
        absolute already or relative to the blob directory. Try in order:
        the path as-is, then <dc_config_dir>/blobs/<basename>.
        """
        if not filename:
            return None
        if os.path.exists(filename):
            logger.debug("Blob path exists as-is: %s", filename)
            return filename
        blob_path = os.path.join(self._get_dc_config_dir(), "blobs", os.path.basename(filename))
        if os.path.exists(blob_path):
            logger.debug("Blob path resolved via blobs dir: %s", blob_path)
            return blob_path
        logger.warning("Media file not found at %r or %r", filename, blob_path)
        return None

    def _copy_to_hermes_cache(self, src: str, kind: str) -> str:
        """Copy a DC blob file into the Hermes cache directory and return the new path.

        DC blob paths are not mounted inside the Docker LLM backend, so files
        must live under ~/.hermes/cache/* for STT and vision to reach them.
        Returns the original path on failure so the caller still has something.
        """
        try:
            ext = os.path.splitext(src)[1] or ""
            data = open(src, "rb").read()
            if kind == "audio":
                from gateway.platforms.base import cache_audio_from_bytes
                dest = cache_audio_from_bytes(data, ext=ext or ".ogg")
            elif kind == "image":
                from gateway.platforms.base import cache_image_from_bytes
                dest = cache_image_from_bytes(data, ext=ext or ".jpg")
            else:
                return src
            logger.info("Copied %s blob to Hermes cache: %s -> %s", kind, src, dest)
            return dest
        except Exception as e:
            logger.warning("Could not copy %s to Hermes cache: %s", src, e, exc_info=True)
        return src

    async def _handle_non_text_message(
        self, msg: Dict, chat_id: str, msg_id: str
    ) -> None:
        """Handle non-text messages (files, images, audio, etc.).

        Args:
            msg: Delta Chat message dictionary (AttrDict — keys already snake_case)
            chat_id: Chat ID (string representation)
            msg_id: Message ID (string representation)
        """
        # AttrDict converts viewType → view_type
        view_type = msg.get("view_type", "")
        filename = msg.get("file", "")
        file_mime = msg.get("file_mime", "") or ""

        # If the file isn't available yet (auto-download still in progress),
        # trigger download_full_message and re-fetch once before proceeding.
        if not filename and view_type not in ("Text", "", None):
            logger.info("_handle_non_text_message: file not ready, triggering download for msg %s", msg_id)
            try:
                await self.rpc.download_full_message(self.account_id, int(msg_id))
                await asyncio.sleep(2)
                msg = await self.rpc.get_message(self.account_id, int(msg_id))
                filename = msg.get("file", "")
                file_mime = msg.get("file_mime", "") or ""
                view_type = msg.get("view_type", "")
                logger.info("_handle_non_text_message: after download: file=%r view_type=%r", filename, view_type)
            except Exception as e:
                logger.warning("_handle_non_text_message: download_full_message failed: %s", e)

        logger.info(f"_handle_non_text_message: view_type={view_type}, chat_id={chat_id}, msg_id={msg_id}, filename={filename[:100] if filename else None}")

        # Resolve sender and chat info (shared by all branches)
        from_id = msg.get("from_id")
        user_name = f"Contact {from_id}" if from_id else "Unknown"
        user_id = str(from_id) if from_id else "unknown"
        try:
            if from_id:
                contact = await self.rpc.get_contact(self.account_id, int(from_id))
                user_name = (contact.get("name") or contact.get("display_name")
                             or contact.get("name_and_addr") or user_name)
        except Exception:
            pass

        chat_name = f"Chat {chat_id}"
        chat_type = "dm"
        try:
            chat = await self.rpc.get_basic_chat_info(self.account_id, int(chat_id))
            chat_name = chat.get("name", chat_name)
            chat_type = "group" if chat.get("is_group", False) else "dm"
        except Exception:
            pass

        source = self.build_source(
            chat_id=str(chat_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )

        token = await _get_or_create_chat_token(self.rpc, self.account_id, int(chat_id))

        from deltachat2.types import MessageViewtype

        # DC sometimes reports viewType=Text for image+caption messages.
        # Infer the real type from file_mime when that happens.
        if view_type in ("Text", "", None) and filename and file_mime:
            if file_mime.startswith("image/"):
                view_type = MessageViewtype.IMAGE.value
            elif file_mime.startswith("audio/"):
                view_type = MessageViewtype.AUDIO.value
            elif file_mime.startswith("video/"):
                view_type = MessageViewtype.VIDEO.value

        # Voice / Audio — let Hermes handle STT via media_urls
        if view_type in (MessageViewtype.VOICE.value, MessageViewtype.AUDIO.value) and filename:
            resolved = self._resolve_blob_path(filename)
            if resolved:
                resolved = self._copy_to_hermes_cache(resolved, "audio")
            is_voice = view_type == MessageViewtype.VOICE.value
            hermes_type = MessageType.VOICE if is_voice else MessageType.AUDIO
            caption = msg.get("text", "") or ""
            text = f"[{'Voice' if is_voice else 'Audio'} message from {user_name}]"
            if caption:
                text = f"{text}: {caption}"
            text = f"{text}\n[dc:chat={token}]"
            if not resolved:
                logger.warning(f"Voice/audio file not found, forwarding without media: {filename}")
            message_event = MessageEvent(
                text=text,
                message_type=hermes_type,
                source=source,
                message_id=str(msg_id),
                media_urls=[resolved] if resolved else [],
                media_types=[file_mime or ("audio/ogg" if is_voice else "audio/mpeg")],
            )
            await self.handle_message(message_event)

        # Image
        elif view_type in (MessageViewtype.IMAGE.value, MessageViewtype.GIF.value, MessageViewtype.STICKER.value) and filename:
            resolved = self._resolve_blob_path(filename)
            if resolved:
                resolved = self._copy_to_hermes_cache(resolved, "image")
            caption = msg.get("text", "") or ""
            text = f"[Image from {user_name}]"
            if caption:
                text = f"{text}: {caption}"
            text = f"{text}\n[dc:chat={token}]"
            message_event = MessageEvent(
                text=text,
                message_type=MessageType.PHOTO,
                source=source,
                message_id=str(msg_id),
                media_urls=[resolved] if resolved else [],
                media_types=[file_mime or "image/jpeg"],
            )
            await self.handle_message(message_event)

        # File / document (including .xdc webxdc apps)
        elif view_type in (MessageViewtype.FILE.value, MessageViewtype.VIDEO.value) and filename:
            resolved = self._resolve_blob_path(filename)
            if resolved:
                try:
                    from gateway.platforms.base import cache_document_from_bytes
                    data = open(resolved, "rb").read()
                    file_name = msg.get("file_name") or os.path.basename(resolved)
                    resolved = cache_document_from_bytes(data, file_name)
                    logger.info("Copied document to Hermes cache: %s", resolved)
                except Exception as e:
                    logger.warning("Could not copy document to Hermes cache: %s", e)
            caption = msg.get("text", "") or ""
            file_name = msg.get("file_name") or os.path.basename(filename)
            text = f"[File from {user_name}: {file_name}]"
            if caption:
                text = f"{text}: {caption}"
            text = f"{text}\n[dc:chat={token}]"
            message_event = MessageEvent(
                text=text,
                message_type=MessageType.DOCUMENT,
                source=source,
                message_id=str(msg_id),
                media_urls=[resolved] if resolved else [],
                media_types=[file_mime or "application/octet-stream"],
            )
            await self.handle_message(message_event)

        elif view_type == "Call":
            # DC sends a Call info message (Missed call / Call ended) after calls.
            # The actual call is handled via IncomingCall/CallEnded events — ignore this.
            logger.debug("Ignoring Call info message msg_id=%s text=%r", msg_id, msg.get("text"))

        else:
            logger.debug(f"Unhandled view_type={view_type}, file={filename}")

    async def _handle_audio_message_UNUSED(
        self, msg: Dict, chat_id: str, msg_id: str, filename: str
    ) -> None:
        # KEPT FOR REFERENCE ONLY — superseded by _handle_non_text_message
        # which emits MessageType.VOICE/AUDIO with media_urls and lets Hermes STT handle it.
        """Handle audio/voice message by transcribing and forwarding as text.

        Args:
            msg: Delta Chat message dictionary
            chat_id: Chat ID (string representation)
            msg_id: Message ID (string representation)
            filename: Local filepath to audio file
        """
        import os

        logger.info(f"_handle_audio_message START: chat_id={chat_id}, msg_id={msg_id}, filename={filename}")
        logger.info(f"Original filename: {filename}")
        logger.info(f"File exists at original path: {os.path.exists(filename)}")
        if filename:
            logger.info(f"Absolute path: {os.path.abspath(filename)}")
            logger.info(f"Filename basename: {os.path.basename(filename)}")
        logger.info(f"Message data: msg_type={msg.get('msg_type')}, from_id={msg.get('from_id')}, timestamp={msg.get('timestamp')}")

        if not filename:
            logger.warning("_handle_audio_message: No filename in audio message, cannot process")
            return

        # For Delta Chat, the file might be in blob directory
        if not os.path.exists(filename):
            logger.info("_handle_audio_message: File not at original path, searching blob directory...")
            dc_blob_dir = os.path.join(self._get_dc_config_dir(), "blobs")
            logger.info(f"Blob directory: {dc_blob_dir}, exists: {os.path.exists(dc_blob_dir)}")
            if os.path.exists(dc_blob_dir):
                blob_path = os.path.join(dc_blob_dir, os.path.basename(filename))
                logger.info(f"Trying blob path: {blob_path}, exists: {os.path.exists(blob_path)}")
                if os.path.exists(blob_path):
                    filename = blob_path
                    logger.info(f"Found file in blob dir: {filename}")
                else:
                    blob_path_no_ext = os.path.join(dc_blob_dir, os.path.splitext(os.path.basename(filename))[0])
                    logger.info(f"Trying blob path (no ext): {blob_path_no_ext}, exists: {os.path.exists(blob_path_no_ext)}")
                    if os.path.exists(blob_path_no_ext):
                        filename = blob_path_no_ext
                        logger.info(f"Found file in blob dir (no ext): {filename}")

        if not os.path.exists(filename):
            logger.error(f"_handle_audio_message: Audio file not found at any location: {filename}")
            logger.error("_handle_audio_message: Cannot transcribe - file unavailable")
            # Still notify about the voice message
            from_id = msg.get("from_id")
            user_name = f"Contact {from_id}" if from_id else "Unknown"
            chat_type = "group" if msg.get("is_group", False) else "dm"
            try:
                chat = await self.rpc.get_basic_chat_info(self.account_id, int(chat_id))
                chat_name = chat.get("name", f"Chat {chat_id}")
            except Exception:
                chat_name = f"Chat {chat_id}"
            source = self.build_source(
                chat_id=str(chat_id),
                chat_name=chat_name,
                chat_type=chat_type,
                user_id=str(from_id) if from_id else "unknown",
                user_name=user_name,
            )
            from gateway.platforms.base import MessageEvent, MessageType
            message_event = MessageEvent(
                text=f"[Voice message from {user_name}]",
                message_type=MessageType.TEXT,
                source=source,
                message_id=str(msg_id),
                metadata={"chat_id": str(chat_id), "msg_type": "voice"},
            )
            logger.info("_handle_audio_message: Forwarding notification message (no file)")
            await self.handle_message(message_event)
            return

        # File exists - get stats
        file_size = os.path.getsize(filename)
        logger.info(f"_handle_audio_message: Audio file found: {filename}, size={file_size} bytes")
        logger.info(f"_handle_audio_message: Transcribing audio message: {filename}")

        # Get sender info
        from_id = msg.get("from_id")
        if from_id:
            try:
                contact = await self.rpc.get_contact(self.account_id, int(from_id))
                user_name = (contact.get("name") or contact.get("display_name")
                             or contact.get("name_and_addr") or f"Contact {from_id}")
            except Exception:
                user_name = f"Contact {from_id}"
        else:
            user_name = "Unknown"

        # Get chat info
        chat_name = ""
        try:
            chat = await self.rpc.get_basic_chat_info(self.account_id, int(chat_id))
            chat_name = chat.get("name", f"Chat {chat_id}")
        except Exception:
            chat_name = f"Chat {chat_id}"

        # Try to transcribe
        transcribed_text = None
        transcription_attempted = False
        logger.info("_handle_audio_message: Checking for LLM transcription capability...")
        try:
            # Try Hermes STT
            try:
                from gateway.llm import llm
                logger.info(f"_handle_audio_message: llm module imported, type={type(llm)}")
                has_transcribe = hasattr(llm, 'transcribe_audio_file')
                logger.info(f"_handle_audio_message: llm.transcribe_audio_file available: {has_transcribe}")
                if has_transcribe:
                    transcription_attempted = True
                    logger.info("_handle_audio_message: Calling llm.transcribe_audio_file...")
                    transcription_result = await llm.transcribe_audio_file(filename)
                    logger.info(f"_handle_audio_message: Transcription result: {transcription_result}")
                    if transcription_result and transcription_result.get("text"):
                        transcribed_text = transcription_result["text"]
                        logger.info(f"_handle_audio_message: Transcribed text (first 200 chars): {transcribed_text[:200]}")
                    else:
                        logger.warning("_handle_audio_message: Transcription returned empty or no text field")
                else:
                    logger.warning("_handle_audio_message: LLM does NOT have transcribe_audio_file method")
            except Exception as e:
                import traceback
                logger.warning(f"_handle_audio_message: llm.transcribe_audio_file failed: {e}")
                logger.debug(f"_handle_audio_message: llm.transcribe_audio_file traceback:\n{traceback.format_exc()}")
        except Exception as e:
            import traceback
            logger.warning(f"_handle_audio_message: Transcription outer exception: {e}")
            logger.debug(f"_handle_audio_message: Transcription traceback:\n{traceback.format_exc()}")

        # Build response
        chat_type = "group" if msg.get("is_group", False) else "dm"
        source = self.build_source(
            chat_id=str(chat_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(from_id) if from_id else "unknown",
            user_name=user_name,
        )

        from gateway.platforms.base import MessageEvent, MessageType

        if transcribed_text:
            full_text = f"[Voice message from {user_name}]: {transcribed_text}"
            logger.info("_handle_audio_message: SUCCESS - voice message transcribed and will be forwarded")
        else:
            full_text = f"[Voice message from {user_name}]"
            if transcription_attempted:
                logger.warning("_handle_audio_message: Transcription attempted but returned no text")
            else:
                logger.warning("_handle_audio_message: NO TRANSCRIPTION - llm.transcribe_audio_file not available, file will be forwarded as notification only")

        logger.debug(f"_handle_audio_message: Final message text: {full_text[:150]}")
        message_event = MessageEvent(
            text=full_text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(msg_id),
            metadata={
                "chat_id": str(chat_id),
                "from_id": str(from_id) if from_id else "unknown",
                "filename": filename,
                "msg_type": "voice",
                "timestamp": msg.get("timestamp"),
                "transcribed": transcribed_text is not None,
                "file_size": file_size,
            },
        )
        logger.info("_handle_audio_message: Forwarding message to Hermes...")
        await self.handle_message(message_event)
        logger.info("_handle_audio_message: COMPLETE - Audio message handled successfully")
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
            "Messages longer than 40 lines will be automatically formatted with HTML. "
            "For very long content, consider sending as a document file instead. "
            "You CAN send voice messages (use send_voice tool), videos, images, files, and delete messages. "
            "When a user sends a voice message, it is automatically transcribed — just respond to the transcribed content normally. "
            "Location messages can be sent to share points of interest on a map. "
            "You CAN build and send webxdc mini apps and other files (PDF, HTML, etc.). "
            "MANDATORY: before attempting to build any webxdc app, you MUST first call "
            "skill_view('plugin:deltachat-platform:webxdc-converter') to load the build instructions. "
            "For file delivery from the Docker sandbox: write output files to /workspace/ (NOT /tmp/), "
            "then use a MEDIA directive — e.g. 'MEDIA:/workspace/app.xdc'. "
            "The adapter maps /workspace/ paths to the host and sends via send_document. "
            "DC core auto-detects .xdc as webxdc — just send it as a regular file. "
            "Each message ends with a [dc:chat=<token>] metadata tag. "
            "IGNORE this tag during normal conversation — it is only needed if you call dc_safe_rpc_call. "
            "Do NOT call dc_safe_rpc_call, dc_chat_rpc_spec, or dc_rpc_spec unless the user explicitly "
            "asks for a Delta Chat-specific operation that cannot be done with the standard tools."
        ),
        max_message_length=3200,
    )

    # Register bundled skills so skill_view('deltachat-platform:<name>') resolves them.
    from pathlib import Path as _Path
    skills_dir = _Path(_plugin_dir) / "skills"
    logger.info(f"Checking for skills in: {skills_dir}")
    if skills_dir.is_dir():
        for skill_dir in skills_dir.iterdir():
            skill_md = skill_dir / "SKILL.md"
            if skill_md.is_file():
                try:
                    ctx.register_skill(skill_dir.name, skill_md)
                    logger.info("Registered plugin skill: %s from %s", skill_dir.name, skill_md)
                except Exception as e:
                    logger.warning("Could not register skill %s: %s", skill_dir.name, e)
    else:
        logger.warning("Skills directory not found: %s", skills_dir)


def register_rpc_tools(ctx) -> None:
    """Register Delta Chat RPC tools.

    Always registers:
      - dc_rpc_spec: full OpenRPC spec
      - dc_chat_rpc_spec: spec filtered to chatId-scoped, non-destructive methods
      - dc_safe_rpc_call: chat-scoped calls with token-validated chatId injection

    Only registers when DELTACHAT_ENABLE_RAW_RPC is set:
      - dc_rpc_call: unrestricted access to any RPC method
    """

    async def _spec_handler(args: dict = None, **kwargs) -> str:
        try:
            return json.dumps(await _fetch_spec(), indent=2)
        except Exception as e:
            return f"Error: {e}"

    async def _call_handler(args: dict, **kwargs) -> str:
        method = (args or {}).get("method")
        params = (args or {}).get("params") or []
        if not method or not isinstance(method, str):
            return json.dumps({"error": "Missing 'method' (snake_case RPC name)."})
        if _active_adapter is None or _active_adapter.rpc is None:
            return json.dumps({"error": "Delta Chat is not connected"})
        try:
            result = await getattr(_active_adapter.rpc, method)(*params)
            return json.dumps(result, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _chat_spec_handler(args: dict = None, **kwargs) -> str:
        """Return only the chatId-scoped, non-destructive methods."""
        try:
            spec = await _fetch_spec()
        except Exception as e:
            return f"Error: {e}"
        safe_methods = [
            m for m in spec.get("methods", [])
            if any(p["name"] == "chatId" for p in m.get("params", []))
            and m["name"] not in _DESTRUCTIVE_METHODS
            and not m["name"].startswith("delete_")
            and not m["name"].startswith("remove_")
        ]
        return json.dumps({**spec, "methods": safe_methods}, indent=2)

    async def _safe_call_handler(args: dict, **kwargs) -> Any:
        method = (args or {}).get("method")
        chat_token = (args or {}).get("chat_token")
        params = (args or {}).get("params") or []
        if not method or not isinstance(method, str):
            return json.dumps({"error": "Missing 'method' (snake_case RPC name). Use dc_chat_rpc_spec to find one."})
        adapter = _active_adapter
        if adapter is None or adapter.rpc is None:
            return {"error": "Delta Chat is not connected"}

        # Resolve token → real chat_id
        real_chat_id = await _resolve_chat_token(adapter.rpc, adapter.account_id, chat_token)
        if real_chat_id is None:
            return json.dumps({"error": "Unknown chat_token — use the [dc:chat=...] value from your message"})

        # Block destructive methods
        if (
            method in _DESTRUCTIVE_METHODS
            or method.startswith("delete_")
            or method.startswith("remove_")
        ):
            return json.dumps({"error": f"'{method}' is not allowed in safe mode"})

        # Verify method exists and has a chatId param
        try:
            spec = await _fetch_spec()
        except Exception as e:
            return json.dumps({"error": f"Could not fetch spec: {e}"})

        method_entry = next((m for m in spec.get("methods", []) if m["name"] == method), None)
        if method_entry is None:
            return json.dumps({"error": f"Unknown method '{method}' — use dc_chat_rpc_spec to browse available methods"})

        param_names = [p["name"] for p in method_entry.get("params", [])]
        if "chatId" not in param_names:
            return json.dumps({"error": f"'{method}' has no chatId parameter — use dc_rpc_call for non-chat methods"})

        # Build positional params: accountId at [0], chatId at [1]
        full_params = [adapter.account_id, real_chat_id] + list(params or [])

        try:
            result = await getattr(adapter.rpc, method)(*full_params)
            return json.dumps(result, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _end_call_handler(args: dict, **kwargs) -> str:
        adapter = _active_adapter
        if adapter is None or adapter._call_manager is None:
            return json.dumps({"error": "No active call"})

        # The AI is in a call — find the active session.
        # There is typically only one active call at a time.
        chat_ids = list(adapter._call_manager._chat_to_msg.keys())
        if not chat_ids:
            return json.dumps({"error": "No active call"})

        success = await adapter._call_manager.request_hangup(chat_ids[0])
        if success:
            return json.dumps({"success": True, "message": "Call ended"})
        return json.dumps({"error": "Failed to end call"})

    async def _start_call_handler(args: dict, **kwargs) -> str:
        args = args or {}
        chat_token = args.get("chat_token")
        # `opening` is the exact line spoken on connect; accept `topic` as alias.
        opening = (args.get("opening") or args.get("topic") or "").strip()
        adapter = _active_adapter
        if adapter is None or adapter._call_manager is None:
            return json.dumps({"error": "Delta Chat not connected"})

        if not opening:
            return json.dumps({"error": "Provide 'opening' — the exact words to say when they pick up."})

        real_chat_id = await _resolve_chat_token(adapter.rpc, adapter.account_id, chat_token)
        if real_chat_id is None:
            return json.dumps({"error": "Unknown chat_token — use the [dc:chat=...] value"})

        try:
            msg_id = await adapter._call_manager.start_call(str(real_chat_id), opening=opening)
            return json.dumps({"success": True, "msg_id": msg_id,
                               "message": "Call connected — the opening line is being "
                                          "spoken and the conversation is live."})
        except asyncio.TimeoutError:
            return json.dumps({"error": "Call was not answered"})
        except Exception as e:
            logger.error("start_call failed: %s", e, exc_info=True)
            return json.dumps({"error": f"Failed to start call: {e}"})

    ctx.register_tool(
        name="dc_rpc_spec",
        toolset="deltachat",
        schema={
            "description": (
                "Fetch the full OpenRPC specification of the running Delta Chat RPC server. "
                "Lists every available method with parameter types and descriptions. "
                "Only call this when the user explicitly asks for low-level Delta Chat API access. "
                "Use dc_chat_rpc_spec instead when you only need chat-scoped methods."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_spec_handler,
        is_async=True,
        emoji="📋",
    )

    ctx.register_tool(
        name="dc_chat_rpc_spec",
        toolset="deltachat",
        schema={
            "description": (
                "Fetch the OpenRPC spec filtered to methods that accept a chatId parameter, "
                "excluding all destructive operations. "
                "Only call this when you are about to use dc_safe_rpc_call for an explicit user request "
                "that cannot be handled by normal messaging tools."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_chat_spec_handler,
        is_async=True,
        emoji="📋",
    )

    if os.getenv("DELTACHAT_ENABLE_RAW_RPC"):
        ctx.register_tool(
            name="dc_rpc_call",
            toolset="deltachat",
            schema={
                "description": (
                    "Call any Delta Chat RPC method directly by name and params. "
                    "Use dc_rpc_spec first to see available methods. "
                    "CAUTION: unrestricted access — can modify or delete account data. "
                    "Prefer dc_safe_rpc_call for chat-scoped operations."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "description": (
                                "RPC method name in snake_case (e.g. 'get_account_info'). "
                                "Use dc_rpc_spec to see all available methods."
                            ),
                        },
                        "params": {
                            "type": "array",
                            "description": "Full positional parameters. account_id is always 1.",
                            "default": [],
                        },
                    },
                    "required": ["method"],
                },
            },
            handler=_call_handler,
            is_async=True,
            emoji="⚡",
        )

    ctx.register_tool(
        name="dc_safe_rpc_call",
        toolset="deltachat",
        schema={
            "description": (
                "Call a chat-scoped Delta Chat RPC method safely. "
                "Only use this when the user explicitly asks for a Delta Chat-specific operation "
                "that cannot be done with the normal send, send_file, send_voice, or delete_message tools. "
                "Do NOT call this for routine message handling, reading messages, or sending replies — "
                "those go through the standard tools. "
                "accountId and chatId are injected automatically from the chat_token. "
                "Destructive methods are blocked. Use dc_chat_rpc_spec first to find the method name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "description": (
                            "RPC method name in snake_case (e.g. 'get_chat_contacts'). "
                            "Must accept chatId. Use dc_chat_rpc_spec to browse available methods."
                        ),
                    },
                    "chat_token": {
                        "type": "string",
                        "description": (
                            "The opaque chat token from the [dc:chat=...] line "
                            "in the current message. Never use a token from a different conversation."
                        ),
                    },
                    "params": {
                        "type": "array",
                        "description": (
                            "Extra positional parameters after accountId and chatId. "
                            "accountId (always 1) and chatId are injected automatically."
                        ),
                        "default": [],
                    },
                },
                "required": ["method", "chat_token"],
            },
        },
        handler=_safe_call_handler,
        is_async=True,
        emoji="🔒",
    )

    ctx.register_tool(
        name="dc_end_call",
        toolset="deltachat",
        schema={
            "description": (
                "End the active voice call. "
                "The goodbye message is spoken first (via normal send), then this "
                "tool waits until TTS finishes playing before disconnecting. "
                "Only use this when the user explicitly says goodbye or asks to end the call. "
                "No parameters needed — there is only one active call at a time."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_end_call_handler,
        is_async=True,
        emoji="📞",
    )

    ctx.register_tool(
        name="dc_start_call",
        toolset="deltachat",
        schema={
            "description": (
                "Place an outgoing voice call to a Delta Chat contact and talk to them. "
                "Use this to proactively call someone — e.g. from a scheduled/cron task "
                "(a reminder, an alert, a check-in). Creates the WebRTC offer, rings the "
                "contact, and blocks until they answer (or times out if unanswered). "
                "Once connected you speak normally; the conversation runs like an incoming "
                "call. Identify the recipient with the chat_token from one of their messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_token": {
                        "type": "string",
                        "description": (
                            "The opaque chat token from the [dc:chat=...] line in a message "
                            "from the person to call. Never use a token from another conversation."
                        ),
                    },
                    "opening": {
                        "type": "string",
                        "description": (
                            "The EXACT words to say the instant they pick up "
                            "(e.g. \"Hi Simon, quick reminder to take your medication.\"). "
                            "Synthesized while the phone is still ringing and played "
                            "immediately on answer — no startup delay. Write it as natural "
                            "speech, not a topic label."
                        ),
                    },
                },
                "required": ["chat_token", "opening"],
            },
        },
        handler=_start_call_handler,
        is_async=True,
        emoji="📞",
    )
