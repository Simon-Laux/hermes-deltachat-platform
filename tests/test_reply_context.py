"""Quote-replies reach Hermes as reply_to_* fields, so the agent sees what was replied to."""
from unittest.mock import AsyncMock

import pytest

import adapter as adapter_mod
from adapter import DC_CONTACT_ID_SELF, DeltaChatAdapter
from tests.conftest import MockPlatform, MockPlatformConfig

QUOTED = {"id": 3, "chat_id": 5, "from_id": DC_CONTACT_ID_SELF,
          "text": "1. realtime bug\n2. something else"}


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_get_or_create_chat_token", AsyncMock(return_value="tok"))
    a = DeltaChatAdapter(MockPlatformConfig(name="deltachat-platform",
                                            platform=MockPlatform.DELTACHAT, extra={}))
    a.account_id = 1
    a.rpc = AsyncMock()
    a.rpc.get_contact.return_value = {"name": "u", "is_key_contact": True}
    a.rpc.get_config.return_value = None
    a.rpc.get_basic_chat_info.return_value = {"chat_type": "Single", "name": "c"}
    a.handle_message = AsyncMock()
    return a


def _msg(quote=None, **kw):
    msg = {"id": 7, "chat_id": 5, "from_id": 9, "text": "re that one", "view_type": "Text",
           **kw}
    if quote:
        msg["quote"] = quote
    return msg


def _with_message(text="1. realtime bug", **kw):
    return {"kind": "WithMessage", "message_id": 3, "text": text,
            "author_display_name": "Hermes", "view_type": "Text", **kw}


async def _event(a, msg, quoted=QUOTED):
    a.rpc.get_message.side_effect = lambda acc, mid: msg if mid == 7 else quoted
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    return a.handle_message.await_args.args[0]


@pytest.mark.asyncio
async def test_quote_reply_carries_the_full_quoted_message(adapter):
    event = await _event(adapter, _msg(_with_message()))
    assert event.reply_to_message_id == "3"
    assert event.reply_to_text == QUOTED["text"]
    assert event.reply_to_author_name == "Hermes"
    assert event.reply_to_is_own_message is True


@pytest.mark.asyncio
async def test_plain_message_has_no_reply_context(adapter):
    event = await _event(adapter, _msg())
    assert event.reply_to_message_id is None and event.reply_to_text is None


@pytest.mark.asyncio
async def test_quote_of_a_message_we_dont_have_is_left_out(adapter):
    event = await _event(adapter, _msg({"kind": "JustText", "text": "old"}))
    assert event.reply_to_message_id is None


@pytest.mark.asyncio
async def test_quoted_message_from_another_chat_only_gives_the_quote_text(adapter):
    other = {**QUOTED, "chat_id": 99, "from_id": 4}
    event = await _event(adapter, _msg(_with_message("what they quoted")), quoted=other)
    assert event.reply_to_text == "what they quoted"
    assert event.reply_to_is_own_message is False


@pytest.mark.asyncio
async def test_quote_of_a_captionless_image_still_has_text(adapter):
    image = {**QUOTED, "text": ""}
    event = await _event(adapter, _msg(_with_message("", view_type="Image")), quoted=image)
    assert event.reply_to_text == "[Image]"


@pytest.mark.asyncio
async def test_media_reply_carries_reply_context(adapter, tmp_path):
    blob = tmp_path / "a.jpg"
    blob.write_bytes(b"x")
    msg = _msg(_with_message(), text="", view_type="Image", file=str(blob),
               file_mime="image/jpeg")
    event = await _event(adapter, msg)
    assert event.reply_to_text == QUOTED["text"]
