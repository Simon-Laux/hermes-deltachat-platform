---
name: webxdc-converter
description: Convert web artifacts (HTML, React, or any self-contained web app) into webxdc (.xdc) mini apps for sharing in Delta Chat and other webxdc-compatible messengers. Use this skill whenever the user mentions "webxdc", ".xdc", "Delta Chat app", "mini app for chat", or wants to package a web artifact/HTML app as a webxdc file. Also trigger when the user asks to convert, export, or package an existing artifact into webxdc format, or asks to build a new webxdc app from scratch. This skill handles both creating new webxdc apps and converting previously-created artifacts into the webxdc format.
---

# Webxdc Converter

This skill converts web artifacts (HTML/CSS/JS apps) into webxdc `.xdc` mini apps — the portable, privacy-preserving mini app format used by Delta Chat and other messengers.

## What is webxdc?

A webxdc app is a ZIP file (renamed to `.xdc`) containing:
- `index.html` (required) — the app entry point
- `manifest.toml` (recommended) — app name and metadata
- `icon.png` or `icon.jpg` (optional) — app icon (128–512px square)
- Any other files the app needs (JS, CSS, images, subdirectories — all fine)

Webxdc apps run in a sandboxed webview with **no internet access**. They are fully self-contained.

## Step 1: Triage — determine what's needed

Before doing anything, figure out which path to take. This is the most important step.

### Read the request

| User says... | What to do |
|---|---|
| "package this as webxdc" / "convert to .xdc" | **Likely simple packaging.** But still inspect the source to decide (see below). |
| "build me a webxdc app that does X" | **Build from scratch.** Analyze X to decide the interactivity level. |
| "make this multiplayer" / "sync between users" | **Needs webxdc API.** |

### Inspect the source and decide the interactivity level

Even when the user just says "package this", look at what the app actually does to determine the right approach:

**Level 0 — No webxdc API (the most common case):**
- Static pages, documentation, reference tools
- Calculators, converters, single-user utilities
- Any app where each user works independently with no need for persistence or sharing

Just inline everything, zip, done.

**Level 1 — Simple sendUpdate for persistence or sharing:**
- Single-player games where scores should persist across sessions and sync across the user's devices (sendUpdate gives you multi-device support for free)
- Polls, votes (each user sets their own value, no conflicts)
- Scoreboards, simple turn-based games
- Any app where state is additive or per-user (no conflicting edits)

Use `sendUpdate` / `setUpdateListener` directly.

**Level 2 — Yjs for collaborative state:**
- Collaborative text editors, shared whiteboards
- Kanban boards, shared to-do lists
- Any app where multiple users edit the same data concurrently

Use the `y-webxdc` provider with Yjs. This requires a bundler (see below).

**Level 3 — Realtime channel:**
- Real-time multiplayer games (pong, drawing races)
- Live cursors, typing indicators
- Apps where latency matters more than durability

Use `joinRealtimeChannel()`.

**Ask the user if the level is unclear.** For example: "This looks like a note-taking app. Should each person have their own notes, or should notes sync between everyone in the chat?" or "Should game scores persist between sessions?"

The default assumption is Level 0. Only go higher when there's a clear reason.

## Step 2: Prepare the HTML

All webxdc apps must be **fully self-contained** — no external CDN links, no fetch calls, no external images.

When converting an existing artifact or HTML file:

1. **Inline or bundle all external dependencies** — no CDN links. For small apps, inline CSS into `<style>` and JS into `<script>`. For larger apps with multiple files, just include the files in the ZIP (subdirectories work fine).
2. **Remove any fetch/XHR calls** to external URLs — no internet access.
3. **Remove localStorage/sessionStorage for anything important** — it works in practice, but can be cleared by OS or messenger updates at any time and doesn't sync across devices. Fine for ephemeral UI preferences (current tab, theme). For anything the user would care about losing, use `sendUpdate` instead (Level 1+).
4. **Ensure everything is in the ZIP** — fonts, images, all assets.

### Choosing the right app structure

**Single-file (small apps):** Inline everything into `index.html`. Best for simple tools and conversions.

**Multi-file (larger apps):** The ZIP can contain any file structure — subdirectories, separate JS/CSS files, multiple HTML pages linked via `<a href="page2.html">`. Use whatever structure makes sense for the app.

**React / JSX:** Several options depending on complexity:
- Rewrite small components in vanilla HTML/CSS/JS (simplest)
- Use a JSX-in-browser approach (e.g., HTM with tagged template literals)
- Use a bundler like esbuild, rollup, or vite to produce a self-contained build (necessary for Yjs or any npm dependency)

**When to use a bundler:** If the app needs npm packages (Yjs, y-webxdc, etc.) or has a complex module structure, use a bundler. esbuild is the fastest option for simple cases. vite works well for larger projects. The output still needs to be self-contained HTML/JS/CSS files in the ZIP.

## Step 3: Package it

**Where to write files:** Write all outputs (source files, the `.xdc`, and any build artifacts) to your **current working directory** — run `pwd` to find it. In the Docker sandbox that is `/workspace/`; on other deployments it is wherever the agent runs (`$PWD`). Never write to `/tmp/` — on Docker it is container-local tmpfs the host cannot read. The examples below use `/workspace` as a concrete stand-in; substitute your actual working directory.

### Create manifest.toml

```toml
name = "App Name"
```

Optionally add `source_code_url = "https://..."` if the user provides one.

### Generate icon

If the user supplies an icon, use it. Otherwise create a small SVG inline — icons are optional but improve the app's appearance in chat:

```bash
cat > /workspace/myapp/icon.svg << 'EOF'
<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128">
  <rect width="128" height="128" rx="20" fill="#4ECDC4"/>
  <text x="64" y="84" font-size="64" font-family="sans-serif" text-anchor="middle" fill="white">AB</text>
</svg>
EOF
```

Replace `AB` with the app's initials and choose a fitting background color.

### Create the .xdc file

A `.xdc` file is a ZIP archive. Use Python's `zipfile` — it is always available, unlike `zip` which may not be installed:

```bash
# Single-file app
python3 -c "
import zipfile
with zipfile.ZipFile('/workspace/myapp.xdc', 'w', zipfile.ZIP_DEFLATED) as zf:
    zf.write('/workspace/myapp/index.html', 'index.html')
    zf.write('/workspace/myapp/manifest.toml', 'manifest.toml')
    zf.write('/workspace/myapp/icon.svg', 'icon.svg')
"

# Multi-file app — walk the entire app directory
python3 -c "
import zipfile, os
base = '/workspace/myapp'
with zipfile.ZipFile('/workspace/myapp.xdc', 'w', zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(base):
        for f in files:
            path = os.path.join(root, f)
            zf.write(path, os.path.relpath(path, base))
"

# React/bundled app — build first, then zip the dist output
npm run build   # produces dist/index.html, dist/assets/, etc.
python3 -c "
import zipfile, os
base = '/workspace/myapp/dist'
with zipfile.ZipFile('/workspace/myapp.xdc', 'w', zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(base):
        for f in files:
            path = os.path.join(root, f)
            zf.write(path, os.path.relpath(path, base))
"
```

`index.html` MUST be at the root of the archive (arcname `'index.html'`, not a subdirectory path). All output files must go to your current working directory (`/workspace/` in the Docker sandbox, `$PWD` otherwise) — **not** `/tmp/`. On Docker the `/tmp/` directory is container-local tmpfs and the host cannot read it.

**Always use ZIP format** — `.xdc` is a ZIP file. Never use tar, tar.gz, or any other archive format; webxdc clients will not open them.

### Validate before sending

Always verify the archive before delivering. This catches wrong arcnames, missing `index.html`, and corrupt zips early:

```bash
python3 -c "
import zipfile, sys
path = '/workspace/myapp.xdc'
with zipfile.ZipFile(path) as zf:
    names = zf.namelist()
    print('Files in archive:', names)
    if 'index.html' not in names:
        print('ERROR: index.html missing from archive root!')
        sys.exit(1)
    print('OK — index.html present, size:', zf.getinfo('index.html').file_size, 'bytes')
"
```

If `index.html` is missing or listed as e.g. `myapp/index.html`, re-package with the correct arcname before sending.

### Size guidance

Aim for under 1 MB. Under 10 MB is the practical ceiling — beyond that it becomes impractical as a chat attachment. Actual hard limits vary by messenger.

### Deliver the file

Write the `.xdc` to your working directory, then emit a MEDIA directive referencing it by **absolute path** — the adapter delivers it via `send_document`, exactly like Telegram does for any other file. (In the Docker sandbox the working directory is `/workspace/`, so the absolute path there is `/workspace/myapp.xdc`.) DC core auto-detects `.xdc` and delivers it as a webxdc mini app.

```
Here's your mini app! Tap Start to launch it.
MEDIA:/workspace/myapp.xdc
```

The same works for any other output file type (use its absolute path):
```
Here is your report. MEDIA:/workspace/report.pdf
```

**For Level 0 apps, you're done here.** The sections below are only for apps that need shared state.

---

## Level 1: Simple sendUpdate for persistence and sharing

Add to the HTML (do NOT include a `webxdc.js` file in the ZIP — the messenger injects it):
```html
<script src="webxdc.js"></script>
```

Core API:
```javascript
// Send a state update to all peers (including yourself)
window.webxdc.sendUpdate({
  payload: { /* any JSON-serializable data */ },
  info: "Alice voted",        // optional: shown in chat (~50 char)
  summary: "3 votes so far",  // optional: shown beside app icon (~20 char)
}, "");

// Receive all state updates (replayed from history on start)
window.webxdc.setUpdateListener((update) => {
  const data = update.payload;
  // rebuild state from update
}, 0);

// Identity
const myName = window.webxdc.selfName;
const myAddr = window.webxdc.selfAddr;  // unique per user, use to distinguish peers
```

Read `references/webxdc-api.md` for the full API reference including `sendToChat`, `importFiles`, and rate limits.

### Example: single-player game with persistent high score

Even a single-player game benefits from sendUpdate — the score persists across sessions and syncs to other devices:

```javascript
window.webxdc.setUpdateListener((update) => {
  if (update.payload.highScore > currentHighScore) {
    currentHighScore = update.payload.highScore;
    renderHighScore();
  }
}, 0);

function reportScore(score) {
  if (score > currentHighScore) {
    window.webxdc.sendUpdate({
      payload: { highScore: score, player: window.webxdc.selfAddr },
      summary: `High score: ${score}`
    }, "");
  }
}
```

---

## Level 2: Yjs for collaborative state

Use the `y-webxdc` provider (`codeberg.org/webxdc/y-webxdc`, `npm i y-webxdc`). It handles autosaving, bundling updates, saving on window close, and setting chat metadata.

This requires a bundler (esbuild, rollup, or vite) since Yjs and y-webxdc are npm packages.

```javascript
import * as Y from 'yjs'
import { WebxdcProvider } from 'y-webxdc'

const ydoc = new Y.Doc()
const provider = new WebxdcProvider({
  webxdc: window.webxdc,
  ydoc,
  autosaveInterval: 10 * 1000,
  getEditInfo: () => ({
    document: 'Shared Notes',
    summary: `Last edit: ${window.webxdc.selfName}`,
    startinfo: `${window.webxdc.selfName} editing Shared Notes`,
  }),
})

// Use ydoc shared types as usual
const ytext = ydoc.getText('content')
ytext.observe(() => { renderEditor() })
```

There's also `webxdc-yjs-provider` by WofWca (`codeberg.org/WofWca/webxdc-yjs-provider`) which offers a more generic/low-level approach — useful if you need custom control over when updates are sent.

When using Yjs, the provider owns `sendUpdate`/`setUpdateListener` — don't also call them manually (use the WofWca generic variant if you need to mix custom payloads with Yjs updates).

---

## Level 3: Realtime channel

For low-latency communication. Data is ephemeral — NOT persisted, NOT replayed on app restart.

```javascript
const channel = window.webxdc.joinRealtimeChannel();
channel.setListener((data) => { /* Uint8Array */ });
channel.send(new TextEncoder().encode("cursor:120,340"));
```

You can check for support and warn the user:
```javascript
if (!window.webxdc.joinRealtimeChannel) {
  showWarning("Realtime channels not supported. Please update your messenger.");
}
```

**When to use it depends on the app:**
- **Realtime game** (e.g., multiplayer pong) — use the realtime channel as primary transport. No fallback needed; the game requires it.
- **Collaborative editor** — use the realtime channel for live keystrokes, but also send periodic checkpoints via `sendUpdate` (or via Yjs autosave) so state survives restarts and late joiners can catch up.
- **Cursor/presence indicators** — purely ephemeral, realtime channel only, no persistence needed.

Inform the user if their app would benefit from a hybrid approach (realtime for live updates + periodic durable saves). Don't add a fallback automatically — decide based on the actual requirements.

## Key constraints

- **No internet access** — #1 rule. No CDN, no API calls, no external anything.
- **Self-contained ZIP** — every asset must be in the file.
- `index.html` is the entry point — the messenger opens this file.
- `index.html` must be at the **root** of the .xdc file — the messenger will not look in subdirectories.
- **Directory paths do not auto-resolve** — always use explicit paths like `href="subdir/index.html"`, not `href="subdir/"`.
- `webxdc.js` is provided by the messenger — never include it in the ZIP, just reference it with a script tag.
- **Keep it small** — aim for under 1 MB; hard limits vary by messenger, ~10 MB is the practical ceiling.
