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
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "LD_LIBRARY_PATH")}
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


def test_reply_over_40_lines_continues_as_new_message(run):
    s = run["long"]
    shown = [s["screen"][k] for k in sorted(s["screen"], key=int)]
    assert len(shown) == 2
    assert not any(CURSOR in m for m in shown)
    # Hermes re-sends the last partial line in the continuation, by design
    assert s["text"].startswith(shown[0]) and s["text"].endswith(shown[1])
    assert len(shown[0]) + len(shown[1]) >= len(s["text"])
    assert s["pending"] == 0
