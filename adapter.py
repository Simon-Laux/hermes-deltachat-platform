"""Delta Chat platform adapter for Hermes Gateway.

Integrates Delta Chat as a messaging platform using deltachat2 (direct JSON-RPC).
"""

import functools
import html
import json
import os
import random
import re
import secrets
import sys
import asyncio
import logging
from typing import Optional, Dict, Any, List

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

# Delta Chat core version window. Below the minimum the plugin refuses to
# connect; above the tested ceiling it connects but warns.
#
# why two constants: DC core does not use semver. Major is reserved for heavily
# breaking wire-format changes and patch is never used, so a *minor* bump is
# just a counter that may still remove JSON-RPC fields — 2.61.0 dropped
# `Account::Configured.addr` and `Contact.nameAndAddr`, both of which this
# adapter reads. A single MIN_DC_VERSION therefore cannot express "known good":
# everything above it would be both allowed and suspect, which is why the
# warning used to fire on every connect for versions we had in fact verified.
MIN_DC_VERSION = "2.51.0"

# Newest release whose full RPC surface was verified against its `--openrpc`
# spec and a live server. Bump it after re-verifying, and keep the ceiling in
# plugin.yaml's `python_dependencies` in step — it is the same claim.
MAX_TESTED_DC_VERSION = "2.60.0"

# Contact id core uses for this account itself (DC_CONTACT_ID_SELF).
DC_CONTACT_ID_SELF = 1

# ---------------------------------------------------------------------------
# Headless onboarding
# ---------------------------------------------------------------------------
# Creating an account is a side effect with a cost outside this machine: it
# registers on somebody else's chatmail relay. So it is strictly opt-in — with
# no onboarding env var set we keep the old behaviour of refusing to start and
# pointing at setup.py. An accounts dir can look empty for boring reasons (wrong
# HERMES_HOME, unmounted volume), and silently replacing an identity that
# contacts have already verified is worse than refusing to boot.

# Registered first in auto mode so the account gets the same first address
# across rebuilds; the extra relays are drawn at random purely for redundancy.
# Which transport DC then treats as the account's *primary* address is not
# verified here — see docs/headless-onboarding.md.
ANCHOR_RELAY = "nine.testrun.org"
AUTO_RELAY_COUNT = 3


def _parse_relay_list(raw: str) -> List[str]:
    """Split a comma-separated relay list into bare hostnames."""
    hosts = []
    for chunk in raw.split(","):
        host = chunk.strip().replace("https://", "").strip("/")
        if host and host not in hosts:
            hosts.append(host)
    return hosts


_SETUP_MODULE_NAME = "deltachat_platform_setup"


def _load_setup_module():
    """Import this plugin's setup.py by explicit path, under a unique name.

    A bare `import setup` would resolve against whatever is first on sys.path
    — and `setup` is about the most collision-prone module name in Python
    (every setuptools project root has one). Loading by path removes the
    ambiguity instead of relying on sys.path ordering.
    """
    if _SETUP_MODULE_NAME in sys.modules:
        return sys.modules[_SETUP_MODULE_NAME]

    import importlib.util

    path = os.path.join(_plugin_dir, "setup.py")
    # spec_from_file_location happily builds a spec for a path that does not
    # exist; the failure only surfaces as FileNotFoundError inside
    # exec_module. Check up front so callers get a coherent ImportError.
    if not os.path.isfile(path):
        raise ImportError(f"Plugin setup module not found at {path}")

    spec = importlib.util.spec_from_file_location(_SETUP_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin setup module from {path}")

    module = importlib.util.module_from_spec(spec)
    # Registered before exec_module so a partially-initialised module can't be
    # loaded twice concurrently — but pulled back out if exec fails, or the
    # cache would serve that half-built module to every later caller.
    sys.modules[_SETUP_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_SETUP_MODULE_NAME, None)
        raise
    return module


def _get_relay_servers() -> List[str]:
    """Fetch the live chatmail relay list (blocking HTTP; keep off the loop)."""
    return _load_setup_module().get_relay_servers()


def _auto_relays(count: int = AUTO_RELAY_COUNT) -> List[str]:
    """Pick relays for `DELTACHAT_EMAIL=auto`: the anchor plus random others.

    Delta Chat core supports several transports on one account, so registering
    on more than one relay buys redundancy if a relay goes away. Falls back to
    the anchor alone when the relay list can't be fetched.
    """
    try:
        available = _get_relay_servers()
    except Exception as e:
        logger.warning("Could not fetch chatmail relay list (%s)", e)
        available = []

    others = [h for h in available if h != ANCHOR_RELAY]
    random.shuffle(others)
    return [ANCHOR_RELAY] + others[: max(0, count - 1)]


def _headless_onboarding() -> Optional[Dict[str, Any]]:
    """Return onboarding parameters, or None when not opted in.

    - DELTACHAT_EMAIL=<address>  -> configure that mailbox (needs a password)
    - DELTACHAT_EMAIL=auto       -> mint a chatmail account
    - DELTACHAT_CHATMAIL_SERVERS -> mint a chatmail account on those relays
    """
    email = (os.getenv("DELTACHAT_EMAIL") or "").strip()
    servers = (os.getenv("DELTACHAT_CHATMAIL_SERVERS") or "").strip()

    if not email and not servers:
        return None
    if email and email.lower() != "auto":
        return {"mode": "email", "email": email}
    return {"mode": "chatmail", "relays": _parse_relay_list(servers)}


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
        True if version is compatible, False if it is too old or could not be
        determined at all. Fail-closed on both counts.
    """
    try:
        # Get system info which includes version
        system_info = await rpc.get_system_info()
        dc_version_str = system_info.get("deltachat_core_version", "0.0.0")
        dc_version = _parse_version(dc_version_str)
        min_version = _parse_version(MIN_DC_VERSION)
        max_tested = _parse_version(MAX_TESTED_DC_VERSION)

        if dc_version < min_version:
            logger.error(
                f"Delta Chat version {dc_version_str} is too old. "
                f"This plugin requires {MIN_DC_VERSION} or higher. "
                f"Please update your Delta Chat installation."
            )
            return False
        elif dc_version > max_tested:
            # Only above the tested ceiling. Warning on everything above the
            # *minimum* cried wolf on versions that were in fact verified, and
            # a warning nobody can act on is one everybody learns to ignore.
            logger.warning(
                f"Delta Chat version {dc_version_str} is newer than the newest "
                f"version this plugin was tested against ({MAX_TESTED_DC_VERSION}). "
                f"Core minor bumps can remove JSON-RPC fields, so contact names or "
                f"the bot's own address may come out blank. Please report problems."
            )

        return True

    except Exception as e:
        # Refuse rather than fall through. A malformed or missing version
        # string is already handled — _parse_version returns (0, 0, 0), which
        # compares as too old above. Reaching here means get_system_info()
        # itself raised, i.e. the RPC transport is broken, and every call after
        # this one would fail too. One clear error beats the cascade.
        logger.error(f"Could not check Delta Chat version: {e}")
        return False


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


def _env_flag(name: str) -> bool:
    """True when *name* is set to something that reads as "on".

    why: plain truthiness on os.getenv treats "0", "false" and "off" as enabled,
    because they are non-empty strings. That is a fail-open kill switch — and
    plugin.yaml prompts the operator for these, which invites exactly a "0".
    """
    return _is_on(os.getenv(name, ""))


def _is_on(value) -> bool:
    """Shared on/off rule for env vars and config.yaml values (see _env_flag)."""
    return str(value).strip().lower() not in ("", "0", "false", "no", "off")


# Methods the RPC tools refuse, beyond the delete_*/remove_* prefix rule.
# Each is here for what it does, not for what it is called.
_BLOCKED_METHODS = frozenset({
    # --- destroys or detaches ---
    "leave_group",                     # detaches the bot from a chat, irreversibly
    "set_chat_ephemeral_timer",        # timed deletion — delete_messages by another
                                       # name. The prefix rule sees names, not effects
    # --- reaches outside the chat the token scopes ---
    "forward_messages",                # messageIds are global and unscoped, so this
                                       # copies messages *out of any private chat*
                                       # into this one. The token scopes the
                                       # destination and does nothing about the source
    "add_contact_to_chat",             # adds a stranger to a private group. Note the
                                       # asymmetry the prefix rule creates on its own:
                                       # remove_contact_from_chat is blocked, this is not
    # --- hands out credentials ---
    "get_chat_securejoin_qr_code",     # the QR *text* is the invite — whoever reads it
    "get_chat_securejoin_qr_code_svg", # can join the verified group. A read method, but
                                       # what it returns is a credential, not data.
                                       # (The adapter still calls these directly for its
                                       # own startup invite link; only the tools refuse.)
    # --- leaks the device, not the chat ---
    "send_locations_to_chat",          # starts streaming real device location for N
                                       # seconds. get_locations stays allowed: reading
                                       # what contacts chose to share is opt-in by them
    # --- hides the conversation from the bot's own operator ---
    "set_chat_mute_duration",          # muting is meaningless for a bot and only ever
    "set_chat_visibility",             # serves to hide traffic from the gateway side
    "block_chat",                      # silences a conversation; cannot be undone over
                                       # chat by the person who was silenced
    # --- reachable by a better route, or not ours to touch ---
    "place_outgoing_call",             # dc_start_call is the purpose-built tool, with
                                       # the opening-line handling this lacks
    "init_webxdc_integration",         # internal plumbing for Delta Chat's own map
                                       # feature, not an integration point for us
})


def _is_blocked(method: str) -> bool:
    """True for methods the RPC tools refuse.

    Two rules. The prefix half is deliberately open-ended: the OpenRPC surface
    grows with every core release, and a new delete_* method should be blocked
    the day it appears, not the day we notice it. The named half above covers
    what the prefixes cannot see.

    Named rather than "destructive" because the list outgrew that: it now also
    covers disclosure (securejoin QR), reaching outside the token's scope
    (forward_messages) and methods better reached another way.

    It is still a *name* rule, so it bounds names rather than capabilities. It
    does not stop set_config(delete_device_after), which wipes the whole message
    store under an innocuous name. File paths (send_msg's data.file and
    friends, #32) are a parameter problem, not a name problem, and are checked
    in the safe call handler. The allowlist is the real control (#22).

    Draft methods are deliberately absent *from the named set*: the agent
    writing and clearing its own drafts is ordinary use. Note remove_draft is
    still refused, by the prefix rule rather than by choice.
    """
    return method in _BLOCKED_METHODS or method.startswith(("delete_", "remove_"))


# Spec parameter names that carry a local filesystem path for core to read.
# send_msg's path is nested as data.file and handled separately.
_PATH_PARAMS = frozenset({"file", "imagePath", "stickerPath"})
# Names that look like paths but are not: filename is the display name only.
_NOT_PATH_PARAMS = frozenset({"filename"})


def _unchecked_path_name(names) -> Optional[str]:
    """First name that looks like a path but has no handling here, else None.

    why: _PATH_PARAMS matches today's spec by name, and the spec is fetched
    from whatever core is installed. A future core that renames `file` or adds
    a chat-scoped `filePath` would otherwise slip past unchecked; refusing
    unknown path-shaped names makes that fail closed instead.
    """
    for name in names:
        lowered = name.lower()
        if (("file" in lowered or "path" in lowered)
                and name not in _PATH_PARAMS and name not in _NOT_PATH_PARAMS):
            return name
    return None


def _safe_delivery_path(adapter, path) -> Optional[str]:
    """The validated host path for `path`, or None if delivery policy refuses it.

    Hermes' policy denylists its own secrets (.env, state.db, ~/.ssh) but knows
    nothing about ours: the Delta Chat account dir holds dc.db (PGP secret key,
    mail password, every chat), accounts.toml and the securejoin invite.txt,
    and core keeps dc.db fresh enough to pass even strict mode's recency rule.
    The logs carry message content too. Refuse both on top of Hermes' check.
    """
    from gateway.config import get_hermes_home

    if not isinstance(path, str):
        return None
    safe = adapter.filter_local_delivery_paths([path])
    if not safe:
        return None
    resolved = os.path.realpath(safe[0])
    for protected in (adapter._get_dc_config_dir(), os.path.join(get_hermes_home(), "logs")):
        root = os.path.realpath(protected)
        if resolved == root or resolved.startswith(root + os.sep):
            return None
    return safe[0]


def _refuse_path(method: str, path) -> str:
    # %r for the same reason as the raw RPC audit line: the path is model-supplied.
    logger.warning("Safe RPC call %r REFUSED (unsafe file path): %r", method, path)
    return json.dumps({
        "error": (
            f"'{method}': file path refused — it does not exist on this host "
            "or lies under a location the delivery policy protects"
        )
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


def _quote_id(reply_to) -> Optional[int]:
    """DC message id to quote, or None when reply_to is not a real DC message.

    why: Hermes anchors replies on the triggering event's message_id, and our
    synthetic events (call notes) carry non-numeric ids; int() on those failed
    the whole send instead of just sending it unquoted.
    """
    s = str(reply_to or "").strip()
    return int(s) if s.isdigit() else None


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
        self._fatal_notify_task: Optional[asyncio.Task] = None
        # why: _running is *shared* with BasePlatformAdapter — it is both this
        # listener's loop condition and the base's `is_connected`. That is what
        # makes the escalation guard in _event_listener correct (the loop can
        # only go false via a deliberate teardown), but it also means you cannot
        # stop the loop without also declaring the adapter disconnected.
        self._running = False
        self._dc_config_dir: Optional[str] = None
        self._call_manager = None
        self._invite_link: Optional[str] = None

        # Group mention gating (opt-in; DMs are never gated). Set
        # platforms.deltachat-platform.require_mention / mention_aliases in
        # config.yaml (Hermes copies them into config.extra), or the
        # DELTACHAT_REQUIRE_MENTION / DELTACHAT_MENTION_ALIASES env vars.
        extra = self.config.extra or {}
        raw = extra.get("require_mention")
        self._require_mention = (
            _env_flag("DELTACHAT_REQUIRE_MENTION") if raw is None
            else _is_on(raw)
        )
        raw = extra.get("mention_aliases")
        if raw is None:
            raw = os.getenv("DELTACHAT_MENTION_ALIASES", "")
        if isinstance(raw, str):
            raw = raw.split(",")
        # "@spooky" and "spooky" both mean the alias spooky
        self._mention_aliases = [a for a in (str(a).strip().lstrip("@").strip() for a in raw) if a]
        self._warned_no_mention_names = False

    async def _intake_allows(self, msg: Dict, chat_id) -> bool:
        """Drop what Hermes' own authorization can't judge, and leave groups
        no approved contact is a member of.

        Who may talk to the agent is Hermes' decision (pairing), keyed on the
        contact ID. That only identifies a person for key-contacts: in Delta
        Chat identity is the key, and a message without one (unencrypted
        classic email) says nothing reliable about its sender. Checked first,
        so such a sender never makes us do anything visible, like leaving.
        """
        from_id = msg.get("from_id")
        try:
            contact = await self.rpc.get_contact(self.account_id, int(from_id))
        except Exception as e:
            logger.warning("Dropping message %s: could not load sender %s: %s",
                           msg.get("id"), from_id, e)
            return False
        if not contact.get("is_key_contact"):
            logger.debug("Dropping message %s from non-key contact %s", msg.get("id"), from_id)
            return False
        if await self._group_has_no_approved_member(chat_id):
            logger.info("Leaving group %s: none of its members is approved", chat_id)
            try:
                await self.rpc.leave_group(self.account_id, int(chat_id))
                await self.rpc.delete_chat(self.account_id, int(chat_id))
            except Exception as e:
                logger.warning("Could not leave group %s: %s", chat_id, e)
            return False
        return True

    async def _group_has_no_approved_member(self, chat_id) -> bool:
        """True only if *chat_id* is a group and Hermes rejects every member.

        Delta Chat doesn't record who added us to a group (a new group sends
        no "member added" message at all), so membership is what we judge.
        Asked the way Hermes judges their messages in this group, so a group
        Hermes would answer is never left. Anything unknown — an RPC error,
        no auth check wired (None) — keeps us in.
        """
        try:
            chat = await self.rpc.get_basic_chat_info(self.account_id, int(chat_id))
            if chat.get("chat_type") != "Group":
                return False
            members = [c for c in await self.rpc.get_chat_contacts(self.account_id, int(chat_id))
                       if c != DC_CONTACT_ID_SELF]
        except Exception as e:
            logger.warning("Could not check members of chat %s: %s", chat_id, e)
            return False
        return bool(members) and all(
            self._is_sender_authorized(str(c), "group", str(chat_id)) is False for c in members)

    async def _mention_gate_allows(self, msg: Dict, chat_id) -> bool:
        """Decide whether a message is for us.

        Commands are addressed Telegram-style, "/cmd@<name>": one addressed to
        us passes with the "@<name>" removed from msg["text"] so Hermes sees a
        plain "/cmd" (in any chat). In a group, one addressed to anyone else
        is dropped, whether or not require_mention is on. Only plain text
        messages are commands — a caption never is.

        With require_mention on, any other group message needs a mention:
        "@<display name>" or "@<alias>" (case-insensitive, whole word) in the
        message's own text or caption — quoted text is not part of it. A
        bare "/cmd" is no exception, so in a group with several bots it only
        reaches the one it names. A quote-reply to one of our own messages
        counts as a mention (also for a bare "/cmd") so a thread can go on
        without repeating it.
        """
        text = msg.get("text") or ""
        is_plain_text = (msg.get("view_type") in ("Text", "", None)
                         and not (msg.get("file") or msg.get("file_mime")))
        command = re.match(r"/[\w-]+@", text) if is_plain_text else None
        if not self._require_mention and not command:
            return True
        try:
            chat = await self.rpc.get_basic_chat_info(self.account_id, int(chat_id))
            is_group = chat.get("chat_type") == "Group"
            if not is_group and not command:
                return True
            # why: read per message, so a renamed bot is matched under its new name.
            names = [n.strip() for n in (await self.rpc.get_config(self.account_id, "displayname"),
                                         *self._mention_aliases) if n and n.strip()]
        except Exception as e:
            logger.warning("Mention gate failed for chat %s, letting message through: %s",
                           chat_id, e)
            return True
        # why: longest first, so with names "Hermes" and "Hermes Bot",
        # "/reset@Hermes Bot" strips the whole name instead of leaving " Bot".
        names.sort(key=len, reverse=True)
        if command:
            addressee = text[command.end():]
            for n in names:
                m = re.match(re.escape(n) + r"(?![\w-])", addressee, re.IGNORECASE)
                if m:
                    msg["text"] = text[:command.end() - 1] + addressee[m.end():]
                    return True
            if not is_group:
                return True
            logger.debug("Ignoring command %s addressed to another bot", text.split()[0])
            return False
        if not is_group:
            return True
        if not names and not self._warned_no_mention_names:
            self._warned_no_mention_names = True
            logger.warning("require_mention is on but the account has no display name and no "
                           "DELTACHAT_MENTION_ALIASES are set: only quote-replies to the bot "
                           "will get through in groups")
        if any(re.search(r"(?<![\w@])@" + re.escape(n) + r"(?![\w-])", text, re.IGNORECASE)
               for n in names):
            return True
        if await self._quotes_own_message(msg):
            return True
        logger.debug("Ignoring unmentioned group message %s (require_mention)",
                     msg.get("id"))
        return False

    async def _quotes_own_message(self, msg: Dict) -> bool:
        """True if *msg* quote-replies to a message this account sent."""
        quote = msg.get("quote") or {}
        if quote.get("kind") != "WithMessage" or not quote.get("message_id"):
            return False
        try:
            quoted = await self.rpc.get_message(self.account_id, int(quote["message_id"]))
        except Exception as e:
            # e.g. the quoted message was deleted locally: no proof it was ours
            logger.debug("Could not load quoted message %s: %s", quote["message_id"], e)
            return False
        return bool(quoted) and quoted.get("from_id") == DC_CONTACT_ID_SELF

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

    @staticmethod
    def _forget_password() -> None:
        """Drop DELTACHAT_PASSWORD from the process environment.

        Delta Chat core has persisted its own copy of the credentials by the
        time a transport is configured, so nothing downstream needs the plain
        value — but os.environ is readable by anything running in this process
        (including agent tooling) and is inherited by subprocesses.

        Only called after a *successful* configure: on failure the value has to
        survive so that a later reconnect can retry.
        """
        if os.environ.pop("DELTACHAT_PASSWORD", None) is not None:
            logger.debug("Cleared DELTACHAT_PASSWORD from the process environment")

    async def _configure_transports(self, onboarding: Dict[str, Any]) -> bool:
        """Attach a transport to the account from env-var config."""
        if onboarding["mode"] == "email":
            return await self._configure_email_transport(onboarding["email"])
        return await self._configure_chatmail_transports(onboarding["relays"])

    async def _configure_email_transport(self, email: str) -> bool:
        """Configure an existing mailbox as the account transport."""
        password = os.getenv("DELTACHAT_PASSWORD") or ""
        if not password:
            logger.error(
                "DELTACHAT_EMAIL is set to %s but DELTACHAT_PASSWORD is empty. "
                "Use DELTACHAT_EMAIL=auto for a chatmail account instead.",
                email,
            )
            return False

        try:
            # add_or_update_transport configures and blocks until finished;
            # the separate configure() call is deprecated as of DC 2025-02.
            await self.rpc.add_or_update_transport(
                self.account_id, {"addr": email, "password": password}
            )
        except Exception as e:
            logger.error("Could not configure transport for %s: %s", email, e)
            return False

        self._forget_password()
        logger.info("Configured Delta Chat transport for %s", email)
        return True

    async def _configure_chatmail_transports(self, relays: List[str]) -> bool:
        """Register on one or more chatmail relays.

        Multiple relays are redundancy, not a requirement — one working
        transport is enough to succeed, so individual failures only warn.
        """
        if not relays:
            # _auto_relays() scrapes chatmail.at over blocking urllib with a
            # 10s timeout — same reason _AsyncRpc exists, keep it off the loop.
            loop = asyncio.get_running_loop()
            relays = await loop.run_in_executor(None, _auto_relays)
        logger.info("Onboarding via chatmail relay(s): %s", ", ".join(relays))

        added = []
        for host in relays:
            try:
                await self.rpc.add_transport_from_qr(
                    self.account_id, f"dcaccount:{host}"
                )
                added.append(host)
                logger.info("Registered chatmail transport on %s", host)
            except Exception as e:
                logger.warning("Chatmail relay %s failed: %s", host, e)

        if not added:
            logger.error(
                "No chatmail relay accepted a registration (tried: %s)",
                ", ".join(relays),
            )
            return False
        return True

    async def _publish_invite_link(self) -> None:
        """Log and persist the SecureJoin invite link.

        Delta Chat needs this link for the initial key exchange — adding the
        bot's address by hand does not establish an encrypted session. setup.py
        prints it to a terminal, but with headless onboarding nobody is
        watching one, so it goes to a 0600 file in the accounts dir and the
        gateway log points at that file.

        The link is deliberately *not* logged at INFO. Under the `pairing` DM
        policy, completing SecureJoin is what makes a contact verified — so
        whoever holds this link can reach the agent. gateway.log is created
        world-readable (0644), so the link only appears there at DEBUG, or as
        a last resort when the file cannot be written.
        """
        try:
            link = await self.rpc.get_chat_securejoin_qr_code(self.account_id, None)
        except Exception as e:
            logger.warning("Could not generate SecureJoin invite link: %s", e)
            return

        if not link:
            return

        self._invite_link = link
        logger.debug("Delta Chat invite link: %s", link)

        path = os.path.join(self._get_dc_config_dir(), "invite.txt")
        try:
            # os.open with an explicit mode instead of open()+chmod: no window
            # where the link sits in a umask-default (usually 0644) file.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(link + "\n")
            # O_CREAT's mode is ignored when the file already exists.
            os.chmod(path, 0o600)
        except OSError as e:
            # Nothing to point at, so the link itself is the only way to pair.
            logger.warning(
                "Could not write invite link to %s (%s). Invite link: %s",
                path,
                e,
                link,
            )
            return

        logger.info("Delta Chat invite link written to %s", path)

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
            onboarding = _headless_onboarding()
            if accounts:
                self.account_id = accounts[0]["id"]
                logger.info(f"Using Delta Chat account: {self.account_id}")
            elif onboarding:
                self.account_id = await self.rpc.add_account()
                logger.info(f"Created Delta Chat account: {self.account_id}")
            else:
                logger.error(
                    f"No Delta Chat accounts found in {dc_accounts_path}. "
                    "Run: python ~/.hermes/plugins/deltachat-platform/setup.py "
                    "— or set DELTACHAT_EMAIL to onboard without a terminal "
                    "(see docs/headless-onboarding.md)"
                )
                self._cleanup()
                return False

            # add_account() persists an account row before any transport is
            # attached, so a bootstrap that fails half way leaves an unusable
            # account behind that get_all_accounts() hands back on the next
            # boot. Gate on is_configured(), never on account existence.
            if not await self.rpc.is_configured(self.account_id):
                if not onboarding:
                    logger.error(
                        f"Delta Chat account {self.account_id} has no working "
                        "transport. Run setup.py, or set DELTACHAT_EMAIL to "
                        "configure one without a terminal."
                    )
                    self._cleanup()
                    return False
                if not await self._configure_transports(onboarding):
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

            # Needs IO running to produce a usable link.
            await self._publish_invite_link()

            # Start event listener
            self._running = True
            self._event_loop_task = asyncio.create_task(self._event_listener())
            self._event_loop_task.add_done_callback(self._on_listener_done)

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
        """Clean up resources and report the adapter as no longer connected.

        Also reached from connect()'s failure paths, which is why it marks
        disconnected itself: a failed connect used to leave the last-written
        runtime status in place, so gateway_state.json kept claiming the
        platform was connected. _mark_disconnected() early-returns when *this
        adapter* has recorded a fatal error, so our own "fatal" state survives.
        It does not protect a "retrying" state the gateway wrote: when the
        reconnect watcher disposes of an adapter whose connect() failed (no
        fatal error recorded), this overwrites it with "disconnected" — as the
        old disconnect() already did.
        """
        global _active_adapter
        if _active_adapter is self:
            _active_adapter = None
        self._running = False
        if self._event_loop_task:
            # Not awaited: _cleanup is sync, and it can be reached *from* the
            # listener task itself via the fatal-error path. _on_listener_done
            # observes the outcome instead.
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
        self._invite_link = None
        self._mark_disconnected()

    @staticmethod
    def _on_listener_done(task: asyncio.Task) -> None:
        """Retrieve the listener's outcome so a crash can't vanish.

        Without this, an exception escaping the task is only reported by
        asyncio as "Task exception was never retrieved" whenever the garbage
        collector happens to get to it — if at all.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Delta Chat event listener died: %s", exc, exc_info=exc)

    async def disconnect(self) -> None:
        """Disconnect from Delta Chat."""
        try:
            if self._call_manager:
                await self._call_manager.teardown()
                self._call_manager = None
        except Exception as e:
            # A raising teardown used to skip _cleanup() entirely, leaking the
            # RPC subprocess and the accounts-dir lock — which then blocked the
            # replacement adapter the gateway builds on reconnect.
            logger.warning("Error tearing down call manager: %s", e)
        finally:
            self._cleanup()
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

    async def _resolve_chat_id(self, chat_id) -> int:
        """Real DC chat id for an outbound target: a numeric id or a chat token.

        why: the agent sees only the [dc:chat=<token>] tag, so when it writes a
        delivery target itself (a cron job's `deliver: deltachat-platform:<x>`)
        it uses the token. Hermes passes that through verbatim as chat_id.
        """
        s = str(chat_id).strip()
        # token_hex(8) is 16 hex chars and can, rarely, be all digits.
        if not s.isdigit() or len(s) == 16:
            real = await _resolve_chat_token(self.rpc, self.account_id, s)
            if real is not None:
                return real
        if not s.isdigit():
            raise ValueError(f"unknown Delta Chat chat id or token: {s!r}")
        return int(s)

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
        # Suppress the AI's reply to an internal "call ended" note so we don't
        # text the user a stray message after a call. Checked before call
        # routing so a late reply can't be spoken into a follow-up call.
        if self._call_manager and self._call_manager.is_call_end_reply(reply_to):
            return SendResult(success=True, message_id=None)

        if self._call_manager and self._call_manager.has_active_call(chat_id):
            thread_id = (metadata or {}).get("thread_id")
            if self._call_manager.is_call_thread(thread_id):
                # Only the turn's final reply is spoken. Hermes also sends status
                # traffic through send() — "💾 Memory updated", tool progress,
                # busy acks, the "⏳ Working" heartbeat, interim commentary,
                # turn errors — and marks only the final reply with
                # metadata["notify"] (base.py `_mark_notify_metadata`; the A2A
                # adapter filters on the same flag). Checked before the call-ack
                # drop so a status line can't use up that one-shot drop.
                if not (metadata or {}).get("notify"):
                    logger.debug("Call %s: not speaking non-final send: %r",
                                 chat_id, (content or "")[:80])
                    return SendResult(success=True, message_id=None)
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
        try:
            if not self.rpc or not self.account_id:
                return SendResult(
                    success=False,
                    error="Delta Chat not connected",
                )

            # Format long messages with HTML
            text_part, html_part = self._format_html_message(content)

            quoted_id = _quote_id(reply_to)

            if html_part:
                from deltachat2.types import MsgData, MessageViewtype

                msg_id = await self.rpc.send_msg(
                    self.account_id,
                    await self._resolve_chat_id(chat_id),
                    MsgData(text=text_part, html=html_part, viewtype=MessageViewtype.TEXT, quoted_message_id=quoted_id),
                )
            else:
                from deltachat2.types import MsgData

                msg_id = await self.rpc.send_msg(
                    self.account_id,
                    await self._resolve_chat_id(chat_id),
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
                await self._resolve_chat_id(chat_id),
                MsgData(file=file_path, text=caption or "", quoted_message_id=_quote_id(reply_to)),
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
                await self._resolve_chat_id(chat_id),
                MsgData(
                    file=image_path,
                    text=caption or "",
                    viewtype=MessageViewtype.IMAGE,
                    quoted_message_id=_quote_id(reply_to),
                ),
            )
            logger.debug(f"Sent image {image_path} as message {msg_id} to chat {chat_id}")
            return SendResult(success=True, message_id=str(msg_id))
        except Exception as e:
            logger.error(f"Error sending image {image_path} to chat {chat_id}: {e}")
            return SendResult(success=False, error=str(e))

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video file to a Delta Chat chat as an inline-playable video.

        why: without this override Hermes falls back to the base class, which
        posts a "couldn't send video" notice instead of the file — on both the
        reply-flow MEDIA path and cron delivery.
        """
        try:
            if not self.rpc or not self.account_id:
                return SendResult(success=False, error="Delta Chat not connected")

            from deltachat2.types import MsgData, MessageViewtype

            msg_id = await self.rpc.send_msg(
                self.account_id,
                await self._resolve_chat_id(chat_id),
                MsgData(
                    file=video_path,
                    text=caption or "",
                    viewtype=MessageViewtype.VIDEO,
                    quoted_message_id=_quote_id(reply_to),
                ),
            )
            logger.debug(f"Sent video {video_path} as message {msg_id} to chat {chat_id}")
            return SendResult(success=True, message_id=str(msg_id))
        except Exception as e:
            logger.error(f"Error sending video {video_path} to chat {chat_id}: {e}")
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
                await self._resolve_chat_id(chat_id),
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
                await self._resolve_chat_id(chat_id),
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

    @staticmethod
    def _mask_for_scan(text: str) -> str:
        """Blank out code blocks / quotes / JSON strings before scanning.

        The base extractors mask these spans so that a path merely *shown* in
        a code sample is never delivered as an attachment; our .xdc extractors
        have to do the same or the skill's own `/workspace/myapp.xdc` examples
        get cut out of the reply and mailed to the user.  Masking is
        offset-preserving (chars → spaces), so match spans stay valid against
        the unmasked text.

        The base helpers are private, so a future core may drop or rename
        them; falling back to the verbatim text only costs false positives.
        """
        from gateway.platforms.base import BasePlatformAdapter

        for name in ("_mask_protected_spans", "_mask_json_string_media"):
            masker = getattr(BasePlatformAdapter, name, None)
            if masker:
                text = masker(text)
        return text

    @classmethod
    def _find_xdc_paths(cls, pattern, text: str):
        """Return [(path, span)] for `pattern` matches outside protected spans.

        Group 1 is the path, group 0 the span to delete from the text.
        """
        masked = cls._mask_for_scan(text)
        return [
            (text[m.start(1):m.end(1)].strip(), m.span())
            for m in pattern.finditer(masked)
        ]

    @staticmethod
    def _delete_spans(text: str, spans) -> str:
        """Delete `spans` from `text`, back to front so offsets stay valid.

        str.replace() would also strip an identical path elsewhere in the
        message — including inside the code block we just took care to mask.
        """
        chars = list(text)
        for start, end in sorted(spans, reverse=True):
            del chars[start:end]
        return "".join(chars).strip()

    @staticmethod
    def _xdc_path_is_deliverable(path: str) -> bool:
        """True for a bare .xdc path worth handing to the delivery pipeline.

        /workspace/ paths are container-side and never exist on the host, so
        they are taken on faith; Hermes's delivery filter translates them to
        the host sandbox (Hermes >= 0.21.5).  Everything else must actually
        exist — the base extractor applies the same os.path.isfile() guard,
        and without it a path merely mentioned in prose is cut from the reply
        text and pushed at the user as an attachment.
        """
        if path.startswith("/workspace/"):
            return True
        try:
            return os.path.isfile(os.path.expanduser(path))
        except (OSError, RuntimeError, ValueError):
            # expanduser raises ValueError("embedded null byte") for ~\x00.
            return False

    def extract_media(self, content: str):
        """Extend base extract_media to also handle .xdc MEDIA tags.

        .xdc is not in Hermes's MEDIA_DELIVERY_EXTS so the base staticmethod
        misses it.  We catch those tags here so they flow through the normal
        filter_media_delivery_paths → send_document pipeline, exactly like
        Telegram handles any other document type.

        An explicit MEDIA: tag is an instruction, not a mention, so unlike
        extract_local_files below there is no isfile() guard — a typo'd path
        should surface as "skipped unsafe path" in the log rather than be
        silently left in the text as if it were prose.
        """
        import re
        from gateway.platforms.base import BasePlatformAdapter

        media_files, remaining = BasePlatformAdapter.extract_media(content)

        # Scan the base's cleaned text rather than `content`: it never removes
        # .xdc tags (wrong extension set), so nothing is missed, and the spans
        # stay valid for the deletion below.
        xdc_re = re.compile(
            r'[`"\']?MEDIA:\s*[`"\']?((?:~/|/)[\w./\- ]+\.xdc)[`"\']?',
            re.IGNORECASE,
        )
        spans = []
        for path, span in self._find_xdc_paths(xdc_re, remaining):
            if not any(p == path for p, _ in media_files):
                media_files.append((path, False))
            spans.append(span)

        return media_files, self._delete_spans(remaining, spans)

    def extract_local_files(self, content: str):
        """Extend base to also pick up bare .xdc paths.

        .xdc is not in Hermes's MEDIA_DELIVERY_EXTS, so the base staticmethod
        never picks up bare .xdc paths.  We add them explicitly here for both
        deployment shapes:
          * Docker sandbox container paths like /workspace/app.xdc, which don't
            exist on the host — Hermes's filter_local_delivery_paths then maps
            them to the host sandbox before validation.
          * Agent-workspace paths on non-Docker deployments (absolute /... or
            home ~/... paths already visible on the host) — these flow
            untouched to the base validator, which enforces the denylist.

        A bare path is a guess at intent, not an instruction, so candidates
        must clear _xdc_path_is_deliverable before they are removed from the
        text; anything else stays visible as ordinary prose.
        """
        import re
        from gateway.platforms.base import BasePlatformAdapter

        files, remaining = BasePlatformAdapter.extract_local_files(content)

        xdc_re = re.compile(r'(?<![/:\w.])((?:~/|/)[\w./\-]+\.xdc)\b', re.IGNORECASE)
        spans = []
        for path, span in self._find_xdc_paths(xdc_re, remaining):
            if not self._xdc_path_is_deliverable(path):
                continue
            if path not in files:
                files.append(path)
            spans.append(span)

        return files, self._delete_spans(remaining, spans)

    def _rpc_server_exit_code(self) -> Optional[int]:
        """Exit code of the deltachat-rpc-server subprocess, or None if alive.

        IOTransport only binds `.process` once start() has been called, so a
        missing attribute means "not started yet", not "dead".
        """
        process = getattr(self._transport, "process", None)
        if process is None:
            return None
        return process.poll()

    async def _event_listener(self) -> None:
        """Listen for Delta Chat events and forward to Hermes.

        If this loop ever stops while we still believe we are connected, the
        adapter is deaf: DC keeps queueing events and nothing drains them. That
        used to be silent and permanent. Now it is escalated to the gateway,
        which owns supervision (see _escalate_listener_death).

        Transient RPC errors are retried in place, but the loop gives up the
        moment the deltachat-rpc-server subprocess is gone — see
        _handle_listener_error.
        """
        try:
            while self._running:
                try:
                    if self.account_id:
                        envelope = await self.rpc.get_next_event()
                        if envelope.get("context_id") == self.account_id:
                            await self._handle_dc_event(envelope.get("event", {}))
                except asyncio.CancelledError:
                    # Re-raise so the task ends cancelled; the finally still
                    # runs, and escalates only if this was not a teardown.
                    raise
                except Exception as e:
                    if not await self._handle_listener_error(e):
                        break
        finally:
            # is_connected is the base class's self._running, which _cleanup(),
            # _mark_disconnected() and _set_fatal_error() all clear — so a
            # deliberate teardown, or an escalation _handle_listener_error has
            # already made, falls through here without escalating again.
            if self.is_connected:
                self._escalate_listener_death(
                    "event_listener_stopped",
                    "Delta Chat event listener stopped while connected",
                )

    def _escalate_listener_death(self, code: str, message: str) -> None:
        """Report a dead event listener to the gateway and let it recover us.

        Hermes owns supervision: _handle_adapter_fatal_error drops this adapter
        and _platform_reconnect_watcher rebuilds a *fresh* one with 30s->300s
        backoff. So we must not restart the listener ourselves — an adapter-side
        supervisor would race that watcher and keep the RPC subprocess and the
        accounts-dir lock alive, which is exactly what blocks the replacement
        adapter from connecting.

        The notify is deliberately fired as its own task rather than awaited
        here: the gateway's fatal handler calls back into disconnect(), which
        cancels *this* task. Awaiting that from inside the task would cancel us
        mid-teardown, and _cleanup()'s cancel would be a task cancelling itself.

        retryable=True either way: a stopped listener or a dead RPC server is
        recovered by rebuilding the adapter, so the platform must not be
        written off.
        """
        self._set_fatal_error(code, message, retryable=True)
        # Held on the instance so the task isn't garbage-collected mid-flight.
        self._fatal_notify_task = asyncio.create_task(self._notify_fatal_error())

    async def _log_failed_message(self, event: Dict[str, Any]) -> None:
        """Log a MSG_FAILED event with the reason DC recorded, not just an id.

        The event itself carries only chatId/msgId, so the human-readable cause
        lives on the message snapshot's `error` field and needs a second RPC.
        That call can itself fail (the message may already be gone, or the
        transport may be the reason we got here), so a missing reason must not
        turn a delivery failure into an exception on the event loop.
        """
        msg_id = event.get("msg_id")
        chat_id = event.get("chat_id")
        error = None
        try:
            msg = await self.rpc.get_message(self.account_id, int(msg_id))
            error = msg.get("error")
        except Exception as e:
            logger.debug("Could not fetch error text for failed msg %s: %s", msg_id, e)

        logger.warning(
            "Message failed: msg_id=%s chat_id=%s reason=%s",
            msg_id,
            chat_id,
            error or "unknown",
        )

    async def _handle_listener_error(self, exc: Exception) -> bool:
        """Return True to keep polling, False to stop.

        Once deltachat-rpc-server has exited there is nothing left to retry
        against: the vendored transport (vendor/deltachat2/transport.py) fails
        every further call with "RPC server disconnected", so without this
        check the listener would log that error once a second forever while
        is_connected still reports True. Retrying cannot fix it and the adapter
        cannot restart the server itself (see _escalate_listener_death), so
        hand the adapter back to the gateway, which rebuilds it — respawning
        the RPC server in the process.

        The exit can lag the error slightly: the server's pipes close before
        poll() sees it exit. Then we retry once, and the transport fails that
        call within a second, by which time poll() reports the exit.
        """
        exit_code = self._rpc_server_exit_code()
        if exit_code is None:
            logger.error(f"Event listener error: {exc}")
            await asyncio.sleep(1)
            return True

        logger.error(
            "deltachat-rpc-server exited (code %s); stopping the event listener "
            "and handing the adapter back to the gateway. Last error: %s",
            exit_code,
            exc,
        )
        if self.is_connected:
            self._escalate_listener_death(
                "rpc_server_died",
                f"deltachat-rpc-server exited with code {exit_code}",
            )
        return False

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
            await self._log_failed_message(event)
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

            # Before the read receipt: a dropped sender learns nothing.
            if not await self._intake_allows(msg, chat_id):
                return

            # Send read receipt immediately
            try:
                await self.rpc.markseen_msgs(self.account_id, [int(msg_id)])
            except Exception as e:
                logger.debug(f"Could not mark message {msg_id} as seen: {e}")

            # Before the text/non-text split so images and voice are gated too.
            if not await self._mention_gate_allows(msg, chat_id):
                return

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
            chat_type = "group" if chat.get("chat_type") == "Group" else "dm"
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
            chat_type = "group" if chat.get("chat_type") == "Group" else "dm"
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
                    await self._resolve_chat_id(chat_id),
                )
                return {
                    "name": chat.get("name", chat_id),
                    "type": "group" if chat.get("chat_type") == "Group" else "dm",
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
            "For file delivery: write output files to your current working directory "
            "(run `pwd` to find it), NOT /tmp/. "
            "Then reference the file by ABSOLUTE path in a MEDIA directive — e.g. 'MEDIA:/abs/path/app.xdc'. "
            "In the Docker sandbox the working directory is /workspace/, so there it is 'MEDIA:/workspace/app.xdc'. "
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
      - dc_rpc_spec: OpenRPC spec, minus the methods we refuse
      - dc_chat_rpc_spec: spec filtered to chatId-scoped methods we do not refuse
      - dc_safe_rpc_call: chat-scoped calls with token-validated chatId injection

    Only registers when DELTACHAT_ENABLE_RAW_RPC is set:
      - dc_rpc_call: unrestricted access to any RPC method
    """

    def _visible_methods(spec: dict, chat_scoped: bool) -> list:
        """Methods worth showing the model: never one it would then be refused.

        why: advertising a method the call gate rejects is not neutral. The
        model has no way to tell "not permitted here" from "I got the name
        wrong", so it retries, rephrases, and burns the turn on a door that is
        never going to open. Two of the blocklist entries — the securejoin QR
        pair — exist specifically so the model is never told they are there.
        """
        return [
            m for m in spec.get("methods", [])
            if not _is_blocked(m["name"])
            and (not chat_scoped
                 or any(p["name"] == "chatId" for p in m.get("params", [])))
        ]

    async def _spec_handler(args: dict = None, **kwargs) -> str:
        try:
            spec = await _fetch_spec()
        except Exception as e:
            return f"Error: {e}"
        return json.dumps({**spec, "methods": _visible_methods(spec, chat_scoped=False)}, indent=2)

    async def _call_handler(args: dict, **kwargs) -> str:
        method = (args or {}).get("method")
        params = (args or {}).get("params") or []
        if not method or not isinstance(method, str):
            return json.dumps({"error": "Missing 'method' (snake_case RPC name)."})
        if _active_adapter is None or _active_adapter.rpc is None:
            return json.dumps({"error": "Delta Chat is not connected"})

        # why: %r, not %s. `method` is model-supplied and has only been checked
        # for being a str — an embedded newline would otherwise let it forge a
        # second, entirely fake audit line in errors.log.
        def _refuse(reason: str, detail: str) -> str:
            logger.warning("Raw RPC call REFUSED (%s): %r", reason, method)
            return json.dumps({"error": detail})

        # Read at call time, not import time — Hermes loads ~/.hermes/.env
        # after this module is imported.
        raw_allowlist = (os.getenv("DELTACHAT_RAW_RPC_ALLOWLIST") or "").strip()
        allowlist = frozenset(m.strip() for m in raw_allowlist.split(",") if m.strip())
        # why: blank means "no allowlist", but a non-blank value that yields no
        # usable names means "allow nothing" — it must not fall back to
        # unrestricted. Otherwise a typo like ALLOWLIST=" , ," silently removes
        # the gate the operator was trying to tighten.
        if raw_allowlist and not allowlist:
            return _refuse(
                "unusable allowlist",
                "DELTACHAT_RAW_RPC_ALLOWLIST is set but lists no method names",
            )
        if allowlist and method not in allowlist:
            return _refuse("not allowlisted", f"'{method}' is not in the raw RPC allowlist")
        if _is_blocked(method):
            return _refuse("blocked", f"'{method}' is blocked")

        # Check the name against the spec rather than relying on getattr to
        # raise: deltachat2.Rpc.__getattr__ returns a lambda for *any* name, so
        # a typo reaches the server and comes back as a bare "Method not found"
        # a round-trip later. Catching it here points at dc_rpc_spec instead. A
        # spec that won't load is not a reason to refuse the call.
        try:
            known = {m["name"] for m in (await _fetch_spec()).get("methods", [])}
        except Exception as e:
            logger.warning("Could not load the RPC spec to validate %r: %s", method, e)
            known = None
        if known is not None and method not in known:
            return _refuse("unknown method", f"Unknown method '{method}' — use dc_rpc_spec to browse available methods")

        # why: logged here, after every gate, so errors.log distinguishes a call
        # that ran from one that was refused. Logging before the gates made both
        # look identical, which is useless as an audit trail.
        logger.warning("Raw RPC call ACCEPTED: %r", method)

        try:
            result = await getattr(_active_adapter.rpc, method)(*params)
            return json.dumps(result, default=str)
        except Exception as e:
            # why: the error goes back verbatim, on purpose. It is tempting to
            # mask it as leaking paths or as an injection channel, but this tool
            # only exists under DELTACHAT_ENABLE_RAW_RPC, where the model can
            # already reach get_message/get_contact/get_system_info and pull the
            # same strings out directly. Blocked methods return above without
            # ever calling, so no error can name something the caller was
            # refused. What masking does cost is real: "This method takes an
            # array of 2 arguments" is how the model fixes its own call.
            logger.error("Raw RPC call %s failed: %s", method, e, exc_info=True)
            return json.dumps({"error": str(e)})

    async def _chat_spec_handler(args: dict = None, **kwargs) -> str:
        """Return only the chatId-scoped methods _is_blocked does not refuse."""
        try:
            spec = await _fetch_spec()
        except Exception as e:
            return f"Error: {e}"
        return json.dumps({**spec, "methods": _visible_methods(spec, chat_scoped=True)}, indent=2)

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

        # Refuse before resolving anything else
        if _is_blocked(method):
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

        # why: bind by name, not by position. The old code built
        # [account_id, chat_id] + params, which assumes chatId is parameter 1.
        # Two spec methods break that assumption — forward_messages(accountId,
        # messageIds, chatId) and search_messages(accountId, query, chatId) —
        # and for those the injected chat id landed in the messageIds/query slot
        # while the caller's own value became the real chatId, defeating the
        # whole point of the token. The spec declares the order, so use it:
        # that fixes those two and every future method with an unusual shape.
        supplied = list(params or [])
        full_params = []
        for name in param_names:
            if name == "accountId":
                full_params.append(adapter.account_id)
            elif name == "chatId":
                full_params.append(real_chat_id)
            elif supplied:
                full_params.append(supplied.pop(0))
            else:
                break  # trailing optional parameters the caller left off
        if supplied:
            return json.dumps({
                "error": (
                    f"'{method}' takes {len(param_names)} parameters "
                    f"({', '.join(param_names)}); accountId and chatId are injected, "
                    f"so pass only the rest — {len(supplied)} too many were given"
                )
            })

        # why: core copies whatever local path it is handed into the blob dir
        # and sends it, so without this one call mails ~/.hermes/.env to any
        # chat the caller can steer (#32). The token scopes the chat, not the
        # file. Run each path through the same filter the adapter's own sends
        # use; that also maps /workspace/ sandbox paths to the host.
        unchecked = _unchecked_path_name(param_names)
        for name, value in zip(param_names, full_params):
            if unchecked is None and name == "data" and isinstance(value, dict):
                unchecked = _unchecked_path_name(value)
        if unchecked is not None:
            logger.warning("Safe RPC call %r REFUSED (unchecked path parameter %r)", method, unchecked)
            return json.dumps({"error": f"'{method}' takes a file path ('{unchecked}') this tool cannot validate"})
        for i, name in enumerate(param_names[:len(full_params)]):
            value = full_params[i]
            if name == "data" and isinstance(value, dict) and value.get("file"):
                safe = _safe_delivery_path(adapter, value["file"])
                if safe is None:
                    return _refuse_path(method, value["file"])
                full_params[i] = {**value, "file": safe}
            elif name in _PATH_PARAMS and value:
                safe = _safe_delivery_path(adapter, value)
                if safe is None:
                    return _refuse_path(method, value)
                full_params[i] = safe

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
                "Fetch the OpenRPC specification of the running Delta Chat RPC server. "
                "Lists the callable methods with parameter types and descriptions; "
                "methods the adapter refuses are omitted. "
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
                "excluding every operation the adapter refuses. "
                "Only call this when you are about to use dc_safe_rpc_call for an explicit user request "
                "that cannot be handled by normal messaging tools."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_chat_spec_handler,
        is_async=True,
        emoji="📋",
    )

    if _env_flag("DELTACHAT_ENABLE_RAW_RPC"):
        ctx.register_tool(
            name="dc_rpc_call",
            toolset="deltachat",
            schema={
                "description": (
                    "Call any Delta Chat RPC method directly by name and params. "
                    "Use dc_rpc_spec first to see available methods. "
                    "CAUTION: reaches the whole account, not just one chat. "
                    "delete_*/remove_* methods are refused, and the deployment "
                    "may restrict this further. "
                    "Prefer dc_safe_rpc_call for chat-scoped operations."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "description": (
                                "RPC method name in snake_case (e.g. 'get_account_info'). "
                                "Use dc_rpc_spec to see the methods this tool will accept."
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
                "Refused methods are rejected before the call. Use dc_chat_rpc_spec first to find the method name."
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
