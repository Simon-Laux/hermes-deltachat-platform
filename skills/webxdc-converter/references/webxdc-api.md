# Webxdc API Reference

This document describes the JavaScript API available to webxdc apps.

Load the API with `<script src="webxdc.js"></script>` before your own scripts. The messenger provides `webxdc.js`, so do not put it in the `.xdc`.

## Core API

### Properties

- `window.webxdc.selfAddr` (string) - Unique address identifying this instance. Same for all instances of the same user. Can be used to distinguish different users in a chat.
- `window.webxdc.selfName` (string) - Display name of the current user
- `window.webxdc.desktopApiVersion` (number) - API version of the webxdc desktop app (if running in desktop). `0` if running in a messenger.

### Methods

#### `sendUpdate(update, description)`
Send an update to all instances of the app in the chat.

- `update`: Object with properties:
  - `payload`: Any JSON-serializable data (required)
  - `info`: Short description shown in chat (~50 chars) (optional)
  - `summary`: Short summary shown beside app icon in chat list (~20 chars) (optional)
- `description`: ?

**Note:** Updates are rate-limited to ~1 per second. If you send more, some may be dropped.

**Example:**
```javascript
window.webxdc.sendUpdate({
  payload: { score: 100 },
  info: "Player reached 100 points",
  summary: "Score: 100"
}, "");
```

#### `setUpdateListener(callback, initialState)`
Register a callback that is called for each update received.

- `callback`: Function called with `update` object containing:
  - `payload`: The payload data
  - `info`: The info string
  - `summary`: The summary string
  - `serialized`: The serialized update (for advanced use)
  - `selfAddr`: Address of the sender
- `initialState`: If `1`, the callback is also called with the last update immediately

**Example:**
```javascript
window.webxdc.setUpdateListener((update) => {
  console.log("Received update:", update.payload);
  // Rebuild state from update.payload
}, 1);  // Call with last update immediately
```

#### `sendToChat(message, description)`
Send a text message to the chat.

- `message`: Text to send (required)
- `description`: ?

**Example:**
```javascript
window.webxdc.sendToChat("Hello from my app!", "");
```

#### `importFiles()`
Open a file picker to import files from the device.

**Returns:** Promise that resolves with an array of File objects.

**Example:**
```javascript
const files = await window.webxdc.importFiles();
```

#### `getAllInstanceIds()`
Get IDs of all instances of this app in the chat.

**Returns:** Promise that resolves with an array of instance IDs.

**Example:**
```javascript
const instanceIds = await window.webxdc.getAllInstanceIds();
```

#### `sendToInstance(instanceId, message)`
Send a message to a specific instance.

- `instanceId`: Target instance ID
- `message`: Message to send

**Example:**
```javascript
const ids = await window.webxdc.getAllInstanceIds();
ids.forEach(id => {
  window.webxdc.sendToInstance(id, "Hello instance!");
});
```

### Realtime Channel

#### `joinRealtimeChannel()`
Join the realtime channel for this chat.

**Returns:** Channel object with methods:
- `channel.send(data)` - Send binary data (Uint8Array)
- `channel.setListener(callback)` - Set listener for incoming data
- `channel.close()` - Close the channel

**Example:**
```javascript
const channel = window.webxdc.joinRealtimeChannel();
channel.setListener((data) => {
  console.log("Received:", new TextDecoder().decode(data));
});
channel.send(new TextEncoder().encode("Hello!"));
```

**Note:** Realtime channels are ephemeral - data is NOT persisted and NOT replayed when the app restarts.

## Rate Limits

- `sendUpdate()`: ~1 update per second. If you send faster, updates may be dropped.
- `sendToChat()`: Similar rate limits apply.

## Best Practices

1. **Use `sendUpdate` for state**: Store all important state in updates so it persists and syncs across devices.
2. **Keep payloads small**: Updates are sent to all chat members, so keep them under ~1KB if possible.
3. **Use summaries wisely**: The summary is shown in the chat list, so make it informative.
4. **Handle initialization**: Use `setUpdateListener(..., 1)` to get the current state when your app starts.
5. **Debounce updates**: For things like text editing, debounce your updates to avoid hitting rate limits.
6. **Use realtime for ephemeral data**: cursor positions, typing indicators, etc.

## Version Compatibility

Check `window.webxdc.desktopApiVersion` to detect feature support:
- Version 0: Basic messenger (all basic features)
- Version 1: Desktop app with additional features

Feature availability:
- `sendUpdate`, `setUpdateListener`: Available in all versions
- `sendToChat`: Available in all versions
- `importFiles`: Available in desktop and newer mobile versions
- `joinRealtimeChannel`: Available in newer versions only
- `getAllInstanceIds`, `sendToInstance`: Available in newer versions only
