"""Tests for answering multiple-choice clarify prompts with keycap (1️⃣-9️⃣) reactions."""

import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapter import DeltaChatAdapter

SESSION = "agent:main:deltachat-platform:dm:5"
CHOICES = ["staging (Recommended)", "production", "both"]


@pytest.fixture
def clarify(monkeypatch):
    """Stand-in for Hermes' tools.clarify_gateway, with its pending entries."""
    cg = ModuleType("tools.clarify_gateway")
    cg._lock = threading.RLock()
    cg._entries = {}

    def resolve(clarify_id, response):
        entry = cg._entries.get(clarify_id)
        if entry is None or entry.response is not None:
            return False
        entry.response = response
        return True

    def mark_awaiting_text(clarify_id):
        entry = cg._entries.get(clarify_id)
        if entry is not None:
            entry.awaiting_text = True
        return entry is not None

    cg.resolve_gateway_clarify = MagicMock(side_effect=resolve)
    cg.mark_awaiting_text = MagicMock(side_effect=mark_awaiting_text)
    tools = ModuleType("tools")
    tools.clarify_gateway = cg
    access = ModuleType("gateway.slash_access")
    # with admins set, only they may run slash commands; clarify answers aren't one
    access.policy_from_extra = lambda extra, scope: SimpleNamespace(
        can_run=lambda user, cmd: user in extra.get("allow_admin_from", [user]))
    monkeypatch.setitem(sys.modules, "gateway.slash_access", access)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.clarify_gateway", cg)
    return cg


def _register(cg, clarify_id="c1", multi_select=False):
    """Register a pending clarify the way Hermes does before calling send_clarify."""
    cg._entries[clarify_id] = SimpleNamespace(
        multi_select=multi_select, awaiting_text=False, response=None)


@pytest.fixture
def registered(clarify):
    _register(clarify)


def _adapter(platform_config, verdict=True, chat_type="Single"):
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.rpc.send_msg.return_value = 42
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": True}
    a.rpc.get_basic_chat_info.return_value = {"chat_type": chat_type, "name": "c"}
    a._is_sender_authorized = MagicMock(return_value=verdict)
    return a


async def _prompt(a, choices=CHOICES, clarify_id="c1", session_key=SESSION):
    return await a.send_clarify(chat_id="5", question="Deploy where?", choices=choices,
                                clarify_id=clarify_id, session_key=session_key)


def _reaction(reaction="2️⃣", msg_id=42, chat_id=5, contact_id=10):
    return {"kind": "IncomingReaction", "msg_id": msg_id, "chat_id": chat_id,
            "contact_id": contact_id, "reaction": reaction}


def _sent_texts(a):
    return [c.args[2].text for c in a.rpc.send_msg.await_args_list]


@pytest.mark.asyncio
async def test_prompt_numbers_choices_with_keycaps(platform_config, clarify, registered):
    a = _adapter(platform_config)
    result = await _prompt(a)
    assert result.success and result.message_id == "42"
    (text,) = _sent_texts(a)
    assert text.startswith("❓ Deploy where?")
    assert "1️⃣ staging (Recommended)\n2️⃣ production\n3️⃣ both" in text
    assert "React to this exact message" in text
    # typed numbers, labels and free answers still reach Hermes' text intercept
    assert clarify._entries["c1"].awaiting_text
    assert a._clarify_prompts == {42: (SESSION, "c1", CHOICES)}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["open", "ten_choices", "multi_select", "unknown_entry", "no_resolver"])
async def test_falls_back_to_hermes_text_prompt(platform_config, clarify, monkeypatch, case):
    choices = CHOICES
    if case == "open":
        choices = None
    elif case == "ten_choices":
        choices = [f"option {i}" for i in range(10)]
    if case != "unknown_entry":
        _register(clarify, multi_select=case == "multi_select")
    if case == "no_resolver":
        del clarify.resolve_gateway_clarify
    a = _adapter(platform_config)
    assert (await _prompt(a, choices=choices)).success
    (text,) = _sent_texts(a)
    assert "️⃣" not in text and "React" not in text
    if choices:
        assert "  1. " in text
    assert a._clarify_prompts == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("reaction,label", [
    ("1️⃣", CHOICES[0]), ("2️⃣", CHOICES[1]), ("3⃣", CHOICES[2])])
async def test_reaction_answers_with_the_choice_label(platform_config, clarify, registered,
                                                      reaction, label):
    a = _adapter(platform_config)
    await _prompt(a)
    await a._handle_dc_event(_reaction(reaction))
    clarify.resolve_gateway_clarify.assert_called_once_with("c1", label)
    assert len(_sent_texts(a)) == 1  # the agent carrying on is the feedback
    a.rpc.send_reaction.assert_not_awaited()
    assert a._clarify_prompts == {}


@pytest.mark.asyncio
async def test_stale_clarify_gets_nothing(platform_config, clarify, registered):
    a = _adapter(platform_config)
    await _prompt(a)
    clarify._entries.clear()  # answered by typing, or timed out
    await a._handle_reaction(_reaction())
    clarify.resolve_gateway_clarify.assert_called_once()
    assert len(_sent_texts(a)) == 1
    assert a._clarify_prompts == {}


@pytest.mark.asyncio
async def test_resolve_error_is_swallowed(platform_config, clarify, registered):
    clarify.resolve_gateway_clarify.side_effect = RuntimeError("boom")
    a = _adapter(platform_config)
    await _prompt(a)
    await a._handle_reaction(_reaction())
    assert len(_sent_texts(a)) == 1


@pytest.mark.asyncio
async def test_second_reaction_does_not_answer_again(platform_config, clarify, registered):
    a = _adapter(platform_config)
    await _prompt(a)
    await a._handle_reaction(_reaction("1️⃣"))
    await a._handle_reaction(_reaction("2️⃣"))
    clarify.resolve_gateway_clarify.assert_called_once_with("c1", CHOICES[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    _reaction("4️⃣"), _reaction("0️⃣"), _reaction("🔟"), _reaction("👍"), _reaction(""),
    _reaction("1️⃣ 2️⃣"), _reaction(msg_id=43), _reaction(chat_id=6)])
async def test_unrelated_reactions_are_ignored(platform_config, clarify, registered, event):
    a = _adapter(platform_config)
    await _prompt(a)
    await a._handle_reaction(event)
    clarify.resolve_gateway_clarify.assert_not_called()
    assert a._clarify_prompts == {42: (SESSION, "c1", CHOICES)}


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,key_contact", [(False, True), (None, True), (True, False)])
async def test_unauthorized_reactor_is_ignored(platform_config, clarify, registered, verdict, key_contact):
    a = _adapter(platform_config, verdict=verdict)
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": key_contact}
    await _prompt(a)
    await a._handle_reaction(_reaction())
    clarify.resolve_gateway_clarify.assert_not_called()
    assert a._clarify_prompts == {42: (SESSION, "c1", CHOICES)}


@pytest.mark.asyncio
async def test_per_user_group_session_only_answers_to_its_user(platform_config, clarify, registered):
    group_session = "agent:main:deltachat-platform:group:5:10"
    a = _adapter(platform_config, chat_type="Group")
    await _prompt(a, session_key=group_session)
    await a._handle_reaction(_reaction(contact_id=11))
    clarify.resolve_gateway_clarify.assert_not_called()
    await a._handle_reaction(_reaction(contact_id=10))
    clarify.resolve_gateway_clarify.assert_called_once()


@pytest.mark.asyncio
async def test_clarify_answers_are_not_slash_gated(platform_config, clarify, registered):
    platform_config.extra = {"allow_admin_from": ["7"]}
    a = _adapter(platform_config)
    await _prompt(a)
    await a._handle_reaction(_reaction(contact_id=10))
    clarify.resolve_gateway_clarify.assert_called_once()
