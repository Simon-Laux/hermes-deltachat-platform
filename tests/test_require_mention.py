"""Group mention gating (require_mention): unmentioned group messages are dropped; DMs,
mentions, quote-replies to the bot and slash commands pass.

Based on the tests in PR #18 by terafin.
"""
from unittest.mock import AsyncMock

import pytest

from adapter import DC_CONTACT_ID_SELF, DeltaChatAdapter
from tests.conftest import MockPlatform, MockPlatformConfig


def _adapter(extra=None, chat_type="Group", quoted_from=None):
    cfg = MockPlatformConfig(name="deltachat-platform", platform=MockPlatform.DELTACHAT,
                             extra=extra or {})
    a = DeltaChatAdapter(cfg)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.rpc.get_basic_chat_info.return_value = {"chat_type": chat_type, "name": "c"}
    a.rpc.get_config.side_effect = lambda acc, key: {
        "configured_addr": "ghost-agent@chat.example", "displayname": "Ghost"}.get(key)
    a.rpc.get_message.return_value = {"from_id": quoted_from}
    return a


def _msg(text, quote_id=None):
    msg = {"id": 7, "text": text, "from_id": 9}
    if quote_id:
        msg["quote"] = {"kind": "WithMessage", "message_id": quote_id, "text": "earlier"}
    return msg


@pytest.mark.asyncio
async def test_off_by_default_everything_passes():
    a = _adapter()
    assert await a._mention_gate_allows(_msg("hello all"), 5)
    a.rpc.get_basic_chat_info.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text,ok", [
    ("hello all", False),
    ("", False),  # captionless image/voice
    ("@ghost can you check", True),
    ("hey @Ghost, ping", True),
    ("ghost is a word, not a mention", False),
    ("mail @ghosted", False),
    ("user@ghost.example", False),
    # the address and its localpart are not names the bot answers to
    ("@ghost-agent hi", False),
    ("ping ghost-agent@chat.example please", False),
    ("/reset", True),
])
async def test_group_messages_need_a_mention(text, ok):
    a = _adapter({"require_mention": True})
    assert await a._mention_gate_allows(_msg(text), 5) is ok


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["Single", "Mailinglist", "InBroadcast"])
async def test_only_groups_are_gated(chat_type):
    a = _adapter({"require_mention": True}, chat_type=chat_type)
    assert await a._mention_gate_allows(_msg("no mention here"), 5)


@pytest.mark.asyncio
async def test_aliases_from_env(monkeypatch):
    monkeypatch.setenv("DELTACHAT_REQUIRE_MENTION", "1")
    monkeypatch.setenv("DELTACHAT_MENTION_ALIASES", "spooky, Casper the Friendly")
    a = _adapter()
    assert await a._mention_gate_allows(_msg("hey @spooky"), 5)
    assert await a._mention_gate_allows(_msg("@casper the friendly hi"), 5)
    assert await a._mention_gate_allows(_msg("@Ghost still works"), 5)
    assert not await a._mention_gate_allows(_msg("hey spooky"), 5)


@pytest.mark.asyncio
async def test_config_false_beats_env(monkeypatch):
    monkeypatch.setenv("DELTACHAT_REQUIRE_MENTION", "1")
    a = _adapter({"require_mention": False})
    assert await a._mention_gate_allows(_msg("hello all"), 5)


@pytest.mark.asyncio
async def test_quote_reply_to_own_message_counts_as_mention():
    a = _adapter({"require_mention": True}, quoted_from=DC_CONTACT_ID_SELF)
    assert await a._mention_gate_allows(_msg("and what about this?", quote_id=3), 5)
    a.rpc.get_message.assert_awaited_once_with(1, 3)


@pytest.mark.asyncio
async def test_quote_reply_to_someone_else_needs_a_mention():
    a = _adapter({"require_mention": True}, quoted_from=12)
    assert not await a._mention_gate_allows(_msg("agreed", quote_id=3), 5)


@pytest.mark.asyncio
async def test_mention_inside_quoted_text_does_not_count():
    a = _adapter({"require_mention": True}, quoted_from=12)
    msg = _msg("agreed", quote_id=3)
    msg["quote"]["text"] = "@Ghost please look"
    assert not await a._mention_gate_allows(msg, 5)


@pytest.mark.asyncio
async def test_rpc_failure_lets_message_through():
    a = _adapter({"require_mention": True})
    a.rpc.get_basic_chat_info.side_effect = RuntimeError("rpc down")
    assert await a._mention_gate_allows(_msg("hello all"), 5)


@pytest.mark.asyncio
async def test_handler_drops_unmentioned_group_message():
    a = _adapter({"require_mention": True})
    a.rpc.get_message.return_value = _msg("hello all") | {"view_type": "Text"}
    a.rpc.get_contact.return_value = {"name": "u"}
    a.handle_message = AsyncMock()
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    a.handle_message.assert_not_awaited()

    a.rpc.get_message.return_value = _msg("@ghost hi") | {"view_type": "Text"}
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 8})
    a.handle_message.assert_awaited_once()
    assert a.handle_message.await_args.args[0].source.chat_type == "group"
