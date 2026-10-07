# Claude JustDoWork Recovery Proxy

A local Anthropic-compatible API proxy that routes [Claude Code](https://docs.anthropic.com/en/docs/claude-code) requests through the JustDoWork upstream endpoint. It bridges the tool compatibility gap between Claude Code and the JustDoWork relay, handling tool-name mapping, streaming simulation, and usage correction automatically.

It fixes the core problem: the relay only supports a handful of native tool names, but Claude Code sends dozens. Without the proxy, most tool calls fail silently or crash the session.

## How it works

The proxy sits on `127.0.0.1:8181` between Claude Code and the upstream:

```
Claude Code  ──▶  localhost:8181 (proxy)  ──▶  upstream relay
                  • maps tool names
                  • emulates extra tools via text protocol
                  • rewrites inflated usage stats
                  • simulates SSE streaming
```

**Native tools** (Read, Write, Edit, Bash, Glob, Grep, WebFetch) are renamed to lowercase and forwarded through the relay's normal tool-calling mechanism.

**Extra tools** (TodoWrite, WebSearch, Agent, MCP tools, etc.) are converted to a `<tool_call>` text protocol — injected into the system prompt with their schemas, and results are wrapped in `<tool_result>` blocks.

**4-Step JSON Repair** — recovers malformed JSON in tool calls (literal newlines, unescaped characters, missing/stray brackets) so complex edits and multi-agent calls never get silently dropped.

**Token Saving & Timeout Protection** — truncates massive tool output bursts and bounds history to prevent Cloudflare 524 timeouts and unnecessary token burn.

**Usage correction** — the relay pads input token counts, which makes Claude Code think the context window is full. The proxy rewrites usage to realistic estimates.

## Requirements

- Python 3.8+
- `flask`, `requests` (plus `pystray` and `pillow` for the tray icon)

## 1-Click Install (Windows)

Just double-click **`setup.bat`** (or run `powershell -ExecutionPolicy Bypass -File setup.ps1`).

It handles the entire setup automatically:
1. Creates the Python virtual environment and installs dependencies.
2. Prompts once for your JustDoWork API key (saved securely to `key.txt`).
3. **Automatically configures Claude Code** (`~/.claude/settings.json`) with the correct base URL, model, and required flags (backing up any existing settings first).
4. Launches the proxy and system tray icon in the background.
5. Runs a health check to confirm everything is working.

Once it completes, you can immediately open any terminal and run:
```bash
claude
```

## Manual Setup (Any OS)

If you prefer to configure manually:

1. Setup environment and dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install flask requests
   ```

2. Export your API key and start the proxy:
   ```bash
   export UPSTREAM_API_KEY='your-key'
   python agent_proxy.py
   ```

3. Update your `~/.claude/settings.json`:
   ```json
   {
     "env": {
       "ANTHROPIC_BASE_URL": "http://127.0.0.1:8181",
       "ANTHROPIC_MODEL": "claude-opus-4-8",
       "ANTHROPIC_API_KEY": "justdowork",
       "ENABLE_TOOL_SEARCH": "false"
     }
   }
   ```
   > **Important:** `ENABLE_TOOL_SEARCH` must be `"false"` — the proxy relies on this.

## Use in Claude Desktop (GUI / CC Switch)

If you use Claude Desktop with custom gateway switching (CC Switch):

1. Start the proxy via `setup.bat`.
2. In your Claude Desktop settings GUI, configure your Gateway / CC Switch profile:
   - **Base URL / Gateway**: `http://127.0.0.1:8181`
   - **Auth Scheme**: `bearer`
   - **API Key**: Any dummy value (e.g. `justdowork` — the proxy attaches your real key)
   - **Model**: `claude-opus-4-8`
3. Save and use Claude Desktop normally!

To switch back to AgentRouter or any other provider, just change the Base URL back to your provider's URL in the GUI.

## Verify

Right-click the tray icon to check status, view logs, restart the proxy, or exit.

The proxy logs per-request summaries to `debug_log.txt`. If something is wrong, check `proxy_out.log` for startup errors.

## Probe tool names

Not sure which tool names the upstream supports? Run the probe first:

```bash
python names_probe.py
```

It sends a request with ~40 candidate tool names and reports which ones the relay accepts. Add new discoveries via the `NAME_MAP_EXTRA` env var.

## Config

| Variable | Default | Description |
| --- | --- | --- |
| `UPSTREAM_API_KEY` | *(required)* | API key for the upstream relay |
| `TARGET_URL` | `https://api.justwoker.icu` | Upstream endpoint |
| `PORT` | `8181` | Local proxy port |
| `DEBUG_DUMP` | off | Set to `1` to dump full request/response JSON to `debug_dump/` |
| `NAME_MAP_EXTRA` | *(none)* | JSON object of additional tool name mappings |

## Log files

| File | Contents |
| --- | --- |
| `debug_log.txt` | Per-request summaries (tools used, stop reasons, usage rewrites). Secrets scrubbed. |
| `proxy_out.log` | Proxy stdout/stderr — look here if it fails to start. |
| `tray.log` | Tray launcher log (spawn, restart, crash detection). |

## Secrets and debug data

- **`key.txt` must never be committed.** It is listed in `.gitignore`, along with `debug_dump/` and `debug_log.txt`.
- **Request dumps are off by default.** With `DEBUG_DUMP=1` the proxy writes every raw request body to `debug_dump/`. Those files contain your full system prompt, conversation history, and the contents of any file the agent read — treat them as sensitive.
- `debug_log.txt` scrubs key-shaped tokens from all entries, including upstream error bodies.

## File overview

| File | What it does |
| --- | --- |
| `agent_proxy.py` | Core Flask proxy server on `127.0.0.1:8181` |
| `proxy_tray.py` | Windows system tray icon — manages the proxy lifecycle |
| `start_proxy.bat` | Windows setup + background launcher |
| `start_tray.vbs` | VBScript wrapper to launch tray with `pythonw.exe` (no console) |
| `names_probe.py` | Discovers which tool names the upstream accepts |

## License

MIT
