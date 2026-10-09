"""Opt-in message editing: off by default, and throttled when on because
every edit is an email through someone else's chatmail relay (#54)."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import adapter as adapter_mod
from adapter import DeltaChatAdapter, DeltaChatEditingAdapter, _edit_interval
from gateway.platforms.base import BasePlatformAdapter
from tests.conftest import MockPlatform, MockPlatformConfig

INTERVAL = 0.05


def _cfg(extra=None):
    return MockPlatformConfig(name="deltachat-platform", platform=MockPlatform.DELTACHAT,
                              extra=extra or {})


@pytest.fixture
def ed(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_MIN_EDIT_INTERVAL", 0.0)
    a = DeltaChatEditingAdapter(_cfg({"message_editing": True,
                                      "edit_min_interval": INTERVAL}))
    a.account_id = 1
    a.rpc = AsyncMock()
    return a


def _sent(a):
    return [c.args for c in a.rpc.send_edit_request.await_args_list]


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("DELTACHAT_MESSAGE_EDITING", raising=False)
    monkeypatch.delenv("DELTACHAT_EDIT_MIN_INTERVAL", raising=False)


def test_default_off_keeps_tool_progress_off():
    # Hermes only shows tool progress if the class overrides edit_message
    assert _edit_interval(_cfg()) is None
    assert "edit_message" not in vars(DeltaChatAdapter)
    assert DeltaChatAdapter.SUPPORTS_MESSAGE_EDITING is False
    assert DeltaChatEditingAdapter.SUPPORTS_MESSAGE_EDITING is True


@pytest.mark.parametrize("extra,env,expected", [
    ({}, {"DELTACHAT_MESSAGE_EDITING": "0"}, None),
    ({}, {"DELTACHAT_MESSAGE_EDITING": "1"}, 5.0),
    ({}, {"DELTACHAT_MESSAGE_EDITING": "1", "DELTACHAT_EDIT_MIN_INTERVAL": ""}, 5.0),
    ({}, {"DELTACHAT_MESSAGE_EDITING": "1", "DELTACHAT_EDIT_MIN_INTERVAL": "8"}, 8.0),
    ({"message_editing": True, "edit_min_interval": 0.1}, {}, 1.0),   # clamped
    ({"message_editing": True, "edit_min_interval": "x"}, {}, 5.0),
    ({"message_editing": True, "edit_min_interval": "inf"}, {}, 5.0),
    ({"message_editing": False}, {"DELTACHAT_MESSAGE_EDITING": "1"}, None),
])
def test_config(monkeypatch, extra, env, expected):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert _edit_interval(_cfg(extra)) == expected


def test_factory_picks_class(monkeypatch):
    captured = {}
    ctx = type("Ctx", (), {"register_platform": lambda self, **kw: captured.update(kw)})()
    adapter_mod.register_platform(ctx)
    factory = captured["adapter_factory"]
    assert type(factory(_cfg())) is DeltaChatAdapter
    assert type(factory(_cfg({"message_editing": "yes"}))) is DeltaChatEditingAdapter


@pytest.mark.asyncio
async def test_edit_sends_request(ed):
    r = await ed.edit_message("5", "123", "hello")   # positional, like the heartbeat
    assert r.success and r.message_id == "123" and r.raw_response is None
    assert _sent(ed) == [(1, 123, "hello")]


@pytest.mark.asyncio
async def test_burst_is_coalesced_to_latest_text(ed):
    await ed.edit_message("5", "123", "a")
    for text in ("ab", "abc", "abcd"):
        r = await ed.edit_message(chat_id="5", message_id="123", content=text, metadata={})
        assert r.success and r.raw_response == {"skipped": True}
    assert _sent(ed) == [(1, 123, "a")]
    await asyncio.sleep(INTERVAL * 3)
    assert _sent(ed) == [(1, 123, "a"), (1, 123, "abcd")]
    assert ed._edit_pending == {}
    assert ed._edit_flusher.done()


@pytest.mark.asyncio
async def test_budget_is_per_account(ed):
    await ed.edit_message("5", "1", "x")
    await ed.edit_message("5", "2", "y")
    await ed.edit_message("6", "3", "z")
    loop = asyncio.get_running_loop()
    times = []
    ed.rpc.send_edit_request.side_effect = lambda *a: times.append(loop.time())
    await asyncio.sleep(INTERVAL * 4)
    assert [a[1] for a in _sent(ed)] == [1, 2, 3]
    assert times[1] - times[0] >= INTERVAL * 0.9


@pytest.mark.asyncio
async def test_finalize_goes_out_now_and_drops_stale_text(ed):
    await ed.edit_message("5", "123", "a ▉")
    await ed.edit_message("5", "123", "ab ▉")
    r = await ed.edit_message("5", "123", "abc", finalize=True)
    assert r.success and r.raw_response is None
    await asyncio.sleep(INTERVAL * 3)
    assert _sent(ed) == [(1, 123, "a ▉"), (1, 123, "abc")]


@pytest.mark.asyncio
async def test_finalize_waits_for_in_flight_flush(ed):
    await ed.edit_message("5", "123", "a ▉")
    await ed.edit_message("5", "123", "ab ▉")
    release = asyncio.Event()

    async def slow(*args):
        if args[2] == "ab ▉":
            await release.wait()
    ed.rpc.send_edit_request.side_effect = slow
    await asyncio.sleep(INTERVAL * 2)       # flusher is now stuck sending "ab ▉"
    final = asyncio.create_task(ed.edit_message("5", "123", "abc", finalize=True))
    await asyncio.sleep(0)
    release.set()
    assert (await final).success
    assert _sent(ed)[-1] == (1, 123, "abc")


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id,content,connected", [
    ("123", "\n".join(["x"] * 41), True),   # would need an HTML part
    ("__no_edit__", "hi", True),
    (None, "hi", True),
    ("123", "hi", False),
])
async def test_refusals(ed, message_id, content, connected):
    if not connected:
        ed.rpc = None
    r = await ed.edit_message("5", message_id, content, finalize=True)
    assert not r.success and r.error in ("edit unavailable", "too long to edit")


@pytest.mark.asyncio
async def test_rpc_error_is_a_plain_failure(ed):
    # "rate" in the error would make Hermes back off as if flood-limited
    ed.rpc.send_edit_request.side_effect = RuntimeError("Can edit only own messages; rate x")
    r = await ed.edit_message("5", "123", "hi")
    assert not r.success and r.error == "edit failed"


@pytest.mark.asyncio
async def test_cleanup_cancels_flusher(ed, monkeypatch):
    monkeypatch.setattr(ed, "_mark_disconnected", lambda: None, raising=False)
    await ed.edit_message("5", "123", "a")
    await ed.edit_message("5", "123", "ab")
    flusher, rpc = ed._edit_flusher, ed.rpc
    ed._cleanup()
    await asyncio.sleep(INTERVAL * 2)
    assert flusher.cancelled()
    assert ed._edit_pending == {}
    assert [c.args for c in rpc.send_edit_request.await_args_list] == [(1, 123, "a")]
