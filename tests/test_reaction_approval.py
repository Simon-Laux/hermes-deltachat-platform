"""Tests for answering exec-approval prompts with 👍/👎 reactions."""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapter import DeltaChatAdapter

SESSION = "agent:main:deltachat-platform:dm:5"


@pytest.fixture
def resolver(monkeypatch):
    approval = ModuleType("tools.approval")
    approval.resolve_gateway_approval = MagicMock(return_value=1)
    monkeypatch.setitem(sys.modules, "tools", ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.approval", approval)
    return approval.resolve_gateway_approval


def _adapter(platform_config, verdict=True, chat_type="Single"):
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.rpc.send_msg.return_value = 42
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": True}
    a.rpc.get_basic_chat_info.return_value = {"chat_type": chat_type, "name": "c"}
    a._is_sender_authorized = MagicMock(return_value=verdict)
    return a


def _prompt(session_key=SESSION, choices=("once", "session", "always", "deny")):
    return SimpleNamespace(chat_id="5", session_key=session_key, metadata=None,
                           text="⚠️ Dangerous command requires approval", choices=list(choices))


def _reaction(reaction="👍", msg_id=42, chat_id=5, contact_id=10):
    return {"kind": "IncomingReaction", "msg_id": msg_id, "chat_id": chat_id,
            "contact_id": contact_id, "reaction": reaction}


def _sent_texts(a):
    return [c.args[2].text for c in a.rpc.send_msg.await_args_list]


@pytest.mark.asyncio
async def test_prompt_explains_reactions_and_is_remembered(platform_config):
    a = _adapter(platform_config)
    result = await a._send_exec_approval_prompt(_prompt(choices=("once", "deny")))
    assert result.message_id == "42"
    text = _sent_texts(a)[0]
    assert "👍 = approve once" in text and "👎 = deny" in text
    assert "`/approve session`" not in text
    assert a._approval_prompts == {42: SESSION}


@pytest.mark.asyncio
@pytest.mark.parametrize("reaction,choice,reply", [
    ("👍", "once", "✅ Approved."), ("👍🏽", "once", "✅ Approved."), ("👎", "deny", "❌ Denied.")])
async def test_reaction_resolves_prompt(platform_config, resolver, reaction, choice, reply):
    a = _adapter(platform_config)
    await a._send_exec_approval_prompt(_prompt())
    await a._handle_dc_event(_reaction(reaction))
    resolver.assert_called_once_with(SESSION, choice)
    assert _sent_texts(a)[-1] == reply
    assert a._approval_prompts == {}


@pytest.mark.asyncio
async def test_second_reaction_does_not_resolve_another_approval(platform_config, resolver):
    a = _adapter(platform_config)
    await a._send_exec_approval_prompt(_prompt())
    await a._handle_reaction(_reaction())
    await a._handle_reaction(_reaction("👎"))
    resolver.assert_called_once()


@pytest.mark.asyncio
async def test_expired_approval_is_reported(platform_config, resolver):
    resolver.return_value = 0
    a = _adapter(platform_config)
    await a._send_exec_approval_prompt(_prompt())
    await a._handle_reaction(_reaction())
    assert _sent_texts(a)[-1].startswith("⌛")


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    _reaction("❤️"), _reaction(""), _reaction("👍 👎"), _reaction(msg_id=43), _reaction(chat_id=6)])
async def test_unrelated_reactions_are_ignored(platform_config, resolver, event):
    a = _adapter(platform_config)
    await a._send_exec_approval_prompt(_prompt())
    await a._handle_reaction(event)
    resolver.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,key_contact", [(False, True), (None, True), (True, False)])
async def test_unauthorized_reactor_is_ignored(platform_config, resolver, verdict, key_contact):
    a = _adapter(platform_config, verdict=verdict)
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": key_contact}
    await a._send_exec_approval_prompt(_prompt())
    await a._handle_reaction(_reaction())
    resolver.assert_not_called()
    assert a._approval_prompts == {42: SESSION}


@pytest.mark.asyncio
async def test_per_user_group_session_only_answers_to_its_user(platform_config, resolver):
    a = _adapter(platform_config, chat_type="Group")
    await a._send_exec_approval_prompt(_prompt("agent:main:deltachat-platform:group:5:10"))
    await a._handle_reaction(_reaction(contact_id=11))
    resolver.assert_not_called()
    await a._handle_reaction(_reaction(contact_id=10))
    resolver.assert_called_once()
    a._is_sender_authorized.assert_called_with("10", "group", "5")


@pytest.mark.asyncio
async def test_remembered_prompts_are_capped(platform_config):
    a = _adapter(platform_config)
    a.rpc.send_msg.side_effect = range(1, a._MAX_APPROVAL_PROMPTS + 2)
    for _ in range(a._MAX_APPROVAL_PROMPTS + 1):
        await a._send_exec_approval_prompt(_prompt())
    assert len(a._approval_prompts) == a._MAX_APPROVAL_PROMPTS
    assert 1 not in a._approval_prompts
