# Changelog

Notable changes for people running the adapter. Internal refactors, test
repairs and doc typo fixes are left out; see the git log for those.

## Unreleased (on `dev`)

### Breaking / requires action

- **Hermes 0.21.5 or newer is required** (`requires_hermes` in plugin.yaml).
  Hermes releases that know the key refuse to load the plugin on older
  versions. Older releases such as 0.15.x ignore it.
- **`deltachat-rpc-server` is pinned to `>=2.51.0,<2.61`.** Delta Chat core
  does not follow semver, and 2.61 removes fields this adapter reads. Hermes
  now installs the plugin's Python dependencies (`deltachat-rpc-server`,
  `aiortc`) itself and re-applies them after `hermes update`, so a Hermes
  update can no longer leave a mismatched pyOpenSSL behind. On NixOS, still
  set `DELTACHAT_RPC_SERVER`, and build `aiortc-env` from the installed Hermes's
  nixpkgs (see the README).
- **The adapter refuses to connect if it cannot determine the Delta Chat
  version.** It used to assume "compatible". The version warning now fires
  only above the newest verified core release, not on every connect.
- **`DELTACHAT_RAW_RPC_BLOCKLIST` is gone.** Use `DELTACHAT_RAW_RPC_ALLOWLIST`.
  An allowlist that is set but names nothing now allows nothing.
- **Group chats were being treated as DMs.** Group detection was broken. Groups
  are now recognized, so group-specific behaviour (including the new mention
  gate) applies to them for the first time.

### New

- **Headless account onboarding.** Set `DELTACHAT_EMAIL=auto` (chatmail),
  `DELTACHAT_EMAIL` + `DELTACHAT_PASSWORD` (existing mailbox), or
  `DELTACHAT_CHATMAIL_SERVERS`. The adapter then creates its account on first
  connect, so Docker/systemd/NixOS deployments no longer need anyone to run
  `setup.py` by hand. Auto mode registers on several relays for redundancy. The
  invite link is written to `invite.txt` (mode 0600) in the accounts dir and is
  kept out of the gateway log.
- **Optional mention gate for groups** (`require_mention`, `mention_aliases`
  in config.yaml, or `DELTACHAT_REQUIRE_MENTION` / `DELTACHAT_MENTION_ALIASES`).
  When on, the bot only sees group messages that mention `@<display name>` or
  an alias, or that quote-reply to one of its own messages. DMs are never gated.
  This only filters what reaches the bot and is not a privacy boundary: the
  RPC tools can still read every message.
- **Telegram-style command addressing in groups.** `/reset@<name>` reaches only
  the bot with that name, so one `/reset` no longer resets every bot in a
  multi-bot group.
- **Videos are sent as videos.** They used to arrive as a "couldn't send video"
  notice.
- **Voice calls hang up on goodbye.** The bot ends its goodbye with a
  `[[hangup]]` marker, so the goodbye plays out and then the call ends. If you
  set a custom `DELTACHAT_CALL_PROMPT`, it must keep this instruction.

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
- **Replies to the "call ended" note leaked into chat or failed to send.** The
  adapter now recognizes them by what they reply to instead of by a counter. A
  non-numeric `reply_to` now sends without a quote instead of failing the send.
- **Delivery failures (`MSG_FAILED`) are logged with their chat and reason.**
- **Startup no longer freezes for up to 10 s** when the chatmail relay list
  is slow to load.

### Security hardening of the RPC tools

- `dc_rpc_call` (only registered with `DELTACHAT_ENABLE_RAW_RPC`) and
  `dc_safe_rpc_call` now refuse 20 methods: every `delete_*`/`remove_*`, plus
  methods that do the same damage under other names (ephemeral timers,
  `add_contact_to_chat`, `block_chat`, `leave_group`), that reach outside the
  chat (`forward_messages`), or that leak credentials or hide traffic (secure-join
  QR codes, location streaming, mute/archive, `place_outgoing_call`).
- `DELTACHAT_ENABLE_RAW_RPC=0`, `false` or `off` now actually disables the tool.
  Before, any non-empty value enabled it.
- Every raw RPC call is audit-logged at WARNING as ACCEPTED or REFUSED.
- `dc_safe_rpc_call` binds parameters by name, so a method whose `chatId` is not
  the second parameter can no longer be pointed at a chat the agent has no
  token for.
- `dc_rpc_spec` / `dc_chat_rpc_spec` only list methods the tools will accept.

### Known gaps

- `send_msg`, `misc_send_msg` and `misc_set_draft` take a `file` path that
  bypasses delivery-path filtering (#32).
- `require_mention` applies to all groups; individual groups cannot be
  exempted yet.
