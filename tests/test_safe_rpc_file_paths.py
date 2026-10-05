"""dc_safe_rpc_call runs local file paths through the delivery filter (#32).

The chat token scopes which chat a call reaches, not which file core reads.
Without this, one send_msg with data.file=~/.hermes/.env mails every API key
to whichever chat the caller can steer.
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import adapter


# Real shapes from deltachat-rpc-openrpc.json.
SPEC = {"methods": [
    {"name": "send_msg", "params": [{"name": "accountId"}, {"name": "chatId"}, {"name": "data"}]},
    {"name": "misc_send_msg", "params": [
        {"name": "accountId"}, {"name": "chatId"}, {"name": "text"}, {"name": "file"},
        {"name": "filename"}, {"name": "location"}, {"name": "quotedMessageId"}]},
    {"name": "misc_set_draft", "params": [
        {"name": "accountId"}, {"name": "chatId"}, {"name": "text"}, {"name": "file"},
        {"name": "filename"}, {"name": "quotedMessageId"}, {"name": "viewType"}]},
    {"name": "set_chat_profile_image", "params": [
        {"name": "accountId"}, {"name": "chatId"}, {"name": "imagePath"}]},
    {"name": "send_sticker", "params": [
        {"name": "accountId"}, {"name": "chatId"}, {"name": "stickerPath"}]},
]}

CHAT_ID = 4242
TOKEN = "deadbeef"
SAFE = "/home/bot/.hermes/cache/out.png"
SECRET = "/home/bot/.hermes/.env"


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
    for m in SPEC["methods"]:
        setattr(fake.rpc, m["name"], AsyncMock(return_value=1))
    # Stand-in for the Hermes policy: SECRET is denylisted, anything else
    # resolves to the canonical SAFE path.
    fake.filter_local_delivery_paths.side_effect = (
        lambda paths: [] if paths[0] == SECRET else [SAFE])
    monkeypatch.setattr(adapter, "_active_adapter", fake)
    monkeypatch.setattr(adapter, "_spec_cache", SPEC)
    monkeypatch.setattr(adapter, "_resolve_chat_token", AsyncMock(return_value=CHAT_ID))
    return fake


async def _call(handler, method, params):
    return await handler({"method": method, "chat_token": TOKEN, "params": params})


class TestRefused:
    @pytest.mark.asyncio
    async def test_send_msg_data_file(self, safe_handler, connected):
        result = json.loads(await _call(safe_handler, "send_msg", [{"text": "hi", "file": SECRET}]))
        assert "refused" in result["error"]
        connected.rpc.send_msg.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["misc_send_msg", "misc_set_draft"])
    async def test_bare_file_param(self, safe_handler, connected, method):
        result = json.loads(await _call(safe_handler, method, ["hi", SECRET]))
        assert "refused" in result["error"]
        getattr(connected.rpc, method).assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["set_chat_profile_image", "send_sticker"])
    async def test_image_and_sticker_paths(self, safe_handler, connected, method):
        result = json.loads(await _call(safe_handler, method, [SECRET]))
        assert "refused" in result["error"]
        getattr(connected.rpc, method).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_string_path(self, safe_handler, connected):
        result = json.loads(await _call(safe_handler, "send_msg", [{"file": ["/etc/passwd"]}]))
        assert "refused" in result["error"]
        connected.rpc.send_msg.assert_not_awaited()


class TestAllowed:
    @pytest.mark.asyncio
    async def test_send_msg_path_is_replaced_by_validated_path(self, safe_handler, connected):
        await _call(safe_handler, "send_msg", [{"text": "hi", "file": "/workspace/out.png"}])
        connected.rpc.send_msg.assert_awaited_once_with(1, CHAT_ID, {"text": "hi", "file": SAFE})

    @pytest.mark.asyncio
    async def test_bare_file_is_replaced(self, safe_handler, connected):
        await _call(safe_handler, "misc_send_msg", ["hi", "/tmp/out.png", "out.png"])
        connected.rpc.misc_send_msg.assert_awaited_once_with(1, CHAT_ID, "hi", SAFE, "out.png")

    @pytest.mark.asyncio
    async def test_text_only_send_skips_the_filter(self, safe_handler, connected):
        await _call(safe_handler, "send_msg", [{"text": "hi", "file": None}])
        await _call(safe_handler, "misc_send_msg", ["hi", None])
        connected.filter_local_delivery_paths.assert_not_called()
        connected.rpc.send_msg.assert_awaited_once()
        connected.rpc.misc_send_msg.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clearing_profile_image(self, safe_handler, connected):
        await _call(safe_handler, "set_chat_profile_image", [None])
        connected.rpc.set_chat_profile_image.assert_awaited_once_with(1, CHAT_ID, None)
