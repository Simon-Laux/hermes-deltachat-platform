"""Messages a previous run stored but never handed to Hermes are replayed,
and the read receipt only goes out once Hermes has the message."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from adapter import DeltaChatAdapter

FRESH, SEEN, OUT_DELIVERED = 10, 16, 26


def _adapter(platform_config):
    a = DeltaChatAdapter(platform_config)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.rpc.get_contact.return_value = {"name": "Eve", "is_key_contact": True}
    a.rpc.get_basic_chat_info.return_value = {"chat_type": "Single", "name": "c"}
    a.rpc.get_config.return_value = None
    a._is_sender_authorized = MagicMock(return_value=True)
    a.handle_message = AsyncMock()
    return a


def _msgs(a, msgs):
    a.rpc.get_next_msgs.return_value = list(msgs)
    a.rpc.get_message.side_effect = lambda _acc, i: msgs[i]


@pytest.mark.asyncio
async def test_only_fresh_downloaded_messages_are_replayed(platform_config):
    a = _adapter(platform_config)
    _msgs(a, {
        11: {"chat_id": 5, "state": FRESH, "download_state": "Done"},
        12: {"chat_id": 5, "state": OUT_DELIVERED, "download_state": "Done"},  # our reply
        13: {"chat_id": 5, "state": SEEN, "download_state": "Done"},
        14: {"chat_id": 6, "state": FRESH, "download_state": "Available"},  # event comes later
        15: {"chat_id": 6, "state": FRESH, "download_state": "Done"},
    })
    assert await a._unhandled_messages() == [
        {"chat_id": 5, "msg_id": 11}, {"chat_id": 6, "msg_id": 15}]


@pytest.mark.asyncio
async def test_lookup_failure_replays_nothing(platform_config):
    a = _adapter(platform_config)
    a.rpc.get_next_msgs.side_effect = RuntimeError("no such method")
    assert await a._unhandled_messages() == []


@pytest.mark.asyncio
async def test_replay_runs_before_live_events(platform_config):
    a = _adapter(platform_config)
    a._replay_events = [{"chat_id": 5, "msg_id": 11}]
    a._running = True
    seen = []

    async def handle(event):
        seen.append(event["msg_id"])

    async def next_event():
        a._running = False
        return {"context_id": 1, "event": {"kind": "IncomingMsg", "chat_id": 5, "msg_id": 12}}

    a._handle_incoming_message = handle
    a.rpc.get_next_event = next_event
    await a._event_listener()
    assert seen == [11, 12]
    assert a._replay_events == []


@pytest.mark.asyncio
async def test_read_receipt_follows_hand_off(platform_config):
    a = _adapter(platform_config)
    a.rpc.get_message.return_value = {"id": 7, "from_id": 10, "text": "hi", "view_type": "Text"}
    order = []
    a.handle_message.side_effect = lambda _e: order.append("hermes")
    a.rpc.markseen_msgs.side_effect = lambda *_: order.append("seen")
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    assert order == ["hermes", "seen"]


@pytest.mark.asyncio
async def test_failed_hand_off_stays_unseen(platform_config):
    """Unseen keeps it above last_msg_id, so the next start replays it."""
    a = _adapter(platform_config)
    a.rpc.get_message.return_value = {"id": 7, "from_id": 10, "text": "hi", "view_type": "Text"}
    a.handle_message.side_effect = RuntimeError("boom")
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    a.rpc.markseen_msgs.assert_not_awaited()
