"""Opt-in message editing: off by default, and throttled when on because
every edit is an email through someone else's chatmail relay (#54)."""
import asyncio
import functools
import random
import selectors
from unittest.mock import AsyncMock

import pytest

import adapter as adapter_mod
from adapter import DeltaChatAdapter, DeltaChatEditingAdapter, _edit_interval
from gateway.platforms.base import BasePlatformAdapter
from tests.conftest import MockPlatform, MockPlatformConfig

INTERVAL = 5.0


class _VirtualSelector(selectors.DefaultSelector):
    def __init__(self, loop):
        super().__init__()
        self._loop = loop

    def select(self, timeout=None):
        # jump straight to the next timer instead of waiting for it
        if timeout:
            self._loop.now += timeout
        return super().select(None if timeout is None else 0)


class _VirtualTimeLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        self.now = 0.0
        super().__init__(_VirtualSelector(self))

    def time(self):
        return self.now


def virtual_time(test):
    """Run an async test on a loop whose clock only moves when it sleeps:
    real intervals, no wall-clock waits, no flakes on a slow CI runner."""
    @functools.wraps(test)
    def run(*args, **kwargs):
        with asyncio.Runner(loop_factory=_VirtualTimeLoop) as runner:
            return runner.run(test(*args, **kwargs))
    return run


def _cfg(extra=None):
    return MockPlatformConfig(name="deltachat-platform", platform=MockPlatform.DELTACHAT,
                              extra=extra or {})


@pytest.fixture
def ed():
    a = DeltaChatEditingAdapter(_cfg({"message_editing": True}), INTERVAL)
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


@virtual_time
async def test_edit_sends_request(ed):
    r = await ed.edit_message("5", "123", "hello")   # positional, like the heartbeat
    assert r.success and r.message_id == "123" and r.raw_response is None
    assert _sent(ed) == [(1, 123, "hello")]


@virtual_time
async def test_burst_is_coalesced_to_latest_text(ed):
    await ed.edit_message("5", "123", "a")
    for text in ("ab", "abc", "abcd"):
        r = await ed.edit_message(chat_id="5", message_id="123", content=text, metadata={})
        # reported as done: Hermes then tracks the queued text as on screen
        assert r.success and r.raw_response is None
    assert _sent(ed) == [(1, 123, "a")]
    await asyncio.sleep(INTERVAL * 3)
    assert _sent(ed) == [(1, 123, "a"), (1, 123, "abcd")]
    assert ed._edit_pending == {}
    assert ed._edit_flusher.done()


@virtual_time
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


@virtual_time
async def test_finalize_goes_out_now_and_drops_stale_text(ed):
    await ed.edit_message("5", "123", "a ▉")
    await ed.edit_message("5", "123", "ab ▉")
    r = await ed.edit_message("5", "123", "abc", finalize=True)
    assert r.success and r.raw_response is None
    await asyncio.sleep(INTERVAL * 3)
    assert _sent(ed) == [(1, 123, "a ▉"), (1, 123, "abc")]


@virtual_time
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


@pytest.mark.parametrize("message_id,content,connected", [
    ("123", "\n".join(["x"] * 41), True),   # would need an HTML part
    ("__no_edit__", "hi", True),
    ("²", "hi", True),                      # isdigit() but not int()-able
    ("123", "  \n", True),
    (None, "hi", True),
    ("123", "hi", False),
])
@virtual_time
async def test_refusals(ed, message_id, content, connected):
    if not connected:
        ed.rpc = None
    r = await ed.edit_message("5", message_id, content, finalize=True)
    assert not r.success
    assert r.error in ("edit unavailable", "too long to edit", "empty edit")
    assert not connected or _sent(ed) == []


@virtual_time
async def test_rpc_error_is_a_plain_failure(ed):
    # "rate" in the error would make Hermes back off as if flood-limited
    ed.rpc.send_edit_request.side_effect = RuntimeError("Can edit only own messages; rate x")
    r = await ed.edit_message("5", "123", "hi")
    assert not r.success and r.error == "edit failed"


@virtual_time
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


@virtual_time
async def test_too_long_interim_is_retryable(ed):
    # a progress bubble past 40 lines stays frozen instead of Hermes sending
    # one new message per tool; the final edit falls back to send()
    long = "\n".join(["x"] * 41)
    assert (await ed.edit_message("5", "123", long)).retryable is True
    assert (await ed.edit_message("5", "123", long, finalize=True)).retryable is False


@virtual_time
async def test_refused_finalize_drops_queued_text(ed):
    await ed.edit_message("5", "123", "a ▉")
    await ed.edit_message("5", "123", "ab ▉")
    r = await ed.edit_message("5", "123", "\n".join(["x"] * 41), finalize=True)
    assert not r.success
    await asyncio.sleep(INTERVAL * 3)
    assert _sent(ed) == [(1, 123, "a ▉")]


@pytest.mark.parametrize("seed", range(200))
@virtual_time
async def test_throttle_invariants(ed, seed):
    """Random traffic on a few messages, each driven like Hermes drives one:
    sequential interim edits, then a finalize. Slow RPCs included."""
    rnd = random.Random(seed)
    loop = asyncio.get_running_loop()
    log = []  # (start time, msg, text)

    async def rpc(_acc, msg, text):
        log.append((loop.time(), msg, text))
        await asyncio.sleep(rnd.choice([0, 0, 0.1, 3, 8]))
    ed.rpc.send_edit_request.side_effect = rpc

    async def drive(msg):
        for i in range(rnd.randint(0, 12)):
            await asyncio.sleep(rnd.choice([0, 0.05, 0.8, 2, 7]))
            assert (await ed.edit_message("5", str(msg), f"{msg}:{i}")).success
        await asyncio.sleep(rnd.choice([0, 1, 6]))
        assert (await ed.edit_message("5", str(msg), f"{msg}:F", finalize=True)).success

    msgs = range(1, rnd.randint(2, 5))
    await asyncio.gather(*(drive(m) for m in msgs))
    await asyncio.sleep(INTERVAL * 20)

    # an interim edit never starts within one interval of the previous edit
    for (t0, _, _), (t1, _, text) in zip(log, log[1:]):
        if not text.endswith(":F"):
            assert t1 - t0 >= INTERVAL - 1e-9, log
    # every message ends on its final text, and nothing is left behind
    for m in msgs:
        assert [text for _, msg, text in log if msg == m][-1] == f"{m}:F", log
    assert ed._edit_pending == {}
    assert ed._edit_flusher is None or ed._edit_flusher.done()


@virtual_time
async def test_interim_queued_behind_a_finalize_respects_the_budget(ed):
    # msg 1's slow edit holds the lock until t=6; msg 2's finalize waits for it.
    # An interim edit for msg 3 arriving exactly at release passes the
    # pre-lock check, but must not go out right after msg 2's finalize.
    loop = asyncio.get_running_loop()
    log = []
    released = asyncio.Event()

    async def rpc(_acc, msg, text):
        log.append((loop.time(), text))
        if text == "1":
            await asyncio.sleep(6)
            released.set()  # wakes late_interim before the finalize's lock waiter

    ed.rpc.send_edit_request.side_effect = rpc

    async def late_interim():
        await released.wait()
        await ed.edit_message("5", "3", "3")

    first = asyncio.create_task(ed.edit_message("5", "1", "1"))
    await asyncio.sleep(1)
    await asyncio.gather(first, ed.edit_message("5", "2", "2F", finalize=True), late_interim())
    await asyncio.sleep(INTERVAL * 3)
    assert log == [(0.0, "1"), (6.0, "2F"), (11.0, "3")], log
