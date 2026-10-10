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
from adapter import DeltaChatAdapter, DeltaChatEditingAdapter, _AsyncRpc
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
    rpc = deltachat2.Rpc(transport)
    acc = rpc.add_account()
    # enough for sending to the self-chat; no transport, so nothing leaves
    rpc.set_config(acc, "configured_addr", "bot@example.org")
    rpc.set_config(acc, "configured", "1")
    chat = rpc.create_chat_by_contact_id(acc, DC_CONTACT_ID_SELF)
    yield rpc, acc, chat
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
    "intro\n```\n" + "\n".join(f"code {i}" for i in range(80)) + "\n```\noutro",
], ids=["lines", "one-paragraph", "code-block"])
async def test_long_reply_arrives_as_messages_shown_in_full(core, text):
    rpc, acc, chat = core
    result = await _adapter(core).send(str(chat), text)
    ids = [*result.continuation_message_ids, result.message_id]
    assert result.success and len(ids) > 1
    shown = [rpc.get_message(acc, int(i)) for i in ids]
    assert not any(m.has_html or m.text.endswith("[...]") for m in shown)
    assert " ".join(m.text for m in shown).split() == text.split()


@pytest.mark.asyncio
async def test_edit_up_to_the_limit_applies_in_full(core):
    rpc, acc, chat = core
    a = _adapter(core, DeltaChatEditingAdapter, interval=1.0)
    sent = await a.send(str(chat), "first chunk ▉")
    # the most an edit may carry: 38 display lines counting the "✏️" prefix
    final = "\n".join(["l"] * 37) + "\n" + "y" * 98
    result = await a.edit_message(str(chat), sent.message_id, final, finalize=True)
    m = rpc.get_message(acc, int(sent.message_id))
    assert result.success and m.is_edited and m.text == final and not m.has_html
