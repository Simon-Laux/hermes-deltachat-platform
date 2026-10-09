"""dc_end_call hangs up the call of the chat the turn came from.

It used to end whichever call was registered first, so with calls running in
two chats, "bye" in one could cut off the other.
"""

import json
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import adapter


@pytest.fixture
def end_call():
    handlers = {}
    ctx = MagicMock()
    ctx.register_tool.side_effect = lambda **kw: handlers.__setitem__(kw["name"], kw["handler"])
    env = {k: v for k, v in os.environ.items() if k != "DELTACHAT_ENABLE_RAW_RPC"}
    with patch.dict(os.environ, env, clear=True):
        adapter.register_rpc_tools(ctx)
    return handlers["dc_end_call"]


def _connect(monkeypatch, active_chats):
    fake = MagicMock()
    fake._call_manager._chat_to_msg = {c: 100 + i for i, c in enumerate(active_chats)}
    fake._call_manager.request_hangup = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter, "_active_adapter", fake)
    return fake._call_manager


def _session(monkeypatch, **env):
    """Stand in for Hermes' gateway.session_context with the given session vars."""
    mod = types.ModuleType("gateway.session_context")
    mod.get_session_env = lambda name, default="": env.get(name, default)
    monkeypatch.setitem(sys.modules, "gateway.session_context", mod)


DC = "deltachat-platform"


@pytest.mark.asyncio
async def test_ends_the_call_of_the_asking_chat(end_call, monkeypatch):
    calls = _connect(monkeypatch, ["10", "20"])
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="20")
    assert json.loads(await end_call({}))["success"]
    calls.request_hangup.assert_awaited_once_with("20")


@pytest.mark.asyncio
async def test_no_call_in_the_asking_chat_leaves_others_alone(end_call, monkeypatch):
    calls = _connect(monkeypatch, ["10"])
    _session(monkeypatch, HERMES_SESSION_PLATFORM=DC, HERMES_SESSION_CHAT_ID="20")
    assert "error" in json.loads(await end_call({}))
    calls.request_hangup.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_platform_chat_id_is_not_trusted(end_call, monkeypatch):
    """A Telegram chat id that happens to equal a DC chat id must not pick that call."""
    calls = _connect(monkeypatch, ["10", "20"])
    _session(monkeypatch, HERMES_SESSION_PLATFORM="telegram", HERMES_SESSION_CHAT_ID="20")
    assert "error" in json.loads(await end_call({}))
    calls.request_hangup.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_chat_context_a_single_call_is_ended(end_call, monkeypatch):
    calls = _connect(monkeypatch, ["10"])
    _session(monkeypatch)  # e.g. a cron turn: platform and chat id are blank
    assert json.loads(await end_call({}))["success"]
    calls.request_hangup.assert_awaited_once_with("10")


@pytest.mark.asyncio
async def test_hermes_without_session_context_falls_back(end_call, monkeypatch):
    calls = _connect(monkeypatch, ["10"])
    monkeypatch.setitem(sys.modules, "gateway.session_context", None)  # import fails
    assert json.loads(await end_call({}))["success"]
    calls.request_hangup.assert_awaited_once_with("10")
