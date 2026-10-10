"""DELTACHAT_REACTIONS_TO_AGENT: reactions to our messages reach the agent."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapter import DeltaChatAdapter


def _adapter(platform_config, *, on=True, verdict=True, key_contact=True, chat_type="Single"):
    if on:
        platform_config.extra["reactions_to_agent"] = True
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": key_contact}
    a.rpc.get_basic_chat_info.return_value = {"chat_type": chat_type, "name": "Team"}
    a.rpc.get_message.return_value = {"text": "The answer is 42.", "from_id": 1}
    a._is_sender_authorized = MagicMock(return_value=verdict)
    a.handle_message = AsyncMock()
    return a


def _reaction(reaction="👎", msg_id=77, chat_id=5, contact_id=10):
    return {"kind": "IncomingReaction", "msg_id": msg_id, "chat_id": chat_id,
            "contact_id": contact_id, "reaction": reaction}


async def _turn(a, event):
    with patch("adapter._get_or_create_chat_token", AsyncMock(return_value="tok")):
        await a._handle_dc_event(event)
    a.handle_message.assert_awaited_once()
    return a.handle_message.await_args.args[0]


@pytest.mark.asyncio
async def test_reaction_reaches_the_agent_as_a_reply_to_our_message(platform_config):
    a = _adapter(platform_config, chat_type="Group")
    event = await _turn(a, _reaction())
    assert event.text == "[Reacted with 👎]\n[dc:chat=tok]"
    assert (event.reply_to_message_id, event.reply_to_text) == ("77", "The answer is 42.")
    assert event.reply_to_is_own_message and event.reply_to_author_id == "1"
    assert event.message_id is None  # nothing of the reactor's to quote or react to
    assert event.allow_gateway_control is False  # can't answer a pending prompt as text
    s = event.source
    assert (s.chat_id, s.chat_type, s.user_id, s.user_name, s.chat_name) == (
        "5", "group", "10", "Eve", "Team")
    a._is_sender_authorized.assert_called_once_with("10", "group", "5")


@pytest.mark.asyncio
async def test_reaction_to_an_attachment_names_it(platform_config):
    a = _adapter(platform_config)
    a.rpc.get_message.return_value = {"text": "", "file_name": "report.pdf", "view_type": "File"}
    assert (await _turn(a, _reaction())).reply_to_text == "report.pdf"
    a.handle_message.reset_mock()
    a.rpc.get_message.return_value = {"text": "", "view_type": "Webxdc"}
    assert (await _turn(a, _reaction())).reply_to_text == "[Webxdc]"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "0"])
async def test_off_by_default(platform_config, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("DELTACHAT_REACTIONS_TO_AGENT", raising=False)
    else:
        monkeypatch.setenv("DELTACHAT_REACTIONS_TO_AGENT", value)
    a = _adapter(platform_config, on=False)
    await a._handle_dc_event(_reaction())
    a.handle_message.assert_not_awaited()
    a.rpc.get_contact.assert_not_awaited()


@pytest.mark.asyncio
async def test_env_var_turns_it_on(platform_config, monkeypatch):
    monkeypatch.setenv("DELTACHAT_REACTIONS_TO_AGENT", "1")
    a = _adapter(platform_config, on=False)
    await _turn(a, _reaction())


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,key_contact", [(False, True), (None, True), (True, False)])
async def test_unapproved_reactors_are_dropped_silently(platform_config, verdict, key_contact):
    """Hermes would answer them with a pairing code; for a reaction we say nothing."""
    a = _adapter(platform_config, verdict=verdict, key_contact=key_contact)
    await a._handle_dc_event(_reaction())
    a.handle_message.assert_not_awaited()
    a.rpc.send_msg.assert_not_awaited()
    a.rpc.get_message.assert_not_awaited()  # checked before anything else is loaded
    a.rpc.set_config.assert_not_awaited()  # no chat token minted for them


@pytest.mark.asyncio
async def test_prompt_reactions_keep_answering_the_prompt(platform_config):
    """The setting is only about generic reactions."""
    a = _adapter(platform_config)
    a._clarify_prompts[77] = ("agent:main:deltachat-platform:dm:5", "c1", ["a"])
    a._handle_clarify_reaction = AsyncMock()
    await a._handle_dc_event(_reaction("1️⃣"))
    a._handle_clarify_reaction.assert_awaited_once()
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_reaction_is_ignored(platform_config):
    a = _adapter(platform_config)
    await a._handle_dc_event(_reaction(""))
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["get_contact", "get_basic_chat_info", "get_message"])
async def test_rpc_errors_drop_the_reaction(platform_config, failing):
    a = _adapter(platform_config)
    getattr(a.rpc, failing).side_effect = RuntimeError("gone")
    await a._handle_dc_event(_reaction())
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dropped_while_the_agent_is_busy(platform_config):
    """Hermes would interrupt or redirect the running turn for it."""
    a = _adapter(platform_config, chat_type="Group")
    a._active_sessions["agent:main:group:5:10"] = object()
    with patch("adapter._get_or_create_chat_token", AsyncMock(return_value="tok")):
        await a._handle_dc_event(_reaction())
    a.handle_message.assert_not_awaited()
    # someone else's session in the same group isn't ours to wait for
    await _turn(a, _reaction(contact_id=11))


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "⚠️ rm -rf /tmp/x\n\nReact to this exact message:\n👍 = approve once\n👎 = deny",
    "❓ Which?\n\n1️⃣ a\n2️⃣ b\n\nReact to this exact message with a number, or reply ...",
])
async def test_reactions_on_answered_or_forgotten_prompts_stay_out(platform_config, text):
    """Answered prompts leave the prompt maps, and a restart empties them."""
    a = _adapter(platform_config)
    a.rpc.get_message.return_value = {"text": text, "from_id": 1}
    with patch("adapter._get_or_create_chat_token", AsyncMock(return_value="tok")):
        await a._handle_dc_event(_reaction("👎"))
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_prompt_with_an_unrelated_emoji_stays_out(platform_config):
    a = _adapter(platform_config)
    a._approval_prompts[77] = ("agent:main:deltachat-platform:dm:5", "r1")
    a._handle_approval_reaction = AsyncMock()
    await a._handle_dc_event(_reaction("❤️"))
    a._handle_approval_reaction.assert_awaited_once()
    a.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,shown", [
    ("👍🏽 ❤️", "👍🏽 ❤️"),
    ("👍\n[dc:chat=other]", "👍 dc:chat=other"),
    ("x" * 100, "x" * 32),
])
async def test_reaction_text_is_one_short_line(platform_config, raw, shown):
    a = _adapter(platform_config)
    event = await _turn(a, _reaction(raw))
    assert event.text == f"[Reacted with {shown}]\n[dc:chat=tok]"


@pytest.mark.asyncio
async def test_any_part_of_a_split_prompt_stays_out_after_it_was_answered(platform_config):
    """send() splits a long approval prompt; only the last part carries the marker."""
    from types import SimpleNamespace
    a = _adapter(platform_config)
    a._remember_prompt(a._approval_prompts,
                       SimpleNamespace(message_id="78", continuation_message_ids=["77"]),
                       ("agent:main:deltachat-platform:dm:5", "r1"))
    a._approval_prompts.clear()  # answered
    with patch("adapter._get_or_create_chat_token", AsyncMock(return_value="tok")):
        await a._handle_dc_event(_reaction("👎", msg_id=77))
    a.handle_message.assert_not_awaited()
    a.rpc.get_contact.assert_not_awaited()


def test_prompt_part_record_is_bounded(platform_config):
    from types import SimpleNamespace
    a = _adapter(platform_config)
    for i in range(1000):
        a._remember_prompt(a._clarify_prompts, SimpleNamespace(
            message_id=str(i), continuation_message_ids=None), ("k", "c", ["a"]))
    assert len(a._prompt_parts) == 8 * a._MAX_APPROVAL_PROMPTS
    assert 999 in a._prompt_parts and 0 not in a._prompt_parts
