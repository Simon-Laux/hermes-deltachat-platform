"""Group mention gating (require_mention): unmentioned group messages are dropped; DMs,
mentions, quote-replies to the bot and slash commands pass.

Based on the tests in PR #18 by terafin.
"""
from unittest.mock import AsyncMock

import pytest

from adapter import DC_CONTACT_ID_SELF, DeltaChatAdapter
from tests.conftest import MockPlatform, MockPlatformConfig


def _adapter(extra=None, chat_type="Group", quoted_from=None, displayname="Ghost"):
    cfg = MockPlatformConfig(name="deltachat-platform", platform=MockPlatform.DELTACHAT,
                             extra=extra or {})
    a = DeltaChatAdapter(cfg)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.rpc.get_basic_chat_info.return_value = {"chat_type": chat_type, "name": "c"}
    a.rpc.get_config.side_effect = lambda acc, key: {
        "configured_addr": "ghost-agent@chat.example", "displayname": displayname}.get(key)
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
    ("", False),
    ("@ghost can you check", True),
    ("hey @Ghost, ping", True),
    ("ghost is a word, not a mention", False),
    ("mail @ghosted", False),
    ("user@ghost.example", False),
    # the address and its localpart are not names the bot answers to
    ("@ghost-agent hi", False),
    ("ping ghost-agent@chat.example please", False),
    ("/home/alice is broken", False),  # not a command
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


@pytest.mark.asyncio
@pytest.mark.parametrize("require_mention", [True, False])
@pytest.mark.parametrize("text,ok,forwarded", [
    ("/reset@Ghost", True, "/reset"),
    ("/reset@ghost now please", True, "/reset now please"),
    ("/reset@spooky", True, "/reset"),  # alias
    ("/reset@Other", False, None),
    ("/reset@Ghostly", False, None),  # another bot whose name starts with ours
    ("/reset@ghost-agent@chat.example", False, None),  # the address is not a name
])
async def test_addressed_commands(require_mention, text, ok, forwarded):
    a = _adapter({"require_mention": require_mention, "mention_aliases": "spooky"})
    msg = _msg(text)
    assert await a._mention_gate_allows(msg, 5) is ok
    if ok:
        assert msg["text"] == forwarded


@pytest.mark.asyncio
async def test_addressed_command_to_name_with_spaces():
    a = _adapter({"require_mention": True}, displayname="Hermes Bot")
    msg = _msg("/model@hermes bot gpt-5")
    assert await a._mention_gate_allows(msg, 5)
    assert msg["text"] == "/model gpt-5"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/reset", "/home/alice is broken", "/"])
async def test_bare_slash_in_group_needs_addressing(text):
    a = _adapter({"require_mention": True})
    assert not await a._mention_gate_allows(_msg(text), 5)


@pytest.mark.asyncio
async def test_without_require_mention_bare_commands_pass_without_lookup():
    a = _adapter()
    assert await a._mention_gate_allows(_msg("/reset"), 5)
    a.rpc.get_basic_chat_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_addressed_commands_are_left_alone_in_dms():
    a = _adapter({"require_mention": True}, chat_type="Single")
    msg = _msg("/reset@Other")
    assert await a._mention_gate_allows(msg, 5)
    assert msg["text"] == "/reset@Other"


@pytest.mark.asyncio
async def test_handler_forwards_addressed_command_without_suffix():
    a = _adapter({"require_mention": True})
    a.rpc.get_message.return_value = _msg("/reset@Ghost") | {"view_type": "Text"}
    a.rpc.get_contact.return_value = {"name": "u"}
    a.handle_message = AsyncMock()
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    assert a.handle_message.await_args.args[0].text == "/reset"


@pytest.mark.asyncio
@pytest.mark.parametrize("displayname,text,ok", [
    ("Hermes Bot", "@hermes bot what's up", True),
    ("Hermes Bot", "@hermes what's up", False),
    ("R2-D2", "beep @R2-D2!", True),
    ("R2-D2", "@R2-D2-fan hi", False),
    ("Dr. Who", "@dr. who are you", True),
    ("Žofie", "ahoj @žofie", True),
    ("Žofie", "ahoj @ŽOFIE", True),
    ("Žofie", "@Žofiex", False),
])
async def test_display_name_shapes(displayname, text, ok):
    a = _adapter({"require_mention": True}, displayname=displayname)
    assert await a._mention_gate_allows(_msg(text), 5) is ok


@pytest.mark.asyncio
async def test_alias_with_leading_at_still_matches():
    a = _adapter({"require_mention": True, "mention_aliases": "@spooky, @@ ,  "})
    assert a._mention_aliases == ["spooky"]
    assert await a._mention_gate_allows(_msg("hey @spooky"), 5)


@pytest.mark.asyncio
async def test_no_display_name_and_no_aliases_warns_once(caplog):
    a = _adapter({"require_mention": True}, displayname=None)
    for _ in range(3):
        assert not await a._mention_gate_allows(_msg("@ghost hi"), 5)
    assert sum("no display name" in r.message for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_no_display_name_quote_reply_still_passes():
    a = _adapter({"require_mention": True}, displayname=None, quoted_from=DC_CONTACT_ID_SELF)
    assert await a._mention_gate_allows(_msg("and this?", quote_id=3), 5)


@pytest.mark.asyncio
async def test_quote_of_deleted_message_is_not_a_mention(caplog):
    a = _adapter({"require_mention": True})
    a.rpc.get_message.side_effect = RuntimeError("Message does not exist")
    assert not await a._mention_gate_allows(_msg("agreed", quote_id=3), 5)
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


@pytest.mark.asyncio
async def test_captionless_image_in_group_is_dropped():
    a = _adapter({"require_mention": True})
    a.rpc.get_message.return_value = {"id": 7, "text": "", "from_id": 9, "view_type": "Image",
                                      "file": "/blobs/x.jpg", "file_mime": "image/jpeg"}
    a._handle_non_text_message = AsyncMock()
    a.handle_message = AsyncMock()
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    a._handle_non_text_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_mentioned_image_caption_passes():
    a = _adapter({"require_mention": True})
    a.rpc.get_message.return_value = {"id": 7, "text": "@ghost look", "from_id": 9,
                                      "view_type": "Image", "file": "/blobs/x.jpg",
                                      "file_mime": "image/jpeg"}
    a._handle_non_text_message = AsyncMock()
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    a._handle_non_text_message.assert_awaited_once()


@pytest.mark.parametrize("value,on", [
    ("enabled", True), ("1", True), (True, True),
    ("off", False), ("0", False), (False, False), ("", False),
])
def test_config_and_env_read_on_off_the_same_way(monkeypatch, value, on):
    assert _adapter({"require_mention": value})._require_mention is on
    if not isinstance(value, bool):
        monkeypatch.setenv("DELTACHAT_REQUIRE_MENTION", value)
        assert _adapter()._require_mention is on
