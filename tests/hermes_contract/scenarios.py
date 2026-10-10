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
        self.files = []    # attachments sent, in order
        self._next_id = 100

    def _record(self, method, msg, text):
        self.log.append([round(time.monotonic() - self.t0, 3), method, msg, text])

    async def send_msg(self, account_id, chat_id, data):
        self._next_id += 1
        if data.file:
            self.files.append(data.file)
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


async def stream(chunks, delay, metadata=None, calls=None):
    """Stream chunks through Hermes' consumer the way a model reply arrives:
    from the agent's worker thread."""
    a = editing_adapter()
    a._call_manager = calls
    consumer = GatewayStreamConsumer(
        a, "42", StreamConsumerConfig(edit_interval=0.05, buffer_threshold=5),
        metadata=metadata)
    task = asyncio.create_task(consumer.run())

    def feed():
        for chunk in chunks:
            consumer.on_delta(chunk)
            time.sleep(delay)
        consumer.finish()
    await asyncio.to_thread(feed)
    await asyncio.wait_for(task, 30)
    await asyncio.sleep(INTERVAL * 3)  # let any queued edit flush
    return {"log": a.rpc.log, "screen": a.rpc.screen, "pending": len(a._edit_pending),
            "text": "".join(chunks), "final_sent": consumer.final_response_sent}


async def call_stream(chunks, delay):
    """Stream a reply into an active call; adds what got spoken."""
    spoken, sends = [], []

    class Calls:  # the CallManager surface send() touches
        def is_call_end_reply(self, reply_to):
            sends.append(reply_to)  # send() asks this first
            return False

        def has_active_call(self, chat_id): return True
        def is_call_thread(self, thread_id): return thread_id == "call-1"
        def consume_call_ack(self, chat_id): return False
        async def play_response(self, chat_id, text): spoken.append(text)

    r = await stream(chunks, delay, {"thread_id": "call-1"}, Calls())
    return {**r, "spoken": spoken, "sends": len(sends)}


async def internal_turn():
    """Our handle_message drops a stored message_id from Hermes' own (internal) turns."""
    from gateway.platforms.base import MessageEvent, MessageType
    a = editing_adapter()
    origin = a.build_source(chat_id="5", chat_type="dm", user_id="10", message_id="7")
    seen = []

    async def handler(event):
        seen.append(event.source.message_id)
    real = BasePlatformAdapter.handle_message
    BasePlatformAdapter.handle_message = lambda self, event: handler(event)
    try:
        await a.handle_message(MessageEvent(
            text="", message_type=MessageType.TEXT, source=origin, internal=True))
    finally:
        BasePlatformAdapter.handle_message = real
    return {"seen": seen, "origin_kept": origin.message_id}


def contract():
    """Facts about the real Hermes API this adapter relies on."""
    from gateway import session_context
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
        "slash_confirm_api": hasattr(slash_confirm, "resolve"),
        "clarify_api": all(hasattr(clarify_gateway, n) for n in (
            "_lock", "_entries", "resolve_gateway_clarify", "mark_awaiting_text"))
            and "multi_select" in {f.name for f in dataclasses.fields(clarify_gateway._ClarifyEntry)},
        # dc_react and dc_end_call find the turn's chat and triggering message through these
        "session_vars": sorted({"HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID",
                                "HERMES_SESSION_MESSAGE_ID"} - set(session_context._VAR_MAP)),
        "build_source_message_id": "message_id" in inspect.signature(
            BasePlatformAdapter.build_source).parameters,
        # tool progress stays off unless editing is enabled
        "base_not_overridden":
            dc.DeltaChatAdapter.edit_message is BasePlatformAdapter.edit_message,
        "base_params_accepted": all(p in ours for p in base),
        "sendresult_fields": [f.name for f in dataclasses.fields(SendResult)],
        # our override drops bare paths a MEDIA: tag already sends
        "deliver_media_params": [
            (n, p.kind.name) for n, p in inspect.signature(
                BasePlatformAdapter._deliver_media_attachments).parameters.items()][:4],
    }


async def media():
    """Run a reply through Hermes' real extract -> filter -> deliver chain.

    Each reply names the file with a MEDIA: tag and again as a bare path;
    both used to go out (once per list).
    """
    import tempfile
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource

    out = {}
    with tempfile.TemporaryDirectory() as d:
        for name in ("app.xdc", "report.pdf"):
            path = os.path.join(d, name)
            with open(path, "wb") as f:
                f.write(b"x")
            a = editing_adapter()
            source = SessionSource(platform=a.platform, chat_id="42", chat_type="dm")
            event = MessageEvent(text="hi", source=source)
            reply = f"Here it is:\nMEDIA:{path}\nSaved at {path} too."
            extracted = await a._extract_response_content(
                reply, event, "s", is_ephemeral_response=False)
            await a._deliver_attachments(event, extracted, {}, anything_sent=True,
                                         record_delivery=lambda r: None)
            out[name] = {"files": [os.path.basename(f) for f in a.rpc.files],
                         "text": extracted.text_content}
    return out


async def main():
    words = [f"tok{i} " for i in range(60)]
    print(json.dumps({
        "contract": contract(),
        "stream": await stream(words, 2.0 / len(words)),
        "long": await stream(words[:10] + [f"\nL{i}" for i in range(90)], 0.02),
        # code block reaching the limit with a long last line, then a pause: the
        # cursor wrap and the fence Hermes closes each add a line to the edit
        "fence": await stream(["```\n" + "\n".join(f"c{i}" for i in range(35)) + "\n",
                               "y" * 99] + [f"\nm{i}" for i in range(5)], 0.4),
        "media": await media(),
        "internal": await internal_turn(),
        "call": await call_stream(words[:20], 0.02),
        # over the 36-line limit: Hermes would split it into heads
        "call_long": await call_stream([f"Item {i}.\n" for i in range(50)], 0.02),
    }))


asyncio.run(main())
