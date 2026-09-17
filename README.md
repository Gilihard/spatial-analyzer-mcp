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
| `sa_average_groups` | Average several point groups into one group by matching target name (`Average a set of Groups`) — the "USMN as an averager" job, no instruments or network solving. Coordinates are a plain arithmetic mean; the step's RMS/avg/max describe the WORST point, not the whole merge. Replaces a same-named result group by default. |
| `sa_unify_groups` | Unify several point groups — **align by LSQ AND merge** ("усреднение с совмещением по МКН"). `method="usmn"` (default) runs `Locate Instruments (USMN)` with `AutoReject Outliers and Resolve` — the iterative auto-rejection of bad points; instruments are derived from the source groups' observations and every other point group of the collection is excluded (a re-used target name makes USMN fail silently), and the solve is retried because it is non-deterministic. `method="fit"` is the explicit fallback: copy → `Best Fit Transformation - Group to Group` → apply → `Average a set of Groups` (no outlier rejection). Reports the step's RMS plus its own deterministic per-source `agreement` block. **`method="usmn"` is not a plain average**: USMN has its own weighting scheme, so its composite sits ~0.03 mm off the mean of the same groups and the solve also relocates the instruments in the job (so the source groups move). `method="fit"` reproduces the arithmetic mean exactly and changes nothing. |
| `sa_create_frame` | Create a СК (frame): on an existing object's local CS, or origin + X-axis through two measured points (Z along working Z). Replaces a same-named frame by default. |
| `sa_set_working_frame` / `sa_reset_working_frame` / `sa_current_working_frame` | Activate a СК / deactivate (back to WORLD) / read the active one. |
| `sa_move_objects` | Move objects **in place** — translation and rotation — relative to a СК: a 6-DOF delta in the active working frame (default), translation only, a delta in WORLD (+scale), or the delta between two named frames. |
| `sa_object_transform` | Read one object's pose (4×4 matrix + Fixed XYZ X/Y/Z, Rx/Ry/Rz) in the active working frame — capture it before/after a move. |
| `sa_best_fit_transform` | **Best-fit (МНК) transform moving one point group ONTO another** (`Best Fit Transformation - Group to Group`), with a full deviation report and an optional move. Choose the fit freedoms (`allow_x/y/z/rx/ry/rz`, `allow_scale`). Every point's deviation is recomputed from the returned transform and listed **worst first**; `tolerance_mm` flags the ones past it as `outliers` without excluding anything. `exclude_points` re-fits without the named targets (temp copies of both groups, removed afterwards) — the "drop the bad point and recompute" loop; excluded points keep their deviation under `excluded_deviations`. `apply=True` moves the corresponding group **plus** everything named in `move_objects` in one step (frames/СК, fitted geometry, vector groups built on it travel with it), and `verify_after_move` re-reads the result. |
| `sa_inspect_project` | List collections, or objects-by-type in a collection (full hierarchical names, optionally points per group). |
| `sa_point_coordinates` | Read-only export of working coordinates (+ stored offsets) of a group or an explicit point list. `format="canvas"` is a token-lean variant: the whole set as one CSV text, constant offsets reported once. |
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

# Move objects relative to a СК (translation + rotation, in place):
sa_move_objects(objects=["MCP_BF_цилиндр"], dx=10, dz=-2.5,
                rx=0, ry=0, rz=90, collection="A")
# -> the 6-DOF delta is applied in the ACTIVE working frame, rotations about
#    its origin; activate the target СК first with sa_set_working_frame.
sa_move_objects(objects=["MCP_BF_цилиндр"], mode="frame_to_frame",
                source_frame="СК_исходная", destination_frame="СК_новая",
                collection="A")          # move BY the delta between two СК
sa_object_transform(object="MCP_BF_цилиндр", collection="A")
# -> {ok: True, matrix: [...], fixed_xyz: {x, y, z, rx, ry, rz}}

# МНК-совмещение одной группы с другой: посмотреть отклонения, убрать плохую
# точку, пересчитать, затем переместить группу вместе с её окружением.
sa_best_fit_transform(reference_group="Номинал", corresponding_group="Замер",
                      collection="A", tolerance_mm=0.2)
# -> {computed: True, matrix: [...], fixed_xyz: {...},
#     stats: {rms_deviation, max_deviation, ...}, pairs: N,
#     deviations: [{target, deviation, included, outlier}, ...],  # worst first
#     outliers: ["4"], worst_point: {...}}
sa_best_fit_transform(reference_group="Номинал", corresponding_group="Замер",
                      collection="A", exclude_points=["4"],
                      apply=True, move_objects=["СК_узла", "цилиндр_узла"])
# -> the fit drops target 4 (temp copies of both groups, then deleted) and the
#    corresponding group + the two companions move in one step;
#    stats_after_move re-reads the result to prove it landed on the reference.

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
