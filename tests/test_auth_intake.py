"""Tests for the intake gate in front of Hermes' authorization (_intake_allows)."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from adapter import DC_CONTACT_ID_SELF, DeltaChatAdapter


def _adapter(platform_config, verdict=None, key_contact=True):
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": key_contact}
    a.rpc.get_basic_chat_info.return_value = {"chat_type": "Single", "name": "c"}
    a.rpc.get_config.return_value = None
    a._is_sender_authorized = MagicMock(return_value=verdict)
    a.handle_message = AsyncMock()
    return a


def _group(a, members, approved):
    """Make chat 5 a group with these member ids; Hermes approves *approved*
    (a verdict for everyone else is False, or None to model "unknown")."""
    a.rpc.get_basic_chat_info.return_value = {"chat_type": "Group", "name": "g"}
    a.rpc.get_chat_contacts.return_value = [DC_CONTACT_ID_SELF, *members]
    a._is_sender_authorized.side_effect = (
        lambda uid, chat_type, chat_id: True if int(uid) in approved else a.verdict)
    a.verdict = False


@pytest.mark.asyncio
async def test_non_key_contact_is_dropped_without_read_receipt(platform_config):
    a = _adapter(platform_config, key_contact=False)
    a.rpc.get_message.return_value = {"id": 7, "from_id": 10, "text": "hi", "view_type": "Text"}
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    a.handle_message.assert_not_awaited()
    a.rpc.markseen_msgs.assert_not_awaited()


@pytest.mark.asyncio
async def test_key_contact_reaches_hermes(platform_config):
    a = _adapter(platform_config)
    a.rpc.get_message.return_value = {"id": 7, "from_id": 10, "text": "hi", "view_type": "Text"}
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    a.handle_message.assert_awaited_once()
    assert a.handle_message.await_args.args[0].source.user_id == "10"


@pytest.mark.asyncio
async def test_unreadable_sender_is_dropped(platform_config):
    a = _adapter(platform_config)
    a.rpc.get_contact.side_effect = RuntimeError("rpc down")
    assert not await a._intake_allows({"id": 7, "from_id": 10}, 5)


@pytest.mark.asyncio
async def test_group_without_approved_member_is_left(platform_config):
    a = _adapter(platform_config)
    _group(a, [10, 11], approved=())
    assert not await a._intake_allows({"id": 2, "from_id": 11}, 5)
    a._is_sender_authorized.assert_any_call("10", "group", "5")
    a.rpc.leave_group.assert_awaited_once_with(1, 5)
    a.rpc.delete_chat.assert_awaited_once_with(1, 5)


@pytest.mark.asyncio
async def test_group_with_an_approved_member_stays(platform_config):
    """Even when the sender is a stranger: Hermes ignores them, we stay."""
    a = _adapter(platform_config)
    _group(a, [10, 11], approved=(10,))
    assert await a._intake_allows({"id": 2, "from_id": 11}, 5)
    a.rpc.leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_removed_still_deletes_the_chat(platform_config):
    a = _adapter(platform_config)
    _group(a, [10], approved=())
    a.rpc.leave_group.side_effect = RuntimeError("not a member")
    assert not await a._intake_allows({"id": 2, "from_id": 10}, 5)
    a.rpc.delete_chat.assert_awaited_once_with(1, 5)


@pytest.mark.asyncio
async def test_group_with_only_us_stays(platform_config):
    a = _adapter(platform_config)
    _group(a, [], approved=())
    assert await a._intake_allows({"id": 2, "from_id": 10}, 5)
    a.rpc.leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_unknown_verdict_among_rejections_stays(platform_config):
    a = _adapter(platform_config)
    _group(a, [10, 11], approved=())
    a._is_sender_authorized.side_effect = lambda uid, ct, cid: None if uid == "11" else False
    assert await a._intake_allows({"id": 2, "from_id": 10}, 5)
    a.rpc.leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_group_chat_types_are_never_left(platform_config):
    a = _adapter(platform_config, verdict=False)
    a.rpc.get_basic_chat_info.return_value = {"chat_type": "InBroadcast", "name": "b"}
    a.rpc.get_chat_contacts.return_value = [1, 10]
    assert await a._intake_allows({"id": 2, "from_id": 10}, 5)
    a.rpc.leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_verdict_stays(platform_config):
    a = _adapter(platform_config)
    _group(a, [10, 11], approved=())
    a.verdict = None
    assert await a._intake_allows({"id": 2, "from_id": 11}, 5)
    a.rpc.leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_lookup_failure_stays(platform_config):
    a = _adapter(platform_config)
    _group(a, [10], approved=())
    a.rpc.get_chat_contacts.side_effect = RuntimeError("rpc down")
    assert await a._intake_allows({"id": 2, "from_id": 10}, 5)
    a.rpc.leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_key_sender_never_makes_us_leave(platform_config):
    """Leaving is visible to the group; a sender without a key gets nothing."""
    a = _adapter(platform_config, key_contact=False)
    _group(a, [10], approved=())
    assert not await a._intake_allows({"id": 2, "from_id": 10}, 5)
    a.rpc.leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_is_never_left(platform_config):
    a = _adapter(platform_config, verdict=False)
    assert await a._intake_allows({"id": 7, "from_id": 10}, 5)
    a.rpc.leave_group.assert_not_awaited()
