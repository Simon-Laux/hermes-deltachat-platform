# Delta Chat plugin installed

Two more steps before the gateway can connect — the plugin cannot create your
Delta Chat account on its own yet.

## 1. Point at the RPC server

The adapter needs the `deltachat-rpc-server` binary. If you were not just
prompted for `DELTACHAT_RPC_SERVER`, set it by hand:

```bash
pip install deltachat-rpc-server        # or: nix profile install nixpkgs#deltachat-rpc-server
echo "DELTACHAT_RPC_SERVER=$(which deltachat-rpc-server)" >> ~/.hermes/.env
```

Voice calls additionally need `aiortc` (`pip install aiortc`). Everything else
works without it.

## 2. Create the Delta Chat account

Interactively — auto-detects your Hermes profiles, fetches the current public
relay list from `chatmail.at/relays` so you can pick one:

```bash
python ~/.hermes/plugins/deltachat-platform/setup.py
```

Or without a terminal — add one line to `~/.hermes/.env` and the adapter
registers the account itself on first connect:

```bash
echo "DELTACHAT_EMAIL=auto" >> ~/.hermes/.env
```

Either way is fine; do one of them. **Skip both and the gateway refuses to
connect**, logging `No Delta Chat accounts found`.

## 3. Enable and start

```bash
hermes plugins enable deltachat-platform
hermes gateway start
```

## Pair your phone

You need the **invite link** — `setup.py` prints it, and the headless path logs
it and writes it to `invite.txt` in the accounts directory. Scan or tap it in
the Delta Chat app; do not just add the bot's email address. Delta Chat
enforces end-to-end encryption, and the invite link carries the key fingerprint
needed to establish the session; adding the address alone will not work.

---

Full docs: `README.md`, `docs/headless-onboarding.md`,
`docs/troubleshooting.md`, `docs/voice-calls.md`.

<!--
  MAINTAINER NOTE — this file has limited reach.

  Hermes renders after-install.md via _display_after_install() in
  hermes_cli/plugins_cmd.py, which is called from exactly one place:
  cmd_install. So this file is shown ONLY to users who run

      hermes plugins install https://github.com/Simon-Laux/hermes-deltachat-platform

  It is NOT shown to anyone who:
    - git clones straight into ~/.hermes/plugins/ (the flow in our README), or
    - installs via the nix flake (docs/nixos-installation.md), which copies the
      tree to $out/share/hermes/plugins/ and then only runs
      `hermes plugins enable`.

  Both of those bypass cmd_install entirely. Keep the same instructions in
  README.md and docs/nixos-installation.md; this file is a convenience for the
  git-install path, not the canonical place to document setup.

  Rich strips HTML comments when rendering (verified against rich 14.3.3), so
  this note is invisible to users — it only costs a blank line in the panel,
  which is why it sits at the end of the file.

-->
