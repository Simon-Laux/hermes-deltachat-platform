# Delta Chat × Hermes — Your AI Assistant

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that adds **Delta Chat** as a gateway channel — so you can reach your AI by text, voice message, or live voice call from a decentralized encrypted messenger that needs no phone number or sign-up.

---

## Why Delta Chat?

Delta Chat is a decentralized private messenger with end-to-end encryption, and a great choice for running a personal AI assistant:

- **Private** — instant onboarding with no phone number, email, or other personal data required
- **No API key dance** — no BotFather, no token registration, no webhook setup; you own the account
- **End-to-end encrypted** — audited encryption safe against network and server attacks
- **Every platform** — Android, iOS, macOS, Windows, Linux (even mobile Linux phones and FreeBSD)
- **Sovereign** — run it with your own email address or server, or use a public [chatmail relay](https://delta.chat/en/chatmail)
- **FOSS** — fully open source, built on internet standards

## What can you do with it?

**Talk to your AI like a person:**
- Send text messages and get AI replies
- Send voice clips — automatically transcribed; the AI responds in text
- Share images and files for the AI to analyze or process
- Have full **voice calls** — call the AI, speak naturally, get a spoken response in real-time (WebRTC, Whisper STT, TTS)

**Let the AI reach out to you:**
- Hermes has built-in cron scheduling — set this chat as your home channel and scheduled tasks deliver here: daily briefings, reminders, status updates, without any user prompt
- The AI can **place outgoing voice calls** from those scheduled tasks — it will literally call you

**Build things together:**
- Ask the AI to build an interactive **webxdc mini-app** (.xdc) and it delivers it straight into the chat — no app store, no install, runs locally inside Delta Chat
- Send PDFs, HTML pages, or any file and the AI will handle them

**Advanced:**
- Drop the agent into a **group chat** to assist everyone
- Run **multiple independent agents** with their own Delta Chat accounts
- Give the AI sandboxed access to the full **Delta Chat JSON-RPC API** to automate your messaging directly

---

## Quick Start

**Prerequisite:** [Hermes Agent](https://github.com/NousResearch/hermes-agent) **0.21.5 or newer** must be installed first.

`plugin.yaml` declares `deltachat-rpc-server` and `aiortc` as `python_dependencies`, so a Hermes
that supports that manifest key installs them into its own venv when you enable the plugin — and
re-installs them after every `hermes update`. Steps 1 and 2 are only needed on older Hermes
releases, which ignore the key, and on NixOS — see
[docs/nixos-installation.md](docs/nixos-installation.md). A `No module named 'aiortc'` in
`~/.hermes/logs/gateway.log` means you are in that case.

```bash
# 1. Install deltachat-rpc-server (only if Hermes did not)
pip install deltachat-rpc-server

# 2. Install aiortc for voice calls (only if Hermes did not)
pip install aiortc

# 3. Clone plugin to Hermes
git clone https://github.com/Simon-Laux/hermes-deltachat-platform ~/.hermes/plugins/deltachat-platform

# 4. Enable plugin
hermes plugins enable deltachat-platform

# 5. Run setup — auto-detects your Hermes profiles, creates a Delta Chat account
python ~/.hermes/plugins/deltachat-platform/setup.py

# 6. Start gateway
hermes gateway start
```

No terminal to run step 5 in (Docker, systemd, NixOS)? Set `DELTACHAT_EMAIL=auto`
in `~/.hermes/.env` instead and skip it — the adapter registers a chatmail
account on first connect and logs the invite link. See
[docs/headless-onboarding.md](docs/headless-onboarding.md).

The setup script prints an **invite link** for your new agent. Scan or tap it in the Delta Chat app on your phone — this is required because Delta Chat enforces end-to-end encryption, and the invite link carries the key fingerprint needed to establish an encrypted session. Adding the address alone won't work.

---

## Features

Deep integration with Delta Chat's native features — voice messages, voice calls, mini-apps, and group chats all work out of the box.

### Messaging
- Bidirectional text, voice messages (auto-transcribed via Hermes STT), images, files, locations
- Group chat support — add the agent to a group you are in
- Read receipts
- Bot mode: auto-accepts contact requests, no manual approval needed
- Only end-to-end encrypted contacts reach the agent — in Delta Chat identity is the key, so
  unencrypted mail is dropped unread. Calls from contacts Hermes hasn't approved are declined.
  In a group where Hermes approves none of the members, the agent leaves (the group sees it
  leave); someone who wants to add it should message it directly first to get approved

### Group Chats
By default the agent answers every message in a group. Set `DELTACHAT_REQUIRE_MENTION=1` and it
only reacts to group messages that mention it (`@<display name>` or an alias from
`DELTACHAT_MENTION_ALIASES`) or quote-reply to one of its messages. DMs are never gated.

**Commands in groups** are addressed Telegram-style: `/reset@<name>`, where `<name>` is the
bot's display name or an alias. Only that bot runs it; a command addressed to another name is
ignored. With `DELTACHAT_REQUIRE_MENTION` on, a bare `/reset` in a group is ignored too, so with
several bots in one group nothing gets reset by accident (unless it is a quote-reply to that bot,
which counts as addressing it). In DMs `/reset@<name>` works too. Give a bot whose display name contains
spaces a one-word alias: `/reset@Hermes Bot` works, but if another bot in the group is called
"Hermes", it will take that command (and `@Hermes Bot` mentions) as meant for itself too.

Messages without a mention are dropped before they reach Hermes, so the agent does not see them
as conversation context either.

Hermes keeps a separate conversation per group member by default, so the agent remembers what
it discussed with each person, but not what it told someone else in the same group. Set Hermes'
`group_sessions_per_user: false` for one shared conversation per group.

This is **not a privacy or security boundary**: the messages are still stored in the bot's Delta
Chat account, and the agent's Delta Chat tools may still reach them in some form. Use it to keep
the bot quiet, not to keep anything from it.

Would you rather have the agent see all group messages as context and still only answer when
mentioned? That isn't implemented yet —
[open an issue](https://github.com/Simon-Laux/hermes-deltachat-platform/issues/new) if you want it.

`DELTACHAT_REQUIRE_MENTION` applies to all groups; exempting single groups is not supported yet — open an issue for that too.

### Who can talk to it
Hermes decides that, not this plugin: someone writing to the bot for the first time gets a
pairing code, and the agent only answers them after you run
`hermes pairing approve deltachat-platform <code>` on the host. In groups, messages from members
you haven't approved are ignored.

Hermes stores those approvals under Delta Chat's contact IDs, which only mean something inside
one Delta Chat database. If that database is lost and recreated, the same IDs go to other
people — so the plugin pairs the database with Hermes' state (`ui.hermes.db_id` in the account,
`.deltachat-db-id` in the Hermes profile directory) and refuses to start when they don't
match. The error tells you what to clear to start over. Installs upgrading to this version adopt
their current database as-is; the check protects from then on.

It can't tell an *older backup* of the same database from the current one: after restoring one,
contacts approved since that backup was made can lose their IDs to new people. Restore Hermes'
state from the same point in time, or clear the approvals as the error message describes.

### Voice Calls (WebRTC)
- **Incoming calls**: auto-answer, live speech-to-text → AI → text-to-speech pipeline
- **Outgoing calls**: the AI can call you from a scheduled task (`dc_start_call` tool)
- Barge-in support: interrupt the AI mid-sentence and it adapts
- Per-call isolated AI session with optional model override and system prompt
- Optional Voxtral cloud STT for fast (~1–2s) transcription

### Proactive Messaging & Cron
Hermes has built-in cron scheduling. To route scheduled task delivery to a Delta Chat chat, set it as the home channel. From within the chat, type:

```
/sethome
```

Or set it manually via env var:

```bash
echo 'DELTACHAT_HOME_CHANNEL=<chat_id>' >> ~/.hermes/.env
```

From there you can schedule daily briefings, reminders, or any recurring task — and the AI can also place outgoing voice calls from those tasks.

### Webxdc Mini-Apps
Ask the AI to build a small interactive app (a game, a form, a calculator, a data viewer) and it delivers a `.xdc` file straight into the chat. The app runs locally inside Delta Chat — no server, no install. Built-in `webxdc-converter` skill handles the packaging.

### Raw Delta Chat API (Advanced)
Three tools are always available once the plugin is loaded:

| Tool | Description |
|------|-------------|
| `dc_rpc_spec` | OpenRPC spec from the running server — params and types for every method the RPC tools will accept; refused methods are omitted |
| `dc_chat_rpc_spec` | Spec filtered to chat-scoped methods, refused ops removed |
| `dc_safe_rpc_call` | Call a chat-scoped method safely — `accountId` and `chatId` are injected from an opaque per-chat token; the AI cannot address a different chat |

Set `DELTACHAT_ENABLE_RAW_RPC=1` to also unlock `dc_rpc_call`, which reaches the whole
account rather than a single chat — only for trusted deployments. Even then it refuses
every `delete_*` / `remove_*` method, and every call it receives is logged at `WARNING`
so it shows up in `~/.hermes/logs/errors.log`. To narrow it further:

```bash
DELTACHAT_RAW_RPC_ALLOWLIST=get_account_info,get_chatlist_entries  # only these
```

Refused everywhere, in both `dc_rpc_call` and `dc_safe_rpc_call`: any `delete_*` /
`remove_*` method, plus twelve named for what they do rather than what they are called —

| | |
|---|---|
| `leave_group`, `set_chat_ephemeral_timer` | destroy or detach (the timer is timed deletion) |
| `forward_messages`, `add_contact_to_chat` | reach outside the chat the token scopes |
| `get_chat_securejoin_qr_code`(`_svg`) | the QR text *is* the group invite |
| `send_locations_to_chat` | streams real device location |
| `block_chat`, `set_chat_mute_duration`, `set_chat_visibility` | hide traffic from the gateway |
| `place_outgoing_call`, `init_webxdc_integration` | reachable better via `dc_start_call`, or internal plumbing |

Reading locations contacts chose to share (`get_locations`) stays allowed; only
broadcasting ours is refused.

That refusal always applies — allowlisting such a method does
not re-enable it. Leaving the allowlist blank allows any method not refused above; setting
it to anything that names no methods allows nothing, rather than quietly allowing all.

Note this is still a rule about method *names*, so it is narrow: it refuses 20 of the 177
spec methods. It cannot see `set_config(delete_device_after)`, which wipes the whole
message store under an innocuous name, nor the `file` parameter on `send_msg`, which takes
any local path. Set `DELTACHAT_RAW_RPC_ALLOWLIST` if you want a real bound on what the
agent can reach.

RPC errors come back to the agent verbatim so it can correct a malformed call. That
discloses nothing a tool with `get_message` and `get_system_info` did not already
grant, and refused methods never reach the server to produce one.

---

## Installation

### 1. Install dependencies

Both are declared in `plugin.yaml` under `python_dependencies`, so on a Hermes that supports that
key you can skip this whole step — PM resolves them against Hermes' own pins when the plugin is
enabled, which is what keeps `pyOpenSSL` in step with the `cryptography` version Hermes pins. Do it
by hand only on releases that ignore the key, or on NixOS where the pip wheels do not work
([docs/nixos-installation.md](docs/nixos-installation.md)).

#### deltachat-rpc-server

**pip (recommended):**
```bash
pip install deltachat-rpc-server
```

**From source:**
```bash
git clone https://github.com/chatmail/core
cd core
cargo build -p deltachat-rpc-server --release
# Binary: target/release/deltachat-rpc-server
```

**NixOS:**
```bash
nix profile install nixpkgs#deltachat-rpc-server
echo 'DELTACHAT_RPC_SERVER=/home/work/.nix-profile/bin/deltachat-rpc-server' >> ~/.hermes/.env
```

#### aiortc (required for voice calls)

**pip:**
```bash
pip install aiortc
```

**NixOS** (add to your `python3.withPackages` in flake.nix):
```nix
(python3.withPackages (ps: with ps; [ deltachat2 aiortc ]))
```

aiortc brings in `av` (PyAV/libav for audio resampling), `aioice`, and Opus support — all required
for the WebRTC call pipeline. `call_handler.py` imports `av` directly but only aiortc is declared,
so `av` arrives as a transitive dependency.

### 2. (Optional) Configure RPC server path

If the binary is not in PATH:
```bash
echo 'DELTACHAT_RPC_SERVER=/path/to/deltachat-rpc-server' >> ~/.hermes/.env
```

### 3. Enable Plugin

```bash
hermes plugins enable deltachat-platform
```

### 4. Create Account

```bash
python ~/.hermes/plugins/deltachat-platform/setup.py
```

The script auto-detects your Hermes profiles, lets you pick one, creates the DC account, and prints an **invite link**. Scan or tap it in Delta Chat — do not just add the email address manually, as the invite link is required for encrypted key exchange.

**Or onboard without a terminal.** Set `DELTACHAT_EMAIL=auto` (chatmail account) or `DELTACHAT_EMAIL` + `DELTACHAT_PASSWORD` (your own mailbox) in `~/.hermes/.env`, and the adapter configures the account itself on first connect — no `setup.py` run needed. The invite link is written to the gateway log and to `invite.txt` in the accounts directory. Full details, including relay selection and password handling, in [docs/headless-onboarding.md](docs/headless-onboarding.md).

### 5. Start the gateway
```bash
hermes gateway start
```

---

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DELTACHAT_RPC_SERVER` | No | `deltachat-rpc-server` | Path to RPC binary |
| `DELTACHAT_HOME_CHANNEL` | No | — | Chat ID for cron/proactive delivery (or use `/sethome` in chat) |
| `DELTACHAT_ENABLE_RAW_RPC` | No | — | Expose the account-wide `dc_rpc_call` tool |
| `DELTACHAT_RAW_RPC_ALLOWLIST` | No | — | Comma-separated methods `dc_rpc_call` may call (blank = any method not refused above) |
| `DELTACHAT_REQUIRE_MENTION` | No | — | In **group** chats, only answer messages that say `@<display name>` (or an alias), or quote-reply to the bot; commands must be addressed as `/cmd@<name>`. DMs are never gated. Also `platforms.deltachat-platform.require_mention: true` in `config.yaml` |
| `DELTACHAT_MENTION_ALIASES` | No | — | Comma-separated extra names that count as a mention (`@<alias>`); also `platforms.deltachat-platform.mention_aliases` |
| `DELTACHAT_COMMANDS_BIO` | No | on | Append the slash commands that work in Delta Chat to the bot's profile bio, below a `Hermes commands:` line, one `/cmd args – what` per line so people can look them up in its profile. Your own text above that line is kept. Commands that only admins may run (`allow_admin_from`) are left out. `0` turns it off and takes the list out again, which saves ~4.9 KB per message: Delta Chat isn't optimized for long bios and sends the whole bio with every message, not only now and then like the avatar. Also `platforms.deltachat-platform.commands_bio` |

### Multiple Agents

Each Hermes profile gets its own Delta Chat account:

```bash
hermes profile create work
hermes profile create personal

hermes -p work gateway start
hermes -p personal gateway start
```

---

## Documentation

- [Voice Calls](docs/voice-calls.md) — setup, tuning, TURN servers, Voxtral STT
- [Version Compatibility](docs/version-compatibility.md) — version requirements
- [File Structure](docs/file-structure.md) — directory layout
- [NixOS Installation](docs/nixos-installation.md) — NixOS-specific setup
- [Troubleshooting](docs/troubleshooting.md) — common issues

---

## Development

The `deltachat2` Python package is vendored in `vendor/` to avoid a manual install step.

**`vendor/deltachat2/` has diverged from upstream — do not blindly overwrite it.** Local patches that a straight copy would silently drop:

- `transport.py`: dead-RPC-server handling — `_fail_all_pending()` from both reader and writer loops, `_server_dead()` probe, and a polled `_Result.wait()` so a `deltachat-rpc-server` that dies mid-call raises instead of hanging the caller forever. Upstream has none of this.
- `transport.py`: `to_attrdict()` on RPC results (camelCase → snake_case, which `adapter.py` depends on) and `close()` guards for the never-started / already-dead cases.
- `transport.py`: bounded teardown — `close(timeout=5, stop_io_timeout=35)` caps the `stop_io_for_all_accounts` call (via `_call(..., timeout=)`; 35 s is above core's own 30 s IMAP/SMTP shutdown budget) and every later wait, and SIGKILLs a server that hasn't exited after stdin EOF (no SIGTERM step: rpc-server treats EOF and SIGTERM alike). Worst case 60 s, about 40 s for a wedged server. `close()` blocks, so `adapter.py` runs it in a worker thread and a reconnect waits for it to release `accounts.lock`. A reader that dies on a malformed line kills the server so the dead-server handling takes over.
- `IOTransport.__init__` takes `rpc_server=`; upstream renamed this kwarg to `rpc_executable=`. `adapter.py` passes `rpc_server=`.

To update it:
1. Fetch the latest from [adbenitez/deltachat2](https://github.com/adbenitez/deltachat2)
2. Merge upstream changes into `vendor/deltachat2/` **without** discarding the patches above — diff, don't copy
3. Test thoroughly — API changes can affect compatibility
4. Update the minimum version check in `adapter.py` if needed

---

## License

Mozilla Public License 2.0 (MPL-2.0)

---

## Vibecoding

This project was built with heavy AI assistance — a mix of Claude, Mistral, and OpenCode models did most of the heavy lifting, with Mistral Medium 3.5 and Claude Opus doing the bulk of the work. The human role was management and quality assurance: directing, testing features, and catching what broke. It should be reasonably stable — but this is an experimental community project provided as-is, with no guarantees.

---

## References

- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [Delta Chat](https://delta.chat/)
- [deltachat2 PyPI](https://pypi.org/project/deltachat2/)
- [deltachat-rpc-server](https://github.com/chatmail/core/tree/main/deltachat-rpc-server)
- [Delta Chat JSON-RPC API](https://github.com/chatmail/core/blob/main/deltachat-jsonrpc/src/api.rs)
- [Webxdc](https://webxdc.org/)
- [aiortc](https://aiortc.readthedocs.io/)
