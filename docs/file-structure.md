# File Structure

This document describes the directory layout and file organization of the
Delta Chat plugin for Hermes Agent.

## Plugin Directory Structure

```
~/.hermes/plugins/deltachat-platform/
├── plugin.yaml              # Plugin manifest and metadata
├── __init__.py              # Plugin entry point and registration
├── adapter.py               # Main Delta Chat adapter implementation
├── setup.py                 # Account setup helper with relay scraping
├── README.md                # Basic usage and installation
├── docs/
│   ├── version-compatibility.md  # Version requirements and checks
│   ├── file-structure.md         # This file
│   └── troubleshooting.md        # Common issues and solutions
└── skills/
    └── webxdc-converter/
        ├── SKILL.md              # Webxdc conversion skill
        └── references/
            └── webxdc-api.md      # Webxdc API documentation
```

## Hermes Profile Directory Structure

Each Hermes profile maintains its own isolated state and configuration.
The Delta Chat plugin creates its own subdirectory within each profile:

```
~/.hermes/profiles/<profile-name>/
├── .env                        # Environment variables (DELTACHAT_RPC_SERVER, etc.)
├── config.yaml                 # Hermes configuration
├── SOUL.md                     # Agent personality and system prompt
├── logs/
│   ├── gateway.log             # Gateway logs (includes version warnings)
│   └── gateway.error.log       # Gateway error logs
├── sessions/                   # Session history
└── deltachat-platform/                  # Delta Chat configuration (auto-created)
    └── <account-id>/
        ├── config.json         # Account configuration
        ├── keys/              # Encryption keys
        └── ...                # Other account data
```

## Default Profile

The default Hermes profile uses `~/.hermes/` directly:

```
~/.hermes/
├── .env
├── config.yaml
├── SOUL.md
├── logs/
│   ├── gateway.log
│   └── gateway.error.log
├── sessions/
└── deltachat-platform/                  # Delta Chat accounts
    └── <account-id>/
        └── ...
```

## Multiple Profiles Example

```
~/.hermes/
├── plugins/
│   └── deltachat-platform/              # Shared plugin code
├── profiles/
│   ├── work/
│   │   ├── .env                # DELTACHAT_RPC_SERVER=/usr/bin/dc-rpc
│   │   └── deltachat-platform/
│   │       └── account-1/     # Work Delta Chat account
│   └── personal/
│       ├── .env                # DELTACHAT_RPC_SERVER=/usr/bin/dc-rpc
│       └── deltachat-platform/
│           └── account-2/     # Personal Delta Chat account
└── deltachat-platform/                  # Default profile's Delta Chat config
    └── account-0/
```

## Key Directories Explained

### `deltachat-platform/`
- Created automatically by the plugin on first connection
- Contains Delta Chat account data for that Hermes profile
- Each profile has its own, ensuring isolation

### `logs/gateway.log`
- Contains all gateway activity
- Version warnings appear here during connection
- Debug messages for event handling

### `skills/webxdc-converter/`
- Bundled with the plugin
- Provides Webxdc conversion capability
- Accessible via: `skill_view("deltachat-platform:webxdc-converter")`
