# Spatial Analyzer MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets
an LLM client drive **SpatialAnalyzer (SA)** (New River Kinematics) through
its COM SDK. SA automation is step-based: `SetStep(name)` →
`Set<Type>Arg(name, value)` → `ExecuteStep()`.

> Agent internals (COM threading, engine ordering, confirmed SA step names):
> see `AGENTS.md`.

## Requirements & install

- Windows, Python 3.10+ (`pywin32`, `mcp`), SpatialAnalyzer installed.
- `pip install -r requirements.txt`
  (if pywin32 import errors: `python Scripts\pywin32_postinstall.py -install`)

## Quickstart

```bat
python test_sdk.py            :: COM bridge smoke test (no SA needed)
:: open SpatialAnalyzer (Utilities → SDK Settings enabled), then:
python test_sdk.py localhost  :: live connect + construct a test point group
python server.py              :: MCP server over stdio (waits for messages)
```

Every tool self-bootstraps: the first COM call launches/attaches SA and the
SDK engine as needed. `sa_status` is the only tool that never spawns the
bridge.

## Deploy to ZCode

```bat
python build_mcp.py           :: byte-compile + tools/list probe + register in ZCode
python build_mcp.py build     :: validate only
python build_mcp.py start     :: run server.py in foreground (debug)
deploy.cmd                    :: double-click wrapper
```

Registers `spatial-analyzer` in `~/.zcode/cli/config.json` (user scope =
every project; `--scope workspace` = only this repo). After changing code,
rerun and reload MCP in ZCode (Settings → MCP → Reload / new session).

## Clients

Claude Desktop — add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{"mcpServers": {"spatial-analyzer": {"command": "python",
  "args": ["C:\\Path\\To\\SA MCP\\server.py"]}}}
```

Manual testing: `npx -y @modelcontextprotocol/inspector python server.py`.

## Tools

| Tool | Purpose |
|------|---------|
| `sa_status` | Server alive + connected? Call first. Never spawns the bridge. |
| `sa_ensure_file(path)` | **Work on a job file, attach-first**: if SA already has it loaded, attach without reloading (in-memory state kept); else save the current job, open the file, force-restart SA with it if the open fails. |
| `sa_current_file()` | Report which job file SA appears to have loaded (session tracker + window caption). |
| `sa_open_file` | Raw open/import of a `.xit` (`Open SA File`/`Import SA File`) — always discards the current job. |
| `sa_launch`, `sa_ensure_running`, `sa_is_running`, `sa_connect` | Process / connect control. |
| `sa_dismiss_dialogs` | Close SA modal dialogs blocking an MCP step, right now (unconditional). |
| `sa_dialog_watchdog` | Background auto-closer of SA modals (`start`/`stop`/`status`). Armed automatically; acts only while a COM call is in flight, so manual GUI work is never interrupted. |
| `sa_run_step`, `sa_construct_point` | Generic step runner + example tool. |
| `sa_delete` | Delete objects (whole groups/frames/geometry: `Delete Objects`) and/or individual points of a group (`Delete Points`) in one call (points first). |
| `sa_create_frame` | Create a СК (frame): on an existing object's local CS, or origin + X-axis through two measured points (Z along working Z). Replaces a same-named frame by default. |
| `sa_set_working_frame` / `sa_reset_working_frame` / `sa_current_working_frame` | Activate a СК / deactivate (back to WORLD) / read the active one. |
| `sa_inspect_project` | List collections, or objects-by-type in a collection (full hierarchical names, optionally points per group). |
| `sa_point_coordinates` | Read-only export of working coordinates (+ stored offsets) of a group or an explicit point list. |
| `sa_best_fit` + `sa_best_fit_<shape>` | Best-fit plane/sphere/cylinder/cone/circle/line. |
| `sa_best_fit_from_points` | Best fit to an explicit point list across any groups/collections. |
| `sa_fit_fixed` + `sa_fit_fixed_<shape>` | Fit with a FIXED radius/diameter/apex angle and a chosen compensation side. |
| `sa_identify_geometry` | Offline shape identification of a point cloud (works from raw coordinates, no SA). |
| `sa_geometry_props`, `sa_fit_quality`, `sa_best_fit_report`, `sa_fit_clean` | Geometry parameters, reflector-compensated deviations/outliers, fit+report, robust iterative rebuild (tolerance or sigma×MAD, optional physical outlier deletion). |
| `sa_show_objects`/`sa_hide_objects`, `sa_show_points`/`sa_hide_points`, `sa_show_hide_by_type` | Graphics show/hide of objects, points, or whole object types. |
| `sa_project_points` | Project point groups/points onto object(s) at their closest point → new point group. |
| `sa_compare_points_objects` | Deviation whiskers: compare points to object(s) → new vector group. |
| `sa_vector_group_props`, `sa_vector_group_style` | Vector-group statistics (+ per-vector dump) / styling (colours, tolerance band, auto-range). |

## Typical flows

```python
# Work on a job without discarding the already-open copy, then inspect:
sa_ensure_file(r"C:\jobs\6.01.25 — обработка.xit")   # attach-first
sa_inspect_project(collection="A")

# Project points onto a fitted plane:
sa_project_points(objects=["Плоскость"], point_groups=["Точки_сверху"],
                  result_group="Точки_проекция", collection="A")
# -> {projected: True, result_count: N, skipped: [...], replaced: ...}
# (status_code is advisory: SA can report code 3 while still creating the
#  points — success is decided by the result group's actual contents)

# СК (frame) on a cylinder's local CS, activate it, then delete leftovers:
sa_create_frame(frame_name="СК_обечайки", reference_object="MCP_BF_цилиндр",
                collection="A")
sa_set_working_frame(frame_name="СК_обечайки", collection="A")
sa_reset_working_frame()                              # back to WORLD
sa_delete(objects=["MCP_BF_цилиндр_старый"],
          points=["Опорная сеть::1", "Опорная сеть::2"], group="Опорная сеть",
          collection="A")

# Identify a cloud's shape fully offline:
sa_identify_geometry(coordinates=[[x, y, z], ...])
# -> {best_geometry: "cylinder", confidence: "high", recognized: True,
#     candidates: [...], notes: [...]}
```

SA quirks that affect results (details in `AGENTS.md`): SA never overwrites
an object name — tools delete a same-named target first (`replaced: True`);
best-fit geometry auto-creates a "cardinal points" group that fit tools
purge; measurements are to the reflector centre, so fit-quality tools
compensate by the stored per-point offset (19.05 mm SMR etc.); mid-step
modal dialogs ("Object Not Found" …) are auto-closed by the watchdog while a
COM call is running, so calls don't hang.

## Extending

1. Find the exact step + argument names (SA MP tree, the SA SDK examples
   folder, or `mp_ref.txt` extracted from `MP Command Reference.pdf`).
2. Add an `@mcp.tool()` in `server.py` that sets the step + args and calls
   `ExecuteStep()`; mirror `sa_construct_point`.
3. Follow the design rules in `AGENTS.md` (COM threading, InvokeTypes
   pattern for enum/out-param args, delete-before-create, cardinal purge).
