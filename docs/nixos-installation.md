# NixOS Installation Guide

This guide covers installing the Delta Chat Hermes plugin on NixOS, including
voice call support (aiortc) which requires special handling because pip-installed
C extension wheels don't work reliably on NixOS.

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
using nixpkgs. The `-o` flag creates a symlink that acts as a **GC root**,
keeping the store path alive through `nix-collect-garbage`.

```bash
nix build --impure \
  --expr 'with import <nixpkgs> {}; python312.withPackages(ps: [ps.aiortc])' \
  -o ~/.hermes/aiortc-env
```

Then add the environment's site-packages to Hermes's `PYTHONPATH` in `~/.hermes/.env`:

```bash
# Add this line (or prepend to existing PYTHONPATH):
PYTHONPATH=/home/$USER/.hermes/aiortc-env/lib/python3.12/site-packages:/home/$USER/.hermes/python-packages
```

Restart Hermes — voice call support is now active.

### Verifying the installation

```bash
HERMES_PYTHON=$(grep HERMES_PYTHON /proc/$(pgrep -f hermes-agent)/environ 2>/dev/null \
  | tr '\0' '\n' | grep HERMES_PYTHON | cut -d= -f2 \
  || echo /nix/store/2lx3hbviihknq6v6zbig8vbkymi0y5an-hermes-agent-env/bin/python3)

PYTHONPATH=~/.hermes/aiortc-env/lib/python3.12/site-packages:~/.hermes/python-packages \
  $HERMES_PYTHON -c "import aiortc; import av; print('aiortc', aiortc.__version__)"
```

### Keeping it up to date

If you upgrade Hermes or nixpkgs and need a newer aiortc, rebuild the env:

```bash
rm ~/.hermes/aiortc-env
nix build --impure \
  --expr 'with import <nixpkgs> {}; python312.withPackages(ps: [ps.aiortc])' \
  -o ~/.hermes/aiortc-env
```

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
