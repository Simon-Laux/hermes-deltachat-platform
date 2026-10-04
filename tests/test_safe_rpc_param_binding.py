"""dc_safe_rpc_call binds parameters by name, not by position.

The token system exists so the agent cannot address a chat it was not given.
Positional binding quietly broke that for any method whose chatId is not
parameter 1 — the caller's own value landed in the chatId slot.
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import adapter


# Real shapes from deltachat-rpc-openrpc.json.
SPEC = {"methods": [
    {"name": "get_basic_chat_info", "params": [{"name": "accountId"}, {"name": "chatId"}]},
    {"name": "send_msg", "params": [{"name": "accountId"}, {"name": "chatId"}, {"name": "data"}]},
    # the two outliers — chatId last, not at index 1
    {"name": "forward_messages", "params": [{"name": "accountId"}, {"name": "messageIds"}, {"name": "chatId"}]},
    {"name": "search_messages", "params": [{"name": "accountId"}, {"name": "query"}, {"name": "chatId"}]},
    {"name": "get_account_info", "params": [{"name": "accountId"}]},  # no chatId
]}

CHAT_ID = 4242
TOKEN = "deadbeef"


@pytest.fixture
def safe_handler():
    handlers = {}
    ctx = MagicMock()
    ctx.register_tool.side_effect = lambda **kw: handlers.__setitem__(kw["name"], kw["handler"])
    env = {k: v for k, v in os.environ.items() if k != "DELTACHAT_ENABLE_RAW_RPC"}
    with patch.dict(os.environ, env, clear=True):
        adapter.register_rpc_tools(ctx)
    return handlers["dc_safe_rpc_call"]


@pytest.fixture
def connected(monkeypatch):
    fake = MagicMock()
    fake.account_id = 1
    fake.rpc = MagicMock()
    for name in ("get_basic_chat_info", "send_msg", "forward_messages", "search_messages"):
        setattr(fake.rpc, name, AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(adapter, "_active_adapter", fake)
    monkeypatch.setattr(adapter, "_spec_cache", SPEC)
    monkeypatch.setattr(adapter, "_resolve_chat_token", AsyncMock(return_value=CHAT_ID))
    return fake


class TestParamBinding:
    @pytest.mark.asyncio
    async def test_ordinary_method_unchanged(self, safe_handler, connected):
        await safe_handler({"method": "get_basic_chat_info", "chat_token": TOKEN, "params": []})
        connected.rpc.get_basic_chat_info.assert_awaited_once_with(1, CHAT_ID)

    @pytest.mark.asyncio
    async def test_trailing_param_after_chat_id(self, safe_handler, connected):
        await safe_handler({"method": "send_msg", "chat_token": TOKEN, "params": [{"text": "hi"}]})
        connected.rpc.send_msg.assert_awaited_once_with(1, CHAT_ID, {"text": "hi"})

    @pytest.mark.asyncio
    async def test_chat_id_last_is_placed_last(self, safe_handler, connected):
        """search_messages(accountId, query, chatId) — chatId is not parameter 1.

        forward_messages has the same shape but is now refused outright (its
        messageIds are unscoped), so search_messages is the live case.
        """
        await safe_handler({"method": "search_messages", "chat_token": TOKEN, "params": ["q"]})
        connected.rpc.search_messages.assert_awaited_once_with(1, "q", CHAT_ID)

    @pytest.mark.asyncio
    async def test_forward_messages_is_refused_not_bound(self, safe_handler, connected):
        """The denylist runs before binding, so this never reaches the server."""
        result = json.loads(await safe_handler(
            {"method": "forward_messages", "chat_token": TOKEN, "params": [[7, 8]]}))
        assert "not allowed" in result["error"]
        connected.rpc.forward_messages.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_caller_cannot_smuggle_a_chat_id(self, safe_handler, connected):
        """Under positional binding this put 99 in the chatId slot."""
        await safe_handler({"method": "search_messages", "chat_token": TOKEN, "params": ["needle"]})
        args = connected.rpc.search_messages.await_args.args
        assert args == (1, "needle", CHAT_ID)
        assert 99 not in args

    @pytest.mark.asyncio
    async def test_omitted_trailing_optional_is_allowed(self, safe_handler, connected):
        await safe_handler({"method": "send_msg", "chat_token": TOKEN, "params": []})
        connected.rpc.send_msg.assert_awaited_once_with(1, CHAT_ID)

    @pytest.mark.asyncio
    async def test_too_many_params_is_refused(self, safe_handler, connected):
        result = json.loads(await safe_handler(
            {"method": "send_msg", "chat_token": TOKEN, "params": [{"text": "hi"}, "extra"]}))
        assert "too many" in result["error"]
        connected.rpc.send_msg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_method_without_chat_id_still_refused(self, safe_handler, connected):
        result = json.loads(await safe_handler(
            {"method": "get_account_info", "chat_token": TOKEN, "params": []}))
        assert "no chatId parameter" in result["error"]
