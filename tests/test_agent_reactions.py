"""dc_react: the agent reacts to the user's latest message with an emoji."""

import json
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import adapter
from adapter import DeltaChatAdapter

DC = "deltachat-platform"


@pytest.fixture
def react():
    handlers = {}
    ctx = MagicMock()
    ctx.register_tool.side_effect = lambda **kw: handlers.__setitem__(kw["name"], kw)
    env = {k: v for k, v in os.environ.items() if k != "DELTACHAT_ENABLE_RAW_RPC"}
    with patch.dict(os.environ, env, clear=True):
        adapter.register_rpc_tools(ctx)
    return handlers["dc_react"]


@pytest.fixture
def a(platform_config, monkeypatch):
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.handle_message = AsyncMock()
    monkeypatch.setattr(adapter, "_active_adapter", a)
    return a


def _session(monkeypatch, **env):
    """Stand in for Hermes' gateway.session_context with the given session vars."""
    mod = types.ModuleType("gateway.session_context")
    mod.get_session_env = lambda name, default="": env.get(name, default)
    monkeypatch.setitem(sys.modules, "gateway.session_context", mod)


async def _receive(a, msg_id, chat_id="5", user_id="10", text="hi"):
    source = a.build_source(chat_id=chat_id, chat_name="c", chat_type="group",
                            user_id=user_id, user_name="u")
    await a._to_hermes(adapter.MessageEvent(text=text, message_type=adapter.MessageType.TEXT,
                                            source=source, message_id=str(msg_id)))


async def _call(react, emoji="👍"):
    return json.loads(await react["handler"]({"emoji": emoji}))


@pytest.mark.asyncio
async def test_reacts_to_the_senders_latest_message(react, a, monkeypatch):
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="5",
             HERMES_SESSION_USER_ID="10")
    await _receive(a, 41)
    await _receive(a, 42)
    assert await _call(react) == {"success": True}
    a.rpc.send_reaction.assert_awaited_once_with(1, 42, ["👍"])
    assert a.handle_message.await_count == 2  # still handed on


@pytest.mark.asyncio
async def test_in_a_group_not_someone_elses_newer_message(react, a, monkeypatch):
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="5",
             HERMES_SESSION_USER_ID="10")
    await _receive(a, 42, user_id="10")
    await _receive(a, 43, user_id="11")
    await _call(react)
    a.rpc.send_reaction.assert_awaited_once_with(1, 42, ["👍"])


@pytest.mark.asyncio
async def test_never_another_chat(react, a, monkeypatch):
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="5",
             HERMES_SESSION_USER_ID="10")
    await _receive(a, 50, chat_id="6")
    assert "error" in await _call(react)
    a.rpc.send_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_commands_are_not_the_target(react, a, monkeypatch):
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="5",
             HERMES_SESSION_USER_ID="10")
    await _receive(a, 42)
    await _receive(a, 43, text="/model")
    await _call(react)
    a.rpc.send_reaction.assert_awaited_once_with(1, 42, ["👍"])


@pytest.mark.asyncio
@pytest.mark.parametrize("emoji", ["", "  "])
async def test_empty_emoji_removes_the_reaction(react, a, monkeypatch, emoji):
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="5",
             HERMES_SESSION_USER_ID="10")
    await _receive(a, 42)
    assert (await _call(react, emoji))["success"]
    a.rpc.send_reaction.assert_awaited_once_with(1, 42, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("env", [
    {},  # cron, CLI
    {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "5",
     "HERMES_SESSION_USER_ID": "10"},  # a Telegram chat id is just a number too
])
async def test_only_in_a_delta_chat_turn(react, a, monkeypatch, env):
    _session(monkeypatch, **env)
    await _receive(a, 42)
    assert "error" in await _call(react)
    a.rpc.send_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_connected(react, monkeypatch):
    monkeypatch.setattr(adapter, "_active_adapter", None)
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="5",
             HERMES_SESSION_USER_ID="10")
    assert "error" in await _call(react)


@pytest.mark.asyncio
async def test_nothing_received_yet(react, a, monkeypatch):
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="5",
             HERMES_SESSION_USER_ID="10")
    assert "error" in await _call(react)


@pytest.mark.asyncio
async def test_rpc_error_is_reported(react, a, monkeypatch):
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="5",
             HERMES_SESSION_USER_ID="10")
    await _receive(a, 42)
    a.rpc.send_reaction.side_effect = RuntimeError("rpc down")
    assert await _call(react) == {"error": "Reaction failed: rpc down"}


@pytest.mark.asyncio
async def test_incoming_messages_are_remembered(a):
    """Every path that hands a chat message to Hermes goes through _to_hermes."""
    a.rpc.get_message.return_value = {"id": 42, "chat_id": 5, "from_id": 10,
                                      "text": "hi", "view_type": "Text"}
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": True}
    a.rpc.get_basic_chat_info.return_value = {"chat_type": "Single", "name": "c"}
    a._intake_allows = AsyncMock(return_value=True)
    a._mention_gate_allows = AsyncMock(return_value=True)
    with patch("adapter._get_or_create_chat_token", AsyncMock(return_value="tok")):
        await a._handle_incoming_message({"chat_id": 5, "msg_id": 42})
    assert a._last_inbound == {("5", "10"): "42"}


def test_tool_is_registered_async_with_emoji_required(react):
    assert react["is_async"] and react["toolset"] == "deltachat"
    assert react["schema"]["parameters"]["required"] == ["emoji"]
