# Headless Onboarding

Create and configure the bot's Delta Chat account from environment variables on
first connect, with no terminal involved. Intended for Docker, systemd and
NixOS deployments where nobody is sitting at a prompt to run `setup.py`.

Interactive setup via `setup.py` still works and is still the nicer path when
you want to choose a relay yourself or manage several Hermes profiles — this
only removes the requirement.

## Opt-in

Headless onboarding is **off unless you set one of its env vars**. With none of
them set the adapter behaves as before: it refuses to start and points you at
`setup.py`.

That is deliberate. Creating an account is a side effect with a cost outside
this machine — it registers on somebody else's chatmail relay. If the accounts
directory ever looks empty when it shouldn't (wrong `HERMES_HOME`, unmounted
volume), silently minting a fresh account — losing the identity your contacts
have already verified — is a much worse outcome than refusing to boot.

## Configuration

| Variable | Description |
|---|---|
| `DELTACHAT_EMAIL` | `auto` to register a free chatmail account, or a real address to use an existing mailbox. Unset = headless onboarding disabled. |
| `DELTACHAT_PASSWORD` | Password for `DELTACHAT_EMAIL`. Required for a real address, unused for `auto`. |
| `DELTACHAT_CHATMAIL_SERVERS` | Comma-separated relays to register on. Implies `auto` if `DELTACHAT_EMAIL` is unset. |

### Chatmail account (no credentials needed)

```bash
DELTACHAT_EMAIL=auto
```

Delta Chat core supports several transports on one account, so `auto` registers
on **three** relays for redundancy: `nine.testrun.org` as a stable anchor, plus
two drawn at random from the live list at `chatmail.at/relays`. The anchor is
registered first, so the account ends up with the same first address across
rebuilds; the random extras mean a single relay disappearing doesn't take the
bot with it.

> Note: which of several transports Delta Chat treats as the account's primary
> address (the one that appears in the invite link) has not been verified here.
> If you need a specific address, pin a single relay with
> `DELTACHAT_CHATMAIL_SERVERS` and check `invite.txt` after the first run.

Registration is best-effort per relay — the first one that works is enough, and
individual failures only warn. Onboarding fails only if *every* relay refuses.

Pin the relays yourself to skip the random draw:

```bash
DELTACHAT_EMAIL=auto
DELTACHAT_CHATMAIL_SERVERS=nine.testrun.org,mehl.cloud
```

### Existing mailbox

```bash
DELTACHAT_EMAIL=bot@example.com
DELTACHAT_PASSWORD='very-secret'
```

Uses `add_or_update_transport`, which autoconfigures IMAP/SMTP from the address
where it can.

## Pairing: the invite link

**Creating the account is not enough to reach the bot.** Delta Chat enforces
end-to-end encryption, and the initial key exchange needs a SecureJoin invite
link — adding the bot's email address by hand does not work.

`setup.py` prints that link to your terminal. Headless there is no terminal, so
the adapter surfaces it three ways on every connect:

1. **The gateway log**, at INFO — `journalctl -u hermes` or `docker logs`:
   ```
   Delta Chat invite link: OPENPGP4FPR:...#a=bot@nine.testrun.org&...
   ```
2. **A file**, `invite.txt` in the Delta Chat accounts directory
   (`<HERMES_HOME>/deltachat-platform/`), mode `0600`:
   ```bash
   cat ~/.hermes/deltachat-platform/invite.txt
   ```
3. **In memory** as `adapter._invite_link`, for a future `get_status()`.

`invite.txt` is written `0600`, but the same link is also in `gateway.log`,
which Hermes creates world-readable (`0644`). Treat the link as a capability to
start an encrypted chat with the bot, not as a secret — anyone who can read the
gateway log can use it. Access control over who may actually *talk* to the bot
is a separate concern this adapter does not implement yet.

Open it on your phone, or turn it into a QR code to scan from another device:

```bash
qrencode -t ANSIUTF8 < ~/.hermes/deltachat-platform/invite.txt
```

## Password handling

`DELTACHAT_PASSWORD` is removed from the process environment once the transport
is configured successfully. Delta Chat core has persisted its own copy by then,
so nothing downstream needs the plain value — and `os.environ` is readable by
anything else running in the process (agent tooling included) and is inherited
by subprocesses.

It is **not** cleared when configuration fails, so that a reconnect can retry.
It is also not removed from `~/.hermes/.env`; this only shortens its lifetime in
memory.

## Failure behaviour

`add_account()` writes an account row *before* any transport is attached, so a
bootstrap that dies half way leaves an unusable account behind that
`get_all_accounts()` will hand back on the next boot. The adapter therefore
gates on `is_configured()` rather than on account existence: an existing but
unconfigured account is re-onboarded rather than used as-is or duplicated.

If an account exists, is unconfigured, and headless onboarding is *not* enabled,
the adapter fails with a message telling you to run `setup.py` or set
`DELTACHAT_EMAIL` — it will not create a second account.

## Example: systemd

```ini
[Service]
Environment=DELTACHAT_RPC_SERVER=/run/current-system/sw/bin/deltachat-rpc-server
Environment=DELTACHAT_EMAIL=auto
ExecStart=/run/current-system/sw/bin/hermes gateway start
```

First start registers the account and logs the invite link; later starts reuse
it. Nothing to run by hand.
