"""Drive DeltaChatEditingAdapter with the REAL Hermes gateway code.

Runs under Hermes' own Python (no pytest there, and tests/conftest.py would
replace `gateway` with mocks), so tests/test_hermes_contract.py starts this
as a subprocess and checks the JSON it prints. The RPC is faked: every
send_msg / send_edit_request is recorded with its time, and a "screen" dict
tracks what each message shows.
"""
import asyncio
import dataclasses
import inspect
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gateway.config import Platform, PlatformConfig  # noqa: E402
from gateway.platforms.base import BasePlatformAdapter, SendResult  # noqa: E402
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig  # noqa: E402

# Hermes only knows the platform once the plugin is registered
Platform._add_pseudo_member("deltachat-platform")

import adapter as dc  # noqa: E402

INTERVAL = 0.3


class FakeRpc:
    def __init__(self):
        self.t0 = time.monotonic()
        self.log = []      # [time, method, msg id, text]
        self.screen = {}   # msg id -> text currently shown
        self._next_id = 100

    def _record(self, method, msg, text):
        self.log.append([round(time.monotonic() - self.t0, 3), method, msg, text])

    async def send_msg(self, account_id, chat_id, data):
        self._next_id += 1
        self.screen[self._next_id] = data.text
        self._record("send_msg", self._next_id, data.text)
        return self._next_id

    async def send_edit_request(self, account_id, msg, text):
        self.screen[msg] = text
        self._record("edit", msg, text)

    def __getattr__(self, name):  # anything else the adapter may touch
        async def noop(*args, **kwargs):
            return None
        return noop


def editing_adapter():
    a = dc.DeltaChatEditingAdapter(PlatformConfig(enabled=True, extra={}), INTERVAL)
    a.rpc = FakeRpc()
    a.account_id = 1
    return a


async def stream(chunks, delay):
    """Stream chunks through Hermes' consumer the way a model reply arrives."""
    a = editing_adapter()
    consumer = GatewayStreamConsumer(
        a, "42", StreamConsumerConfig(edit_interval=0.05, buffer_threshold=5))
    task = asyncio.create_task(consumer.run())
    for chunk in chunks:
        consumer.on_delta(chunk)
        await asyncio.sleep(delay)
    consumer.finish()
    await asyncio.wait_for(task, 30)
    await asyncio.sleep(INTERVAL * 3)  # let any queued edit flush
    return {"log": a.rpc.log, "screen": a.rpc.screen, "pending": len(a._edit_pending),
            "text": "".join(chunks)}


def contract():
    """Facts about the real Hermes API this adapter relies on."""
    from tools import clarify_gateway, slash_confirm

    def params(cls, name):
        return list(inspect.signature(getattr(cls, name)).parameters)

    base = inspect.signature(BasePlatformAdapter.edit_message).parameters
    ours = inspect.signature(dc.DeltaChatEditingAdapter.edit_message).parameters
    return {
        # reaction-answerable prompts override these; Hermes calls them by keyword
        "prompt_params_match": {
            name: params(dc.DeltaChatAdapter, name) == params(BasePlatformAdapter, name)
            for name in ("send_slash_confirm", "send_clarify")},
        "slash_confirm_api": all(hasattr(slash_confirm, n) for n in (
            "resolve",)),
        "clarify_api": all(hasattr(clarify_gateway, n) for n in (
            "_lock", "_entries", "resolve_gateway_clarify", "mark_awaiting_text"))
            and "multi_select" in {f.name for f in dataclasses.fields(clarify_gateway._ClarifyEntry)},
        # tool progress stays off unless editing is enabled
        "base_not_overridden":
            dc.DeltaChatAdapter.edit_message is BasePlatformAdapter.edit_message,
        "base_params_accepted": all(p in ours for p in base),
        "sendresult_fields": [f.name for f in dataclasses.fields(SendResult)],
    }


async def main():
    words = [f"tok{i} " for i in range(60)]
    print(json.dumps({
        "contract": contract(),
        "stream": await stream(words, 2.0 / len(words)),
        "long": await stream(words[:10] + [f"\nL{i}" for i in range(45)], 0.03),
    }))


asyncio.run(main())
