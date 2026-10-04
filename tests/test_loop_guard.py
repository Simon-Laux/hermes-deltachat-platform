"""Tests for the bot-to-bot loop guard (DELTACHAT_MAX_CONSECUTIVE_REPLIES)."""

from unittest.mock import AsyncMock

import pytest

from adapter import DC_CONTACT_ID_SELF, DeltaChatAdapter

GROUP = [DC_CONTACT_ID_SELF, 10, 11]
SOLO_GROUP = [DC_CONTACT_ID_SELF, 10]


def make_adapter(platform_config, members, limit=3):
    adapter = DeltaChatAdapter(platform_config)
    adapter._max_consecutive_replies = limit
    adapter.account_id = 1
    adapter.rpc = AsyncMock()
    adapter.rpc.get_chat_contacts.return_value = members
    adapter.send = AsyncMock()
    return adapter


async def run(adapter, senders, chat_id=5):
    return [await adapter._loop_guard_allows(chat_id, s) for s in senders]


@pytest.mark.asyncio
async def test_trips_after_limit_and_notifies_once(platform_config):
    adapter = make_adapter(platform_config, GROUP)
    assert await run(adapter, [10] * 5) == [True, True, True, False, False]
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_sender_resets_streak(platform_config):
    adapter = make_adapter(platform_config, GROUP)
    assert await run(adapter, [10] * 4 + [11] + [10] * 3) == [
        True, True, True, False, True, True, True, True,
    ]
    # tripping again after a reset sends a fresh notice
    assert await run(adapter, [10]) == [False]
    assert adapter.send.await_count == 2


@pytest.mark.asyncio
async def test_streaks_are_per_chat(platform_config):
    adapter = make_adapter(platform_config, GROUP)
    await run(adapter, [10] * 3, chat_id=5)
    assert await run(adapter, [10], chat_id=6) == [True]


@pytest.mark.parametrize("members", [SOLO_GROUP, [10]], ids=["solo-group", "dm"])
@pytest.mark.asyncio
async def test_never_trips_without_a_second_member(platform_config, members):
    adapter = make_adapter(platform_config, members)
    assert all(await run(adapter, [10] * 10))
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_lookup_failure_fails_open(platform_config):
    adapter = make_adapter(platform_config, GROUP)
    adapter.rpc.get_chat_contacts.side_effect = RuntimeError("rpc down")
    assert all(await run(adapter, [10] * 10))


@pytest.mark.asyncio
async def test_disabled_with_zero(platform_config):
    adapter = make_adapter(platform_config, GROUP, limit=0)
    assert all(await run(adapter, [10] * 50))
    adapter.rpc.get_chat_contacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_list_not_fetched_below_limit(platform_config):
    adapter = make_adapter(platform_config, GROUP)
    await run(adapter, [10] * 3)
    adapter.rpc.get_chat_contacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_drops_tripped_message(platform_config):
    adapter = make_adapter(platform_config, GROUP, limit=1)
    adapter.rpc.get_message.return_value = {"text": "hi", "view_type": "Text", "from_id": 10}
    adapter.rpc.get_basic_chat_info.return_value = {"name": "g"}
    adapter.rpc.get_contact.return_value = {"name": "bot"}
    adapter.handle_message = AsyncMock()
    for msg_id in (1, 2):
        await adapter._handle_incoming_message({"chat_id": 5, "msg_id": msg_id})
    assert adapter.handle_message.await_count == 1


def test_bad_env_value_falls_back_to_default(platform_config, monkeypatch):
    monkeypatch.setenv("DELTACHAT_MAX_CONSECUTIVE_REPLIES", "lots")
    assert DeltaChatAdapter(platform_config)._max_consecutive_replies == 20
