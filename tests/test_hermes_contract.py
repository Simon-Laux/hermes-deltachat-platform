"""Message editing against the real Hermes gateway code (#54).

The rest of the suite mocks Hermes. Here tests/hermes_contract/scenarios.py
streams replies through Hermes' real GatewayStreamConsumer into
DeltaChatEditingAdapter, under Hermes' own Python, and we check how many
emails that cost and what ends up on screen. Skipped when no Hermes install
is found; set HERMES_PYTHON to point at one explicitly.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from tests.conftest import core_truncates

SCRIPT = os.path.join(os.path.dirname(__file__), "hermes_contract", "scenarios.py")
INTERVAL = 0.3  # must match scenarios.py
CURSOR = "▉"


def _hermes_python():
    if os.getenv("HERMES_PYTHON"):
        return os.environ["HERMES_PYTHON"]
    hermes = shutil.which("hermes")
    if not hermes:
        return None
    # pip/venv install: python3 sits next to hermes. Nix: hermes is a wrapper
    # script that execs .../hermes-agent-env/bin/hermes, with python3 next to that.
    candidates = [os.path.dirname(os.path.realpath(hermes))]
    try:
        with open(os.path.realpath(hermes), errors="replace") as f:
            candidates += re.findall(r"(/[^\s'\"]+/bin)/hermes\b", f.read(4096))
    except OSError:
        pass
    for bin_dir in candidates:
        python = os.path.join(bin_dir, "python3")
        if os.access(python, os.X_OK):
            return python
    return None


@pytest.fixture(scope="module")
def run():
    python = _hermes_python()
    if not python:
        pytest.skip("no Hermes install found (set HERMES_PYTHON)")
    # HERMES_* media-policy settings would decide which test files may be sent
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "LD_LIBRARY_PATH") and not k.startswith("HERMES_")}
    env["HOME"] = "/nonexistent"  # keep the live ~/.hermes out of it
    proc = subprocess.run([python, SCRIPT], capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout)


def test_api_contract(run):
    c = run["contract"]
    # Hermes shows tool progress only for adapters whose class overrides edit_message
    assert c["base_not_overridden"]
    assert c["base_params_accepted"]
    assert {"success", "message_id", "error", "retryable"} <= set(c["sendresult_fields"])
    assert all(c["prompt_params_match"].values()), c["prompt_params_match"]
    assert c["slash_confirm_api"] and c["clarify_api"]
    # our override passes the lists by keyword, so they must stay nameable
    assert [n for n, _ in c["deliver_media_params"]] == [
        "self", "event", "media_files", "local_files"]
    assert c["deliver_media_params"][1][1] == "POSITIONAL_OR_KEYWORD"
    assert all(k != "POSITIONAL_ONLY" for _, k in c["deliver_media_params"])


def test_streamed_reply_is_one_throttled_message(run):
    s = run["stream"]
    log, text = s["log"], s["text"]
    assert [e[1] for e in log].count("send_msg") == 1
    # every message after the first waits one interval; only the final edit may not
    for (t0, *_), (t1, *_) in zip(log, log[1:-1]):
        assert t1 - t0 >= INTERVAL * 0.9, log
    duration = log[-1][0] - log[0][0]
    assert len(log) <= duration / INTERVAL + 2
    assert log[-1][1:] == ["edit", log[0][2], text]  # final text lands last
    assert s["screen"] == {str(log[0][2]): text}
    assert s["pending"] == 0


def test_code_block_at_the_limit_keeps_streaming(run):
    s = run["fence"]
    assert not any(e[3] is None for e in s["log"])
    assert not any(core_truncates("✏️" + e[3]) for e in s["log"]), "a message would be folded"
    shown = list(s["screen"].values())
    assert not any(CURSOR in m for m in shown), "stream froze with the cursor on screen"
    # nothing repeated: what's on screen is the reply plus at most Hermes' fences
    assert sum(len(m) for m in shown) <= len(s["text"]) + 20
    assert s["pending"] == 0


def test_long_reply_is_split_into_messages_shown_in_full(run):
    # Hermes splits by the adapter's message_len_fn while streaming, so no
    # message gets past what Delta Chat shows without "Show full message"
    s = run["long"]
    shown = [s["screen"][k] for k in sorted(s["screen"], key=int)]
    assert len(shown) == 3
    assert all(len(m.split("\n")) <= 38 for m in shown)
    assert not any(CURSOR in m for m in shown)
    assert "\n".join(shown) == s["text"]
    assert s["pending"] == 0


def test_tagged_and_bare_file_is_sent_once(run):
    """MEDIA: tag plus a bare mention of the same file: one attachment."""
    for name, r in run["media"].items():
        assert r["files"] == [name], r
        assert name not in r["text"] and "MEDIA:" not in r["text"], r


@pytest.mark.parametrize("case", ["call", "call_long"])
def test_streamed_reply_in_a_call_is_spoken_once_in_full(run, case):
    """A streamed preview reported as delivered left Hermes sending only the
    unseen tail as the final, so the reply was cut short or never spoken. Over
    the length limit, failed previews had Hermes retry its split in a loop that
    never yields, speaking the head twice."""
    c = run[case]
    assert [s.strip() for s in c["spoken"]] == [c["text"].strip()], c["spoken"]
    assert c["log"] == []  # nothing leaks into the chat as text
    assert c["final_sent"]
    assert c["sends"] < 20, c["sends"]


def test_reaction_turn_reads_as_a_reply_and_waits_for_idle(run):
    r = run["reaction_turn"]
    assert r["gateway_control"] is False and r["turns"] == 1, r  # none while busy
    text = r["text"]
    assert text.startswith('[Replying to your previous message: "The answer is 42."]\n\n'
                           "[Reacted with 👎]"), text
