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
| `sa_ensure_file(path, ...)` | **Work on a job file, attaching to the already-open copy** (no reload when SA already has it loaded); otherwise opens it — saving the current job first — and force-restarts SA with the file if the open cannot succeed. |
| `sa_current_file()` | Report which job file SA appears to have loaded (session tracker + SA window caption). COM-free. |
| `sa_dismiss_dialogs` | Close SA modal dialogs that are blocking MCP steps, right now. |
| `sa_dialog_watchdog` | Background auto-closer of SA modals: `start` / `stop` / `status`. Only acts while a COM call is in flight. |
| `sa_run_step(...)` | **Generic**: run any SA step by name with typed args + read outputs. |
| `sa_construct_point(group, name, x, y, z)` | Concrete example / pipeline test. |
| `sa_project_points(...)` | **Project points onto object(s)** at their closest point into a new point group (SA's «Проецировать точки на объекты»). |
| `sa_compare_points_objects(...)` | **Compare points to objects** ("Сравнить > Точки > Объекты"): create a vector group of deviation whiskers — one arrow per point showing how far it is from the closest object. |
| `sa_vector_group_props(...)` | **Vector group statistics** (`Get Vector Group Properties`: counts, in/out-of-tolerance, magnitude stats) + optional per-vector dump (begin/end/delta/ijk/magnitude). |
| `sa_vector_group_style(...)` | **Style vector groups** (arrows/colour bar/tolerance band), incl. auto-range of the saturation limits. |
| `sa_best_fit(...)` + `sa_best_fit_<shape>` | Best-fit plane/sphere/cylinder/cone/circle/line to a point group or raw coordinates. |
| `sa_best_fit_from_points(...)` | Best fit to an **explicit list of points** — a subset of one group or points across several groups/collections (step `Fit Geometry to Points`). |
| `sa_identify_geometry(...)` | **Identify the shape** a point cloud was measured from: fits line/plane/circle/sphere/cylinder/cone offline and ranks them (`best_geometry`, `confidence`, `recognized`, `notes`). Works from raw `coordinates` with no SA running. |
| `sa_point_coordinates(...)` | **Read raw point coordinates** for offline analysis: a whole group or an explicit list of points (`points` may span groups/collections), each with its stored probe/reflector offsets. Read-only; returns points in order + `bounds` (min/max/centroid). |

### Example: project a point group onto a plane

```
sa_project_points(
  objects=["Плоскость"],              # target: any fitted geometry
  point_groups=["Точки_сверху"],      # source: whole point group(s) and/or `points`
  result_group="Точки_проекция",      # new point group (default "<source>_proj")
  collection="A"
)
# -> {projected: True, result_count: N, result_group: "Точки_проекция",
#     status_code: 2|3|4 (advisory), skipped: [...], replaced: ...}
```
Every source point lands exactly ON the object (live-verified: plane z=25 →
z≈0). SA 2015 can report a fatal status while still creating the points and
can silently skip unprojectable points, so `projected`/`result_count` come
from what the new group actually contains; missing targets are listed under
`skipped_points`. A pre-existing group with the result name is deleted first
(`replaced: True`). `projection_type="Points on Offset Object"` +
`probe_offset_mm` backs the points off the surface along its normal.

### Example: deviation whiskers — compare points to an object

```
sa_compare_points_objects(
  objects=["MCP_BF_плоскость"],        # targets: fitted geometry / surface
  point_groups=["Точки_сверху"],        # sources: whole group(s) and/or `points`
  result_group="Точки_сверху_dev",      # new VECTOR group (default "<source>_dev")
  collection="A"
)
# -> {created: True, vector_count: N, vector_group: "A::Точки_сверху_dev",
#     properties: {total_vectors, average_magnitude, ...},
#     status_code: 2|3|4 (advisory), replaced: ...}
```
Each point produces one whisker whose magnitude is its deviation from the
closest object (default direction `Object To Probe Vectors` = arrows from
the object to the measured point; `Probe To Object Vectors` reverses them,
same magnitudes). Stored per-point probe/reflector offsets are applied by
default (`use_stored_offsets=True`) — for measured groups the whisker is the
true surface deviation. Same flaky-status caveat as the projection: success
is decided by the created group. Read the arrows back with
`sa_vector_group_props(vector_group=..., include_vectors=True)` and style
them (colours, tolerance band, colour bar) with `sa_vector_group_style`.

### Modal dialogs that hang MCP — auto-closed

SA sometimes pops a modal dialog mid-step (e.g. **"Object Not Found"** for a
wrong object name). The MP step waits on it, so every MCP call looks hung
until a human clicks the dialog away. Two things fix that:

- **Watchdog (automatic):** the first COM tool call arms a background thread
  (`sa_dialog_watchdog`) that polls the SA GUI + SDK engine processes every
  0.5 s and closes blocking dialogs (WM_CLOSE, then Cancel/single-OK button
  click for survivors — a plain MB_OK error box has no close button, only its
  OK ends it). A mid-step modal now self-heals in about half a second.
  **Crucially, it only scans while a COM call is actually in flight** (the
  server waiting on SA). When no MCP step is running, SA is idle or being
  driven by hand, and no window is touched — construction dialogs, the
  save-on-exit prompt, etc. are left alone, so auto-close cannot interfere
  with manual work or make SA impossible to close.
- **One-shot tool:** `sa_dismiss_dialogs` closes whatever is up right now —
  call it and retry the stuck step. It needs no COM, so it works even while a
  step is stuck. (Unconditional: it also closes a dialog a human is looking
  at, so use it deliberately.)

Control: `sa_dialog_watchdog stop` disarms it for the session; `start` re-arms
it. With the step gate above you normally do NOT need to stop it while working
SA by hand — only if a COM step and manual work would overlap (e.g. you keep a
construction dialog open while the bot runs a step). Poll interval and an
optional title filter (`title_contains`) are parameters. Pure Win32 calls —
the watchdog never touches COM and never kills anything; a wedged
`SpatialAnalyzerSDK.exe` engine still needs `taskkill //F //IM
SpatialAnalyzerSDK.exe` (see AGENTS.md). Offline regression (no SA needed):
`python _t_dialogs.py`.

### Open a job file without discarding the already-open copy

SA holds one job at a time, and its `Open SA File` step always **discards** the
loaded job and reloads from disk — re-opening a file that is already open
silently throws away unsaved in-memory state (previous fits, manual GUI
edits). `sa_ensure_file` is the file-first bootstrap to use before acting on a
job: if the file is already the loaded job it just attaches the SDK bridge and
does nothing else; otherwise it opens the file (saving the current job first
when its own file is known) and force-restarts SA with the file only if the
open cannot succeed.

```
sa_ensure_file(r"C:\jobs\6.01.25 — обработка.xit")
# -> {attached: True, already_open: True, opened: False, restarted: False,
#     evidence: {tracked_path: ..., window_title: "SpatialAnalyzer - ...",
#                how: "tracked"|"window-title"}, error: None}
```

SA 2015's SDK cannot report which file the GUI has open, so detection uses the
two signals that exist: what THIS server session loaded/launched, and the SA
main-window caption (catches a job opened by hand or by an earlier MCP
session). `sa_current_file()` shows that evidence so you can decide what to do
before opening. Offline logic regression (no SA needed): `python _t_openlogic.py`.

### Example: identify the shape of a point cloud

```
# Coordinates only -> fully offline, no SA needed.
sa_identify_geometry(
  coordinates=[[x,y,z], ...]
)
# -> {ok: True, best_geometry: "cylinder", best_parameters: {...},
#     best_rms_mm: 0.02, confidence: "high", recognized: True,
#     candidates: [...ranked fits...], notes: [...]}
```

`candidates` ranks all six primitives by fit residual, `best` is the semantic
pick (e.g. a coplanar ring → `circle`, not the plane that also fits it),
`recognized: False` means nothing fits well — the cloud is probably not a
single primitive. Same tool reads an existing `point_group` or individual
`points` (SA must be connected then).

### Example: best-fit to a subset of points from several groups

```
sa_best_fit_from_points(
  geometry_type="cylinder",
  object_name="Цилиндр_по_выборке",
  points=["A::т контур::12", "A::т контур::45", "B::Другие::3"],  # any groups
  collection="A"
)
# -> {constructed: True, geometry: {radius: ...}, stats: {rms: ...},
#     point_source: {point_count: 3, groups: ["т контур", "Другие"]}}
```

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

### Example: pull raw point coordinates for offline analysis
```
sa_point_coordinates(
  point_group="т контур",              # whole group (or `points` + `group`/`collection`)
  collection="A",
  max_points=5                          # cap a huge group; omit for all
)
# -> {ok: True, count: 5, total_points: 376, truncated: True,
#     offsets_read: True,
#     bounds: {min: [x,y,z], max: [x,y,z], centroid: [x,y,z]},
#     points: [{name: "т контур::1", full: "A::т контур::1",
#               x: ..., y: ..., z: ..., planar_offset: 0.0,
#               radial_offset: 19.05}, ...]}
```
Read-only export of the working coordinates (+ stored probe/reflector
offsets) of a point group or of an explicit point list, for the agent to
analyze on its own. `include_offsets=False` skips the per-point offset step
(halves the COM round trips on large groups); point names come back in group
order.

---

## Extending

To add a typed tool for a new SA operation:
1. Find the exact step + argument names (SA MP tree, or
   `…\SA SDK\Examples\SDKTesterCSharp\Form1.cs`).
2. In `server.py`, add a `@mcp.tool()` that sets the step + args and calls
   `ExecuteStep()`. Mirror `sa_construct_point`.

See `AGENTS.md` for the design rules (COM threading, `_unwrap`, etc.).
