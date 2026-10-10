"""dc_react: the agent reacts with an emoji to the message it is answering."""

import json
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import adapter
from adapter import DeltaChatAdapter

DC = "deltachat-platform"
TURN = {"HERMES_SESSION_PLATFORM": DC, "HERMES_SESSION_CHAT_ID": "5",
        "HERMES_SESSION_MESSAGE_ID": "42"}


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
    monkeypatch.setattr(adapter, "_active_adapter", a)
    return a


def _session(monkeypatch, **env):
    """Stand in for Hermes' gateway.session_context with the given session vars."""
    mod = types.ModuleType("gateway.session_context")
    mod.get_session_env = lambda name, default="": env.get(name, default)
    monkeypatch.setitem(sys.modules, "gateway.session_context", mod)


async def _call(react, args):
    return json.loads(await react["handler"](args))


@pytest.mark.asyncio
async def test_reacts_to_the_turns_message(react, a, monkeypatch):
    _session(monkeypatch, **TURN)
    assert await _call(react, {"emoji": "😂"}) == {"success": True}
    a.rpc.send_reaction.assert_awaited_once_with(1, 42, ["😂"])


@pytest.mark.asyncio
@pytest.mark.parametrize("emoji", ["", "  "])
async def test_empty_emoji_removes_the_reaction(react, a, monkeypatch, emoji):
    _session(monkeypatch, **TURN)
    assert (await _call(react, {"emoji": emoji}))["success"]
    a.rpc.send_reaction.assert_awaited_once_with(1, 42, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {},  # no emoji at all must not remove the reaction
    {"emoji": None}, {"emoji": 0}, {"emoji": ["👍"]},
    {"emoji": "thanks a lot"}, {"emoji": "x" * 30}, None,
    {"emoji": "👨🏻‍👩🏻‍👧🏻‍👦🏻"},  # 11 code points but 41 bytes: core would remove instead
])
async def test_anything_but_one_emoji_is_refused(react, a, monkeypatch, args):
    _session(monkeypatch, **TURN)
    assert "error" in await _call(react, args)
    a.rpc.send_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_emoji_sequences_pass(react, a, monkeypatch):
    _session(monkeypatch, **TURN)
    couple = "👩🏽‍🤝‍👩🏻"  # 26 bytes
    assert (await _call(react, {"emoji": couple}))["success"]
    a.rpc.send_reaction.assert_awaited_once_with(1, 42, [couple])


@pytest.mark.asyncio
@pytest.mark.parametrize("env", [
    {},  # cron, CLI
    # a Telegram chat id is just a number too
    {**TURN, "HERMES_SESSION_PLATFORM": "telegram"},
    # call turns carry no message; reacting must not hit an old text instead
    {**TURN, "HERMES_SESSION_MESSAGE_ID": ""},
    {**TURN, "HERMES_SESSION_MESSAGE_ID": "callend-123"},
])
async def test_only_on_a_delta_chat_message_of_this_turn(react, a, monkeypatch, env):
    _session(monkeypatch, **env)
    assert "error" in await _call(react, {"emoji": "👍"})
    a.rpc.send_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_connected(react, monkeypatch):
    monkeypatch.setattr(adapter, "_active_adapter", None)
    _session(monkeypatch, **TURN)
    assert "error" in await _call(react, {"emoji": "👍"})


@pytest.mark.asyncio
async def test_rpc_error_is_reported(react, a, monkeypatch):
    _session(monkeypatch, **TURN)
    a.rpc.send_reaction.side_effect = RuntimeError("rpc down")
    assert await _call(react, {"emoji": "👍"}) == {"error": "Reaction failed: rpc down"}


def test_tool_is_registered_async_with_emoji_required(react):
    assert react["is_async"] and react["toolset"] == "deltachat"
    assert react["schema"]["parameters"]["required"] == ["emoji"]


# -- the triggering message reaches Hermes ---------------------------------

def _intake(a, msg):
    a.rpc.get_message.return_value = msg
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": True}
    a.rpc.get_basic_chat_info.return_value = {"chat_type": "Single", "name": "c"}
    a._intake_allows = AsyncMock(return_value=True)
    a._mention_gate_allows = AsyncMock(return_value=True)
    a.handle_message = AsyncMock()


@pytest.mark.asyncio
@pytest.mark.parametrize("msg", [
    {"text": "hi", "view_type": "Text"},
    {"text": "", "view_type": "Image", "file": "/blobs/x.jpg", "file_mime": "image/jpeg",
     "file_name": "x.jpg"},
])
async def test_source_names_the_triggering_message(a, msg):
    """Hermes binds source.message_id as HERMES_SESSION_MESSAGE_ID for the turn."""
    _intake(a, {"id": 42, "chat_id": 5, "from_id": 10, **msg})
    a._resolve_blob_path = MagicMock(return_value="/blobs/x.jpg")
    a._copy_to_hermes_cache = MagicMock(return_value="/cache/x.jpg")
    with patch("adapter._get_or_create_chat_token", AsyncMock(return_value="tok")):
        await a._handle_incoming_message({"chat_id": 5, "msg_id": 42})
    a.handle_message.assert_awaited_once()
    assert a.handle_message.await_args.args[0].source.message_id == "42"


@pytest.mark.asyncio
async def test_hermes_own_turns_lose_the_stored_message_id(a, monkeypatch):
    """Notification and auto-resume turns reuse the session's stored source,
    whose message_id is the chat's first message."""
    import tests.conftest as c
    seen = []

    async def base_handle(self, event):
        seen.append(event.source.message_id)
    monkeypatch.setattr(c.MockBasePlatformAdapter, "handle_message", base_handle)
    origin = a.build_source(chat_id="5", chat_name="c", chat_type="dm", user_id="10",
                            user_name="u", message_id="7")
    for internal in (True, False):
        event = adapter.MessageEvent(text="", message_type=adapter.MessageType.TEXT,
                                     source=origin, message_id="42")
        event.internal = internal
        await DeltaChatAdapter.handle_message(a, event)
    assert seen == [None, "7"]
    assert origin.message_id == "7"  # the stored origin itself is untouched
