# NixOS Installation Guide

This guide covers installing the Delta Chat Hermes plugin on NixOS, including
voice call support (aiortc) which requires special handling because pip-installed
C extension wheels don't work reliably on NixOS.

## What the declared `python_dependencies` mean here

`plugin.yaml` declares `deltachat-rpc-server` and `aiortc`, so a Hermes that supports that manifest
key will pip-install both into its own venv when the plugin is enabled. On NixOS you still want the
nix-built ones, and the nix path wins without any extra work:

- **aiortc** — `PYTHONPATH` entries are placed ahead of the venv's `site-packages` on `sys.path`,
  and `call_handler.py` additionally inserts `~/.hermes/aiortc-env`'s site-packages at `sys.path[0]`
  when that symlink exists. So the nix build shadows whatever pip put in the venv. Keep following
  the GC-root recipe below; treat the venv copy as inert ballast.
- **deltachat-rpc-server** — `DELTACHAT_RPC_SERVER` takes precedence over PATH lookup, so the
  `nix profile` binary keeps being used. If you leave that variable unset, the adapter falls back to
  `shutil.which("deltachat-rpc-server")` and can pick the venv's wheel binary, which may not exec on
  NixOS. Always set `DELTACHAT_RPC_SERVER` (step 1 below does).

Dependency resolution happens when the plugin is enabled, and failure leaves the previous
environment and plugin selection untouched — a wheel Hermes cannot install will not take your
working setup down with it.

## Basic Installation

### 1. Install deltachat-rpc-server

```bash
nix profile install nixpkgs#deltachat-rpc-server
echo 'DELTACHAT_RPC_SERVER=/home/$USER/.nix-profile/bin/deltachat-rpc-server' >> ~/.hermes/.env
```

### 2. Clone and enable the plugin

```bash
git clone https://github.com/Simon-Laux/hermes-deltachat-platform ~/.hermes/plugins/deltachat-platform
hermes plugins enable deltachat-platform
```

### 3. Create a Delta Chat account

```bash
python3 ~/.hermes/plugins/deltachat-platform/setup.py
```

Or skip this step entirely — put `DELTACHAT_EMAIL=auto` in `~/.hermes/.env` and
the adapter registers a chatmail account on first connect, writing the invite
link to the gateway log and to `invite.txt` in the accounts directory. That is
the better fit for a declarative or headless NixOS deployment where there is no
terminal to run the script in; see
[headless-onboarding.md](headless-onboarding.md).

### 4. Start the gateway

```bash
hermes gateway start
```

---

## Voice Call Support (aiortc)

Voice calls require `aiortc` and its C extension dependencies (`av`/PyAV,
`pylibsrtp`, etc.). These cannot be installed via `pip` on NixOS because the
manylinux wheels link against `libstdc++.so.6` which is not in NixOS's standard
library paths.

### Solution: nix-managed Python environment

Build a Python 3.12 environment containing aiortc and all its dependencies
from **the nixpkgs your installed Hermes was built with** — not your system
`<nixpkgs>`. The env is loaded into the Hermes process, so it has to agree with
Hermes on two things:

- **glibc** — a newer nixpkgs links PyAV/ALSA against a newer glibc, and the
  import dies with ``version `GLIBC_2.4x' not found``.
- **cryptography** — Hermes imports its own `cryptography` first; a pyOpenSSL
  built for an older one then fails with
  `module 'lib' has no attribute 'GEN_EMAIL'` and the adapter cannot connect.

The expression reads the installed Hermes's locked flake from your `nix
profile` and takes nixpkgs from that flake's inputs. The `-o` flag creates a
symlink that acts as a **GC root**, keeping the store path alive through
`nix-collect-garbage`.

```bash
rm -f ~/.hermes/aiortc-env
nix build --impure -o ~/.hermes/aiortc-env --expr '
  let
    manifest = builtins.fromJSON (builtins.unsafeDiscardStringContext
      (builtins.readFile (builtins.getEnv "HOME" + "/.nix-profile/manifest.json")));
    hermes = builtins.head (builtins.filter
      (e: builtins.match ".*NousResearch/hermes-agent.*" (e.originalUrl or "") != null)
      (builtins.attrValues manifest.elements));
    pkgs = import (builtins.getFlake hermes.url).inputs.nixpkgs { system = builtins.currentSystem; };
  in pkgs.python312.withPackages (ps: [ ps.aiortc ])'
```

If Hermes comes from a NixOS or home-manager flake input instead of `nix
profile`, replace `hermes.url` with that input's locked ref (e.g.
`github:NousResearch/hermes-agent/v2026.9.24`).

Then add the environment's site-packages to Hermes's `PYTHONPATH` in `~/.hermes/.env`:

```bash
# Add this line (or prepend to existing PYTHONPATH):
PYTHONPATH=/home/$USER/.hermes/aiortc-env/lib/python3.12/site-packages:/home/$USER/.hermes/python-packages
```

Restart Hermes — voice call support is now active.

### Verifying the installation

```bash
# Hermes's own interpreter, found through the wrapper's runtime closure
HERMES_PYTHON=$(nix-store -qR "$(readlink -f "$(which hermes)")" \
  | grep -- '-hermes-agent-env$')/bin/python3

PYTHONPATH=~/.hermes/aiortc-env/lib/python3.12/site-packages:~/.hermes/python-packages \
  $HERMES_PYTHON -c "import aiortc, av; from OpenSSL import crypto; print('aiortc', aiortc.__version__)"
```

### Keeping it up to date

**Rebuild the env after every Hermes update** — rerun the build command above.
Skipping it is what produces the `GEN_EMAIL` / `GLIBC_2.4x` errors.

### Reverting / removing voice call support

```bash
# Remove GC root (store path will be cleaned up on next nix-collect-garbage)
rm ~/.hermes/aiortc-env

# Remove PYTHONPATH addition from ~/.hermes/.env

# Remove the profile-level install if you ran nix profile install earlier
nix profile remove nixpkgs#python312Packages.aiortc 2>/dev/null || true

# Clean up nix store
nix-collect-garbage
```

---

## Why not `pip install aiortc`?

On NixOS, `pip install` works for pure-Python packages, but packages with C
extensions (like `av`/PyAV) link against system libraries (`libstdc++.so.6`,
`libav*`, etc.) at non-standard paths. The manylinux wheels assume
`/usr/lib` exists, which it doesn't on NixOS.

The `nix build` approach uses nixpkgs-compiled packages where all shared
library paths are encoded in the binary RPATH, so no `LD_LIBRARY_PATH`
hacks are needed and everything works correctly across reboots and
garbage collection cycles.

## Dev shell

For development and testing (not for the running Hermes daemon), a nix dev
shell with all dependencies is available:

```bash
cd ~/.hermes/plugins/deltachat-platform
nix develop          # enter dev shell with aiortc, deltachat2, pytest, etc.
nix develop --command pytest   # run tests
```
