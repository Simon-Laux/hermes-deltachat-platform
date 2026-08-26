"""Tests for the dc_rpc_call allowlist/blocklist gate.

dc_rpc_call reaches the whole account, and anything that can get text in front
of the model can try to steer it — so the refusals here are the security
boundary, not a convenience.
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import adapter
from adapter import _is_destructive, _parse_method_list


class TestParseMethodList:
    def test_unset_is_empty(self):
        assert _parse_method_list(None) == frozenset()
        assert _parse_method_list("") == frozenset()

    def test_strips_and_drops_blanks(self):
        assert _parse_method_list(" a , b ,, c ") == frozenset({"a", "b", "c"})


class TestIsDestructive:
    @pytest.mark.parametrize("method", [
        "delete_chat",
        "leave_group",
        "remove_draft",
        "delete_something_added_next_release",  # prefix rule, not the literal set
        "remove_something_added_next_release",
    ])
    def test_blocked(self, method):
        assert _is_destructive(method)

    @pytest.mark.parametrize("method", ["get_basic_chat_info", "send_msg", "misc_get_info"])
    def test_allowed(self, method):
        assert not _is_destructive(method)


@pytest.fixture
def raw_rpc_handler():
    """Register the tools with DELTACHAT_ENABLE_RAW_RPC set, return dc_rpc_call's handler."""
    handlers = {}
    ctx = MagicMock()
    ctx.register_tool.side_effect = lambda **kw: handlers.__setitem__(kw["name"], kw["handler"])

    with patch.dict(os.environ, {"DELTACHAT_ENABLE_RAW_RPC": "1"}):
        adapter.register_rpc_tools(ctx)

    assert "dc_rpc_call" in handlers, "raw tool should register when the env var is set"
    return handlers["dc_rpc_call"]


@pytest.fixture(autouse=True)
def stub_spec():
    """Keep the gate's method-name check off the real deltachat-rpc-server binary."""
    spec = {"methods": [
        {"name": "get_basic_chat_info", "params": [{"name": "accountId"}, {"name": "chatId"}]},
        {"name": "set_config", "params": [{"name": "accountId"}]},
        {"name": "delete_chat", "params": [{"name": "accountId"}, {"name": "chatId"}]},
    ]}
    with patch.object(adapter, "_spec_cache", spec):
        yield spec


@pytest.fixture
def connected_adapter():
    """Patch in a connected adapter whose every RPC method returns {'ok': True}."""
    fake = MagicMock()
    fake.rpc = MagicMock()
    fake.rpc.get_basic_chat_info = AsyncMock(return_value={"ok": True})
    fake.rpc.delete_chat = AsyncMock(return_value={"ok": True})
    fake.rpc.set_config = AsyncMock(return_value={"ok": True})
    with patch.object(adapter, "_active_adapter", fake):
        yield fake


class TestRawRpcGate:
    @pytest.mark.asyncio
    async def test_not_registered_without_env(self):
        handlers = {}
        ctx = MagicMock()
        ctx.register_tool.side_effect = lambda **kw: handlers.__setitem__(kw["name"], kw["handler"])
        env = {k: v for k, v in os.environ.items() if k != "DELTACHAT_ENABLE_RAW_RPC"}
        with patch.dict(os.environ, env, clear=True):
            adapter.register_rpc_tools(ctx)
        assert "dc_rpc_call" not in handlers
        assert "dc_safe_rpc_call" in handlers

    @pytest.mark.asyncio
    async def test_allows_ordinary_method(self, raw_rpc_handler, connected_adapter):
        result = json.loads(await raw_rpc_handler({"method": "get_basic_chat_info", "params": [1, 2]}))
        assert result == {"ok": True}
        connected_adapter.rpc.get_basic_chat_info.assert_awaited_once_with(1, 2)

    @pytest.mark.asyncio
    async def test_blocks_destructive_method(self, raw_rpc_handler, connected_adapter):
        result = json.loads(await raw_rpc_handler({"method": "delete_chat", "params": [1, 2]}))
        assert "blocked" in result["error"]
        connected_adapter.rpc.delete_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blocklist_is_honoured(self, raw_rpc_handler, connected_adapter):
        with patch.dict(os.environ, {"DELTACHAT_RAW_RPC_BLOCKLIST": "set_config, other"}):
            result = json.loads(await raw_rpc_handler({"method": "set_config", "params": []}))
        assert "blocked" in result["error"]
        connected_adapter.rpc.set_config.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowlist_excludes_everything_else(self, raw_rpc_handler, connected_adapter):
        with patch.dict(os.environ, {"DELTACHAT_RAW_RPC_ALLOWLIST": "get_basic_chat_info"}):
            allowed = json.loads(await raw_rpc_handler({"method": "get_basic_chat_info", "params": []}))
            denied = json.loads(await raw_rpc_handler({"method": "set_config", "params": []}))
        assert allowed == {"ok": True}
        assert "allowlist" in denied["error"]
        connected_adapter.rpc.set_config.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowlist_does_not_override_destructive_block(self, raw_rpc_handler, connected_adapter):
        """An operator listing delete_chat still does not get to call it via the model."""
        with patch.dict(os.environ, {"DELTACHAT_RAW_RPC_ALLOWLIST": "delete_chat"}):
            result = json.loads(await raw_rpc_handler({"method": "delete_chat", "params": []}))
        assert "blocked" in result["error"]
        connected_adapter.rpc.delete_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_call_is_logged_at_warning(self, raw_rpc_handler, connected_adapter, caplog):
        with caplog.at_level("WARNING", logger="hermes_plugins.deltachat"):
            await raw_rpc_handler({"method": "get_basic_chat_info", "params": []})
        assert any("get_basic_chat_info" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_rpc_error_is_forwarded_and_logged(self, raw_rpc_handler, connected_adapter, caplog):
        """The model needs the real message to fix its own call.

        Nothing an error can disclose is out of reach of a tool that already
        exposes get_message and get_system_info.
        """
        connected_adapter.rpc.get_basic_chat_info = AsyncMock(
            side_effect=RuntimeError("This method takes an array of 2 arguments")
        )
        with caplog.at_level("ERROR", logger="hermes_plugins.deltachat"):
            result = json.loads(await raw_rpc_handler({"method": "get_basic_chat_info", "params": []}))
        assert result == {"error": "This method takes an array of 2 arguments"}
        assert any("get_basic_chat_info" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unknown_method_is_named_as_such(self, raw_rpc_handler, connected_adapter):
        """deltachat2.Rpc.__getattr__ answers any name, so getattr never raises.

        Without the spec check a typo reaches the server and comes back as the
        generic failure, leaving the model nothing to correct.
        """
        result = json.loads(await raw_rpc_handler({"method": "get_bassic_chat_info", "params": []}))
        assert "Unknown method" in result["error"]

    @pytest.mark.asyncio
    async def test_unloadable_spec_does_not_block_the_call(self, raw_rpc_handler, connected_adapter):
        with patch.object(adapter, "_fetch_spec", AsyncMock(side_effect=RuntimeError("no binary"))):
            result = json.loads(await raw_rpc_handler({"method": "get_basic_chat_info", "params": []}))
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_missing_method_arg(self, raw_rpc_handler, connected_adapter):
        result = json.loads(await raw_rpc_handler({}))
        assert "Missing 'method'" in result["error"]
