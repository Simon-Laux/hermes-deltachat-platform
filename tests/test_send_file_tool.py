"""dc_send_file — proactive file delivery.

Deliberately file-only. Hermes's built-in send_message already routes text to
any plugin platform via _send_via_adapter; what it cannot do is carry a file
(for platforms outside its hardcoded list it errors on media-only sends and
silently drops the attachment otherwise). That gap is the reason this tool
exists, and it matters here because webxdc delivery is what this plugin is for.
"""

import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import adapter as adapter_mod
from adapter import DeltaChatAdapter, register_rpc_tools


@pytest.fixture
def tools():
    """Collect the tools register_rpc_tools() hands to Hermes, by name."""
    registered = {}

    ctx = MagicMock()
    ctx.register_tool.side_effect = lambda **kw: registered.__setitem__(kw["name"], kw)
    register_rpc_tools(ctx)
    return registered


@pytest.fixture
def adapter(platform_config, monkeypatch, tmp_path):
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.send_document = AsyncMock(
        return_value=MagicMock(success=True, message_id="55", error=None)
    )
    # Pass paths through untouched; the real filter is covered by its own tests.
    a.filter_local_delivery_paths = MagicMock(side_effect=lambda paths, **kw: list(paths))
    monkeypatch.setattr(adapter_mod, "_active_adapter", a)
    monkeypatch.delenv("DELTACHAT_HOME_CHANNEL", raising=False)
    return a


@pytest.fixture
def send_file(tools):
    return tools["dc_send_file"]["handler"]


async def _call(send_file, **args):
    return json.loads(await send_file(args))


class TestRegistration:
    def test_is_registered(self, tools):
        assert "dc_send_file" in tools

    def test_file_path_is_required(self, tools):
        assert tools["dc_send_file"]["schema"]["parameters"]["required"] == ["file_path"]

    def test_points_the_agent_at_send_message_for_text(self, tools):
        """The two must not compete; the description says which is which."""
        assert "send_message" in tools["dc_send_file"]["schema"]["description"]

    def test_no_general_purpose_send_tool_is_registered(self, tools):
        """We deliberately did not port the fork's dc_send_message."""
        assert "dc_send_message" not in tools


class TestTargeting:
    @pytest.mark.asyncio
    async def test_chat_token_resolves_to_a_chat_id(self, adapter, send_file):
        with patch.object(adapter_mod, "_resolve_chat_token", AsyncMock(return_value=42)):
            out = await _call(send_file, file_path="/w/app.xdc", chat_token="abc")

        assert out == {"success": True, "message_id": "55"}
        assert adapter.send_document.await_args.args[0] == "42"

    @pytest.mark.asyncio
    async def test_unknown_token_is_rejected(self, adapter, send_file):
        with patch.object(adapter_mod, "_resolve_chat_token", AsyncMock(return_value=None)):
            out = await _call(send_file, file_path="/w/app.xdc", chat_token="bogus")

        assert "Unknown chat_token" in out["error"]
        adapter.send_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_home_channel(self, adapter, send_file, monkeypatch):
        monkeypatch.setenv("DELTACHAT_HOME_CHANNEL", "77")

        out = await _call(send_file, file_path="/w/report.pdf")

        assert out["success"] is True
        assert adapter.send_document.await_args.args[0] == "77"

    @pytest.mark.asyncio
    async def test_no_token_and_no_home_channel_is_an_error(self, adapter, send_file):
        out = await _call(send_file, file_path="/w/app.xdc")

        assert "DELTACHAT_HOME_CHANNEL" in out["error"]
        adapter.send_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_numeric_home_channel_is_an_error(
        self, adapter, send_file, monkeypatch
    ):
        monkeypatch.setenv("DELTACHAT_HOME_CHANNEL", "not-an-id")

        out = await _call(send_file, file_path="/w/app.xdc")

        assert "not a valid chat id" in out["error"]


class TestDeliveryPolicy:
    @pytest.mark.asyncio
    async def test_path_goes_through_the_delivery_filter(
        self, adapter, send_file, monkeypatch
    ):
        """This is the only check between an agent path and a host file read."""
        monkeypatch.setenv("DELTACHAT_HOME_CHANNEL", "77")

        await _call(send_file, file_path="/w/app.xdc")

        adapter.filter_local_delivery_paths.assert_called_once_with(["/w/app.xdc"])

    @pytest.mark.asyncio
    async def test_a_rejected_path_is_not_sent(self, adapter, send_file, monkeypatch):
        monkeypatch.setenv("DELTACHAT_HOME_CHANNEL", "77")
        adapter.filter_local_delivery_paths = MagicMock(return_value=[])

        out = await _call(send_file, file_path="/etc/shadow")

        assert "blocked by policy" in out["error"]
        adapter.send_document.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_remapped_path_is_what_gets_sent(
        self, adapter, send_file, monkeypatch
    ):
        """/workspace/ paths are rewritten to a host cache path before sending."""
        monkeypatch.setenv("DELTACHAT_HOME_CHANNEL", "77")
        adapter.filter_local_delivery_paths = MagicMock(return_value=["/host/cache/app.xdc"])

        await _call(send_file, file_path="/workspace/app.xdc")

        assert adapter.send_document.await_args.args[1] == "/host/cache/app.xdc"


class TestArgumentsAndFailures:
    @pytest.mark.asyncio
    async def test_missing_file_path_is_an_error(self, adapter, send_file):
        out = await _call(send_file, caption="hi")
        assert "file_path" in out["error"]

    @pytest.mark.asyncio
    async def test_caption_is_forwarded(self, adapter, send_file, monkeypatch):
        monkeypatch.setenv("DELTACHAT_HOME_CHANNEL", "77")

        await _call(send_file, file_path="/w/a.pdf", caption="the report")

        assert adapter.send_document.await_args.kwargs["caption"] == "the report"

    @pytest.mark.asyncio
    async def test_blank_caption_becomes_none(self, adapter, send_file, monkeypatch):
        monkeypatch.setenv("DELTACHAT_HOME_CHANNEL", "77")

        await _call(send_file, file_path="/w/a.pdf", caption="   ")

        assert adapter.send_document.await_args.kwargs["caption"] is None

    @pytest.mark.asyncio
    async def test_not_connected_is_reported(self, send_file, monkeypatch):
        monkeypatch.setattr(adapter_mod, "_active_adapter", None)
        out = await _call(send_file, file_path="/w/a.pdf")
        assert "not connected" in out["error"].lower()

    @pytest.mark.asyncio
    async def test_a_failed_send_is_reported(self, adapter, send_file, monkeypatch):
        monkeypatch.setenv("DELTACHAT_HOME_CHANNEL", "77")
        adapter.send_document = AsyncMock(
            return_value=MagicMock(success=False, error="over quota")
        )

        out = await _call(send_file, file_path="/w/a.pdf")

        assert out["error"] == "over quota"

    @pytest.mark.asyncio
    async def test_a_raising_send_does_not_leak_internals(
        self, adapter, send_file, monkeypatch
    ):
        monkeypatch.setenv("DELTACHAT_HOME_CHANNEL", "77")
        adapter.send_document = AsyncMock(side_effect=Exception("/secret/path blew up"))

        out = await _call(send_file, file_path="/w/a.pdf")

        assert out["error"] == "Send failed"
        assert "secret" not in out["error"]
