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

**Prerequisite:** [Hermes Agent](https://github.com/NousResearch/hermes-agent) must be installed first.

```bash
# 1. Install deltachat-rpc-server
pip install deltachat-rpc-server

# 2. Install aiortc (required for voice calls)
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
- Group chat support — drop the agent into any group
- Read receipts
- Bot mode: auto-accepts contact requests, no manual approval needed

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
| `dc_rpc_spec` | Full OpenRPC spec from the running server — all methods, params, types |
| `dc_chat_rpc_spec` | Spec filtered to chat-scoped methods, destructive ops removed |
| `dc_safe_rpc_call` | Call a chat-scoped method safely — `accountId` and `chatId` are injected from an opaque per-chat token; the AI cannot address a different chat |

Set `DELTACHAT_ENABLE_RAW_RPC=1` to also unlock `dc_rpc_call`, which reaches the whole
account rather than a single chat — only for trusted deployments. Even then it refuses
every `delete_*` / `remove_*` method, and every call it receives is logged at `WARNING`
so it shows up in `~/.hermes/logs/errors.log`. To narrow it further:

```bash
DELTACHAT_RAW_RPC_ALLOWLIST=get_account_info,get_chatlist_entries  # only these
DELTACHAT_RAW_RPC_BLOCKLIST=set_config,add_or_update_transport     # never these
```

An allowlist, when set, wins over everything else; the blocklist stacks on top of the
built-in `delete_*` / `remove_*` refusal.

---

## Installation

### 1. Install dependencies

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

aiortc brings in `av` (PyAV/libav for audio resampling), `aioice`, and Opus support — all required for the WebRTC call pipeline.

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
| `DELTACHAT_RAW_RPC_ALLOWLIST` | No | — | Comma-separated methods `dc_rpc_call` may call (unset = any non-destructive one) |
| `DELTACHAT_RAW_RPC_BLOCKLIST` | No | — | Comma-separated methods `dc_rpc_call` may never call |

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

The `deltachat2` Python package is vendored in `vendor/` to avoid a manual install step. To update it:
1. Fetch the latest from [adbenitez/deltachat2](https://github.com/adbenitez/deltachat2)
2. Copy `deltachat2/` contents to `vendor/deltachat2/`
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
