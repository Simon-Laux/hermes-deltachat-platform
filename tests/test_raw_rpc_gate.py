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
from adapter import _is_destructive


class TestIsDestructive:
    @pytest.mark.parametrize("method", [
        "leave_group",
        "set_chat_ephemeral_timer",  # timed deletion
        "add_contact_to_chat",       # membership change the prefix rule misses
        "block_chat",
        "delete_something_added_next_release",  # prefix rule, not a literal list
        "remove_something_added_next_release",
    ])
    def test_blocked(self, method):
        assert _is_destructive(method)

    @pytest.mark.parametrize("method", [
        "get_basic_chat_info",
        "undelete_chat",      # prefix match, not substring
        "misc_set_draft",     # the agent managing its own draft is ordinary use
        "misc_send_draft",
    ])
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
    @pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off", "OFF", " 0 "])
    def test_not_registered_unless_flag_reads_as_on(self, value):
        """A kill switch must not fail open.

        plugin.yaml prompts for this, so an operator answering "0" to mean "no"
        is the expected input, not an exotic one.
        """
        handlers = {}
        ctx = MagicMock()
        ctx.register_tool.side_effect = lambda **kw: handlers.__setitem__(kw["name"], kw["handler"])
        env = {} if value is None else {"DELTACHAT_ENABLE_RAW_RPC": value}
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
    @pytest.mark.parametrize("value", [" , ,", ",", "   ,"])
    async def test_allowlist_naming_nothing_allows_nothing(self, raw_rpc_handler, connected_adapter, value):
        """A typo'd allowlist must not silently become "no allowlist"."""
        with patch.dict(os.environ, {"DELTACHAT_RAW_RPC_ALLOWLIST": value}):
            result = json.loads(await raw_rpc_handler({"method": "get_basic_chat_info", "params": []}))
        assert "lists no method names" in result["error"]
        connected_adapter.rpc.get_basic_chat_info.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["", "   "])
    async def test_blank_allowlist_means_unrestricted(self, raw_rpc_handler, connected_adapter, value):
        with patch.dict(os.environ, {"DELTACHAT_RAW_RPC_ALLOWLIST": value}):
            result = json.loads(await raw_rpc_handler({"method": "get_basic_chat_info", "params": []}))
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_allowlist_tolerates_spacing(self, raw_rpc_handler, connected_adapter):
        with patch.dict(os.environ, {"DELTACHAT_RAW_RPC_ALLOWLIST": " get_basic_chat_info , set_config ,"}):
            result = json.loads(await raw_rpc_handler({"method": "set_config", "params": []}))
        assert result == {"ok": True}

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
    async def test_refusal_is_logged_distinguishably(self, raw_rpc_handler, connected_adapter, caplog):
        """errors.log must not read the same for a blocked call and an executed one."""
        with caplog.at_level("WARNING", logger="hermes_plugins.deltachat"):
            await raw_rpc_handler({"method": "delete_chat", "params": []})
        lines = [r.getMessage() for r in caplog.records]
        assert any("REFUSED" in m for m in lines)
        assert not any("ACCEPTED" in m for m in lines)

    @pytest.mark.asyncio
    async def test_method_name_cannot_forge_a_log_line(self, raw_rpc_handler, connected_adapter, caplog):
        """%r, not %s — an embedded newline must not fake a second audit entry."""
        forged = "delete_chat\nWARNING hermes_plugins.deltachat: Raw RPC call ACCEPTED: 'get_account_info'"
        with caplog.at_level("WARNING", logger="hermes_plugins.deltachat"):
            await raw_rpc_handler({"method": forged, "params": []})
        for r in caplog.records:
            assert "\n" not in r.getMessage(), "raw newline reached the log"

    @pytest.mark.asyncio
    async def test_accepted_call_is_logged_once_past_the_gates(self, raw_rpc_handler, connected_adapter, caplog):
        with caplog.at_level("WARNING", logger="hermes_plugins.deltachat"):
            await raw_rpc_handler({"method": "get_basic_chat_info", "params": []})
        assert sum("ACCEPTED" in r.getMessage() for r in caplog.records) == 1

    @pytest.mark.asyncio
    async def test_missing_method_arg(self, raw_rpc_handler, connected_adapter):
        result = json.loads(await raw_rpc_handler({}))
        assert "Missing 'method'" in result["error"]
