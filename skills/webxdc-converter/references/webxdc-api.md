# Webxdc API Reference

This document describes the JavaScript API available to webxdc apps. The authoritative spec is <https://webxdc.org/docs/spec/api.html>.

Load the API with `<script src="webxdc.js"></script>` before your own scripts. The messenger provides `webxdc.js`, so do not put it in the `.xdc`.

## Core API

### Properties

- `window.webxdc.selfAddr` (string) - Unique ID of the current user within this app. Same for the same user on every device and every launch; different for every other user. Use it to distinguish peers and as a key in `notify`. It has no meaning outside the app; do not show it in the UI (use `selfName`).
- `window.webxdc.selfName` (string) - Display name of the current user, for showing in the UI.
- `window.webxdc.sendUpdateInterval` (number) - Minimum milliseconds between `sendUpdate` calls. May be missing; assume `10000` then.
- `window.webxdc.sendUpdateMaxSize` (number) - Maximum size of one serialized update in bytes. May be missing; assume `128000` then.

### Methods

#### `sendUpdate(update, descr)`
Send an update to all instances of the app in the chat, including your own (it comes back through the update listener). Exception: if the app is in a contact request or a group the user has left, the update is neither sent nor passed to the listener.

- `update`: Object with properties:
  - `payload`: Any JSON-serializable data (required)
  - `info`: Short message added to the chat (~50 chars) (optional)
  - `summary`: Short text shown beside the app icon (~20 chars) (optional)
  - `document`: Name of the document being edited, shown in the UI (~20 chars) (optional)
  - `href`: Relative URL, e.g. `"index.html#about"`, the app navigates to when a receiver starts it from this update (optional)
  - `notify`: Object mapping `selfAddr` values to notification text, e.g. `{ [addr]: "It's your turn" }`. The key `"*"` is a catch-all for everyone not listed (optional)
- `descr`: Deprecated. Pass `""`.

**Example:**
```javascript
window.webxdc.sendUpdate({
  payload: { score: 100 },
  info: "Player reached 100 points",
  summary: "Score: 100"
}, "");
```

#### `setUpdateListener(callback, serial)`
Register a callback that is called for each update, including your own.

- `callback`: Function called with an `update` object containing:
  - `payload`: The payload data
  - `serial`: Serial of this update (> 0, higher is newer)
  - `max_serial`: Highest serial known at the time of the call. When `serial === max_serial`, the replay is caught up
  - `info`, `summary`, `document`, `href`: As passed to `sendUpdate` (if set)
  - `notify`: List of addresses that were notified (if set)
- `serial`: The last serial the app already knows (default `0`). All updates after it are replayed, then new ones arrive live. Pass `0` to rebuild state from the full history.

Call `setUpdateListener` only once; calling it again is undefined behavior (currently only the last listener works).

**Returns:** Promise that resolves once all updates known at the time of the call have been passed to the callback.

**Example:**
```javascript
window.webxdc.setUpdateListener((update) => {
  console.log("Received update:", update.payload);
  // Rebuild state from update.payload
}, 0);  // Replay all updates from the start
```

#### `sendToChat(message)`
Prepare a message with text and/or a file for the user to send to a chat of their choice. The user can edit or cancel it; the app is not told about that.

- `message`: Object with at least one of:
  - `text`: Message text (string)
  - `file`: Object with `name` (filename incl. extension) and exactly one of `blob` (Blob/File), `base64` (string), or `plainText` (string)

**Returns:** Promise that rejects on error. It may not resolve before the app is closed.

**Example:**
```javascript
await window.webxdc.sendToChat({
  text: "My results",
  file: { name: "results.txt", plainText: "Score: 100" },
});
```

Use `plainText` instead of `btoa()` for text files; `btoa()` breaks on non-ASCII characters.

#### `importFiles(filter)`
Open a file picker so the user can import files, e.g. recent chat attachments.

- `filter`: Object with optional properties:
  - `extensions`: Array of extensions starting with a dot, e.g. `[".ics"]`
  - `mimeTypes`: Array of MIME types; files matching either list are shown
  - `multiple`: Allow selecting several files (default `false`)

**Returns:** Promise that resolves with an array of `File` objects.

**Example:**
```javascript
const files = await window.webxdc.importFiles({
  mimeTypes: ["text/calendar"],
  extensions: [".ics"],
});
```

### Realtime Channel

#### `joinRealtimeChannel()`
Join the realtime channel of this app instance. Experimental and not available in every messenger (Delta Chat since 1.48).

**Returns:** Channel object with methods:
- `channel.send(data)` - Send a `Uint8Array` (max 128000 bytes) to currently connected peers. Delivery is not guaranteed. Unlike `sendUpdate`, your own data is not passed back to your own listener, so apply local changes directly
- `channel.setListener(callback)` - Set the listener for incoming `Uint8Array` data. A second call replaces the first listener
- `channel.leave()` - Leave the channel. The object is unusable afterwards; call `joinRealtimeChannel()` again to rejoin

Only one channel per app can be open at a time; joining again without `leave()` throws.

**Example:**
```javascript
const channel = window.webxdc.joinRealtimeChannel();
channel.setListener((data) => {
  console.log("Received:", new TextDecoder().decode(data));
});
channel.send(new TextEncoder().encode("Hello!"));
```

**Note:** Realtime channels are ephemeral - data only reaches peers connected right now. It is NOT persisted and NOT replayed when the app restarts.

## Rate Limits

- `sendUpdate()`: Read `sendUpdateInterval` and `sendUpdateMaxSize` (defaults 10000 ms and 128000 bytes if missing). Split larger data across several updates. Sending faster than the interval does not make updates arrive sooner; the messenger may delay them for much longer than the interval.
- `joinRealtimeChannel()`: Max 128000 bytes per `send()`.

Actual values in Delta Chat (chatmail core, last checked 2026-10-10 against `main` at `694ef8ba7`, latest release v2.63.0); other messengers may differ:
- `sendUpdateInterval` is 1000 ms (burst of 3 messages, then about 1 per second).
- `sendUpdateMaxSize` is the recommended attachment size, about 22 MB.
- Updates still waiting to be sent are packed together into outgoing messages of up to about 100 KiB of JSON each.

## Delta Chat Quirks

Behaviour of Delta Chat's implementation that the spec does not make obvious (checked 2026-10-10 in chatmail core and the Desktop, Android and iOS sources).

- **Oversized realtime data is dropped silently.** No client checks the size; data above about 128 KiB fails inside core and the app gets no error. Keep each `send()` at or below 128000 bytes.
- **Serials are bookmarks, not counters.** They have gaps and differ between peers and even between a user's devices. Updates already arrive in order, so don't sort by them. Use them to resume: if you compact state, store it with the last serial it includes and pass that serial to `setUpdateListener` on the next start to get only newer updates.
- **A new `info` can replace the previous one.** If the last message in the chat is an `info` message from the same sender and app, a new `info` overwrites its text instead of adding a line.
- **`selfAddr` belongs to one app instance.** It is derived from the user's key and the app message, so forwarding or re-sending the app gives a new `selfAddr` and starts with empty state. Don't rely on `selfAddr` values to match up data from an exported backup imported into another instance.
- **Never ship a file named `webxdc.js`.** The messenger replaces it with its own API script when loaded (on iOS even in subdirectories, e.g. `lib/webxdc.js`).

## Best Practices

1. **Use `sendUpdate` for state**: Store all important state in updates so it persists and syncs across devices.
2. **Keep payloads small**: Updates are sent to all chat members and stay in the chat history; send only what changed, not the whole state.
3. **Use summaries wisely**: The summary is shown beside the app icon in the chat, so make it informative.
4. **Handle initialization**: Use `setUpdateListener(..., 0)` to replay the full history when your app starts.
5. **Debounce updates**: For things like text editing, debounce your updates to avoid hitting rate limits.
6. **Use realtime for ephemeral data**: cursor positions, typing indicators, etc.

## Feature Detection

There is no API version number. Check whether a function exists before using it, especially for newer ones:

```javascript
if (window.webxdc.joinRealtimeChannel !== undefined) {
  // realtime is supported
}
```

## Upstream Docs

- [API overview](https://webxdc.org/docs/spec/api.html)
- [`sendUpdate`](https://webxdc.org/docs/spec/sendUpdate.html)
- [`setUpdateListener`](https://webxdc.org/docs/spec/setUpdateListener.html)
- [`sendToChat`](https://webxdc.org/docs/spec/sendToChat.html)
- [`importFiles`](https://webxdc.org/docs/spec/importFiles.html)
- [`selfAddr` and `selfName`](https://webxdc.org/docs/spec/selfAddr_and_selfName.html)
- [`joinRealtimeChannel`](https://webxdc.org/docs/spec/joinRealtimeChannel.html)
