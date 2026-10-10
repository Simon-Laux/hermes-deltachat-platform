"""Tests for answering slash-command confirms (/reset, /reload-mcp, ...) with 👍/👎 reactions."""

import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapter import DeltaChatAdapter

SESSION = "agent:main:deltachat-platform:dm:5"
MESSAGE = ("⚠️ **Confirm /reset**\n\nThis clears the conversation.\n\n"
           "_Text fallback: reply `/approve`, `/always`, or `/cancel`._")


@pytest.fixture
def confirm(monkeypatch):
    """Stand-in for Hermes' tools.slash_confirm, with resolve()'s semantics."""
    mod = ModuleType("tools.slash_confirm")
    mod.DEFAULT_TIMEOUT_SECONDS = 300
    mod._pending = {}
    mod.handler = AsyncMock(return_value="Session reset.")

    def get_pending(session_key):
        entry = mod._pending.get(session_key)
        return dict(entry) if entry else None

    async def resolve(session_key, confirm_id, choice, timeout=mod.DEFAULT_TIMEOUT_SECONDS):
        entry = mod._pending.get(session_key)
        if not entry or entry["confirm_id"] != confirm_id:
            return None
        del mod._pending[session_key]
        if time.time() - entry["created_at"] > timeout:
            return None
        result = await mod.handler(choice)
        return result if isinstance(result, str) else None

    mod.get_pending = get_pending
    mod.resolve = AsyncMock(side_effect=resolve)
    tools = ModuleType("tools")
    tools.slash_confirm = mod
    access = ModuleType("gateway.slash_access")
    # stand-in for Hermes' policy: with admins set, only they may /approve or /deny
    access.policy_from_extra = lambda extra, scope: SimpleNamespace(
        can_run=lambda user, cmd: user in extra.get(
            {"dm": "allow_admin_from", "group": "group_allow_admin_from"}[scope], [user]))
    monkeypatch.setitem(sys.modules, "gateway.slash_access", access)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.slash_confirm", mod)
    return mod


def _register(confirm, confirm_id="1", session_key=SESSION, age=0):
    """Register a pending confirm the way Hermes does before calling send_slash_confirm."""
    confirm._pending[session_key] = {"confirm_id": confirm_id, "command": "reset",
                                     "created_at": time.time() - age}


@pytest.fixture
def registered(confirm):
    _register(confirm)


def _adapter(platform_config, verdict=True, chat_type="Single"):
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.rpc.send_msg.return_value = 42
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": True}
    a.rpc.get_basic_chat_info.return_value = {"chat_type": chat_type, "name": "c"}
    a._is_sender_authorized = MagicMock(return_value=verdict)
    return a


async def _prompt(a, session_key=SESSION, confirm_id="1", message=MESSAGE):
    return await a.send_slash_confirm(chat_id="5", title="/reset", message=message,
                                      session_key=session_key, confirm_id=confirm_id)


def _reaction(reaction="👍", msg_id=42, chat_id=5, contact_id=10):
    return {"kind": "IncomingReaction", "msg_id": msg_id, "chat_id": chat_id,
            "contact_id": contact_id, "reaction": reaction}


def _sent_texts(a):
    return [c.args[2].text for c in a.rpc.send_msg.await_args_list]


@pytest.mark.asyncio
async def test_prompt_explains_reactions_and_is_remembered(platform_config, registered):
    a = _adapter(platform_config)
    result = await _prompt(a)
    assert result.success and result.message_id == "42"
    (text,) = _sent_texts(a)
    assert text.startswith(MESSAGE)
    assert "👍 = approve once" in text and "👎 = cancel" in text
    assert text.count("/approve") == 1  # Hermes' own fallback line isn't repeated
    assert a._slash_confirm_prompts == {42: (SESSION, "1")}
    assert a._approval_prompts == {}


@pytest.mark.asyncio
async def test_prompt_without_fallback_line_gets_one(platform_config, registered):
    a = _adapter(platform_config)
    await _prompt(a, message="Reload MCP servers?")
    (text,) = _sent_texts(a)
    assert "`/approve`, `/always`, or `/cancel`" in text


@pytest.mark.asyncio
async def test_always_is_typed_only(platform_config, confirm, registered):
    """"Always" persists a global opt-out: no reaction may trigger it, the prompt
    still offers it as /always."""
    assert "always" not in DeltaChatAdapter._SLASH_CONFIRM_REACTIONS.values()
    a = _adapter(platform_config)
    await _prompt(a)
    assert "`/always`" in _sent_texts(a)[0]
    for emoji in ("❤️", "🔁", "♾️", "✅"):
        await a._handle_reaction(_reaction(emoji))
    confirm.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_hermes_slash_confirm_falls_back_to_text(platform_config, monkeypatch):
    monkeypatch.setitem(sys.modules, "tools.slash_confirm", None)
    a = _adapter(platform_config)
    result = await _prompt(a)
    assert not result.success
    a.rpc.send_msg.assert_not_awaited()
    assert a._slash_confirm_prompts == {}


@pytest.mark.asyncio
async def test_failed_send_is_not_remembered(platform_config, registered):
    a = _adapter(platform_config)
    a.rpc.send_msg.side_effect = RuntimeError("offline")
    assert not (await _prompt(a)).success
    assert a._slash_confirm_prompts == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("reaction,choice", [("👍", "once"), ("👍🏽", "once"), ("👎", "cancel")])
async def test_reaction_resolves_its_confirm(platform_config, confirm, registered, reaction, choice):
    a = _adapter(platform_config)
    await _prompt(a)
    await a._handle_dc_event(_reaction(reaction))
    confirm.resolve.assert_awaited_once_with(SESSION, "1", choice)
    confirm.handler.assert_awaited_once_with(choice)
    assert _sent_texts(a)[-1] == "Session reset."
    assert a.rpc.send_msg.await_args_list[-1].args[2].quoted_message_id == 42
    assert a._slash_confirm_prompts == {}


@pytest.mark.asyncio
async def test_str_subclass_reply_is_sent(platform_config, confirm, registered):
    """/reset answers with an EphemeralReply, which is a str subclass."""
    class EphemeralReply(str):
        pass
    confirm.handler.return_value = EphemeralReply("✨ Session reset!")
    a = _adapter(platform_config)
    await _prompt(a)
    await a._handle_reaction(_reaction())
    assert _sent_texts(a)[-1] == "✨ Session reset!"


@pytest.mark.asyncio
async def test_prompt_swallowed_by_a_call_is_a_failure(platform_config, registered):
    """send() drops a call thread's non-final sends with success and no message id;
    reporting success would make Hermes skip its text fallback."""
    a = _adapter(platform_config)
    a.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id=None))
    assert not (await _prompt(a)).success
    assert a._slash_confirm_prompts == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["timed_out", "superseded", "answered"])
async def test_stale_confirm_gets_nothing(platform_config, confirm, stale):
    if stale == "timed_out":
        _register(confirm, age=confirm.DEFAULT_TIMEOUT_SECONDS + 1)
    elif stale == "superseded":
        _register(confirm, confirm_id="2")
    a = _adapter(platform_config)
    await _prompt(a)
    await a._handle_reaction(_reaction())
    confirm.handler.assert_not_awaited()
    assert len(_sent_texts(a)) == 1
    assert a._slash_confirm_prompts == {}


@pytest.mark.asyncio
async def test_resolve_error_is_swallowed(platform_config, confirm, registered):
    confirm.resolve.side_effect = RuntimeError("boom")
    a = _adapter(platform_config)
    await _prompt(a)
    await a._handle_reaction(_reaction())
    assert len(_sent_texts(a)) == 1


@pytest.mark.asyncio
async def test_second_reaction_does_not_resolve_again(platform_config, confirm, registered):
    a = _adapter(platform_config)
    await _prompt(a)
    await a._handle_reaction(_reaction())
    _register(confirm, confirm_id="2")  # the next confirm of the session
    await a._handle_reaction(_reaction("👎"))
    confirm.resolve.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    _reaction("❤️"), _reaction(""), _reaction("👍 👎"), _reaction(msg_id=43), _reaction(chat_id=6)])
async def test_unrelated_reactions_are_ignored(platform_config, confirm, registered, event):
    a = _adapter(platform_config)
    await _prompt(a)
    await a._handle_reaction(event)
    confirm.resolve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,key_contact", [(False, True), (None, True), (True, False)])
async def test_unauthorized_reactor_is_ignored(platform_config, confirm, registered, verdict, key_contact):
    a = _adapter(platform_config, verdict=verdict)
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": key_contact}
    await _prompt(a)
    await a._handle_reaction(_reaction())
    confirm.resolve.assert_not_awaited()
    assert a._slash_confirm_prompts == {42: (SESSION, "1")}


@pytest.mark.asyncio
async def test_per_user_group_session_only_answers_to_its_user(platform_config, confirm):
    group_session = "agent:main:deltachat-platform:group:5:10"
    _register(confirm, session_key=group_session)
    a = _adapter(platform_config, chat_type="Group")
    await _prompt(a, session_key=group_session)
    await a._handle_reaction(_reaction(contact_id=11))
    confirm.resolve.assert_not_awaited()
    await a._handle_reaction(_reaction(contact_id=10))
    confirm.resolve.assert_awaited_once()
    a._is_sender_authorized.assert_called_with("10", "group", "5")


@pytest.mark.asyncio
@pytest.mark.parametrize("reaction", ["👍", "👎"])
async def test_non_admin_cannot_react_past_slash_gating(platform_config, confirm, registered, reaction):
    platform_config.extra = {"allow_admin_from": ["7"]}
    a = _adapter(platform_config)
    await _prompt(a)
    await a._handle_reaction(_reaction(reaction, contact_id=10))
    confirm.resolve.assert_not_awaited()
    await a._handle_reaction(_reaction(reaction, contact_id=7))
    confirm.resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_remembered_prompts_are_capped(platform_config, confirm):
    n = DeltaChatAdapter._MAX_APPROVAL_PROMPTS + 1
    a = _adapter(platform_config)
    a.rpc.send_msg.side_effect = range(1, n + 1)
    for i in range(n):
        await _prompt(a, confirm_id=str(i))
    assert len(a._slash_confirm_prompts) == n - 1
    assert 1 not in a._slash_confirm_prompts
