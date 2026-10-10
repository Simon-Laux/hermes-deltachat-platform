"""Long replies and edits against the REAL Delta Chat core, offline.

Starts deltachat-rpc-server with a local, unconfigured account and sends to
its own chat ("Saved messages"), so nothing goes over the network. A non-bot
account truncates its own copy by the same rule receivers use, so the
sender's stored text shows what a receiver would see. Skipped when
deltachat-rpc-server isn't installed.
"""
import random
import shutil

import pytest

# first: puts the vendored deltachat2 (the one the adapter runs with) on sys.path
from adapter import DeltaChatAdapter, DeltaChatEditingAdapter, _AsyncRpc, _DC_TEXT_LIMIT, _dc_len, _dc_split
from tests.conftest import MockPlatform, MockPlatformConfig, core_truncates

pytestmark = pytest.mark.skipif(not shutil.which("deltachat-rpc-server"),
                                reason="deltachat-rpc-server not installed")

DC_CONTACT_ID_SELF = 1


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    import deltachat2
    from deltachat2.transport import IOTransport

    transport = IOTransport(accounts_dir=str(tmp_path_factory.mktemp("dc")),
                            rpc_server="deltachat-rpc-server")
    transport.start()
    try:
        rpc = deltachat2.Rpc(transport)
        acc = rpc.add_account()
        # enough for sending to the self-chat; no transport, so nothing leaves
        rpc.set_config(acc, "configured_addr", "bot@example.org")
        rpc.set_config(acc, "configured", "1")
        # core skips truncating a bot's own copy, which would make it say nothing
        rpc.set_config(acc, "bot", "0")
        chat = rpc.create_chat_by_contact_id(acc, DC_CONTACT_ID_SELF)
        yield rpc, acc, chat
    finally:
        transport.close()


def _folded(rpc, acc, msg_id, text):
    m = rpc.get_message(acc, int(msg_id))
    return m.text != text or m.has_html


def test_core_folds_exactly_where_our_port_says(core):
    # guards the 38-line / 100-char rule against core changing it
    from deltachat2.types import MsgData

    rpc, acc, chat = core
    texts = ["\n".join(["l"] * 38), "\n".join(["l"] * 39),
             "x" * 3800, "x" * 3801, "\n".join(["y" * 100] * 38), "\n".join(["y" * 101] * 19)]
    rnd = random.Random(0)
    texts += ["".join(rnd.choice(["\n", "z" * rnd.randint(1, 250), " "])
                      for _ in range(rnd.randint(20, 60))).strip() or "z" for _ in range(40)]
    for text in texts:
        msg_id = rpc.send_msg(acc, chat, MsgData(text=text))
        assert _folded(rpc, acc, msg_id, text) == core_truncates(text), repr(text[:80])


def _adapter(core, cls=DeltaChatAdapter, **kw):
    rpc, acc, _ = core
    a = cls(MockPlatformConfig(
        name="deltachat-platform", platform=MockPlatform.DELTACHAT, extra={}), **kw)
    a.rpc, a.account_id = _AsyncRpc(rpc), acc
    return a


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "\n".join(f"Line {i}" for i in range(100)),
    " ".join(f"word{i}" for i in range(2000)),
], ids=["lines", "one-paragraph"])
async def test_long_reply_arrives_as_messages_shown_in_full(core, text):
    rpc, acc, chat = core
    result = await _adapter(core).send(str(chat), text)
    ids = [*result.continuation_message_ids, result.message_id]
    assert result.success and len(ids) > 1
    shown = [rpc.get_message(acc, int(i)) for i in ids]
    assert not any(m.has_html or m.text.endswith("[...]") for m in shown)
    assert [m.text for m in shown] == [p.strip("\n") for p in _dc_split(text) if p.strip()]


@pytest.mark.asyncio
async def test_edit_at_the_limit_is_not_folded_one_more_line_is(core):
    # core stores the new text on the original untruncated; what receivers get
    # is a hidden "✏️" + text message (the next id), folded by the usual rule.
    # So the "✏️" counts, and _dc_len counts it.
    rpc, acc, chat = core
    a = _adapter(core, DeltaChatEditingAdapter, interval=1.0)
    # 38 lines; "✏️" goes in front of the first, making it exactly 100 chars
    final = "y" * 98 + "\n" + "\n".join(["l"] * 37)
    assert _dc_len(final) == _DC_TEXT_LIMIT

    sent = await a.send(str(chat), "first chunk ▉")
    assert (await a.edit_message(str(chat), sent.message_id, final, finalize=True)).success
    edit = rpc.get_message(acc, int(sent.message_id) + 1)
    assert edit.text == "✏️" + final and not edit.has_html

    sent = await a.send(str(chat), "first chunk ▉")
    longer = "y" + final   # first line wraps: 39 lines; edit_message would refuse it
    assert _dc_len(longer) > _DC_TEXT_LIMIT
    rpc.send_edit_request(acc, int(sent.message_id), longer)
    assert rpc.get_message(acc, int(sent.message_id) + 1).text.endswith("[...]")
