"""Tests for 👀/✅/❌ status reactions and the reaction:added hook."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapter import DeltaChatAdapter
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome


def _adapter(platform_config, *, reactions=True, verdict=True, view_type="Text"):
    if reactions:
        platform_config.extra["reactions"] = True
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.rpc.get_message.return_value = {"view_type": view_type}
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": True}
    a.rpc.get_basic_chat_info.return_value = {"chat_type": "Single", "name": "c"}
    a._is_sender_authorized = MagicMock(return_value=verdict)
    return a


def _event(msg_id="42", chat_type="dm"):
    source = SimpleNamespace(chat_id="5", chat_type=chat_type, user_id="10")
    return MessageEvent(text="hi", message_type=MessageType.TEXT, source=source, message_id=msg_id)


def _reactions(a):
    return [c.args[1:] for c in a.rpc.send_reaction.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,final", [
    (ProcessingOutcome.SUCCESS, ["✅"]),
    (ProcessingOutcome.FAILURE, ["❌"]),
    (ProcessingOutcome.CANCELLED, []),
])
async def test_eyes_then_outcome(platform_config, outcome, final):
    a = _adapter(platform_config)
    event = _event()
    await a.on_processing_start(event)
    await a.on_processing_complete(event, outcome)
    assert _reactions(a) == [(42, ["👀"]), (42, final)]
    a._is_sender_authorized.assert_called_once_with("10", "dm", "5")


@pytest.mark.asyncio
async def test_env_var_turns_it_on(platform_config, monkeypatch):
    monkeypatch.setenv("DELTACHAT_REACTIONS", "1")
    a = _adapter(platform_config, reactions=False)
    await a.on_processing_start(_event())
    assert _reactions(a) == [(42, ["👀"])]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "0", "false", "off"])
async def test_off_by_default_and_when_disabled(platform_config, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("DELTACHAT_REACTIONS", raising=False)
    else:
        monkeypatch.setenv("DELTACHAT_REACTIONS", value)
    a = _adapter(platform_config, reactions=False)
    event = _event()
    await a.on_processing_start(event)
    await a.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    a.rpc.send_reaction.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [False, None])
async def test_no_reaction_for_contacts_hermes_does_not_approve(platform_config, verdict):
    """Hermes runs the hook before its own auth check: a 👀 would tell a
    stranger, or anyone while no auth check is wired, that a bot reads along."""
    a = _adapter(platform_config, verdict=verdict)
    event = _event()
    await a.on_processing_start(event)
    await a.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    a.rpc.send_reaction.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_id", ["callend-123", "", None])
async def test_no_reaction_without_a_message_id(platform_config, msg_id):
    a = _adapter(platform_config)
    event = _event(msg_id=msg_id)
    await a.on_processing_start(event)
    await a.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    a.rpc.send_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_call_turns_are_not_reacted_to(platform_config):
    """Every utterance of a call is a turn on the call message."""
    a = _adapter(platform_config, view_type="Call")
    event = _event()
    await a.on_processing_start(event)
    await a.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    a.rpc.send_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_unloadable_message_is_not_reacted_to(platform_config):
    a = _adapter(platform_config)
    a.rpc.get_message.side_effect = RuntimeError("gone")
    event = _event()
    await a.on_processing_start(event)
    await a.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    a.rpc.send_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_eyes_is_not_swapped(platform_config):
    a = _adapter(platform_config)
    a.rpc.send_reaction.side_effect = RuntimeError("rpc down")
    event = _event()
    await a.on_processing_start(event)  # must not raise
    a.rpc.send_reaction.side_effect = None
    await a.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert _reactions(a) == [(42, ["👀"])]


@pytest.mark.asyncio
async def test_complete_without_start_does_nothing(platform_config):
    a = _adapter(platform_config)
    await a.on_processing_complete(_event(), ProcessingOutcome.SUCCESS)
    a.rpc.send_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_complete_does_not_react_again(platform_config):
    a = _adapter(platform_config)
    event = _event()
    await a.on_processing_start(event)
    await a.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    await a.on_processing_complete(event, ProcessingOutcome.FAILURE)
    assert _reactions(a) == [(42, ["👀"]), (42, ["✅"])]


# -- reaction:added hook ---------------------------------------------------

def _incoming(reaction="❤️", msg_id=77, chat_id=5, contact_id=10):
    return {"kind": "IncomingReaction", "msg_id": msg_id, "chat_id": chat_id,
            "contact_id": contact_id, "reaction": reaction}


@pytest.mark.asyncio
async def test_reaction_reaches_hermes_hooks(platform_config):
    a = _adapter(platform_config, reactions=False)
    a._reaction_handler = AsyncMock()
    event = _incoming()
    await a._handle_dc_event(event)
    a._reaction_handler.assert_awaited_once_with({
        "platform": "deltachat-platform", "event_name": "reaction:added", "reaction": "❤️",
        "user_id": "10", "channel_id": "5", "message_ts": "77", "raw_event": event})


@pytest.mark.asyncio
async def test_prompt_reaction_reaches_hooks_too(platform_config):
    a = _adapter(platform_config, reactions=False)
    a._reaction_handler = AsyncMock()
    a._clarify_prompts[77] = ("agent:main:deltachat-platform:dm:5", "c1", ["a"])
    a._handle_clarify_reaction = AsyncMock()
    await a._handle_reaction(_incoming("1️⃣"))
    a._handle_clarify_reaction.assert_awaited_once()
    a._reaction_handler.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,key_contact", [(False, True), (None, True), (True, False)])
async def test_unapproved_reactions_stay_out_of_hooks(platform_config, verdict, key_contact):
    a = _adapter(platform_config, verdict=verdict)
    a.rpc.get_contact.return_value = {"name": "Mallory", "is_key_contact": key_contact}
    a._reaction_handler = AsyncMock()
    await a._handle_reaction(_incoming())
    a._reaction_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_reaction_or_no_handler_is_ignored(platform_config):
    a = _adapter(platform_config)
    await a._handle_reaction(_incoming())  # no handler wired: no error, no lookups
    a.rpc.get_contact.assert_not_awaited()
    a._reaction_handler = AsyncMock()
    await a._handle_reaction(_incoming(""))
    a._reaction_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_failing_hook_or_rpc_does_not_raise(platform_config):
    a = _adapter(platform_config)
    a._reaction_handler = AsyncMock(side_effect=RuntimeError("hook broke"))
    await a._handle_reaction(_incoming())
    a._reaction_handler.reset_mock(side_effect=True)
    a.rpc.get_contact.side_effect = RuntimeError("rpc down")
    await a._handle_reaction(_incoming())
    a._reaction_handler.assert_not_awaited()
