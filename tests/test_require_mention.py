"""Group mention gating (require_mention): unmentioned group messages are dropped, DMs and
mentioned group messages pass."""
from unittest.mock import AsyncMock

import pytest

from adapter import DeltaChatAdapter
from tests.conftest import MockMessageEvent, MockMessageType, MockPlatform, MockPlatformConfig, MockSource


def _adapter(extra=None, env=None, monkeypatch=None):
    cfg = MockPlatformConfig(name="deltachat-platform", platform=MockPlatform.DELTACHAT,
                             extra=extra or {})
    a = DeltaChatAdapter(cfg)
    a.account_id = 1
    a.rpc = AsyncMock()
    a.rpc.get_config.side_effect = lambda acc, key: {
        "configured_addr": "ghost-agent@chat.example", "addr": "ghost-agent@chat.example",
        "displayname": "Ghost"}.get(key)
    return a


def _event(text, chat_type):
    return MockMessageEvent(text=text, message_type=MockMessageType.TEXT,
                            source=MockSource(chat_id="5", chat_name="c", chat_type=chat_type,
                                              user_id="9", user_name="u"),
                            message_id="1")


async def _passes(a, text, chat_type):
    seen = []

    async def base(self, event):
        seen.append(event)

    from tests.conftest import MockBasePlatformAdapter
    orig = MockBasePlatformAdapter.handle_message
    MockBasePlatformAdapter.handle_message = base
    try:
        await a.handle_message(_event(text, chat_type))
    finally:
        MockBasePlatformAdapter.handle_message = orig
    return bool(seen)


@pytest.mark.asyncio
async def test_off_by_default_everything_passes():
    a = _adapter()
    await a._load_self_mention_patterns()
    assert await _passes(a, "hello all", "group")


@pytest.mark.asyncio
@pytest.mark.parametrize("text,ok", [
    ("hello all", False),
    ("@ghost can you check", True),
    ("hey @Ghost-agent", True),
    ("ping ghost-agent@chat.example please", True),
    ("ghost is a word, not a mention", False),
    ("mail @ghosted", False),
    ("user@ghost.example", False),
])
async def test_group_messages_need_a_mention(text, ok):
    a = _adapter({"require_mention": True})
    await a._load_self_mention_patterns()
    assert await _passes(a, text, "group") is ok


@pytest.mark.asyncio
async def test_dms_are_never_gated():
    a = _adapter({"require_mention": True})
    await a._load_self_mention_patterns()
    assert await _passes(a, "no mention here", "dm")


@pytest.mark.asyncio
async def test_env_and_custom_patterns(monkeypatch):
    monkeypatch.setenv("DELTACHAT_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DELTACHAT_MENTION_PATTERNS", '["\\\\bspooky\\\\b"]')
    a = _adapter()
    await a._load_self_mention_patterns()
    assert a.require_mention
    assert await _passes(a, "hey spooky", "group")
    assert not await _passes(a, "hey there", "group")
