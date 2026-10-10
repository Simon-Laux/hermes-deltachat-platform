"""dc_start_call calls the chat the turn came from when no chat_token is given.

Asked "call me", the agent had to copy the [dc:chat=...] token into the call,
and often did not realise it could call at all.
"""

import json
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import adapter


@pytest.fixture
def start_call():
    handlers = {}
    ctx = MagicMock()
    ctx.register_tool.side_effect = lambda **kw: handlers.__setitem__(kw["name"], kw["handler"])
    env = {k: v for k, v in os.environ.items() if k != "DELTACHAT_ENABLE_RAW_RPC"}
    with patch.dict(os.environ, env, clear=True):
        adapter.register_rpc_tools(ctx)
    return handlers["dc_start_call"]


@pytest.fixture
def calls(monkeypatch):
    fake = MagicMock()
    fake._call_manager.start_call = AsyncMock(return_value=7)
    fake._call_manager.has_active_call = lambda chat_id: False
    monkeypatch.setattr(adapter, "_active_adapter", fake)
    return fake._call_manager


def _session(monkeypatch, **env):
    """Stand in for Hermes' gateway.session_context with the given session vars."""
    mod = types.ModuleType("gateway.session_context")
    mod.get_session_env = lambda name, default="": env.get(name, default)
    monkeypatch.setitem(sys.modules, "gateway.session_context", mod)


DC = "deltachat-platform"


@pytest.mark.asyncio
async def test_without_token_calls_the_asking_chat(start_call, calls, monkeypatch):
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="20")
    assert json.loads(await start_call({"opening": "Hi!"}))["success"]
    calls.start_call.assert_awaited_once_with("20", opening="Hi!")


@pytest.mark.asyncio
async def test_without_token_or_chat_context_refuses(start_call, calls, monkeypatch):
    _session(monkeypatch)  # e.g. a cron turn: platform and chat id are blank
    assert "error" in json.loads(await start_call({"opening": "Hi!"}))
    calls.start_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_platform_chat_id_is_not_trusted(start_call, calls, monkeypatch):
    """A Telegram chat id that happens to equal a DC chat id must not be called."""
    _session(monkeypatch, HERMES_SESSION_PLATFORM="telegram", HERMES_SESSION_CHAT_ID="20")
    assert "error" in json.loads(await start_call({"opening": "Hi!"}))
    calls.start_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_wins_over_the_asking_chat(start_call, calls, monkeypatch):
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="20")
    monkeypatch.setitem(adapter._chat_token_to_id, "tok", 30)
    assert json.loads(await start_call({"chat_token": "tok", "opening": "Hi!"}))["success"]
    calls.start_call.assert_awaited_once_with("30", opening="Hi!")


@pytest.mark.asyncio
async def test_no_second_call_into_a_live_one(start_call, calls, monkeypatch):
    """A spoken turn has the call's chat as its session; dialling it again would
    take over the routing of the running call."""
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="20")
    calls.has_active_call = lambda chat_id: chat_id == "20"
    assert "error" in json.loads(await start_call({"opening": "Hi!"}))
    calls.start_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_token_does_not_fall_back(start_call, calls, monkeypatch):
    """A made-up or stale token must fail, not quietly ring the asking chat."""
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="20")
    assert "error" in json.loads(await start_call({"chat_token": "nope", "opening": "Hi!"}))
    calls.start_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_blank_token_calls_the_asking_chat(start_call, calls, monkeypatch):
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="20")
    assert json.loads(await start_call({"chat_token": "  ", "opening": "Hi!"}))["success"]
    calls.start_call.assert_awaited_once_with("20", opening="Hi!")


@pytest.mark.asyncio
async def test_no_second_call_into_a_live_one_by_token(start_call, calls, monkeypatch):
    """The token resolves to an int while call state is keyed by str."""
    _session(monkeypatch)
    monkeypatch.setitem(adapter._chat_token_to_id, "tok", 20)
    calls.has_active_call = lambda chat_id: chat_id == "20"
    assert "error" in json.loads(await start_call({"chat_token": "tok", "opening": "Hi!"}))
    calls.start_call.assert_not_awaited()
