"""Messages a previous run stored but never handed to Hermes are replayed,
and the read receipt only goes out once Hermes has the message."""

import logging
import sys
import types

import pytest
from unittest.mock import AsyncMock, MagicMock

import adapter as adapter_mod
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
        # unanswered call: never gets IncomingMsg
        16: {"chat_id": 6, "state": FRESH, "download_state": "Done", "view_type": "Call"},
    })
    ids = await a._unhandled_message_ids()
    assert ids == [11, 12, 13, 14, 15, 16]
    assert await a._unhandled_messages(ids) == [
        {"chat_id": 5, "msg_id": 11}, {"chat_id": 6, "msg_id": 15}]


@pytest.mark.asyncio
async def test_lookup_failure_replays_nothing(platform_config):
    a = _adapter(platform_config)
    a.rpc.get_next_msgs.side_effect = RuntimeError("no such method")
    assert await a._unhandled_message_ids() == []


@pytest.mark.asyncio
async def test_replay_runs_before_live_events(platform_config):
    a = _adapter(platform_config)
    _msgs(a, {11: {"chat_id": 5, "state": FRESH, "download_state": "Done"}})
    a._replay_ids = [11]
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
    assert a._replay_ids == []


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


def _text(msg_id=7):
    return {"id": msg_id, "from_id": 10, "text": "hi", "view_type": "Text"}


@pytest.mark.asyncio
async def test_mention_gate_drop_is_marked_seen_once(platform_config):
    a = _adapter(platform_config)
    a.rpc.get_message.return_value = _text()
    a._mention_gate_allows = AsyncMock(return_value=False)
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    a.handle_message.assert_not_awaited()
    a.rpc.markseen_msgs.assert_awaited_once_with(1, [7])


@pytest.mark.asyncio
async def test_voice_message_is_marked_seen_once_after_hand_off(
        platform_config, tmp_path, monkeypatch):
    # conftest's MockMessageEvent has no media_urls; any kwargs will do here.
    monkeypatch.setattr(adapter_mod, "MessageEvent", lambda **kw: types.SimpleNamespace(**kw))
    a = _adapter(platform_config)
    a._dc_config_dir = str(tmp_path)
    a.rpc.get_message.return_value = {
        "id": 7, "from_id": 10, "text": "", "view_type": "Voice",
        "file": "/nonexistent/voice.ogg", "file_mime": "audio/ogg"}
    order = []
    a.handle_message.side_effect = lambda _e: order.append("hermes")
    a.rpc.markseen_msgs.side_effect = lambda *_: order.append("seen")
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    assert order == ["hermes", "seen"]


@pytest.mark.asyncio
async def test_intake_drop_is_never_marked_seen(platform_config):
    a = _adapter(platform_config)
    a.rpc.get_message.return_value = _text()
    a.rpc.get_contact.return_value = {"name": "Mallory", "is_key_contact": False}
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    a.handle_message.assert_not_awaited()
    a.rpc.markseen_msgs.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_receipt_warns_and_replay_makes_no_second_turn(platform_config, caplog):
    """The message stays fresh, so the next connect lists it again."""
    a = _adapter(platform_config)
    a.rpc.get_message.return_value = _text()
    a.rpc.markseen_msgs.side_effect = RuntimeError("io")
    with caplog.at_level(logging.WARNING, logger="hermes_plugins.deltachat"):
        await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    assert "Could not mark message 7 as seen" in caplog.text

    a.rpc.markseen_msgs.side_effect = None
    await a._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    a.handle_message.assert_awaited_once()
    assert a.rpc.markseen_msgs.await_count == 2  # the replay retries the receipt


@pytest.mark.asyncio
async def test_dedup_is_carried_into_a_rebuilt_adapter(platform_config):
    """The gateway hands MessageDeduplicator attributes to the adapter it
    rebuilds (helpers.inbound_dedup_caches / carry_inbound_dedup)."""
    from gateway.platforms.helpers import MessageDeduplicator

    old = _adapter(platform_config)
    old.rpc.get_message.return_value = _text()
    old.rpc.markseen_msgs.side_effect = RuntimeError("rpc server died")
    await old._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    caches = {k: v for k, v in vars(old).items() if isinstance(v, MessageDeduplicator)}
    assert caches

    new = _adapter(platform_config)
    new.__dict__.update(caches)
    new.rpc.get_message.return_value = _text()
    await new._handle_incoming_message({"chat_id": 5, "msg_id": 7})
    new.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_logs_one_info_line(platform_config, caplog):
    a = _adapter(platform_config)
    _msgs(a, {11: {"chat_id": 5, "state": FRESH, "download_state": "Done"},
              12: {"chat_id": 5, "state": FRESH, "download_state": "Done"}})
    a._replay_ids = [11, 12]
    a._handle_incoming_message = AsyncMock()
    with caplog.at_level(logging.INFO, logger="hermes_plugins.deltachat"):
        await a._replay_unhandled()
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert infos == ["Handling 2 message(s) the last run never got to"]
    assert a._handle_incoming_message.await_count == 2


@pytest.mark.asyncio
async def test_connect_lists_unhandled_ids_before_start_io(platform_config, monkeypatch):
    """Before start_io, so the list can't overlap with live events; and only
    the ids, so connect() doesn't load every message first."""
    monkeypatch.delenv("DC_ACCOUNTS_PATH", raising=False)  # connect() sets it
    a = DeltaChatAdapter(platform_config)
    rpc = AsyncMock()
    rpc.get_all_accounts.return_value = [{"id": 1}]
    rpc.is_configured.return_value = True
    rpc.get_next_msgs.return_value = [11]
    transport = types.ModuleType("deltachat2.transport")
    transport.IOTransport = MagicMock()
    monkeypatch.setitem(sys.modules, "deltachat2.transport", transport)
    monkeypatch.setitem(sys.modules, "deltachat2", types.SimpleNamespace(Rpc=MagicMock()))
    monkeypatch.setitem(sys.modules, "call_handler", types.SimpleNamespace(CallManager=MagicMock()))
    monkeypatch.setattr(adapter_mod, "_check_dc2_available", lambda: True)
    monkeypatch.setattr(adapter_mod, "_check_dc_version", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter_mod, "_AsyncRpc", lambda _: rpc)
    monkeypatch.setattr(adapter_mod.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(adapter_mod, "_active_adapter", None)
    monkeypatch.setattr(a, "_get_dc_config_dir", lambda: "/nonexistent")
    monkeypatch.setattr(a, "_check_db_id", AsyncMock(return_value=True))
    monkeypatch.setattr(a, "_update_commands_bio", AsyncMock())
    monkeypatch.setattr(a, "_publish_invite_link", AsyncMock())
    monkeypatch.setattr(a, "_event_listener", AsyncMock())
    monkeypatch.setattr(a, "get_my_address", AsyncMock(return_value=None))

    assert await a.connect()
    calls = [c[0] for c in rpc.mock_calls]
    assert calls.index("get_next_msgs") < calls.index("start_io")
    assert "get_message" not in calls
    assert a._replay_ids == [11]
    await a._event_loop_task
