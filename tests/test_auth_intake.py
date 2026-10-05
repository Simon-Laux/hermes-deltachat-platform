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


def _group(a, history, verdict):
    """Make chat 5 a group whose messages, oldest first, have these from_ids.

    Shapes taken from a live run: a new group starts with the local
    "end-to-end encrypted" note (from 2), then the adder's first message.
    """
    a.rpc.get_basic_chat_info.return_value = {"chat_type": "Group", "name": "g"}
    a.rpc.get_message_ids.return_value = list(range(len(history)))
    a.rpc.get_message.side_effect = lambda acc, mid: {"id": mid, "from_id": history[mid]}
    a._is_sender_authorized.return_value = verdict


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
async def test_group_started_by_unauthorized_contact_is_left(platform_config):
    a = _adapter(platform_config)
    _group(a, [2, 10, 11], verdict=False)
    assert not await a._intake_allows({"id": 2, "from_id": 11}, 5)
    a._is_sender_authorized.assert_called_once_with("10", "dm")
    a.rpc.leave_group.assert_awaited_once_with(1, 5)
    a.rpc.delete_chat.assert_awaited_once_with(1, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [True, None])
async def test_group_started_by_authorized_or_unchecked_contact_stays(platform_config, verdict):
    a = _adapter(platform_config)
    _group(a, [2, 10], verdict=verdict)
    assert await a._intake_allows({"id": 1, "from_id": 10}, 5)
    a.rpc.leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_we_created_stays(platform_config):
    a = _adapter(platform_config)
    _group(a, [2, DC_CONTACT_ID_SELF, 10], verdict=False)
    assert await a._intake_allows({"id": 2, "from_id": 10}, 5)
    a._is_sender_authorized.assert_not_called()
    a.rpc.leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_is_judged_once(platform_config):
    a = _adapter(platform_config)
    _group(a, [2, 10], verdict=True)
    await a._intake_allows({"id": 1, "from_id": 10}, 5)
    await a._intake_allows({"id": 1, "from_id": 10}, 5)
    a.rpc.get_message_ids.assert_awaited_once()


@pytest.mark.asyncio
async def test_dm_is_never_left(platform_config):
    a = _adapter(platform_config, verdict=False)
    assert await a._intake_allows({"id": 7, "from_id": 10}, 5)
    a.rpc.leave_group.assert_not_awaited()
