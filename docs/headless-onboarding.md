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
the adapter writes it to **`invite.txt`** in the Delta Chat accounts directory
(`<HERMES_HOME>/deltachat-platform/`), mode `0600`, and logs the *path* to it:

```bash
cat ~/.hermes/deltachat-platform/invite.txt
```

```
Delta Chat invite link written to /home/you/.hermes/deltachat-platform/invite.txt
```

### Why the link isn't in the log

Treat the invite link as a credential, not just an address.

Under the `pairing` DM policy — the sensible default for a bot — completing
SecureJoin is *what makes a contact verified*. So anyone holding this link can
pair with the agent and talk to it. Hermes creates `gateway.log` world-readable
(`0644`), and logs get shipped to aggregators, pasted into issues and captured
in support bundles, so the link stays out of it at INFO level.

It is still available when you need it:

- `--log-level DEBUG` logs the full link.
- If `invite.txt` can't be written, the link is logged at WARNING instead —
  with no file to point at, that's the only way to pair.
- In memory as `adapter._invite_link`, for whatever surfaces it later.

The file is the canonical copy. Hermes has no way to display it: adapters
report health through `write_runtime_status()`, whose schema is a fixed set of
fields (`platform_state`, `error_code`, `error_message`, `needs_attention`, …)
with no free-form slot, and `get_status()` — which the fork adds — is not a
Hermes hook and is never called by the gateway.

This is defence in depth, not a guarantee: anyone who can read the accounts
directory can read the file, and re-running `setup.py` prints the link again.
It also does **not** substitute for access control over who may talk to the
bot — `DELTACHAT_ALLOWED_USERS`-style policy is a separate concern this adapter
does not implement yet. With an allowlist in place, a leaked link is only good
for pairing; without one, on `pairing` policy, it is effectively agent access.

To pair from another device, turn it into a QR code:

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

## Changing the settings later

**The onboarding variables are read only when there is no configured account.**
Once an account exists and has a working transport, editing `DELTACHAT_EMAIL`
or `DELTACHAT_CHATMAIL_SERVERS` does nothing — the adapter uses the existing
account and never compares it against the env.

This is deliberate: the account carries a cryptographic identity your contacts
have verified, so a changed env var must not silently swap it for a new one.
It does mean a typo you fix later has no effect on an already-onboarded bot.

To actually move to a different address, either add a transport yourself
through a Delta Chat client, or delete the accounts directory
(`<HERMES_HOME>/deltachat-platform/`) and re-onboard from scratch — which gives
you a new identity and a new invite link, and every existing contact will have
to pair again.

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
