# Version Compatibility

The plugin supports Delta Chat core **2.51.0 up to and including 2.60.0**.

## Why there is a ceiling at all

Delta Chat core does **not** use semantic versioning:

| Field | What a bump means |
|---|---|
| major | only heavily breaking **wire format** changes |
| minor | a plain counter — **may contain breaking API changes** |
| patch | never used |

So a minor bump carries no compatibility promise, and "newer" cannot be read as
"at least as good". 2.61.0 is the concrete case: it removed
`Account::Configured.addr` and `Contact.nameAndAddr`, both of which this adapter
reads (`adapter.py` `get_my_address()` and the contact-name fallbacks). Neither
removal crashes — every read is a `.get()` inside a fallback chain — so the
symptom is a blank sender name or a missing bot address rather than an error,
which is precisely why the ceiling is explicit instead of discovered in
production.

The window is also expressed as `deltachat-rpc-server>=2.51.0,<2.61` in
`plugin.yaml`'s `python_dependencies`, so a Hermes that installs the plugin's
dependencies lands inside it by itself. The two must be kept in step.

## Version Check

When the plugin connects, it automatically checks the Delta Chat core version:

- **Older than `MIN_DC_VERSION`**: connection is **rejected**
- **Inside the window** (min .. `MAX_TESTED_DC_VERSION`): no warning — the whole
  range is verified, so warning on it would be a false alarm on every connect
- **Above `MAX_TESTED_DC_VERSION`**: a **WARNING** is logged and the connection
  proceeds. It names what to watch for (blank contact names, missing own
  address), because that is how a removed field shows up here
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

## Tested Ceiling

`MAX_TESTED_DC_VERSION` is **2.60.0**: the newest release whose entire RPC
surface — every method the plugin calls, with its positional parameters, plus
the `MsgData` send payload — was checked against that release's own `--openrpc`
spec and against a live server, not inferred from release notes.

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

## Updating the Window

Raising the **floor** (a feature needs a newer core):

1. `MIN_DC_VERSION` in `adapter.py`
2. the `>=` floor in `plugin.yaml`'s `python_dependencies`
3. this document

Raising the **ceiling** after verifying a newer release:

1. diff the new release's spec against the current one, e.g.
   ```bash
   deltachat-rpc-server --openrpc > new.json
   jq -n --slurpfile a deltachat-rpc-openrpc.json --slurpfile b new.json \
     '($a[0].methods|map(.name)) - ($b[0].methods|map(.name))'   # removed methods
   jq '.components.schemas.Contact.properties | keys' new.json    # removed fields
   ```
   Field removals matter as much as method removals, and they do not show up in
   the method list — check the schemas for every type the adapter reads.
2. `MAX_TESTED_DC_VERSION` in `adapter.py`
3. the `<` ceiling in `plugin.yaml`
4. this document, and regenerate `deltachat-rpc-openrpc.json` from the new
   release so the checked-in spec matches what actually runs

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
