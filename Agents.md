# deltachat-hermes – Agent Reference

## About This File

This file is a living document. Agents should improve it over time when they discover something important that belongs here — but the bar is high: **prefer a code comment at the site where the knowledge is needed.** Only escalate to this file when the insight is cross-cutting, environmental, or genuinely has no good home in the code (e.g. nix environment quirks, RPC naming conventions, Hermes integration contracts). If it fits in a `# why:` comment next to the relevant line, put it there instead.

## Environment (NixOS)

This project runs on NixOS. All tools are provided via `nix develop`. Never run `python3`, `pytest`, `pip`, or other commands bare — they won't be found.

```bash
nix develop                              # enter dev shell
nix develop --command python3 script.py  # run a script
nix develop --command pytest tests/      # run tests — the path is required, see below
nix develop --command deltachat-rpc-server --openrpc  # inspect RPC spec
```

Pass `tests/` explicitly. Bare `pytest` collects the repo root, where `__init__.py` does
`from .adapter import ...` — a relative import with no parent package, which fails and takes every
test module down with it (~134 collection errors that say nothing about the tests). `make test`
sidesteps it by running from inside `tests/`.

**The call tests** (`test_call_handler.py`, `test_call_webrtc_loopback.py`) fail to collect in
the dev shell: `call_handler.py` puts `~/.hermes/aiortc-env` on `sys.path`, and that env is built
from Hermes's nixpkgs, not the dev shell's (glibc mismatch → `GLIBC_2.4x not found`). Run the
whole suite with a Python from Hermes's own nixpkgs instead:

```bash
nix build --impure -o ./.calltest-py --expr 'let p = import (builtins.getFlake
  "github:NousResearch/hermes-agent/<tag>").inputs.nixpkgs { system = builtins.currentSystem; };
  in p.python312.withPackages (ps: [ ps.aiortc ps.pytest ps.pytest-asyncio ps.numpy ])'
env -u LD_LIBRARY_PATH -u PYTHONPATH HOME=/nonexistent ./.calltest-py/bin/python3 \
  -m pytest tests/ -p no:cacheprovider          # add `-m slow` for the WebRTC loopback tests
```

`HOME=/nonexistent` keeps `call_handler.py` from loading the live `~/.hermes/aiortc-env`.

## Finding Hermes Source

### Locating your installed Hermes

Always derive the path — never copy a store hash out of this file. Hashes
change on every rebuild, and different people run different Hermes versions.

The catch: `hermes` on PATH resolves to a *wrapper* derivation containing
`bin/`, `share/` and `ui-tui/` but **no `lib/`**, so grepping it for Python
source silently finds nothing. The code lives in a separate store path:

```bash
hermes --version
readlink -f "$(which hermes)"          # the wrapper — not what you want to grep
# the Python source, via the wrapper's runtime closure:
find $(nix-store -qR "$(readlink -f "$(which hermes)")" | grep -E 'hermes-agent-[0-9]') \
     -maxdepth 3 -name site-packages -type d
```

Non-nix installs (pip/venv) have no such split — `python -c 'import gateway,
os; print(os.path.dirname(os.path.dirname(gateway.__file__)))'` gets you
there.

What's worth reading in there:

```
gateway/platforms/base.py       # base adapter class, MessageEvent, MessageType
gateway/platforms/telegram.py   # reference for voice/image/location sending
gateway/platforms/matrix.py     # reference for read receipts
gateway/platforms/bluebubbles.py
gateway/platforms/signal.py
gateway/run.py                  # how inbound events are routed by message_type
hermes_cli/                     # plugin install/config CLI, manifest handling
```

Anything in this repo citing a Hermes `file:line` is pinned to whatever
version its author ran, so re-check line numbers before trusting them — the
`hermes_cli` internals in particular move a lot between releases.

### Reading a different Hermes version without installing it

Useful when checking whether behaviour we depend on changed in a release you
don't run. Upstream is public at `github:NousResearch/hermes-agent`; if `gh
api` or `raw.githubusercontent.com` are rate-limited (HTTP 429 through some
proxies), nix's fetcher takes a different route and works:

```bash
nix flake prefetch github:NousResearch/hermes-agent/<tag> --json
# .storePath -> a full source tree
```

Notes:
- Tags are date-based (`v2026.8.19`); the real semver is in `pyproject.toml`
  (that tag is 0.20.5).
- PyPI's `hermes-agent` lags GitHub — prefer a tag over the PyPI version.
- The prefetched path is not a GC root; re-run the prefetch to get it back
  (same rev → same path).

See `docs/upstreaming-to-hermes.md` for a worked example of comparing an
installed Hermes against a newer one.

## Logging

The adapter logger must use the `hermes_plugins.*` prefix to appear in `~/.hermes/logs/gateway.log`. Using `__name__` (which resolves to `"adapter"`) routes to `agent.log` only and is invisible from the gateway log.

```python
logger = logging.getLogger("hermes_plugins.deltachat")  # correct
logger = logging.getLogger(__name__)                     # wrong — goes to agent.log only
```

Hermes log files:
- `~/.hermes/logs/gateway.log` — `gateway.*` and `hermes_plugins.*` loggers (INFO+)
- `~/.hermes/logs/agent.log` — every logger, at the configured level (INFO unless `logging.level` says otherwise)
- `~/.hermes/logs/errors.log` — WARNING+ only

Keep the adapter's own INFO+ lines free of message text, captions, call transcripts and
the names of files people send. IDs (chat, msg, contact), view types, sizes, error text
and our own paths are fine; they are what makes the logs useful. Content belongs at
DEBUG, which is opt-in. This only covers our lines: Hermes itself logs an excerpt of
every inbound message at INFO (`inbound message: ... msg=%r`, the first 80 characters
of `event.text`, which includes captions and file names), so `gateway.log` is never
content-free. Hermes's `RedactingFormatter` masks credential-shaped strings, not
message content.

## Architecture

- `adapter.py` – the platform adapter; registers with Hermes via `register_platform()` and `register_rpc_tools()`
- `vendor/deltachat2/` – vendored Python client for the DC JSON-RPC server
  - `rpc.py` – `Rpc` class; `send_msg` is the only manually defined method (serializes `MsgData` via `_snake2camel`); all other methods are proxied via `__getattr__` → `transport.call(method_name, *args)`
  - `_utils.py` – `AttrDict` (camelCase → snake_case on receive), `_snake2camel` (snake_case → camelCase on send)
  - `types.py` – `MsgData`, `MessageViewtype`, `EventType`, `MessageState`, etc.
- `deltachat-rpc-openrpc.json` – OpenRPC spec; inspect for available methods and their params

## DC JSON-RPC — Always Check the Spec First

**Do not guess or hallucinate RPC method names, parameter names, or field names.**

`jq` is available in the dev shell. Before writing any RPC call, look up the method in `deltachat-rpc-openrpc.json`:

```bash
# List all method names
jq '[.methods[].name]' deltachat-rpc-openrpc.json

# Find methods matching a pattern, with their parameter names
jq '[.methods[] | select(.name | contains("send")) | {name, params: [.params[].name]}]' deltachat-rpc-openrpc.json

# Full spec for a specific method
jq '.methods[] | select(.name == "markseen_msgs")' deltachat-rpc-openrpc.json

# Schema for a type (e.g. Message, MessageData, BasicChat)
jq '.components.schemas.BasicChat' deltachat-rpc-openrpc.json
```

## DC JSON-RPC Conventions

**Method names**: all `snake_case` — e.g. `send_msg`, `markseen_msgs`, `get_basic_chat_info`.

**Parameter names** in the JSON spec: `camelCase` — e.g. `accountId`, `chatId`, `msgIds`. These appear in the spec but are handled automatically by the Python client.

**Incoming messages** (`get_message` returns AttrDict):
- JSON `viewType` → Python key `view_type`
- JSON `fromId` → `from_id`, `chatId` → `chat_id`, `fileMime` → `file_mime`, etc.
- `is_group` lives on **chat** objects (`get_basic_chat_info`), not message objects

**Outgoing** (`MsgData` dataclass → JSON via `_snake2camel`):
- `quoted_message_id` → `quotedMessageId`
- `override_sender_name` → `overrideSenderName`
- `viewtype` (no underscore) → `viewtype` (unchanged)

## Hermes MessageEvent Contract

Hermes routes incoming events in `run.py` by `message_type`. Always set this correctly and populate `media_urls`/`media_types` for non-text content:

| `MessageType` | When to use | Notes |
|---|---|---|
| `TEXT` | plain text | default |
| `VOICE` | voice/opus message | Hermes auto-runs STT if `media_urls` set |
| `AUDIO` | audio file attachment | never STT |
| `PHOTO` | image | Hermes routes to vision pipeline if `media_urls` set |
| `DOCUMENT` | file attachment | |

Do **not** transcribe audio or analyze images inside the adapter — set the type and `media_urls`, let Hermes handle it.

## Useful RPC Methods

```python
markseen_msgs(account_id, [msg_id])       # mark message seen (read receipt)
marknoticed_chat(account_id, chat_id)     # mark all as noticed (not seen)
get_message(account_id, msg_id)           # fetch full message snapshot
get_basic_chat_info(account_id, chat_id)  # name, is_group
send_msg(account_id, chat_id, MsgData)    # send any message type
get_contact(account_id, contact_id)       # name, addr
```
