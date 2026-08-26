# Version Compatibility

The plugin requires Delta Chat core version **2.51.0** or higher.

## Version Check

When the plugin connects, it automatically checks the Delta Chat core version:

- **Older version**: Connection will be **rejected** (plugin requires 2.51.0+)
- **Newer version**: A **WARNING** is logged (API may have changed)
- **Compatible**: No warning for exact match
- **Unknown**: Connection is **rejected** too. An unreadable version string
  parses as `0.0.0` and compares as too old, and a `get_system_info` that
  raises means the RPC transport is broken anyway.

## Minimum Version

The minimum required version is **2.51.0** (current stable from nixpkgs).
This is the version available via:
```bash
nix run nixpkgs#deltachat-rpc-server -- --version
# Output: 2.51.0
```

If you need to use an older version, you must downgrade the plugin or update your Delta Chat installation.

## Check Your Version

```bash
# Using the binary directly
deltachat-rpc-server --version

# On NixOS
nix run nixpkgs#deltachat-rpc-server -- --version

# Via RPC — the key is deltachat_core_version, and the value carries a
# leading "v" (e.g. "v2.51.0")
rpc.call("get_system_info")["deltachat_core_version"]
```

## Updating the Minimum Version

If you add features that require a newer Delta Chat version:

1. Update the minimum in `adapter.py`:
   ```python
   MIN_DC_VERSION = "2.51.0"  # Bump this
   ```

2. Update this documentation

## API Reference

To view the full Delta Chat JSON-RPC API:

```bash
# Direct from binary
deltachat-rpc-server --openrpc

# On NixOS
nix run nixpkgs#deltachat-rpc-server -- --openrpc

# Save to file
deltachat-rpc-server --openrpc > dc-api.json
```

The OpenRPC schema includes all available methods, parameters, and return types.
