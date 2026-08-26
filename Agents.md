# deltachat-hermes – Agent Reference

## About This File

This file is a living document. Agents should improve it over time when they discover something important that belongs here — but the bar is high: **prefer a code comment at the site where the knowledge is needed.** Only escalate to this file when the insight is cross-cutting, environmental, or genuinely has no good home in the code (e.g. nix environment quirks, RPC naming conventions, Hermes integration contracts). If it fits in a `# why:` comment next to the relevant line, put it there instead.

## Environment (NixOS)

This project runs on NixOS. All tools are provided via `nix develop`. Never run `python3`, `pytest`, `pip`, or other commands bare — they won't be found.

```bash
nix develop                              # enter dev shell
nix develop --command python3 script.py  # run a script
nix develop --command pytest             # run tests
nix develop --command deltachat-rpc-server --openrpc  # inspect RPC spec
```

## Finding Hermes Source

### The version we actually run — 0.15.1

`hermes` on PATH resolves to a wrapper; the Python code lives in a *different*
store path than the wrapper. When checking behaviour against what is really
installed, use the site-packages path:

```
/nix/store/5naa7x31xmvsj1bqqrgjzqsas99j7pc8-hermes-agent-0.15.1/lib/python3.12/site-packages/
  gateway/platforms/base.py       # base adapter class, MessageEvent, MessageType
  gateway/platforms/telegram.py   # reference for voice/image/location sending
  gateway/platforms/matrix.py     # reference for read receipts
  gateway/platforms/bluebubbles.py
  gateway/platforms/signal.py
  hermes_cli/                     # plugin install/config CLI
```

The wrapper (`bin/hermes`, plus `share/` and `ui-tui/`) is the other path,
`/nix/store/h5w63b5x0fj882sr1xpdfnqdrj37qgzz-hermes-agent-0.15.1/` — it has no
`lib/`, so grepping it for Python source finds nothing. Re-derive both if the
profile is rebuilt:

```bash
readlink -f "$(which hermes)"                     # -> the h5w63b… wrapper
hermes --version
```

### Reading a newer Hermes without installing it

Upstream is public at `github:NousResearch/hermes-agent`, but the sandbox's HTTP
proxy rate-limits `gh api` and `raw.githubusercontent.com` (HTTP 429). Nix uses a
different fetcher and works:

```bash
nix flake prefetch github:NousResearch/hermes-agent/v2026.8.19 --json
# -> /nix/store/ypgrl16c8ijc0bb260afvlszzp53zhih-source   (0.20.5, full source tree)
```

That path is a GC root only until the next `nix-collect-garbage`; re-run the
prefetch to get it back (same rev → same path). PyPI's `hermes-agent` lags
GitHub — it was on 0.19.0 when GitHub was on 0.20.5 — so prefer the tag.
Check `pyproject.toml` for the real semver; the git tags are date-based
(`v2026.8.19` == 0.20.5).

Useful when deciding whether a Hermes-side behaviour we depend on has changed —
`gateway/run.py` call shapes, `gateway/platforms/base.py` signatures, and the
`hermes_cli/` plugin manifest handling all move between releases. See
`docs/fork-backport-notes.md` for a worked example.

## Logging

The adapter logger must use the `hermes_plugins.*` prefix to appear in `~/.hermes/logs/gateway.log`. Using `__name__` (which resolves to `"adapter"`) routes to `agent.log` only and is invisible from the gateway log.

```python
logger = logging.getLogger("hermes_plugins.deltachat")  # correct
logger = logging.getLogger(__name__)                     # wrong — goes to agent.log only
```

Hermes log files:
- `~/.hermes/logs/gateway.log` — `gateway.*` and `hermes_plugins.*` loggers (INFO+)
- `~/.hermes/logs/agent.log` — everything (the catch-all)
- `~/.hermes/logs/errors.log` — WARNING+ only

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
