# Changelog

Notable changes for people running the adapter, as a short overview; the git log
has the details. Internal refactors, test repairs and doc typo fixes are left out.
Until a release, `Unreleased` describes the end state: a later change to something
already listed rewrites that entry instead of adding another.
Breaking changes are the exception: they go first, under `### Breaking / requires
action`, and get as much space as people need to upgrade safely.

## 2.1.0 (2026-10-10)

### Breaking / requires action

- **NixOS with voice calls: rebuild `~/.hermes/aiortc-env`.** Calls now
  need `numpy` in it, and its Python has to match Hermes's: 3.14 since
  Hermes 0.21.6, 3.12 for 0.21.5. An env built with the 2.0.0 instructions
  (Python 3.12, no `numpy`) leaves calls disabled, with an ERROR in
  `gateway.log`; text messaging keeps working. Rerun the build command in
  [docs/nixos-installation.md](docs/nixos-installation.md) and update the
  `python3.x` in the `PYTHONPATH` line. Other installs get `numpy` and `av`
  from `plugin.yaml` automatically.
- **`stt.provider: local` calls use `stt.local.model`.** Calls used to force
  Whisper `medium`; set `stt.local.model: medium` to keep that.

### New

- **"Call me" works.** Asked in a chat, the agent rings that chat; it can
  also schedule a call for later, and never starts a second call into a chat
  that is already on one.
- **Answer with reactions.** 👍 approves and 👎 cancels slash-command
  confirmations (`/reset`, `/new`, `/model`, …), and 1️⃣–9️⃣ picks an option
  when the agent asks you to choose.
- **The bot sees which message you replied to.** Quote-replies pass the
  quoted message and its author to Hermes, for text, voice, image and file
  messages.
- **The text chat can look up what was said on a call.** The "call ended"
  note names the call's session, so the bot can read the transcript when
  asked.
- **Optional message editing, experimental** (`DELTACHAT_MESSAGE_EDITING`).
  Streamed replies, tool progress and approval prompts are edited in place,
  at most once per `DELTACHAT_EDIT_MIN_INTERVAL` seconds (default 3). With
  editing off, replies are no longer streamed and arrive as one message.

### Webxdc skill

The bundled webxdc skill was reworked, so apps the bot builds work the first
time far more often.

- **The agent finds and loads the skill reliably.** It is called by the name
  Hermes registers (`deltachat-platform:webxdc-converter`) and shows a
  description in the skill list.
- **Every `.xdc` is checked before sending.** One script packages the app and
  flags a misplaced `index.html`, a bundled `webxdc.js` and anything loaded
  from the network.
- **The API reference matches the webxdc spec** and lists Delta Chat's update
  limits and quirks; apps always load `webxdc.js` and get a real PNG icon.
- **Safer, sturdier apps.** User text is escaped, baked-in data ships as a
  JSON file, the skill explains last-writer-wins vs. event sourcing for shared
  state, and the bot says when it hasn't opened an app itself.
- **The agent can react to your message.** A new `dc_react` tool lets it
  answer with an emoji (❤️ for a thank-you, 😂 for a joke) instead of, or as
  well as, a reply. It only reacts to the latest message of the person it is
  answering, in that chat.

### Fixed

- **Voice calls work on a fresh install.** Missing `numpy` left calls deaf,
  the first call got no Whisper warmup, and "OK", "Thanks" or "Bye" were
  dropped as hallucinations. Missing call libraries now disable only calls,
  and STT, TURN and crash problems are logged at ERROR.
- **Calls speak the whole streamed reply.** With streaming on, only its end
  or nothing was spoken; "Let me check…" before a tool now plays first, never
  over the answer.
- **Calls on cloud STT providers failed with "Invalid model: medium".** Calls
  now use the configured STT provider and model, like voice messages.
- **Calls end when they should.** A call whose peer vanished ends after about
  30 s, and `dc_end_call` hangs up the call of the chat that asked, not one in
  another chat.
- **Call authorization.** A call is declined when Hermes' authorization check
  fails, and declined callers get the same reply as an unknown contact's
  message (a pairing code by default).
- **A hung `deltachat-rpc-server` no longer freezes the gateway** on shutdown
  or after a malformed line; it gets 30 s to close cleanly, then is killed.
- **Attachments are sent once.** A file mentioned twice, under two spellings
  or as a bare `/workspace/…` path is no longer sent again, and text after a
  `MEDIA:` tag is kept.
- **Hermes' inbound size cap (`gateway.max_inbound_media_bytes`) applies to
  every attachment.** Oversized files reach the agent as a note, and videos
  arrive as videos instead of documents.

### Changed

- **Long replies are split into several messages** of at most 38 lines,
  instead of one folded "Show full message".
- **Message content stays out of the default logs.** Captions, transcripts
  and file names are logged only at DEBUG.

## 2.0.0 (2026-10-05)

### Breaking / requires action

- **Hermes 0.21.5 or newer is required** (`requires_hermes` in plugin.yaml).
  Hermes releases that know the key refuse to load the plugin on older
  versions. Older releases such as 0.15.x ignore it.
- **`deltachat-rpc-server` is pinned to `>=2.51.0,<2.61`.** Delta Chat core
  does not follow semver, and 2.61 removes fields this adapter reads. The
  plugin now declares its Python dependencies (`deltachat-rpc-server`,
  `aiortc`), so Hermes installs them and re-applies them after
  `hermes update`, and an update can no longer leave a mismatched pyOpenSSL
  behind. On NixOS, still
  set `DELTACHAT_RPC_SERVER`, and build `aiortc-env` from the installed Hermes's
  nixpkgs (see the README).
- **The adapter refuses to connect if it cannot determine the Delta Chat
  version.** It used to assume "compatible". The version warning now fires
  only above the newest verified core release, not on every connect.
- **Group chats were being treated as DMs.** Group detection was broken. Groups
  are now recognized, so group-specific behaviour (including the new mention
  gate) applies to them for the first time. In particular, Hermes now keeps a
  separate conversation per group member by default; set
  `group_sessions_per_user: false` for one shared conversation per group.
- **The adapter refuses to start on a Delta Chat database that isn't Hermes'
  own.** Hermes keys pairing approvals, sessions, `DELTACHAT_HOME_CHANNEL` and
  cron targets on Delta Chat contact and chat IDs, which are only meaningful
  inside one database. A recreated database could hand an approved contact's ID
  to a stranger. The two are now paired by an ID stored in the account
  (`ui.hermes.db_id`) and in `<HERMES_HOME>/.deltachat-db-id`. On a mismatch the
  adapter stops with steps to clear the stale state. Existing installs adopt
  their current database on first start. Restoring an *older backup* of the
  same database is not detected (see the README).
- **Senders without a key are dropped, unread.** Identity in Delta Chat is the
  key, so unencrypted mail is ignored before any read receipt.
- **The bot leaves groups in which Hermes approves no member.** Existing
  installs: such a group is left on its next message. Someone who wants to add
  the bot should message it directly first and get approved.
- **Calls from contacts Hermes hasn't approved are declined** instead of
  answered.

### New

- **The bot's profile bio lists its slash commands**, below a `Hermes commands:`
  line, so people can look them up in its profile. Text you put
  above that line is kept; the list below it is rewritten on connect when the
  commands change. Admin-only commands are left out when `allow_admin_from`
  is set. `DELTACHAT_COMMANDS_BIO=0` (or `commands_bio: false`) turns it off
  and takes the list out again.
- **Headless account onboarding.** Set `DELTACHAT_EMAIL=auto` (chatmail),
  `DELTACHAT_EMAIL` + `DELTACHAT_PASSWORD` (existing mailbox), or
  `DELTACHAT_CHATMAIL_SERVERS`. The adapter then creates its account on first
  connect, so Docker/systemd/NixOS deployments no longer need anyone to run
  `setup.py` by hand. Auto mode registers on several relays for redundancy.
- **The invite link is written to `invite.txt`** (mode 0600) in the accounts
  dir on every connect, however the account was set up. The gateway log only
  names the file at INFO; the link itself appears at DEBUG, or at WARNING if
  the file cannot be written.
- **Optional mention gate for groups** (`require_mention`, `mention_aliases`
  in config.yaml, or `DELTACHAT_REQUIRE_MENTION` / `DELTACHAT_MENTION_ALIASES`).
  When on, the bot only sees group messages that mention `@<display name>` or
  an alias, or that quote-reply to one of its own messages. DMs are never gated.
  This only filters what reaches the bot and is not a privacy boundary: the
  RPC tools can still read every message.
- **Telegram-style command addressing in groups.** `/reset@<name>` reaches only
  the bot with that name. With `require_mention` on, a bare `/reset` in a
  group is ignored; with it off (the default), a bare command still reaches
  every bot.
- **Videos are sent as videos.** They used to arrive as a "couldn't send video"
  notice.
- **Voice calls hang up on goodbye.** The bot ends its goodbye with a
  `[[hangup]]` marker, so the goodbye plays out and then the call ends. If you
  set a custom `DELTACHAT_CALL_PROMPT`, it must keep this instruction.
- **Approval prompts can be answered with reactions.** React 👍 to approve once
  or 👎 to deny; `/approve` and `/deny` still work. A reaction only answers the
  prompt it was given to, and only from someone who could have typed
  `/approve` in that chat. Prompts arriving during a voice call are not
  delivered.

### Fixed

- **Replies with files or local paths crashed** with
  `TypeError: … unexpected keyword argument 'session_key'` on newer Hermes.
  They now work on both old and new Hermes cores.
- **File delivery outside Docker.** The agent was told to write files to
  `/workspace/`, which only exists in the Docker sandbox. It now writes to its
  working directory, and `.xdc` paths anywhere are picked up. Paths that only
  appear in prose or code samples are no longer sent as attachments.
- **Cron delivery to a chat failed** with `invalid literal for int()`, because
  the scheduled job carried a chat token instead of a numeric id. Tokens are
  now resolved on every send path.
- **The bot no longer goes deaf when `deltachat-rpc-server` dies.** Before, the
  event listener hung silently forever while the gateway still showed
  "connected". Now RPC calls fail fast, and the adapter reports a fatal,
  retryable error so that Hermes's reconnect watcher rebuilds it with a fresh
  server.
- **Each voice call gets its own session.** All calls used to share one
  ever-growing thread, which was slow and made the model copy old replies
  instead of following the call prompt. The text chat still gets the
  end-of-call note.
- **Voice calls no longer read status messages aloud.** Memory-update
  notices, tool progress, busy and "⏳ Working" notices, interim commentary
  and error notices were spoken like replies. Only the turn's final reply is
  spoken now. Text-chat messages sent during a call are unaffected.
- **Replies to the "call ended" note leaked into chat or failed to send.** The
  adapter now recognizes them by what they reply to instead of by a counter. A
  non-numeric `reply_to` now sends without a quote instead of failing the send.
- **Delivery failures (`MSG_FAILED`) are logged with their chat and reason.**
- **No more "Docker MEDIA path … did not resolve" warning on every file sent
  from the Docker sandbox.** Hermes now maps `/workspace/` paths to the host
  itself, so the adapter no longer copies them into `cache/documents/`. If you
  run with `HERMES_MEDIA_DELIVERY_STRICT=1`, add the sandbox directory to
  `HERMES_MEDIA_ALLOW_DIRS`, or files older than about 10 minutes are refused
  (see docs/troubleshooting.md).
- **`DELTACHAT_CALL_MODEL` was silently ignored on Hermes 0.21.5**, so calls
  ran on the default model. It applies again, and if the gateway can't be
  reached the adapter now logs a warning instead of a debug line. If your call
  model doesn't support reasoning, see docs/voice-calls.md: a global
  `reasoning_effort` makes it reject every call turn.
- **Clearer formatting instructions for the agent.** It is told not to use
  markdown, to keep replies under 40 lines (longer ones get cut into a
  "show full message" HTML part), and to send long output as a PDF or webxdc
  app instead.
- **The webxdc skill's snippets work outside Docker.** They wrote to
  `/workspace/` literally and now use the working directory (#3).

### Security hardening of the RPC tools

- `dc_rpc_call` (only registered with `DELTACHAT_ENABLE_RAW_RPC`) used to call
  any method. It and `dc_safe_rpc_call` now refuse 20 methods of the current
  core spec:
  - every `delete_*` / `remove_*` method
  - the same damage under other names: `set_chat_ephemeral_timer`,
    `add_contact_to_chat`, `block_chat`, `leave_group`
  - reaching outside the chat: `forward_messages`
  - leaking credentials or the device: secure-join QR codes, location streaming
  - hiding traffic: mute and archive
  - duplicating a dedicated tool: `place_outgoing_call` (use `dc_start_call`)
- **New `DELTACHAT_RAW_RPC_ALLOWLIST`** limits `dc_rpc_call` further to the
  methods it names. Set but naming nothing means nothing is allowed.
- `DELTACHAT_ENABLE_RAW_RPC=0`, `false` or `off` now actually disables the tool.
  Before, any non-empty value enabled it.
- Every raw RPC call is audit-logged at WARNING as ACCEPTED or REFUSED.
- `dc_safe_rpc_call` binds parameters by name, so a method whose `chatId` is not
  the second parameter can no longer be pointed at a chat the agent has no
  token for.
- `dc_rpc_spec` / `dc_chat_rpc_spec` no longer list the refused methods. They
  do not apply the allowlist.
- **File paths passed through `dc_safe_rpc_call` are validated** (#32).
  `send_msg`, `misc_send_msg`, `misc_set_draft`, `set_chat_profile_image` and
  `send_sticker` used to hand any local path to core, so a steered call could
  send `~/.hermes/.env` or the account database. Paths now go through Hermes's
  delivery policy. On top of that, every Hermes profile's Delta Chat folder and
  `logs/` are refused, compared by inode so a differently spelled path can't
  slip past. Unknown path-like parameters refuse the call.

### Known gaps

- `require_mention` applies to all groups; individual groups cannot be
  exempted yet.
- The opt-in raw `dc_rpc_call` does not validate file paths; only
  `dc_safe_rpc_call` does.
- A file in the Docker sandbox can be swapped for a symlink between Hermes's
  path check and Delta Chat core reading it. The fix belongs in Hermes (see
  docs/upstreaming-to-hermes.md).
