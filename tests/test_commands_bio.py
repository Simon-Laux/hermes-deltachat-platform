"""The profile bio lists Hermes' slash commands as help,
below whatever the operator wrote there themselves."""
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapter import DeltaChatAdapter, _commands_bio, _own_bio
from tests.conftest import MockPlatform, MockPlatformConfig

INTRO = "Hermes AI assistant – just write to me."


@pytest.fixture
def registry(monkeypatch):
    cmds = [SimpleNamespace(name="new", args_hint="[name]",
                            description="Start a new session (fresh session ID + history)"),
            SimpleNamespace(name="start", args_hint="", description="Ack start pings"),
            SimpleNamespace(name="topic", args_hint="[off]", description="Telegram DM topics"),
            SimpleNamespace(name="help", args_hint="", description="Show available commands")]
    platforms = types.ModuleType("hermes_cli.commands_platforms")
    platforms._gateway_available_commands = lambda: cmds
    commands = types.ModuleType("hermes_cli.commands")
    commands._iter_plugin_command_entries = lambda: [("mine", "Plugin\ncommand", "<x>")]
    access = types.ModuleType("gateway.slash_access")
    # stand-in for Hermes' policy: with admins set, non-admins only get /help
    access.policy_from_extra = lambda extra, scope: SimpleNamespace(
        can_run=lambda user, cmd: not extra.get("allow_admin_from") or cmd == "help")
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.commands_platforms", platforms)
    monkeypatch.setitem(sys.modules, "hermes_cli.commands", commands)
    monkeypatch.setitem(sys.modules, "gateway.slash_access", access)


LIST = ("Hermes commands:\n"
        "/new [name] – Start a new session\n"
        "/help – Show available commands\n"
        "/mine <x> – Plugin command")


def test_bio_lists_usable_commands(registry):
    assert _commands_bio("", {}) == f"{INTRO}\n\n{LIST}"
    assert _commands_bio("Ask me.", {}) == f"Ask me.\n\n{LIST}"


def test_admin_only_commands_left_out(registry):
    bio = _commands_bio("", {"allow_admin_from": ["1"]})
    assert bio == f"{INTRO}\n\nHermes commands:\n/help – Show available commands"


@pytest.mark.parametrize("current,own", [
    ("plain bio", None),
    (f"Ask me.\n\n{LIST}", "Ask me."),
    (f"Ask me.\n\n{LIST}".replace("\n", "\r\n"), "Ask me."),  # edited on another client
    ("Ask me.\nHermes commands: \n/x – y", "Ask me."),
    ("Ask me.\n\nHermes commands:", "Ask me."),
    (LIST, ""),
])
def test_own_bio(current, own):
    assert _own_bio(current) == own


def test_no_registry_no_bio(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli.commands_platforms", None)
    assert _commands_bio("mine", {}) is None


def _adapter(bio, extra=None):
    cfg = MockPlatformConfig(name="deltachat-platform", platform=MockPlatform.DELTACHAT,
                             extra=extra or {})
    a = DeltaChatAdapter(cfg)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.rpc.get_config.return_value = bio
    return a


@pytest.mark.asyncio
@pytest.mark.parametrize("current,extra,written", [
    (None, {}, f"{INTRO}\n\n{LIST}"),
    ("Ask me.", {}, f"Ask me.\n\n{LIST}"),
    (f"Ask me.\n\n{LIST.replace('/new', '/old')}", {}, f"Ask me.\n\n{LIST}"),
    (f"Ask me.\n\n{LIST}", {}, None),                               # unchanged: no sync write
    (f"Ask me.\n\n{LIST}", {"commands_bio": False}, "Ask me."),     # off: list taken out
    (f"{INTRO}\n\n{LIST}", {"commands_bio": False}, ""),
    ("Ask me.", {"commands_bio": False}, None),
])
async def test_update_commands_bio(registry, current, extra, written):
    a = _adapter(current, extra)
    await a._update_commands_bio()
    if written is None:
        a.rpc.set_config.assert_not_called()
    else:
        a.rpc.set_config.assert_called_once_with(1, "selfstatus", written)


@pytest.mark.asyncio
async def test_rpc_failure_does_not_raise(registry):
    a = _adapter("x")
    a.rpc.get_config.side_effect = RuntimeError("rpc down")
    await a._update_commands_bio()


@pytest.mark.parametrize("extra,env,expected", [
    ({}, None, True),
    ({}, "", True),
    ({}, "0", False),
    ({"commands_bio": False}, "1", False),
])
def test_opt_out(monkeypatch, extra, env, expected):
    if env is None:
        monkeypatch.delenv("DELTACHAT_COMMANDS_BIO", raising=False)
    else:
        monkeypatch.setenv("DELTACHAT_COMMANDS_BIO", env)
    assert _adapter("", extra)._commands_bio_enabled is expected
