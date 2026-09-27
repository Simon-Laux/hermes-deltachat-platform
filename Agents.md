# Delta Chat × Hermes — Agent Reference

> This file is intended for AI coding agents. The reader is assumed to know nothing about the project.

## Project Overview

This is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) platform plugin that adds **Delta Chat** as a gateway channel. It lets users talk to an AI assistant through Delta Chat — a decentralized, end-to-end encrypted messenger built on email — supporting text, voice messages, images, files, locations, live voice calls, and webxdc mini-apps.

The project is a pure-Python plugin (no compiled extensions of its own). It is loaded by Hermes at runtime from `~/.hermes/plugins/deltachat-platform/` and communicates with the Delta Chat core through the `deltachat-rpc-server` binary via line-delimited JSON-RPC.

## Technology Stack

- **Language**: Python 3 (targeted for the Hermes environment, commonly Python 3.12)
- **Plugin host**: Hermes Agent gateway
- **External binary dependency**: `deltachat-rpc-server` (Delta Chat core JSON-RPC server)
- **Optional voice-call dependencies**: `aiortc`, `av` (PyAV), plus STT/TTS providers configured in Hermes
- **Build/dev environment**: NixOS flake (`flake.nix`)
- **Formatter/linter**: `black`, `flake8`
- **Test runner**: `pytest`
- **License**: MPL-2.0

### Key Files

| File | Purpose |
|---|---|
| `__init__.py` | Plugin entry point; calls `register_platform()` and `register_rpc_tools()` |
| `adapter.py` | Main platform adapter (~2100 lines); connection, event loop, messaging, RPC tools |
| `call_handler.py` | WebRTC voice call handler (~1600 lines); incoming/outgoing calls, STT/TTS bridge |
| `setup.py` | Interactive Delta Chat account setup helper |
| `plugin.yaml` | Plugin manifest, name, description, environment variable schema |
| `flake.nix` | Nix development shell and package derivation |
| `Makefile` | Shortcuts for test / lint / format / check / clean |
| `deltachat-rpc-openrpc.json` | Frozen copy of the RPC spec; use `jq` to inspect methods |
| `vendor/deltachat2/` | Vendored minimal Python client for the RPC server |
| `tests/` | pytest suite with mocked Hermes gateway classes |
| `skills/webxdc-converter/` | Bundled skill for packaging web apps as `.xdc` mini apps |
| `docs/` | Human-readable documentation (voice calls, NixOS install, troubleshooting, etc.) |

## Runtime Architecture

1. Hermes loads the plugin and calls `register(ctx)` in `__init__.py`.
2. `register_platform()` registers `DeltaChatAdapter` as the platform adapter.
3. `register_rpc_tools()` registers Delta Chat-specific tools (`dc_rpc_spec`, `dc_chat_rpc_spec`, `dc_safe_rpc_call`, `dc_start_call`, `dc_end_call`, `dc_send_message`, and optionally `dc_rpc_call`).
4. When the gateway starts, `DeltaChatAdapter.connect()`:
   - Resolves `DELTACHAT_RPC_SERVER` / config `extra.rpc_server`.
   - Starts `deltachat-rpc-server` with `DC_ACCOUNTS_PATH` set to `<HERMES_HOME>/deltachat-platform/`.
   - Verifies the core version is at least `MIN_DC_VERSION` (`2.51.0`).
   - Uses the first existing account or errors out (account creation is done via `setup.py`).
   - Enables bot mode, starts IO, and launches the async event listener.
   - Instantiates `CallManager` for voice calls.
5. `_event_listener()` polls `get_next_event()` and dispatches `INCOMING_MSG`, `INCOMING_CALL`, `CALL_ENDED`, `OUTGOING_CALL_ACCEPTED`, etc.
6. Incoming messages are converted to Hermes `MessageEvent` objects with `MessageType.TEXT/VOICE/AUDIO/PHOTO/DOCUMENT` and handed to `handle_message()` (provided by the base class).
7. Outgoing replies are produced by Hermes and sent via `DeltaChatAdapter.send()`, `send_file()`, `send_voice()`, `send_image_file()`, `send_location()`, or `send_document()`.

### Important Threading/Layering Notes

- `deltachat2.Rpc.transport.call()` is **synchronous and blocks on a `threading.Event`**. The adapter wraps it in `_AsyncRpc`, which runs every call in the default `ThreadPoolExecutor` so the gateway asyncio loop is not frozen.
- Voice calls run on a **dedicated event loop in a daemon thread** inside `CallManager` so ICE/media tasks are never starved by AI/TTS work on the main loop.
- The adapter stores per-process state in module globals (`_active_adapter`, `_chat_id_to_token`, `_spec_cache`, `_seen_ids`, `_rate_limiter`).

## Module Divisions

### `adapter.py`

- Version check helpers (`_parse_version`, `_check_dc_version`)
- Access control (`_RateLimiter`, `_MessageCache`, `_check_dm`, `_check_group`, `_gate_inbound`)
- Async RPC wrapper (`_AsyncRpc`)
- Chat token management (`_get_or_create_chat_token`, `_resolve_chat_token`)
- OpenRPC spec fetching (`_fetch_spec`)
- `DeltaChatAdapter` class — the core platform adapter
- Plugin registration (`register_platform`, `register_rpc_tools`)

### `call_handler.py`

- `CallManager` — owns the dedicated call event loop and all active `CallSession`s
- `HermesAudioTrack` — outgoing audio track that plays queued TTS frames
- Audio buffering, silence detection, STT/TTS orchestration, barge-in handling
- Incoming and outgoing WebRTC signalling using `aiortc`

### `setup.py`

- `DeltaChatAccountSetup` — interactive account creation
- Relay server scraping from `chatmail.at/relays`
- `get_profiles()` / `select_profile()` for Hermes profile discovery
- `__main__` entry point used by the install instructions

### `vendor/deltachat2/`

- `transport.py` — `IOTransport` starts `deltachat-rpc-server` as a subprocess and handles JSON-RPC over stdin/stdout
- `rpc.py` — `Rpc` proxy; all methods forwarded via `__getattr__`, except `send_msg` which serializes `MsgData`
- `_utils.py` — `AttrDict` (camelCase → snake_case), `_snake2camel`, `to_attrdict`
- `types.py` — dataclasses/enums for events, message data, view types, etc.

## Build and Test Commands

> **Environment**: This project is developed on NixOS. Always enter the dev shell first; bare `python3`, `pytest`, `pip`, etc. are usually not available.

```bash
# Enter the development shell
nix develop

# Run the full test suite (excludes slow WebRTC loopback tests)
nix develop --command bash -c "cd tests && python3 -m pytest -v --tb=short"

# Run slow WebRTC loopback diagnostics
nix develop --command bash -c "cd tests && python3 -m pytest test_call_webrtc_loopback.py -q -s -m slow"

# Inspect the RPC spec
nix develop --command deltachat-rpc-server --openrpc
```

### Makefile Targets

```bash
make test     # cd tests && python3 -m pytest -v --tb=short
make lint     # flake8 adapter.py setup.py __init__.py tests/ --max-line-length=100 --extend-ignore=E203,W503,F401
make format   # black adapter.py setup.py __init__.py tests/ docs/ skills/
make check    # black --check ...
make clean    # remove __pycache__ and .pytest_cache
```

### Quick Syntax Check (outside NixOS)

If Nix is unavailable, you can still verify syntax:

```bash
python3 -m py_compile adapter.py setup.py __init__.py call_handler.py
python3 -m py_compile vendor/deltachat2/*.py
```

## Code Style Guidelines

- **Formatter**: `black`
- **Linter**: `flake8` with `--max-line-length=100 --extend-ignore=E203,W503,F401`
- **Docstrings**: Google-style docstrings are used throughout.
- **Imports**: Standard library first, third-party next, project/vendor last. `isort` is not configured; keep groups separated by blank lines.
- **Line length**: 100 characters.
- **Logging**: Use `logging.getLogger("hermes_plugins.deltachat")` (or a sub-logger like `"hermes_plugins.deltachat.calls"`). Using `__name__` routes logs to `agent.log` only and they will not appear in the gateway log.
- **Comments**: Prefer a `# why:` comment at the relevant line over adding cross-cutting notes to this file. Escalate only when the insight is environmental or RPC-convention related.

## Testing Instructions

- Tests live in `tests/`.
- `tests/conftest.py` installs mock `gateway` modules (`gateway.platforms.base`, `gateway.config`) before any adapter import, so tests can import `adapter` and `call_handler` without Hermes installed.
- `pytest.ini` excludes `slow` tests by default. Mark long-running tests (e.g. WebRTC ICE loopback) with `@pytest.mark.slow`.
- The suite covers:
  - `test_setup.py` — relay-server scraping regex logic and version formatting
  - `test_config.py` — config directory and RPC path resolution logic
  - `test_version.py` — version parsing
  - `test_adapter_integration.py` — adapter integration with mocked Hermes/RPC
  - `test_call_handler.py` — pure/unit-testable parts of `call_handler.py`
  - `test_call_webrtc_loopback.py` — live `aiortc` loopback diagnostics (slow, opt-in)

### Adding Tests

- Use the existing fixtures in `conftest.py` for `platform_config`, `mock_platform_config`, and `mock_rpc`.
- Mock Hermes classes live in `conftest.py`; update them if you introduce new base-class behavior.
- Do not add real network or real Delta Chat account dependencies to the default test run.

## Security Considerations

- **Authentication is relationship-based**: Delta Chat uses Autocrypt/SecureJoin. The setup script prints a SecureJoin invite link; users must scan it in Delta Chat. Adding only the email address does not establish an encrypted session.
- **Access control is configured in `plugin.yaml` / env vars**:
  - `DELTACHAT_DM_POLICY` — `open`, `allowlist`, `pairing` (default), or `disabled`
  - `DELTACHAT_GROUP_POLICY` — `open` (default), `allowlist`, or `disabled`
  - `DELTACHAT_ALLOWED_USERS`, `DELTACHAT_DM_ALLOWED_USERS`, `DELTACHAT_GROUP_ALLOWED_USERS`
  - `DELTACHAT_ALLOW_ALL_USERS=true` disables all sender checks (dev/open mode only)
  - `DELTACHAT_SEND_REJECTION_REPLIES` — whether rejected senders get an explanation
  - **Hermes-core has its own separate authorization gate** (`gateway.authz_mixin._is_user_authorized`), layered *on top of* this adapter's own checks. It does not trust `dm_policy: open` / `group_policy: open` as authorization (by design — an `open` policy forwards every sender, which core refuses to treat as an operator-configured allowlist). Setting `dm_policy`/`group_policy` to `open` here is **not enough** to let messages through if core rejects the sender first; `DELTACHAT_ALLOW_ALL_USERS=true` (or per-user pairing approval) is required for core to actually deliver the message to this adapter. `register_platform()` declares `allowed_users_env`/`allow_all_env` so core recognizes `DELTACHAT_ALLOWED_USERS`/`DELTACHAT_ALLOW_ALL_USERS` — without that declaration, core has no way to know these env vars exist and silently drops every sender regardless of this adapter's own config.
- **Rate limiting**: per-sender sliding window, default 30 messages / 60 seconds.
- **Raw RPC is opt-in and filtered**: `dc_rpc_call` (unrestricted RPC access) is only registered when `DELTACHAT_ENABLE_RAW_RPC=1` is set. It logs every call at `WARNING`, blocks all destructive methods (`delete_*`, `remove_*`, and the internal destructive list), and supports an optional allowlist (`DELTACHAT_RAW_RPC_ALLOWLIST=method1,method2`) or extra blocklist (`DELTACHAT_RAW_RPC_BLOCKLIST=method1`). Prefer `dc_safe_rpc_call`, which validates the chat token, injects `accountId`/`chatId`, and blocks destructive methods.
- **Chat tokens**: Opaque per-chat tokens (`[dc:chat=<token>]`) are appended to incoming message text so the LLM can call `dc_safe_rpc_call`. They are persisted in Delta Chat UI config keys (`ui.hermes.chat_token.<chat_id>`) and cached in-process under an `asyncio.Lock`.
- **Proactive sends (`dc_send_message`)**: lets the agent push text to a chat outside the reply flow — e.g. a cron/scheduled task, or one agent posting into a shared multi-agent group without having seen an inbound message from it yet. Still token-gated like `dc_safe_rpc_call` (no raw chat_id parameter, so it can't target an arbitrary/unauthorized chat); omitting `chat_token` falls back to `DELTACHAT_HOME_CHANNEL` if configured, and errors otherwise. An optional `address` param cold-opens a 1:1 chat with a Delta Chat email instead of a `chat_token` — guarded by `_is_address_in_known_rosters()` to addresses already seen via `get_chat_contacts` in a group this bot participates in (populated by the same roster cache `_get_group_roster` builds for group message metadata); an address outside every cached roster is rejected. `chat_token` takes precedence if both are given. Resolution is `lookup_contact_id_by_addr` → `create_contact` (only if unknown) → `create_chat_by_contact_id`.
- **Bot-loop guard**: `DELTACHAT_MAX_CONSECUTIVE_REPLIES` (default 20, `<=0` disables) stops the adapter from processing further messages from the same sender in a chat once they've sent that many in a row with no other participant chiming in — guards against two bots ping-ponging (one single other sender, from this bot's view) forever. Sends one notice (subject to `DELTACHAT_SEND_REJECTION_REPLIES`) the first time a streak trips; resets as soon as anyone else speaks. Both this guard and the bot-exchange guard below are skipped entirely for a group whose roster (`_get_group_roster`) has one or zero other members — that one member is structurally the only possible sender, same as a DM, so "someone else chiming in" could never happen and a trip would be permanent (and, with `send_rejection_replies: false`, silent).
- **Bot-exchange guard**: `DELTACHAT_MAX_BOT_EXCHANGES` (default 12) caps total messages in a chat from any sender not in `DELTACHAT_HUMAN_USERS`, before requiring a check-in from one of those addresses. Covers what the bot-loop guard above can't: 3+ bots round-robining a shared group, where the sender keeps changing so no single-sender streak ever trips. Inactive unless `DELTACHAT_HUMAN_USERS` is set (with no known human address there is no way to detect a "check-in").
- **Free-response channels**: `DELTACHAT_FREE_RESPONSE_CHANNELS` (comma-separated group chat IDs) exempts those groups from `DELTACHAT_REQUIRE_MENTION` — e.g. a shared multi-bot group where every message should get a reply without an `@mention`. Use alongside the bot-exchange guard above to keep that group's cross-bot chatter open but bounded.
- **Mention gate is silent by design**: an unmentioned group message under `DELTACHAT_REQUIRE_MENTION` is dropped without any reply, even when `DELTACHAT_SEND_REJECTION_REPLIES=true` — this check does not honor that flag. In a multi-bot group every bot enforces the gate independently, so a "please mention me" notice would fire once per bot per unmentioned message; silence is the only option that doesn't spam the chat.
- **Quote-reply-to-self exemption applies on both the text and non-text paths**: a quote-reply to the bot's own message is an implicit mention on the text path (`_handle_incoming_message`) and, since v1.7.9, on the image/file/voice path (`_handle_non_text_message`) too. Both paths go through the single `_gate_mention` helper (quote check via `_quote_is_self_authored`, then `_check_mention` on the body/caption only) — change the rule there, not in either caller.
- **Mention matching requires an explicit `@` and tolerates case-ending variation** (`_build_mention_pattern` in `adapter.py`): a bare display name in prose (e.g. "napiš Alici, aby to udělala" — asking someone else to message Alice) is *about* the bot, not addressed to it, so only `@Name`-style tokens count as a mention. Case-ending tolerance is built for Czech declension (e.g. `display_name: Alice` also matches "@Alici"/"@Alicí"; "Anikke" also matches the incorrectly-declined "@Anikko"), implemented generically as stem + up to 2 trailing word characters, not a Czech-specific grammar table. The stem must be ≥3 chars — shorter names (e.g. "Tom") fall back to an exact match instead, to avoid matching unrelated words that happen to share a short prefix. `DELTACHAT_MENTION_ALIASES` (comma-separated) adds extra exact-ish forms (each still gets the same stem-tolerant treatment, and each still requires the `@` prefix) for names/nicknames the automatic stemming doesn't cover.
- **File paths from containers**: When an agent writes files to `/workspace/` inside the Docker sandbox, the adapter resolves the path, verifies it stays inside the sandbox (rejects `..` and symlink escapes), rejects symlinks, and copies the file to the Hermes documents cache before validation/sending.
- **Secrets**: Do not commit real accounts, keys, or `.env` files. `DC_ACCOUNTS_PATH` lives under the Hermes profile directory (default `~/.hermes/deltachat-platform/`). The account password is cleared from memory as soon as configuration completes or fails.
- **Inbound access control is fail-closed**: If the adapter cannot fetch chat info to determine DM/group policy, the message is rejected. RPC/version-check failures also reject instead of falling through.
- **Voice-call audio buffering is capped**: Continuous speech is forced to flush after a 60-second ceiling so the incoming audio buffer cannot grow without bound.

## DC JSON-RPC Conventions

**Always check the spec first** (`deltachat-rpc-openrpc.json` or `deltachat-rpc-server --openrpc`). Do not guess method names or parameter names.

- **Method names**: `snake_case` (e.g. `send_msg`, `markseen_msgs`, `get_basic_chat_info`).
- **Spec parameter names**: `camelCase` (e.g. `accountId`, `chatId`, `msgIds`). The Python client handles this automatically.
- **Incoming data** (`get_message` returns `AttrDict`):
  - JSON `viewType` → Python `view_type`
  - JSON `fromId` → `from_id`, `chatId` → `chat_id`, `fileMime` → `file_mime`, etc.
  - Group status comes from `get_basic_chat_info` (`chat_type == "Group"`), not from messages.
- **Outgoing data** (`MsgData` dataclass → JSON via `_snake2camel`):
  - `quoted_message_id` → `quotedMessageId`
  - `override_sender_name` → `overrideSenderName`
  - `viewtype` stays `viewtype`

Useful `jq` examples:

```bash
jq '[.methods[].name]' deltachat-rpc-openrpc.json
jq '[.methods[] | select(.name | contains("send")) | {name, params: [.params[].name]}]' deltachat-rpc-openrpc.json
jq '.methods[] | select(.name == "markseen_msgs")' deltachat-rpc-openrpc.json
jq '.components.schemas.BasicChat' deltachat-rpc-openrpc.json
```

## Hermes MessageEvent Contract

Hermes routes incoming events by `message_type`. Set the correct type and populate `media_urls`/`media_types` for non-text content. Do **not** transcribe audio or analyze images inside the adapter.

| `MessageType` | When to use | Notes |
|---|---|---|
| `TEXT` | Plain text | Default |
| `VOICE` | Voice/opus message | Hermes auto-runs STT if `media_urls` set |
| `AUDIO` | Audio file attachment | Never STT |
| `PHOTO` | Image | Hermes routes to vision pipeline if `media_urls` set |
| `DOCUMENT` | File attachment | `.xdc` files become webxdc apps automatically |

Group messages carry additional context beyond the per-message sender: `metadata["is_group"]` is set, and `metadata["participants"]` is populated with `[{"name", "address"}, ...]` for every other member of the group (fetched via `get_chat_contacts`, self excluded, cached per `chat_id` for `_ROSTER_CACHE_TTL` seconds — see `_get_group_roster` in `adapter.py`). DMs never get a `participants` key.

## Deployment / Installation

The plugin is not a standalone executable; it is installed into the Hermes plugins directory:

```bash
git clone https://github.com/Simon-Laux/hermes-deltachat-platform ~/.hermes/plugins/deltachat-platform
hermes plugins enable deltachat-platform
```

Then create the Delta Chat account:

```bash
python3 ~/.hermes/plugins/deltachat-platform/setup.py
```

And start the gateway:

```bash
hermes gateway start
```

### NixOS Notes

- Install `deltachat-rpc-server` via `nix profile install nixpkgs#deltachat-rpc-server`.
- For voice calls, build a GC-rooted Python environment containing `aiortc`:
  ```bash
  nix build --impure --expr 'with import <nixpkgs> {}; python312.withPackages(ps: [ps.aiortc])' -o ~/.hermes/aiortc-env
  ```
  Then add its site-packages to `PYTHONPATH` in `~/.hermes/.env`.
- The `flake.nix` also provides a `packages.default` derivation that installs the plugin files to `$out/share/hermes/plugins/deltachat-platform/`.

## Environment / Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `DELTACHAT_RPC_SERVER` | Yes (unless in PATH) | `deltachat-rpc-server` | Path to RPC binary |
| `DELTACHAT_HOME_CHANNEL` | No | — | Chat ID for cron/proactive delivery |
| `DELTACHAT_ENABLE_RAW_RPC` | No | unset | Enables unrestricted `dc_rpc_call` tool |
| `DELTACHAT_DM_POLICY` | No | `pairing` | Direct message policy |
| `DELTACHAT_GROUP_POLICY` | No | `open` | Group chat policy |
| `DELTACHAT_ALLOWED_USERS` | No | — | Comma-separated allowed emails |
| `DELTACHAT_ALLOW_ALL_USERS` | No | `false` | Disable all sender checks |
| `DELTACHAT_SEND_REJECTION_REPLIES` | No | `true` | Send explanations on rejection |
| `DELTACHAT_RATE_LIMIT_MAX` | No | `30` | Max inbound messages per sender per window |
| `DELTACHAT_RATE_LIMIT_WINDOW` | No | `60` | Rate-limit window in seconds |
| `DELTACHAT_MAX_CONSECUTIVE_REPLIES` | No | `20` | Stop auto-replying to same sender after N in a row with no other participant (bot-loop guard); `<=0` disables |
| `DELTACHAT_HUMAN_USERS` | No | — | Comma-separated addresses treated as human check-ins for the bot-exchange guard |
| `DELTACHAT_MAX_BOT_EXCHANGES` | No | `12` | Stop auto-replying after N messages from non-`DELTACHAT_HUMAN_USERS` senders (bot-exchange guard); active only when `DELTACHAT_HUMAN_USERS` is set |
| `DELTACHAT_MAX_MESSAGE_LENGTH` | No | `3600` | Character limit per outbound message before splitting (100–10000) |
| `DELTACHAT_MAX_MESSAGE_LINES` | No | `20` | Line limit per outbound message (1–200); longer replies are markdown-stripped and split into ordered plain-text messages |
| `DELTACHAT_REQUIRE_MENTION` | No | `false` | Require `@mention` in group chats before responding |
| `DELTACHAT_MENTION_ALIASES` | No | — | Comma-separated extra names/forms that also count as a mention, beyond `DELTACHAT_DISPLAY_NAME` and its auto-tolerated case endings |
| `DELTACHAT_FREE_RESPONSE_CHANNELS` | No | — | Comma-separated group chat IDs exempt from `DELTACHAT_REQUIRE_MENTION` |
| `DELTACHAT_CALL_STT_VOXTRAL` | No | `false` | Use Mistral Voxtral cloud STT for calls |
| `DELTACHAT_CALL_MODEL` | No | unset | Per-call LLM override |
| `DELTACHAT_DEBUG` | No | unset | Enables debug logging for `deltachat2` |

## Common Gotchas

- **Never run `python3` bare on NixOS** — always use `nix develop --command ...`.
- **Logger names matter**: `hermes_plugins.deltachat` appears in `gateway.log`; `__name__` does not.
- **Account creation is separate from connection**: `setup.py` creates the account; the adapter only uses the first existing account.
- **Voice messages**: set `MessageType.VOICE` and pass the file path in `media_urls`; Hermes handles STT.
- **Outgoing calls need a dedicated loop**: if you modify call scheduling, keep media tasks on the `CallManager` loop, not the gateway loop.
- **Container path mapping**: output files from the Docker sandbox must be under `/workspace/`, not `/tmp/`, or the host cannot reach them.

## Useful RPC Methods

```python
markseen_msgs(account_id, [msg_id])       # read receipt
get_message(account_id, msg_id)           # full message snapshot
get_basic_chat_info(account_id, chat_id)  # name, chat_type
get_contact(account_id, contact_id)       # name, address, is_verified
send_msg(account_id, chat_id, MsgData)    # send any message type
accept_chat(account_id, chat_id)          # accept contact request
get_chat_securejoin_qr_code(account_id, None)  # invite link
```
