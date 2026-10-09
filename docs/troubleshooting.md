# Troubleshooting

Common issues and solutions for the Delta Chat plugin.

## Plugin Not Loading

**Symptom:** Plugin doesn't appear in `hermes plugins list`

**Check:**
```bash
HERMES_PLUGINS_DEBUG=1 hermes plugins list
```

**Common causes:**
- Plugin not in correct directory: `~/.hermes/plugins/deltachat-platform/`
- Missing `plugin.yaml` or `__init__.py`
- Syntax error in plugin files
- Missing dependencies

**Fix:**
```bash
# Verify directory structure
ls -la ~/.hermes/plugins/deltachat-platform/

# Check for syntax errors
python3 -m py_compile ~/.hermes/plugins/deltachat-platform/__init__.py
python3 -m py_compile ~/.hermes/plugins/deltachat-platform/adapter.py

# Install dependencies
pip install deltachat2
```

## Connection Errors

**Symptom:** Gateway fails to connect to Delta Chat

**Check:**
```bash
# View gateway logs
tail -f ~/.hermes/profiles/<name>/logs/gateway.log

# Verify RPC server is accessible
which deltachat-rpc-server

# Test RPC server manually
deltachat-rpc-server --help
```

**Common causes:**
- `deltachat-rpc-server` binary not in PATH
- Binary not installed
- Permission issues
- Firewall blocking RPC communication

**Fix:**
```bash
# Set explicit path in environment
echo 'DELTACHAT_RPC_SERVER=/path/to/deltachat-rpc-server' >> ~/.hermes/.env

# Or for a specific profile
hermes -p my-profile config set env.DELTACHAT_RPC_SERVER /path/to/deltachat-rpc-server

# Install binary
pip install deltachat-rpc-server
```

## No Accounts Found

**Symptom:** "No Delta Chat accounts found" error

**Check:**
```bash
# Verify deltachat-platform directory exists
ls -la ~/.hermes/deltachat-platform/

# Or for specific profile
ls -la ~/.hermes/profiles/<name>/deltachat-platform/
```

**Common causes:**
- No Delta Chat account created yet
- Account created in different config directory
- DC_ACCOUNTS_PATH pointing to wrong location

**Fix:**
```bash
# Create an account using setup.py or Delta Chat desktop/mobile app first
# Then the account will be in the correct directory
```

## Version Warning

**Symptom:** "Delta Chat version X.X.X is newer than expected" warning

**What it means:**
- Your Delta Chat version is newer than what the plugin was tested with
- Most features should still work
- Some newer features may not be available through the plugin

**Check your version:**
```bash
deltachat-rpc-server --version
```

**Solutions:**
1. **Update the plugin:** Check if a newer version of this plugin exists
2. **Update MIN_DC_VERSION:** Edit `adapter.py` to match your version:
   ```python
   MIN_DC_VERSION = "2.52.0"  # Change to your version
   ```
3. **Ignore it:** The warning is informational; connection will still work

## Message Sending Fails

**Symptom:** Messages don't send, `send()` returns error

**Check:**
```bash
# Check for specific errors in logs
grep -i error ~/.hermes/profiles/<name>/logs/gateway.log
```

For more detail, set `logging.level: DEBUG` in the profile's `config.yaml` and
restart the gateway; the DEBUG lines land in `agent.log` (`gateway.log` stays at
INFO). DEBUG logs every JSON-RPC request and response in full, which includes
message text, contact addresses, file paths, and the transport password while
an account is being set up. Switch it back off afterwards, and read the log
before you share it. At the default INFO level the adapter logs IDs and states,
not message content.

**Common causes:**
- Invalid chat_id (must be integer string)
- Account not connected
- Network connectivity issues

**Fix:**
```bash
# Verify chat_id is valid
# Use get_chat_info() to check if chat exists
```

## File Sending Fails

**Symptom:** `.xdc` or other files don't send

**Check:**
```bash
# Verify file exists and is readable
ls -la /path/to/your/file.xdc

# Check file permissions
file /path/to/your/file.xdc
```

**Common causes:**
- File path is incorrect
- File permissions prevent reading
- File type not supported by Delta Chat

**Fix:**
```bash
# Use absolute paths for files
# Ensure file exists before sending
```

**Docker sandbox (`MEDIA:/workspace/...`) in strict mode:** Hermes maps
`/workspace/` to the host sandbox (`~/.hermes/sandboxes/docker/<...>/workspace/`)
before validating it. With `HERMES_MEDIA_DELIVERY_STRICT=1`, that sandbox is not
an allowed root, so a file is only accepted while it is "recently produced"
(default 600 s, never if `trust_recent_files` is off). Older files are dropped
with `Skipping MEDIA directive path (denied by the delivery policy)`. Add the
sandbox directory to `HERMES_MEDIA_ALLOW_DIRS` (or
`gateway.media_delivery_allow_dirs`) to deliver them regardless of age. The same
fix applies if your `HERMES_HOME` sits under a denied system prefix
(e.g. `/var/lib/...`), which rejects sandbox files even in default mode.

## Voice Call Issues

**Symptom:** Incoming call events not handled

**Note:** Voice call support is Phase 4 (stretch goal) and not yet implemented.

**Current status:**
- `IncomingCall` events are logged but not processed
- WebRTC bridge not yet implemented
- Requires aiortc and additional setup

## Performance Issues

**Symptom:** Slow message delivery, high CPU usage

**Check:**
```bash
# Monitor RPC server process
top -p $(pgrep -f deltachat-rpc-server)

# Check event loop latency
# Enable debug logging for timing info
```

**Common causes:**
- Many active chats
- Large message history
- Slow network connection

**Solutions:**
- Limit number of active chats
- Archive old messages
- Use faster network connection

## Cleanup

**To completely remove the plugin:**
```bash
# Remove plugin files
rm -rf ~/.hermes/plugins/deltachat-platform/

# Remove profile-specific config
rm -rf ~/.hermes/profiles/*/deltachat-platform/
rm -rf ~/.hermes/deltachat-platform/

# Remove from enabled plugins
hermes plugins disable deltachat-platform
```
