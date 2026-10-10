"""Tests for answering exec-approval prompts with 👍/👎 reactions."""

import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapter import DeltaChatAdapter

SESSION = "agent:main:deltachat-platform:dm:5"


@pytest.fixture
def approval(monkeypatch):
    """Stand-in for Hermes' tools.approval, with its pending-approval queue."""
    approval = ModuleType("tools.approval")
    approval.resolve_gateway_approval = MagicMock(return_value=1)
    approval._lock = threading.Lock()
    approval._gateway_queues = {}
    tools = ModuleType("tools")
    tools.approval = approval
    run = ModuleType("gateway.run")
    run._redact_approval_command = lambda cmd: cmd.replace("hunter2", "***")
    access = ModuleType("gateway.slash_access")
    # stand-in for Hermes' policy: with admins set, only they may /approve or /deny
    access.policy_from_extra = lambda extra, scope: SimpleNamespace(
        can_run=lambda user, cmd: user in extra.get(
            {"dm": "allow_admin_from", "group": "group_allow_admin_from"}[scope], [user]))
    monkeypatch.setitem(sys.modules, "gateway.slash_access", access)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.approval", approval)
    monkeypatch.setitem(sys.modules, "gateway.run", run)
    return approval


@pytest.fixture
def resolver(approval):
    return approval.resolve_gateway_approval


def _pending(approval, request_id, command="rm -rf /tmp/x", description="dangerous command",
             session_key=SESSION):
    """Queue a pending approval the way Hermes does before it notifies us."""
    approval._gateway_queues.setdefault(session_key, []).append(SimpleNamespace(
        data={"request_id": request_id, "command": command, "description": description}))


def _adapter(platform_config, verdict=True, chat_type="Single"):
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.rpc.send_msg.return_value = 42
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": True}
    a.rpc.get_basic_chat_info.return_value = {"chat_type": chat_type, "name": "c"}
    a._is_sender_authorized = MagicMock(return_value=verdict)
    return a


def _prompt(session_key=SESSION, choices=("once", "session", "always", "deny"),
            command="rm -rf /tmp/x", description="dangerous command"):
    return SimpleNamespace(chat_id="5", session_key=session_key, metadata=None,
                           command=command, description=description,
                           text="⚠️ Dangerous command requires approval", choices=list(choices))


def _reaction(reaction="👍", msg_id=42, chat_id=5, contact_id=10):
    return {"kind": "IncomingReaction", "msg_id": msg_id, "chat_id": chat_id,
            "contact_id": contact_id, "reaction": reaction}


def _sent_texts(a):
    return [c.args[2].text for c in a.rpc.send_msg.await_args_list]


@pytest.fixture
def queued(approval):
    """One approval pending in SESSION, request_id "r1"."""
    _pending(approval, "r1")


@pytest.mark.asyncio
async def test_prompt_explains_reactions_and_is_remembered(platform_config, queued):
    a = _adapter(platform_config)
    result = await a._send_exec_approval_prompt(_prompt(choices=("once", "deny")))
    assert result.message_id == "42"
    text = _sent_texts(a)[0]
    assert "👍 = approve once" in text and "👎 = deny" in text
    assert "`/approve session`" not in text
    assert a._approval_prompts == {42: (SESSION, "r1")}


@pytest.mark.asyncio
@pytest.mark.parametrize("reaction,choice,reply", [
    ("👍", "once", "✅ Approved."), ("👍🏽", "once", "✅ Approved."), ("👎", "deny", "❌ Denied.")])
async def test_reaction_resolves_its_prompt(platform_config, resolver, queued, reaction, choice, reply):
    a = _adapter(platform_config)
    await a._send_exec_approval_prompt(_prompt())
    await a._handle_dc_event(_reaction(reaction))
    resolver.assert_called_once_with(SESSION, choice, request_id="r1")
    assert _sent_texts(a)[-1] == reply
    assert a._approval_prompts == {}


@pytest.mark.asyncio
async def test_reaction_resolves_the_prompt_reacted_to_not_the_oldest(platform_config, approval):
    """Parallel tool calls: both entries are queued before either prompt goes out."""
    _pending(approval, "r1", command="rm -rf /tmp/a")
    _pending(approval, "r2", command="rm -rf /tmp/b")
    a = _adapter(platform_config)
    a.rpc.send_msg.side_effect = [42, 43, 44]
    await a._send_exec_approval_prompt(_prompt(command="rm -rf /tmp/b"))
    await a._send_exec_approval_prompt(_prompt(command="rm -rf /tmp/a"))
    await a._handle_reaction(_reaction(msg_id=42))
    approval.resolve_gateway_approval.assert_called_once_with(SESSION, "once", request_id="r2")


@pytest.mark.asyncio
async def test_identical_prompts_each_claim_their_own_entry(platform_config, approval):
    _pending(approval, "r1")
    _pending(approval, "r2")
    a = _adapter(platform_config)
    a.rpc.send_msg.side_effect = [42, 43]
    await a._send_exec_approval_prompt(_prompt())
    await a._send_exec_approval_prompt(_prompt())
    assert a._approval_prompts == {42: (SESSION, "r1"), 43: (SESSION, "r2")}


@pytest.mark.asyncio
async def test_prompt_matches_on_the_redacted_command_and_description(platform_config, approval):
    _pending(approval, "r1", command="curl -u me:hunter2 x", description="network")
    _pending(approval, "r2", command="curl -u me:hunter2 x", description="exfiltration")
    a = _adapter(platform_config)
    await a._send_exec_approval_prompt(_prompt(command="curl -u me:*** x", description="exfiltration"))
    assert a._approval_prompts == {42: (SESSION, "r2")}


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", ["answered", "internals_moved"])
async def test_prompt_without_known_request_id_offers_no_reactions(platform_config, approval, unknown):
    """A reaction may only answer the approval its prompt shows: if that one can't be
    identified (already answered, or Hermes' queue moved), reactions do nothing."""
    if unknown == "answered":
        _pending(approval, "r1", command="something else")
    else:
        del approval._gateway_queues
    a = _adapter(platform_config)
    assert (await a._send_exec_approval_prompt(_prompt())).success
    text = _sent_texts(a)[0]
    assert "👍" not in text and "`/approve`" in text and "`/deny`" in text
    assert a._approval_prompts == {}
    await a._handle_reaction(_reaction())
    approval.resolve_gateway_approval.assert_not_called()


@pytest.mark.asyncio
async def test_second_reaction_does_not_resolve_another_approval(platform_config, resolver, queued):
    a = _adapter(platform_config)
    await a._send_exec_approval_prompt(_prompt())
    await a._handle_reaction(_reaction())
    await a._handle_reaction(_reaction("👎"))
    resolver.assert_called_once()


@pytest.mark.asyncio
async def test_expired_approval_is_reported(platform_config, resolver, queued):
    resolver.return_value = 0
    a = _adapter(platform_config)
    await a._send_exec_approval_prompt(_prompt())
    await a._handle_reaction(_reaction())
    assert _sent_texts(a)[-1].startswith("⌛")


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    _reaction("❤️"), _reaction(""), _reaction("👍 👎"), _reaction(msg_id=43), _reaction(chat_id=6)])
async def test_unrelated_reactions_are_ignored(platform_config, resolver, queued, event):
    a = _adapter(platform_config)
    await a._send_exec_approval_prompt(_prompt())
    await a._handle_reaction(event)
    resolver.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,key_contact", [(False, True), (None, True), (True, False)])
async def test_unauthorized_reactor_is_ignored(platform_config, resolver, queued, verdict, key_contact):
    a = _adapter(platform_config, verdict=verdict)
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": key_contact}
    await a._send_exec_approval_prompt(_prompt())
    await a._handle_reaction(_reaction())
    resolver.assert_not_called()
    assert a._approval_prompts == {42: (SESSION, "r1")}


@pytest.mark.asyncio
async def test_per_user_group_session_only_answers_to_its_user(platform_config, approval):
    group_session = "agent:main:deltachat-platform:group:5:10"
    _pending(approval, "r1", session_key=group_session)
    a = _adapter(platform_config, chat_type="Group")
    await a._send_exec_approval_prompt(_prompt(group_session))
    await a._handle_reaction(_reaction(contact_id=11))
    approval.resolve_gateway_approval.assert_not_called()
    await a._handle_reaction(_reaction(contact_id=10))
    approval.resolve_gateway_approval.assert_called_once()
    a._is_sender_authorized.assert_called_with("10", "group", "5")


@pytest.mark.asyncio
async def test_per_user_session_whose_user_id_equals_the_chat_id_is_not_the_chats(platform_config, approval):
    # group 12, contact 12's own session: its key ends in ":12" like a shared session's would
    group_session = "agent:main:deltachat-platform:group:12:12"
    _pending(approval, "r1", session_key=group_session)
    a = _adapter(platform_config, chat_type="Group")
    await a._send_exec_approval_prompt(_prompt(group_session))
    await a._handle_reaction(_reaction(chat_id=12, contact_id=10))
    approval.resolve_gateway_approval.assert_not_called()
    await a._handle_reaction(_reaction(chat_id=12, contact_id=12))
    approval.resolve_gateway_approval.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reaction", ["👍", "👎"])
async def test_non_admin_cannot_react_past_slash_gating(platform_config, resolver, queued, reaction):
    platform_config.extra = {"allow_admin_from": ["7"]}
    a = _adapter(platform_config)
    await a._send_exec_approval_prompt(_prompt())
    await a._handle_reaction(_reaction(reaction, contact_id=10))
    resolver.assert_not_called()
    await a._handle_reaction(_reaction(reaction, contact_id=7))
    resolver.assert_called_once()


@pytest.mark.asyncio
async def test_remembered_prompts_are_capped(platform_config, approval):
    n = DeltaChatAdapter._MAX_APPROVAL_PROMPTS + 1
    for i in range(n):
        _pending(approval, f"r{i}")
    a = _adapter(platform_config)
    a.rpc.send_msg.side_effect = range(1, n + 1)
    for _ in range(n):
        await a._send_exec_approval_prompt(_prompt())
    assert len(a._approval_prompts) == n - 1
    assert 1 not in a._approval_prompts


@pytest.mark.asyncio
async def test_split_prompt_answers_a_reaction_on_any_part(platform_config, resolver, queued):
    # send() splits a long prompt; "React to this exact message" may land in
    # the first part, so a reaction there must work too
    a = _adapter(platform_config)
    a.rpc.send_msg.side_effect = [42, 43]
    prompt = _prompt()
    prompt.text = "\n".join(["⚠️ Dangerous command requires approval"] + ["line"] * 35)
    result = await a._send_exec_approval_prompt(prompt)
    assert (result.message_id, result.continuation_message_ids) == ("43", ("42",))
    assert a._approval_prompts == {42: (SESSION, "r1"), 43: (SESSION, "r1")}
    await a._handle_dc_event(_reaction("👍", msg_id=42))
    resolver.assert_called_once_with(SESSION, "once", request_id="r1")


def test_approval_preview_counts_characters(platform_config):
    # a 40-line script must not be cut at 30 display lines before approval
    a = _adapter(platform_config)
    script = "\n".join(f"step {i}" for i in range(40))
    assert a._ea_fit(script, 3000) == script
    assert a._ea_fit("x" * 3001, 3000) == "x" * 3000 + "..."
