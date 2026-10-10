# Changelog

Notable changes for people running the adapter. Internal refactors, test
repairs and doc typo fixes are left out; see the git log for those.

## Unreleased

### New

- **Slash-command confirmations can be answered with reactions.** When
  `/reset`, `/new`, `/undo`, `/reload-mcp` or a costly `/model` asks for
  confirmation, react 👍 to approve once or 👎 to cancel. "Always approve"
  turns the prompt off for good in `config.yaml`, so it stays typed-only:
  `/always`. The same contacts as for approval prompts may react, and
  `allow_admin_from` applies. The command's reply quotes the prompt.
- **Questions with up to 9 choices can be answered with a reaction.** When
  the agent asks you to pick an option, the choices are numbered 1️⃣–9️⃣;
  react with one to answer. Typing the number, the option text or your own
  answer still works. Multi-select questions and those with more choices
  keep the plain numbered list.
- **The text chat can look up what was said on a call.** The "call ended"
  note sent to the text-chat session now names the call's Hermes session,
  so the bot reads the transcript with `session_search` instead of saying it
  has no record of the call. Shared-history mode is unchanged.
- **The webxdc skill shows two ways to structure shared state.** For
  multi-user apps built on plain `sendUpdate`, it now compares
  last-writer-wins (one key per user, e.g. polls) with event sourcing (send
  actions and replay them, e.g. games), and points to Yjs when several
  people edit the same data at once.
- **The webxdc skill checks the `.xdc` before sending it.** One script now
  packages the app and reports an error if `index.html` isn't at the
  archive root, if `webxdc.js` was packaged, or if the app loads anything
  from the network (CDN scripts and modules, web fonts — webxdc apps are
  offline, so these break the app). It leaves out dotfiles and
  `node_modules` and notes archives over 1 MB and 10 MB. Generated icons are 256 px instead of 128. When the bot sends
  a new version of a shared-state app it now tells you the old data stays
  in the previous message.
- **The webxdc skill guards against unsafe or broken app content.** It
  tells the bot to show user text with `textContent` instead of unescaped
  `innerHTML` (XSS in shared apps), to ship baked-in data as a JSON file
  loaded with `fetch`, and to say when that data will go stale. When the
  bot has an image generator it may draw the icon with it, and the skill
  explains how to prompt it and how to catch an image whose format doesn't
  match its file name. The bot now
  says when it hasn't opened an app itself, and it makes changes in the
  sources rather than only inside the `.xdc`.
- **Optional message editing, experimental** (`DELTACHAT_MESSAGE_EDITING`, off by default).
  Hermes can then edit streamed replies, tool progress, heartbeats and
  approval prompts in place. Every edit is an email through the chatmail
  relay, so in-progress edits are limited to one per
  `DELTACHAT_EDIT_MIN_INTERVAL` seconds (default 5) for the whole account;
  the final text always goes out. See the README. (#54)

### Fixed

- **Voice calls never heard the caller on a plain install.** The call code
  needs `numpy`, which neither aiortc nor Hermes's default install brings
  in; the receive loop died on its first frame without a log line, so the
  bot greeted and then never answered. `plugin.yaml` now declares `numpy`
  and `av`, and a crash in a call's background task is logged at ERROR.
- **Call problems on a fresh instance are now visible in `gateway.log`.**
  A failed transcription (no STT provider, failed model download) is logged
  at ERROR with Hermes's reason instead of being dropped at DEBUG. So are a
  missing TURN server and an SDP of ours without a relay candidate, which
  behind NAT mean the call will not connect.
- **Declined callers now hear back.** A call from a contact Hermes doesn't
  know yet used to be hung up silently. The call is still declined, but the
  caller now gets what an unknown contact's message gets: a pairing code by
  default, or the decline text, or nothing, per `unauthorized_dm_behavior`,
  rate-limited by Hermes.
- **The first incoming call on a fresh install had no Whisper warmup.** It
  wrote into a folder that didn't exist yet. The warmup also no longer runs
  on the call's event loop, where Hermes installing faster-whisper on first
  use stalled call setup for minutes.
- **"OK", "Thanks" and "Bye" spoken in a call were thrown away** as Whisper
  hallucinations, so a plain "bye" couldn't end the call. Calls only
  transcribe audio that is clearly speech, so these now reach the agent.
- **A missing aiortc no longer stops the whole adapter.** Text messaging
  connects and calls are disabled, with an ERROR saying what to install.
- **The agent couldn't load the bundled webxdc skill.** The system prompt
  told it to call `skill_view('plugin:deltachat-platform:webxdc-converter')`,
  which Hermes reads as a plugin called `plugin`, so the call returned "Skill
  not found". The prompt now uses `deltachat-platform:webxdc-converter`, the
  name Hermes actually registers the skill under.
- **The bundled webxdc skill showed no description in the skill list.**
  Hermes doesn't read it from the skill's `SKILL.md` for plugin skills, so
  the adapter now passes it along. The description also says it is the
  Delta Chat-specific webxdc skill, to tell it apart from any other webxdc
  skill installed alongside it.

- **The bot now sees which message you replied to.** A quote-reply used to
  reach Hermes as plain text, so the agent had to guess what "this" meant.
  The quoted message (in full, if it is from the same chat) and its author
  are now passed on, for text, voice, image and file messages alike.
- **Attachments could arrive twice.** A reply that sent a file with a
  `MEDIA:` tag and also mentioned its path ("saved at /…/app.xdc"), or
  mentioned one file under two spellings (`~/app.xdc` and its full path, or
  through a symlink), delivered it once per mention. It now goes out once,
  for every file type.
- **A webxdc app in the Docker sandbox could be re-sent on later replies.**
  A bare `/workspace/app.xdc` mention was sent without checking, so when
  Hermes could not tell it had already been delivered, the app went out
  again. A bare `/workspace/…` path is now left as text, as it already was
  for other file types; the agent sends files with a `MEDIA:` tag, as the
  webxdc skill tells it to.
- **Text after a `MEDIA:` tag for a `.xdc` could vanish.** In
  "MEDIA:/workspace/a b.xdc and /workspace/c.xdc" the tag ran on to the last
  `.xdc` of the line, so if the file could not be sent the rest of the
  sentence was cut from the reply.
- **Attachments over Hermes' size cap still reached the agent.** When
  Hermes refused to cache a file over `gateway.max_inbound_media_bytes`
  (default 128 MiB), the adapter passed the raw Delta Chat file instead, and
  documents and videos were never checked. Oversized attachments now reach
  the agent as text with a note that the file was too large. Set the option
  to 0 to turn the cap off.
- **Calls failed with "Invalid model: medium" on cloud STT providers.** With
  `DELTACHAT_CALL_STT_VOXTRAL` off, or after a Voxtral error, call audio was
  sent with the local Whisper size `medium` to whatever `stt.provider` is
  set. With `mistral` (or any other cloud provider) every utterance failed,
  and the bot never answered in calls. Calls now use the configured provider
  and model, the same as voice messages. The Voxtral shortcut honours
  `stt.mistral.model`. If you use `stt.provider: local`, calls now use
  `stt.local.model` instead of a forced `medium`. Set it explicitly if you
  relied on that.
- **Webxdc apps from the skill could ship without `webxdc.js`.** The skill
  now says plainly that any app using the webxdc API must load
  `<script src="webxdc.js"></script>` before its own scripts. The messenger
  provides the file, so it is still not packaged, but without the tag
  `window.webxdc` is undefined.
- **Webxdc apps from the skill had no icon.** The skill generated an
  `icon.svg`, but messengers only use `icon.png` or `icon.jpg`. It now
  generates a PNG.
- **The skill's webxdc API reference described functions that don't
  exist.** `desktopApiVersion`, `getAllInstanceIds()` and `sendToInstance()`
  are gone, and the signatures of `setUpdateListener`, `sendToChat`,
  `importFiles` and the realtime channel now match the webxdc spec. The
  reference also lists Delta Chat's actual update limits and quirks. The
  skill tells the bot not to use `alert`/`confirm`/`prompt` or
  `window.open`, and to show an error instead of a blank page when the app
  fails to start.

### Changed

- **Hermes no longer streams replies on Delta Chat while editing is off.**
  Before, streaming (if enabled in Hermes) sent the first chunk with a `▉`
  cursor that could never be removed, then the rest as a second message.
  Now the reply arrives as one message.

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
