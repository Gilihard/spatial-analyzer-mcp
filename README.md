# Spatial Analyzer MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets an
LLM client (Claude Desktop, opencode, etc.) drive **SpatialAnalyzer (SA)** from
New River Kinematics through its COM automation SDK.

> Status: **minimal working prototype**. Connect + generic step runner +
> one example tool (construct point). Ready to extend.

---

## What this gives you

Once connected, an LLM can perform SA operations by name:

- Connect to a running SA process
- Run **any** SA "Measurement Plan step" (Construct Point/Sphere/Plane,
  transforms, reports, exports, …) via one generic tool
- Read back results

SA exposes its automation as a **step-based** COM API:
`SetStep(name) → Set<Type>Arg(name, value) → ExecuteStep() → Get<Type>Arg(...)`.

---

## Requirements

- Windows (SA is COM-only)
- Python 3.10+ (developed on 3.14)
- **SpatialAnalyzer installed** (registers `SpatialAnalyzerSDK.SpatialAnalyzerSDKClass`)
- A Python environment with `pywin32` and `mcp`

## Install

```bat
pip install -r requirements.txt
```

> If `pywin32` errors on import, run once:
> `python Scripts\pywin32_postinstall.py -install`

---

## Step 1 — verify the COM bridge (no SA needed)

```bat
python test_sdk.py
```

Expected: `[OK]` for the COM object creation. This confirms SA's SDK is
registered on this machine.

## Step 2 — live test against a running SA

1. Open SpatialAnalyzer.
2. Make sure SDK connections are enabled
   (Utilities → SDK Settings, or default on many builds).
3. Run:

```bat
python test_sdk.py localhost
```

If it connects and builds `TestGrp / McpTestPt`, the whole pipeline works.

## Step 3 — run the MCP server

```bat
python server.py
```

It speaks MCP over stdio — it will just wait for messages. That is correct.

---

## Step 4 — plug it into a client

### Claude Desktop
Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "spatial-analyzer": {
      "command": "python",
      "args": ["C:\\Path\\To\\SA MCP\\server.py"]
    }
  }
}
```
Restart Claude Desktop. You should see `spatial-analyzer` tools available.

### opencode / opencode
Add to your opencode server config (MCP section):

```json
{
  "mcpServers": {
    "spatial-analyzer": {
      "type": "local",
      "command": ["python", "C:\\Path\\To\\SA MCP\\server.py"]
    }
  }
}
```

### MCP Inspector (great for manual testing)

```bat
npx -y @modelcontextprotocol/inspector python server.py
```

Opens a browser UI: list tools, call `sa_status`, `sa_connect`, `sa_run_step`.

---

## Dev loop — auto build & deploy to ZCode (WIP server)

The server is still being changed, so don't hand-edit client configs. One
command validates the current state of the project and (re)registers it in
ZCode:

```bat
python build_mcp.py            :: validate + register + next steps
python build_mcp.py deploy     :: same as default
python build_mcp.py build      :: validate only (syntax + MCP tools/list probe)
python build_mcp.py start      :: run server.py in foreground (debug)
deploy.cmd                     :: double-click shortcut for the default
```

What it does:

- Byte-compiles every `*.py` (syntax gate), then starts `server.py` over MCP
  stdio and lists its tools. COM is never touched during this: importing
  `server.py` is COM-free, and the SDK engine (`SpatialAnalyzerSDK.exe`) is
  born only on the first tool call against a running SA GUI.
- Writes the `spatial-analyzer` entry into the **user** config
  `~/.zcode/cli/config.json` (every project/session, regardless of which
  folder is open — this is the default scope).
  `--scope workspace` / `--workspace` writes `<repo>/.zcode/config.json`
  instead (auto-connects only while that folder is the opened project, so
  after a restart ZCode may open another project and the server is gone —
  use user scope when the tools must always be available). The entry points
  straight at `<python.exe> <repo>\server.py`, so ZCode always launches the
  current code as a fresh process.

After changing code: rerun `python build_mcp.py`, then reload the MCP server
in ZCode (Settings → MCP → Reload, or start a new session in this folder) —
the server is a per-launch process and never picks up edits while running.

## Tools provided

| Tool | Purpose |
|------|---------|
| `sa_status` | Is the server alive + connected? Call first. |
| `sa_connect(host)` | Connect to a running SA process (default `localhost`). |
| `sa_dismiss_dialogs` | Close SA modal dialogs that are blocking MCP steps, right now. |
| `sa_dialog_watchdog` | Background auto-closer of SA modals: `start` / `stop` / `status`. |
| `sa_run_step(...)` | **Generic**: run any SA step by name with typed args + read outputs. |
| `sa_construct_point(group, name, x, y, z)` | Concrete example / pipeline test. |

### Modal dialogs that hang MCP — auto-closed

SA sometimes pops a modal dialog mid-step (e.g. **"Object Not Found"** for a
wrong object name). The MP step waits on it, so every MCP call looks hung
until a human clicks the dialog away. Two things fix that:

- **Watchdog (automatic):** the first COM tool call arms a background thread
  (`sa_dialog_watchdog`) that polls the SA GUI + SDK engine processes every
  0.5 s and closes blocking dialogs (WM_CLOSE, then Cancel/single-OK button
  click for survivors — a plain MB_OK error box has no close button, only its
  OK ends it). A mid-step modal now self-heals in about half a second.
- **One-shot tool:** `sa_dismiss_dialogs` closes whatever is up right now —
  call it and retry the stuck step. It needs no COM, so it works even while a
  step is stuck.

Control: `sa_dialog_watchdog stop` disarms it for the session (do this if you
are operating SA's GUI by hand); `start` re-arms it. Poll interval and an
optional title filter (`title_contains`) are parameters. Pure Win32 calls —
the watchdog never touches COM and never kills anything; a wedged
`SpatialAnalyzerSDK.exe` engine still needs `taskkill //F //IM
SpatialAnalyzerSDK.exe` (see AGENTS.md). Offline regression (no SA needed):
`python _t_dialogs.py`.

### Example: construct a sphere via the generic tool
```
sa_run_step(
  step_name="Construct Sphere",
  string_args={"Sphere Name": "MySphere"},
  vector_args={"Sphere Center (in working coordinates)": [6, 12, 18]},
  double_args={"Sphere Radius": 11.5}
)
```
The exact step/arg names come from SA's MP tree or the SDK examples folder.

---

## Extending

To add a typed tool for a new SA operation:
1. Find the exact step + argument names (SA MP tree, or
   `…\SA SDK\Examples\SDKTesterCSharp\Form1.cs`).
2. In `server.py`, add a `@mcp.tool()` that sets the step + args and calls
   `ExecuteStep()`. Mirror `sa_construct_point`.

See `AGENTS.md` for the design rules (COM threading, `_unwrap`, etc.).
