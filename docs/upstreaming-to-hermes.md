# Upstreaming: what we get by becoming a bundled Hermes platform plugin

Status: **not started.** This is a scouting document for a future PR against
`github:NousResearch/hermes-agent` that moves this adapter into
`plugins/platforms/deltachat-platform/`.

All references below were verified against Hermes **0.20.5** (tag
`v2026.8.19`, rev `fcbd107`), fetched with:

```bash
nix flake prefetch github:NousResearch/hermes-agent/v2026.8.19 --json
```

Related: `docs/fork-backport-notes.md` (fork feature survey — several items
there are prerequisites for a credible upstream PR). That file is
**gitignored / local-only** for now; the prerequisites it feeds are
summarised in §6 below, so this document stands on its own without it.

---

## 1. Integrated interactive onboarding

**This is the main prize.** Today our Delta Chat env vars are invisible to the
Hermes setup UI, and account creation is a separate manual script. Both are
consequences of living outside the Hermes tree.

### What changes automatically once bundled

`_inject_platform_plugin_env_vars()` (`hermes_cli/config.py:5981`) scans
`site-packages/plugins/platforms/*/plugin.yaml`, merges each manifest's
`requires_env` **and** `optional_env`, and injects them into
`OPTIONAL_ENV_VARS` — honouring `description`, `prompt`, `url`, `password`
and `category`. They then appear as editable fields in the `hermes config`
wizard.

It only scans the bundled directory; `~/.hermes/plugins/` is never read. So
this is a pure relocation win: our `plugin.yaml` already declares the
manifest entries (the fork's declares ~23 with hand-written prompts), and
they start working the moment the directory moves. Nothing to write.

The IRC manifest states the contract explicitly:

```yaml
# ``requires_env`` entries are surfaced in ``hermes config`` UI via the
# platform-plugin env var injector in ``hermes_cli/config.py``.
```

Bundled `kind: platform` plugins also **auto-load** — no `plugins.enabled`
entry required. Per `PluginManifest.kind`'s docstring, that gate exists for
user-installed plugins specifically because they are "untrusted code".

### What still needs building

Being bundled surfaces *env vars*. It does not give us an interactive
account-creation step — **Hermes has no plugin setup hook at all.** The only
post-install affordances are `_prompt_plugin_env_vars()` and a static
`after-install.md`, and neither can run our relay picker.

So the account-creation story has to be solved on our side regardless:

- Add the **headless fallback** in `connect()` (see
  `docs/fork-backport-notes.md` §1) so `DELTACHAT_EMAIL=auto` +
  `DELTACHAT_CHATMAIL_SERVER` is sufficient to come up unattended. This is
  what makes the plugin viable as a bundled default — a bundled platform that
  demands a manual TTY script before it works is not shippable.
- Keep `setup.py` as the opt-in richer path (live relay discovery from
  `chatmail.at/relays` via `scrape_relay_servers()`).

Optionally, propose a **plugin setup hook** upstream as a separate PR: a
manifest key (e.g. `setup_entrypoint: setup:interactive_setup`) that
`hermes plugins install` and a `hermes plugins setup <name>` command would
invoke. Delta Chat is an unusually good motivating case — it is the only
platform that can mint its own account with no user credentials at all, so
"pick a relay" is a genuinely interactive step no env var can express. Land
the adapter first; this is a follow-up.

### On `after-install.md`

Cheap, but **it does not help our documented install path.**
`_display_after_install()` (`plugins_cmd.py:503`) is called from exactly one
place: `cmd_install` (`:1036`). Our README and `docs/nixos-installation.md`
tell users to `hermes plugins enable deltachat-platform` after a nix build
copies the tree into place — `hermes plugins install` never runs, so the file
would never be displayed. Worth shipping for the git-install path, but it is
not a fix for onboarding.

---

## 2. Repository layout

Bundled platforms are flat — `irc/` and `simplex/` are each exactly
`__init__.py`, `adapter.py`, `plugin.yaml`. Our tree carries `vendor/`,
`tests/`, `docs/`, `skills/`, `call_handler.py`, `setup.py`, `flake.nix`,
`Makefile`.

Open questions for the PR:
- `call_handler.py` (75 KB) — a second module is probably fine, but no
  bundled platform currently has one.
- `skills/webxdc-converter/` — no bundled platform ships a skill. Hermes has
  a top-level `skills/` and `optional-skills/`; this may need to move there.
- `tests/` — bundled platforms have almost no tests; the only match in
  `tests/` is `test_telegram_polling_progress_ptb.py`. Ours would land in the
  repo-root `tests/`.
- `flake.nix` / `Makefile` / `result` symlink — drop.
- Naming: keep **`deltachat-platform`**. `irc/plugin.yaml` uses
  `name: irc-platform`, `label: IRC`, confirming `-platform` is the
  convention (and that the fork's v1.6.0 rename was correctly reverted in
  v1.7.0).

---

## 3. Dependencies

`vendor/deltachat2/` will not fly upstream. The manifest key is
`python_dependencies` (`_print_python_dependencies`, `plugins_cmd.py`), but
note it is a **declaration seam only** — the docstring is explicit that
"Hermes never auto-installs plugin pip dependencies (isolation design
deferred; see #64165 / #15220)". It just prints a copy-pasteable
`pip install` hint at install time.

That is a problem for a *bundled* plugin that is supposed to work out of the
box. Options to raise in the PR:
- `deltachat2` as a real optional extra in Hermes's `pyproject.toml`.
- Keep the lazy-import guard we already have (`_check_dc2_available()`), so
  the plugin degrades to a clear error rather than breaking CLI import.
- `aiortc` (voice calls) is heavy — almost certainly an extra, with calls
  gated behind its availability. `irc/plugin.yaml` advertises "No external
  dependencies" as a virtue, so expect scrutiny here.

Also needs an answer: `deltachat-rpc-server` is a **native binary**, not a pip
package. No bundled platform has a comparable requirement.

---

## 4. Logging gets simpler

Our `CLAUDE.md` rule — use a `hermes_plugins.*` logger prefix, never
`__name__` — exists because `__name__` resolves to `"adapter"` for a
user-installed plugin and lands only in `agent.log`.

Once bundled, `__name__` becomes `plugins.platforms.deltachat-platform.adapter`,
and `hermes_logging.py:241` routes it to `gateway.log`:

```python
"gateway": ("gateway", "hermes_plugins", "plugins.platforms"),
```

Every bundled platform adapter uses plain `logging.getLogger(__name__)`. The
adjacent comment — "``plugins.platforms`` covers messaging-platform adapters
that migrated" — suggests this exact migration is an anticipated path.

Switch to `__name__` in the PR; update `CLAUDE.md` to scope the existing rule
to the out-of-tree build.

---

## 4b. No way for a plugin to surface first-run state

Adapters report health via `BasePlatformAdapter._write_runtime_status_safe()`
→ `gateway.status.write_runtime_status()` → `gateway_state.json`, read by
`web_server.py` (`/api/status`), `service_manager.py` and the readiness
probes. Its signature is a closed set of keyword arguments — `gateway_state`,
`platform`, `platform_state`, `error_code`, `error_message`,
`needs_attention`, `retrying_since`, `served_profiles` — with **no free-form
field**.

That leaves a plugin no way to communicate first-run state that isn't an
error. Our concrete case is the SecureJoin invite link: the gateway is
perfectly healthy, but the operator needs a one-off value out of it to pair
their phone. Today it goes to a file and the log names the path.

Note also that `get_status()` — which the fork implements on the adapter — is
**not a Hermes hook**. Checked at 0.20.5: `BasePlatformAdapter` has no such
method, and nothing in `gateway/` or `hermes_cli/` calls
`adapter.get_status()` (the only `.get_status()` hits are `proxy_cli.py`
against an unrelated inference-proxy object). It is dead code unless the
plugin calls it itself. Do not back-port it expecting the gateway to display
anything.

Worth proposing upstream: an optional `details: dict` passed through
`write_runtime_status` and rendered in the dashboard's platform card.

## 5. Upstream fixes worth carrying in (or alongside) the PR

Small, independently useful, and they make the PR read as a contribution
rather than a dump:

1. **`_prompt_plugin_env_vars` masks on the wrong key.**
   `plugins_cmd.py:475` reads `spec.get("secret", False)`, while the config
   injector reads `meta.get("password") or meta.get("secret")`. A manifest
   using the documented-in-config `password: true` gets its secret **echoed
   in plaintext** at install time. Affects any plugin, not just ours.
2. **`_prompt_plugin_env_vars` ignores `optional_env`.** Only `requires_env`
   is read (`:448`). `optional_env` appears nowhere in `PluginManifest`
   either — the config injector is its sole reader in the entire codebase.
3. **The config injector could scan `~/.hermes/plugins/`.** ~5 lines. Weaker
   argument than the two above, since the bundled/user split is a deliberate
   trust boundary — but surfacing env vars is not code execution, and the
   injector's own docstring frames the mechanism as existing "so users can
   configure Teams / IRC / Google Chat without the core repo ever needing to
   know they exist."

Fixing 1–2 also benefits us directly if we *stay* out of tree.

---

## 6. Prerequisites before opening a PR

From `docs/fork-backport-notes.md`, the items that would be embarrassing to
ship bundled without:

- Access control + the `allowed_users_env` / `allow_all_env` declaration in
  `register_platform()` — without it Hermes-core's own authz gate drops every
  sender. A bundled platform must not be wide open.
- Headless onboarding (§1 above).
- Message splitting + markdown stripping.
- `dc_rpc_call` hardening (allowlist, destructive-method block).
- `_event_supervisor` so a crashed listener does not silently end the session.
- ~~Delete `_handle_audio_message_UNUSED`~~ — done (174 dead lines removed).

Then run `hermes plugins doctor` (`hermes_cli/plugin_dev.py`, new since
0.15.1) — it validates manifest, import and registration through the real
plugin load path, and `resolve_plugin_path()` already searches
`bundled/platforms/`.
