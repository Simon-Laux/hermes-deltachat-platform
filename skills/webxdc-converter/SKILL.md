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

All webxdc apps must be **fully self-contained** — no external CDN links, no requests to external URLs, no external images. Loading files packaged in the `.xdc` by relative path (e.g. `fetch("data.json")`) works.

When converting an existing artifact or HTML file:

1. **Inline or bundle all external dependencies** — no CDN links. For small apps, inline CSS into `<style>` and JS into `<script>`. For larger apps with multiple files, just include the files in the ZIP (subdirectories work fine).
2. **Replace or remove calls to external servers** — no internet access. Many server features can be remade with the webxdc API: shared state, scores, chat-wide lists and multiplayer via `sendUpdate` or `joinRealtimeChannel`, sharing results via `sendToChat`, loading user files via `importFiles`. If a feature can't be remade (live data from a third-party API, AI calls, accounts on an outside service), ask the user whether to drop it or rework it before you remove it.
3. **Remove localStorage/sessionStorage for anything important** — it works in practice, but can be cleared by OS or messenger updates at any time and doesn't sync across devices. Fine for ephemeral UI preferences (current tab, theme). For anything the user would care about losing, use `sendUpdate` instead (Level 1+).
4. **Ensure everything is in the ZIP** — fonts, images, all assets.
5. **If the app uses the webxdc API, add `<script src="webxdc.js"></script>`** before your own scripts — the one reference to a file not in the ZIP. The messenger provides it; see "Rule: always load webxdc.js" below.

### Choosing the right app structure

**Single-file (small apps):** Inline everything into `index.html`. Best for simple tools and conversions.

**Multi-file (larger apps):** The ZIP can contain any file structure — subdirectories, separate JS/CSS files, multiple HTML pages linked via `<a href="page2.html">`. Use whatever structure makes sense for the app.

**React / JSX:** Several options depending on complexity:
- Rewrite small components in vanilla HTML/CSS/JS (simplest)
- Use a JSX-in-browser approach (e.g., HTM with tagged template literals)
- Use a bundler like esbuild, rollup, or vite to produce a self-contained build (necessary for Yjs or any npm dependency)

**When to use a bundler:** If the app needs npm packages (Yjs, y-webxdc, etc.) or has a complex module structure, use a bundler. esbuild is the fastest option for simple cases. vite works well for larger projects. The output still needs to be self-contained HTML/JS/CSS files in the ZIP.

## Step 3: Package it

**Where to write files:** Write all outputs (source files, the `.xdc`, and any build artifacts) to your **current working directory** — run `pwd` to find it. In the Docker sandbox that is `/workspace/`; on other deployments it is wherever the agent runs (`$PWD`). Never write to `/tmp/` — on Docker it is container-local tmpfs the host cannot read. The examples below use paths **relative to the working directory**, so they run unchanged in Docker and elsewhere. Never assume `/workspace/` exists — outside the Docker sandbox you usually cannot create it.

Build the app in a directory of its own (the examples use `myapp/`) so that the directory's contents are exactly what goes into the archive: `index.html`, `manifest.toml`, the icon and any other assets. For a bundled app that directory is the build output (e.g. `myapp/dist/`): put `manifest.toml` and `icon.png` in `public/` so the bundler copies them, or copy them in after the build.

### Create manifest.toml

```toml
name = "App Name"
```

Optionally add `source_code_url = "https://..."` if the user provides one.

### Generate icon

If the user supplies an icon, use it (convert it to PNG or JPEG if needed). Otherwise, if you have an image generation tool, you can generate one with it (see the note after the script); if not, use the script. Icons are optional but improve the app's appearance in chat. Messengers only use `icon.png` or `icon.jpg`; an `icon.svg` is ignored.

```bash
mkdir -p myapp
python3 - << 'EOF'
import struct, zlib
initials, color, size = "AB", (0x4E, 0xCD, 0xC4), 256  # app initials, background RGB
try:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (size, size), color)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", size // 2)
    except OSError:
        font = ImageFont.load_default(size=size // 2)
    ImageDraw.Draw(img).text((size / 2, size / 2), initials, fill="white", font=font, anchor="mm")
    img.save("myapp/icon.png")
except Exception:
    # No (usable) Pillow: plain-colour PNG with the standard library only
    print("Pillow unavailable, writing a plain-colour icon")
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    rows = b"".join(b"\0" + bytes(color) * size for _ in range(size))
    with open("myapp/icon.png", "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
EOF
```

Replace `AB` with the app's initials (one or two letters, more don't fit) and choose a fitting background color. Without Pillow the icon is a plain colored square.

**Icon from an image generator.** Unless the user asked for a particular style, ask for a flat icon: one centred symbol on a solid background of an exact hex colour, with no text, gradients or texture. Vaguer prompts tend to give grainy or off-centre results. Keep the subject in the middle ~70% of the image, because the chat shows the icon on a rounded tile. Generators sometimes return a JPEG under a `.png` name, so check the first bytes: a PNG starts with `\x89PNG`, a JPEG with `\xff\xd8`. Save a JPEG as `icon.jpg` (or convert it to PNG) instead of packing it as `icon.png`.

### Create and check the .xdc file

A `.xdc` file is a ZIP archive — not tar or tar.gz, webxdc clients will not open those. This zips the app directory and then checks the result, because the usual mistakes (`index.html` ending up in a subfolder, a leftover CDN link) are invisible until someone opens the app in a chat:

```bash
python3 - myapp myapp.xdc << 'EOF'
import os, re, sys, zipfile
src, out = sys.argv[1], sys.argv[2]
if not os.path.isdir(src):
    sys.exit(f"ERROR: {src} is not a directory")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "node_modules")
        for name in sorted(files):
            path = os.path.join(root, name)
            if not name.startswith(".") and not os.path.samefile(path, out):
                zf.write(path, os.path.relpath(path, src))

errors, notes = [], []
with zipfile.ZipFile(out) as zf:
    names = zf.namelist()
    if "index.html" not in names:
        errors.append("index.html is not at the archive root")
    if any(n.split("/")[-1] == "webxdc.js" for n in names):
        errors.append("the archive contains webxdc.js; the messenger provides that file")
    if not {"icon.png", "icon.jpg"} & set(names):
        notes.append("no icon.png or icon.jpg at the archive root")
    url = r"""(?:https?:)?//[^"'\s>)]+"""
    markup = [  # in .html and .css
        r"""<(?:script|img|iframe|source|video|audio|track|embed|object)\b[^>]*?(?<![\w-])(?:src|srcset|data|poster)\s*=\s*["']?(?:[^"'>]*[\s,])?(?<!base64,)""" + url,  # base64 data can start with //, e.g. inline MP3
        r"""<link\b(?=[^>]*\brel\s*=\s*["']?[^"'>]*\b(?:stylesheet|icon|preload|modulepreload|manifest))[^>]*?\bhref\s*=\s*["']?""" + url,
        r"""url\(\s*["']?""" + url,
        r"""@import\s+["']""" + url,
    ]
    imports = [r"""\b(?:import|export)\b[^;"'()]*?\bfrom\s*["']""" + url, r"""\bimport\s*\(?\s*["']""" + url]  # in .html and .js
    for n in names:
        patterns = (markup if n.endswith((".html", ".htm", ".css")) else []) + (imports if n.endswith((".html", ".htm", ".js", ".mjs")) else [])
        text = zf.read(n).decode("utf-8", "replace") if patterns else ""
        for pattern in patterns:
            for h in re.findall(pattern, text, re.I):
                errors.append(f"{n} loads something from the network: {h[:90]}")
size = os.path.getsize(out)
if size > 10_000_000:
    notes.append("over 10 MB, too large for most chats")
elif size > 1_000_000:
    notes.append("over 1 MB, consider shrinking the assets")
print(f"{out}: {len(names)} files, {size / 1024:.0f} KiB")
for n in sorted(names):
    print("  " + n)
for msg in notes:
    print("NOTE: " + msg)
for msg in errors:
    print("ERROR: " + msg)
sys.exit(1 if errors else 0)
EOF
```

For a bundled app, run the build first and pass the build output instead: `python3 - myapp/dist myapp.xdc`.

Fix every ERROR and package again. The messenger blocks network access, so anything loaded from outside the `.xdc` (a CDN script, a web font, an API call) just fails, even though the app works in a browser. The check catches URLs in tags, CSS and imports, but not requests made from JavaScript: search the JS for `http://`, `https://`, `ws://`, `wss://` and `"//`, and make sure the app doesn't need any of them to work. Bundle what it needs into the `.xdc`; for features that need a server, see "Replace or remove calls to external servers" above.

Make every change in the app directory (or in the sources, for a bundled app) and package again. Don't patch files only inside the `.xdc`, so that the sources still reproduce the app.

### Size guidance

Aim for under 1 MB; the script notes anything bigger. Under 10 MB is the practical ceiling for a chat attachment. Actual hard limits vary by messenger.

### Deliver the file

Write the `.xdc` to your working directory, then emit a MEDIA directive referencing it by **absolute path** — the adapter delivers it via `send_document`, exactly like Telegram does for any other file. This is the one place a relative path will not do; get the absolute one with `echo "$PWD/myapp.xdc"` (in the Docker sandbox that resolves to `/workspace/myapp.xdc`). DC core auto-detects `.xdc` and delivers it as a webxdc mini app.

```
Here's your mini app! Tap Start to launch it.
MEDIA:<absolute path of your working directory>/myapp.xdc
```

The same works for any other output file type (use its absolute path):
```
Here is your report. MEDIA:<absolute path of your working directory>/report.pdf
```

For Level 1+ apps, shared state belongs to the one app message it was sent in. Sending the `.xdc` again — including a fixed or improved version — starts a separate, empty instance: earlier scores, votes or entries stay in the old message. Say so when you send a new version, so nobody wonders where their data went.

The packaging checks don't open the app. Unless you actually ran it, say in your reply that it is untested and ask the user to try it on their device.

**For Level 0 apps, you're done here.** The sections below are only for apps that need shared state.

---

## Rule: always load webxdc.js before using the API

Every app that touches `window.webxdc` (Levels 1, 2 and 3) **must** load `webxdc.js` with a script tag in `index.html` (and in every other HTML page that uses the API — from a page in a subdirectory use `../webxdc.js`), placed **before** any script that uses it:

```html
<script src="webxdc.js"></script>
<script src="app.js"></script>  <!-- or your inline <script> / bundle -->
```

`webxdc.js` is provided by the host messenger at runtime, so it is **not** packaged — never put a `webxdc.js` file in the ZIP. But the messenger does not inject it on its own: without the script tag, `window.webxdc` is `undefined` and every API call fails. This applies to bundled apps too — a bundler does not provide it; keep the plain `<script src="webxdc.js">` tag in the HTML ahead of the bundle.

---

## Level 1: Simple sendUpdate for persistence and sharing

Load `webxdc.js` first (see the rule above), then use the core API:
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

### Design patterns for shared state

Two ways to structure the payloads when using `sendUpdate` directly. If several users can edit the same piece of data at the same time, neither is enough — go to Level 2.

#### Last-writer-wins
Simplest approach — each user owns one key and the latest value per key wins. Send only the key that changed, not the whole state. Works for simple apps like polls.

```javascript
let state = { votes: {} };

window.webxdc.setUpdateListener((update) => {
  Object.assign(state.votes, update.payload.votes);
  render();
}, 0);

function vote(option) {
  state.votes[window.webxdc.selfAddr] = option;
  window.webxdc.sendUpdate({
    payload: { votes: { [window.webxdc.selfAddr]: option } },
    info: `${window.webxdc.selfName} voted`,
    summary: `${Object.keys(state.votes).length} votes`
  }, "");
}
```

#### Event sourcing
Send individual actions and apply them one by one to build up the state. Good for games and collaborative tools. Apply each update as it arrives instead of replaying the whole list every time, and draw only once the history is caught up.

```javascript
let state = newGame();
render();  // empty state, until updates arrive

window.webxdc.setUpdateListener((update) => {
  applyMove(state, update.payload);
  if (update.serial === update.max_serial) render();  // caught up
}, 0);

function makeMove(move) {
  window.webxdc.sendUpdate({
    payload: { player: window.webxdc.selfAddr, ...move },
    info: `${window.webxdc.selfName} made a move`
  }, "");
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
channel.leave();  // when done; only one channel can be open at a time
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
- **Always load `webxdc.js` before using the API** — `<script src="webxdc.js"></script>` before your own scripts. The messenger provides the file, so never include it in the ZIP, but without the tag `window.webxdc` is undefined.
- **Keep it small** — aim for under 1 MB; hard limits vary by messenger, ~10 MB is the practical ceiling.
- **Never name a file `webxdc.js`** — the messenger replaces any file with that name with its own API script.
- **No `alert()`, `confirm()` or `prompt()`** — the host webview may not implement them (they return immediately without showing anything). Build dialogs in the page.
- **`window.open()` is blocked** — open pages in the same window or show content in the page.
- **Don't rely on browser permissions** — camera, microphone, clipboard, geolocation and similar requests may be denied. Feature-detect and keep the app usable without them. WebRTC is blocked as part of the no-internet rule.
- **External links: offer to copy them** — some clients ask the user before opening an outbound link, others do nothing when it is clicked. Show the URL as selectable text so the user can copy it (a copy button is a bonus, but fall back to selectable text if `navigator.clipboard` fails).
- **Escape what you show** — chat members can type anything into a shared app, and text inserted with `innerHTML` is parsed as HTML, so it can run script on every device (XSS). Show text with `textContent` / `innerText`; if you need markup, escape `&`, `<`, `>`, `"` and `'` in the user text first.
- **Ship baked-in data as a file** — put data (notes, lists) in a JSON file in the `.xdc` and load it with `fetch("data.json")`, showing an error if that fails. If you inline it into a `<script>` instead, replace `</` with `<\/` in the JSON, or a `</script>` in the data ends the script block early.
- **Baked-in data goes stale** — an app with data embedded at build time doesn't update when the source changes. Say so when you send it; package it again to refresh it.
- **Never leave a white page** — if something essential fails (a script error during startup, a WebAssembly module that fails to load, missing data), show an error message in the page. Add a `window.addEventListener("error", …)` and `"unhandledrejection"` handler that displays the error.
