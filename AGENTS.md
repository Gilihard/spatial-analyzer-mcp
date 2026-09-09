# AGENTS.md — Spatial Analyzer MCP Server

Concise operating guide for AI agents editing this repo. Tool-by-tool docs
live in `server.py` docstrings + `README.md`; this file carries the
**load-bearing knowledge**: the hard-won rules and the confirmed SA step/arg
names. Facts marked **live** were verified against SA 2015.07.28; **PDF**
means taken from `Documentation\MP Command Reference.pdf` (printed page N ≈
PDF page N+25; grep-able copy at `mp_ref.txt`) and possibly not yet
live-checked.

## What this is
An MCP server letting an LLM drive **SpatialAnalyzer (SA)** through its COM
SDK. SA automation is **step-based**, not a flat function API:
`SetStep("<exact name>")` → `Set<Type>Arg("<arg>", value)` → `ExecuteStep()`
→ `Get<Type>Arg(...)` / `GetMPStepResult()`. Step + arg names live in SA's MP
tree, the SDK examples folder (`...\SA SDK\Examples\`), the PDF, and
`mp_ref.txt`; **copy the exact names** from there. Step result codes: 2
DoneSuccess, 4 minor/partial, 3 fatal (may still have done the work — see
Query steps), -1 SdkError (usually a wrong step/arg name).

## Files & threading
- `sa_sdk.py` — `SABridge`, the ONLY module that touches the SA COM object.
  All COM runs on one worker thread (COM is thread-affine); public methods are
  blocking. Never call win32com elsewhere.
- `sa_app.py` — no COM: SA GUI process discovery/launch/kill + window checks
  (ctypes Toolhelp32/EnumWindows), main-window title, modal-dialog dismissal.
- `server.py` — FastMCP tools over stdio; import is COM-free.
- `sa_fitmath.py` — pure-Python (no numpy) LSQ fits used by offline tools.
- `test_sdk.py` — standalone COM smoke test (no MCP). Run before touching
  `sa_sdk.py`.

## Non-negotiables (learned the hard way)
1. **Engine birth order.** `Dispatch()` spawns a separate out-of-process
   `SpatialAnalyzerSDK.exe` engine. Born while the SA GUI's SDK listener is
   not up (SA closed; stuck zombie — no window, ~30 MB; parked behind a modal
   nag), it pops a MODAL "10061 connection refused" dialog and wedges the
   listener: every later Connect fails on an otherwise healthy GUI. An
   UNCONNECTED engine silently no-ops every step (empty results on a loaded
   job — `sa_inspect_project` → `{"collections": []}` while the GUI has data).
   Baked into the code:
   - Bridge created lazily by `_ensure_sa()`/`_ensure_bridge()`, which gates
     engine birth on a VISIBLE SA window (`sa_app.sa_has_visible_window`) and
     Connect()s right after. Every COM tool self-bootstraps; only `sa_status`
     deliberately skips the bridge.
   - `sa_ensure_running()` is GUI-first: launch SA (`wShowWindow =
     SW_SHOWNORMAL` — OS default SW_HIDE can leave it invisible) → poll for a
     visible window (timeout ⇒ stuck zombie: clear error, NO engine) → wait
     the rest of ~20 s cold-start so the listener binds → bridge → Connect.
   - Startup/late modals on an instance WE launched are auto-dismissed
     (WM_CLOSE via PostMessage, non-blocking); a user's own running SA is
     never touched.
   - Wedged: `taskkill //F //IM SpatialAnalyzerSDK.exe`, close SA, relaunch,
     retry. A 10061 right after a cold start usually just means "listener not
     bound yet" — retry, don't assume a wedge.
2. **One long-lived bridge per SA session.** Every python process that does a
   real `_ensure_sa()` spawns its own engine; several short-lived processes
   leak engines and wedge the listener. Test by hand with ONE persistent
   `python -i` calling `server.sa_ensure_running()` once. Abrupt kills don't
   run the atexit release — never remove `atexit.register(self.shutdown)` in
   `sa_sdk.py`.
3. **The COM object has NO type library** (`GetTypeInfoCount()==0`): dynamic
   dispatch cannot return `[out]` byref params (`GetMPStepResult`,
   `GetVectorArg`, `Get*NameArg`, all `Get*RefListArg` …) and mangles enum
   INPUT args (`Object Type`, `Geometry Type`, `Coordinate System Type`, … —
   the step pops an interactive picker or hangs). Such calls MUST bypass
   dispatch: `SABridge._invoke_method(name, ret_desc, arg_descs, *args)`
   with ret `(VT_BOOL, 0)` and combined `VT_* | VT_BYREF` arg descriptors.
   Use the existing helpers (`set_object_type_arg`, `set_geometry_type_arg`,
   `set_colorization_options_arg`, `get_*` getters, `_set_variant_list`); any
   new out-param getter or "Type" enum setter follows this pattern — NOT
   `_call()`/`_unwrap()`. SAFEARRAY args must be a PLAIN Python list under an
   explicit `VT_VARIANT|VT_BYREF` descriptor; wrapping in a win32com VARIANT
   raises DISP_E_TYPEMISMATCH. (Plain-value args like Projection Options go
   through dynamic dispatch fine.)
4. **Non-ASCII paths** (Cyrillic `.xit` paths): never type them inline into a
   REPL over a pipe (console codepage mangles bytes → "file not found").
   Read them from a UTF-8 `.py` file or pass programmatic values only.
5. **Modal dialogs auto-close only while a COM call is in flight** (bridge
   `is_busy()` — an idle bridge means SA is being driven by a human and no
   window is closed: manual construction dialogs and the save-on-exit prompt
   survive). Dismissal order: WM_CLOSE → posted BM_CLICK on Cancel/No/Нет,
   then the single OK/Yes/Retry button (a plain MB_OK box has NO close
   button), then WM_COMMAND IDCANCEL. `require_action=True` (public path)
   skips windows that cannot be waiting on the user (e.g. the engine's
   invisible `#32770` splash with only Static children — not a blocker).
   Public tools: `sa_dismiss_dialogs` (one-shot, UNCONDITIONAL — deliberate)
   and `sa_dialog_watchdog start|stop|status` (armed by `_ensure_bridge`;
   interval env `SA_MCP_WATCHDOG_INTERVAL`, default 0.5 s; optional
   `title_contains`). A dialog that pops between steps is cured by the next
   step blocking on it. Offline regressions: `_t_dialogs.py`, `_t_openlogic.py`.

## SA data model — names & ref lists
- A job (.xit) = collections → objects (point groups, vector groups, frames,
  geometry …) → points. SA holds ONE job; `Open SA File` always DISCARDS the
  loaded job (unsaved in-memory state lost).
- **Ref lists are flat arrays of joined full names**, one per element:
  `"A::т контур"`, `"A::WORLD"`, `"::т контур::1"` (leading `::` = empty
  collection part). Getters return `list[str]`; setters take the same layout.
  Never chunk into pairs/triples (a bogus old parser silently dropped every
  3rd point of 376).
- Group-name step inputs must be BARE (no `C::` prefix) — SA resolves them in
  the current collection; `"A::т контур"` → code 3, bare `"т контур"` → code
  2. Object inputs take joined full names.
- One object may appear under several Object Types (best-fit geometry keeps
  its group's name) — correct output, not a duplicate bug.
- **SA never overwrites**: constructing an existing name silently suffixes
  (`X`, `X1` …) and reading under the plain name returns the FIRST object's
  stale parameters. Tools delete-before-create (`replaced: True` /
  `replaced_object` in responses).
- **Auto cardinal-point groups**: the fit profile creates
  `<object>Кардинальные точки` / `...Cardinal Points` beside every best-fit
  (no arg disables it) → every fit tool purges it
  (`_purge_auto_cardinal_groups`; response key `cardinal_points_removed`).

## Conventions
- Tools are sync (`def`, not `async def`), return a dict, `error` key on
  failure (never raise).
- Tools return joined-full-name forms; normalize bare inputs via
  `_object_full_name` / `_point_full_name`.
- No code comments unless asked; explain design here / README.

## Commands
- Install: `pip install -r requirements.txt` (pywin32 import errors → run
  `python Scripts\pywin32_postinstall.py -install` once).
- Build + deploy to ZCode: `python build_mcp.py` — byte-compiles every *.py,
  probes tools/list over stdio (COM-free, engine stays unborn), writes the
  `spatial-analyzer` entry to USER `~/.zcode/cli/config.json` (default; tools
  in every project). `--scope workspace` targets `<repo>/.zcode/config.json`
  instead. `deploy.cmd` = double-click wrapper. `python build_mcp.py build` =
  validate only; `start` = run server.py in foreground. After code changes
  rerun it and reload MCP in ZCode (fresh process per launch).
- Debug: `python server.py`; Inspector: `npx -y
  @modelcontextprotocol/inspector python server.py`.
- Bridge tests: `python test_sdk.py` (no SA) / `python test_sdk.py localhost`
  (live SA running).
- Syntax: `python -m py_compile sa_sdk.py sa_app.py server.py test_sdk.py`.
- Offline regressions (no SA/COM): `python _t_dialogs.py` (dialog close),
  `_t_openlogic.py` (watchdog gate + attach logic), `_t_identify.py`
  (shape classification), `_t_frames_delete.py` (delete/frame/СК logic),
  `_t_fitmath.py` (fixed-fit math), `_t_cardinal_logic.py` (cardinal purge).
- Live harnesses (SA free, no other engine connected): `_live_vectors.py`
  (compare/vector groups), `_live_frames_delete.py` (delete/frame/СК),
  `_live_proj.py` (projection), `_live_showhide.py`, `_live_fixed2.py` /
  `_live_fixed3.py` (fixed fits). Keep one engine alive; cleanup MCP_* objects.

## Tools (server.py, ~36)
- Process/boot: `sa_status` (never spawns the bridge), `sa_is_running`,
  `sa_launch`, `sa_ensure_running`, `sa_open_file` (raw `Open SA File`
  semantics), `sa_connect`.
- Job file, attach-first: `sa_ensure_file(path, force_restart=True)` +
  `sa_current_file()` — see "Attach-first open" below.
- Dialogs: `sa_dismiss_dialogs`, `sa_dialog_watchdog`.
- Generic: `sa_run_step`, `sa_construct_point`.
- Delete/frames: `sa_delete(objects=…, points=…)` — both deletion steps in
  one call, points half runs FIRST (so an emptied group can be deleted in the
  same call); `sa_create_frame` (`method="on_object"` | `"origin_x_axis"`,
  replace same-named frame by default); `sa_set_working_frame` (activate),
  `sa_reset_working_frame` (= set WORLD), `sa_current_working_frame`.
- Inspect/data: `sa_inspect_project` (collections, or objects-by-type in a
  collection — full hierarchical names, optionally the points per point
  group), `sa_point_coordinates` (READ-ONLY working-coord export of a group
  or explicit point list + stored offsets; `include_offsets=False` halves COM
  round trips; `max_points` caps; nothing created in SA; NOT yet exercised
  live).
- Fit: `sa_best_fit` + typed `sa_best_fit_{plane,sphere,cylinder,cone,
  circle,line}`; `sa_best_fit_from_points` (explicit point list across any
  groups); `sa_fit_fixed` + `sa_fit_fixed_{cylinder,sphere,circle,cone}`
  (constrained LSQ offline + `Construct <Type>`); `sa_identify_geometry`
  (offline shape classification, works from raw `coordinates` with no SA).
- Fit quality/robust: `sa_geometry_props`, `sa_fit_quality`,
  `sa_best_fit_report`, `sa_fit_clean` (robust pass loop) — see "Fit
  quality" below.
- View: `sa_show_objects`/`sa_hide_objects` (collection objects),
  `sa_show_points`/`sa_hide_points` (points of a group), `sa_show_hide_by_type`.
- Projection: `sa_project_points` (points ON objects as a new point group).
- Compare (deviation whiskers): `sa_compare_points_objects` (new VECTOR
  group), `sa_vector_group_props` (stats + optional per-vector dump),
  `sa_vector_group_style` (colour-range/tolerance/colour bar, auto-range).
  Step names PDF-based, NOT yet confirmed live.

## Confirmed SA 2015 step + arg names (reference)
**File loading** (live; .xit/.xit64 both work through `Open SA File`):
- `Open SA File` — in `SA File Name` (**NOT** "File Path"); discards job.
- `Import SA File` — in `SA File Name`, `Allow Operator Selections` (False =
  silent import; True pops a picker); merges into the current job.

**Enumeration** (SA 2015 has NO "Get the Names of all …" steps — count +
i-th pattern and type-filtered builders; 0-indexed):
- `Get Number of Collections` (out `Total Count`) + loop `Get i-th Collection
  Name` (in `Collection Index`, out `Resultant Name`) → `_list_collections`.
- `Make a Collection Object Name Ref List - By Type` — in `Collection`,
  `Object Type` (enum via `set_object_type_arg`); out `Resultant Collection
  Object Name List` → `_objects_in_collection_by_type`. Point-groups-only
  builder: `Make a Collection Object Name Ref List from all Groups in a
  Collection` (in `Collection Name`).
- `Make a Point Name Ref List From a Group` — in `Group Name` (BARE name via
  `set_object_name_arg`); out `Resultant Point Name List` → `_points_in_group`.
- `Make a Vector Name Ref List From a Vector Group` — in `Vector Group Name`;
  out `Resultant Vector Name List`.
- Point readback: `Get Point Coordinate` (in `Point Name`; out `X Value`/`Y
  Value`/`Z Value`), `Get Point Properties` (out `Radial Offset`/`Planar
  Offset`), `Get Number of Points in Group`, `Get Number of Vectors in Vector
  Group` (out `Total Count`).

**Object Type enum values** (all code 2; "Any" = every type):
`Any`, `Point Group`, `Vector Group`, `Frame`, `Circle`, `Cylinder`, `Plane`,
`Sphere`, `Cone`, `Line`, `Perimeter`, `B-Spline`, `Surface`, `Scale Bar`
(`server.SA_OBJECT_TYPES`). Cloud is NOT "Point Cloud" (returns code 3).

**Delete** (live): `Delete Objects` — in `Object Names` (Collection Object
Name Ref List); 2 OK, 4 partial (some names not found), 3 nothing found.
Removes ANY object incl. a whole point group. `Delete Points` — in `Point
Names` (Point Name Ref List).

**Best fit** (live; ONE step — the "Construct a Best Fit <Shape>" names do
NOT exist): `Fit Geometry to Point Group`, list variant `Fit Geometry to
Points` (in `Points to Fit` instead of `Group to Fit`). Args: `Geometry
Type` (enum — MUST go via `set_geometry_type_arg`, or it never reaches SA),
`Group to Fit`/`Points to Fit` (full/joined names), `Resulting Object Name`,
`Fit Profile Name` (blank = default), `Report Deviations` (False — SA never
pops the results dialog), `Fit Interface Tolerance (-1.0 use profile)`
(0.0 = no limit), `Ignore Out of Tolerance Points` (Boolean), optional
`Starting Condition Geometry`. No return args; success = code 2 + the object
under `Resulting Object Name`. Stored per-point reflector offsets are
applied; underdetermined (e.g. plane over 2 points) → code 3; a point that
was not fitted keeps deviation 100.0. `_try_steps` probes candidate names —
only `sa_open_file` still uses it; don't reintroduce socket-style probing.

**Construct + properties readback** (PDF; used by `sa_fit_fixed`):
- `Construct Cylinder` — in `Cylinder Name`, `Cylinder End Point (in working
  coordinates)`, `Cylinder Axis (in working coordinates)`, `Cylinder
  Diameter`, `Cylinder Length` (Diameter = 2× fitted radius).
- `Construct Sphere` — `Sphere Name`, `Sphere Center`, `Sphere Radius`.
- `Construct Circle` — `Circle Name`, `Circle Center`, `Circle Normal`,
  `Circle Radius`.
- `Construct Cone` — `Cone Name`, `Cone End Point (in working coordinates)`,
  `Cone Axis (in working coordinates)`, `Cone Length`, `Cone Theta Start`,
  `Cone Theta Span`, `Cone Included Angle` (fit params carry no sweep angles
  → defaults theta_start 0, theta_span 360).
- `Get <Type> Properties` labels (PDF; per-type sets in
  `server._GEOMETRY_PROP_SPECS`, tried in order): cylinder `Begin
  Coordinate`, `End Coordinate`, `Axis Direction`, `Length`, `Radius`,
  `Diameter`; plane `Normal Direction`, `Point on Plane`, `D Parameter`;
  sphere `Center Coordinate`, `Radius`, `Diameter`; circle `Center
  Coordinate`, `Normal Direction`, `Radius`, `Diameter`; cone `Cone End
  Point (in working coordinates)`, `Cone Axis (in working coordinates)`,
  `Cone Length`, `Cone Theta Start`, `Cone Theta Span`, `Cone Included
  Angle`. Read via `get_double_arg`/`get_vector_arg`.

**Query Points to Objects** (live; GUI Construct > Points > Project Points
to > Objects > Closest Point and Compare > Points > Objects): engine creates
either a point group of projected points or a deviation-whisker VECTOR group
per the Projection Options type. in: `Point Names` (joined C::G::T), `Object
Name List (Objects to Project to)` (joined C::O), `Resulting Object Name`,
`Projection Options` (`SetProjectionOptionsArg`, plain values → dynamic
dispatch OK), `RMS Tolerance (0.0 for none)`, `Maximum Absolute Tolerance
(0.0 for none)`, `Show Results Dialog?` (False). out (best-effort): `RMS
Deviation`, `Max Absolute Deviation`, `Average Deviation`, `Standard
Deviation`.
- projectionType ∈ {`Points on Object` (default — closest point ON the
  object), `Points on Offset Object`, `Points on Probe Surface`, `Offset
  Object To Target Vectors`, `Target To Offset Object Vectors`, `Object To
  Probe Vectors`, `Probe To Object Vectors`} + ignoreEdgeProjections,
  bOverrideTargetOffsets/overrideTargetOffsetsValue,
  bAddExtraMaterialThickness/extraMaterialThicknessValue.
- Semantics (live): created points land EXACTLY on the object; `Points on
  Object` IGNORES probe_offset_mm / extra_material_mm; back-away is `Points
  on Offset Object` + probe_offset_mm. Created points' stored offsets read
  0. `use_stored_offsets=False` (projection default) zeroes stored offsets
  (deterministic — avoids SA 2015 fatals on offset-carrying groups);
  compare's default is True (SA query commands account for target offset).
  Compare direction: `Object To Probe Vectors` (default, "inspect" view:
  arrow object→measured point), `Probe To Object Vectors` (reversed,
  "build"), the Offset Surface pair anchored at the point centre.
- **Status is advisory** (live quirk): SA intermittently returns code 3 while
  STILL creating the group, and silently skips unprojectable points. Decide
  success by what the result group actually contains (count via
  `Make a Point Name Ref List From a Group`); report `status_code`/`status`
  for reference and list missing targets under `skipped`. Delete a
  same-named result group first (SA never overwrites — it would merge).

**Vector groups** (PDF — NOT yet live):
- `Get Vector Group Properties` — in `Vector Group Name`; out Integer `Total
  Vectors`, `Vectors In Tolerance`, `Vectors Out Of Tolerance`; Double `%
  Vectors In Tolerance`, `% Vectors Out Of Tolerance`, `Absolute Max
  Magnitude`, `Absolute Min Magnitude`, `Max Magnitude`, `Min Magnitude`,
  `Standard Deviation`, `Standard Deviation Mean Zero`, `Average Magnitude`,
  `Avg of Abs Magnitude`, `High Tolerance Value`, `Low Tolerance Value`.
- `Get i-th Vector From Vector Group` — in `Vector Group Name`, Integer
  `Vector Index` (0-based — unverified); out String `Vector Name`, Vector
  `Begin in Working`/`End in Working`/`Total Delta in Working`/`ijk Unit
  Vector in Working`, Double `Magnitude`.
- Style: `Set Vector Group Colorization Options (Selected)` /
  `Auto-Range and Set Vector Group Colorization (Selected)` — in `Vector
  Groups to be Set` (Collection Vector Group Name Ref List), `Treat
  Individually?` (auto-range only) and the `Colorization Options` /
  `Colorization Options (Uses Mode Only)` compound via
  `set_colorization_options_arg` (20-param `SetColorizationOptionsArg`:
  colour-range method string + 3 base colours + draw/arrowhead/tube/blotch/
  colour-bar flags + magnification + width + saturation + tolerance values;
  routed through `InvokeTypes` — the compound embeds two enum strings; enum
  spellings "Go/No-Go", "Continuous", … from SA docs/GUI, unverified). No
  per-field getter exists → omitted fields keep the defaults, no partial
  updates. Tolerance values feed the in/out stats `Get Vector Group
  Properties` reads back.

**Frames / working frame** (PDF — NOT yet live, pending
`_live_frames_delete.py`):
- `Construct Frame On Object` — in `Reference Object` (Collection Object
  Name): frame with the object's local CS (cylinder axis / plane normal /
  line direction / another frame …).
- `Construct Frame, Pick origin and point on X axis - clock Z along working
  Z` — in `Origin Point`, `Point on X-Axis` (Point Names): frame at a
  measured point, X through a second point, Z ∥ working Z (levelled СК).
- Result-name arg tried as `Frame Name (Optional)` then `Frame Name`
  (`_set_first_arg`).
- `Set Working Frame` — in `New Working Frame Name` (Collection Object
  Name); only ONE frame is working — setting B deactivates A (no inactive
  state). `sa_reset_working_frame()` = set `WORLD`.
- `Get Working Frame Properties` — out `Frame Name`, `Collection Name`
  (String; always succeeds).
- No transform-based `Construct Frame`: `SetTransformArg` takes a 4×4
  SAFEARRAY VARIANT whose Python transport is not live-verified — do not add
  without a live check.

**Show/Hide** (live): `Show Objects` / `Hide Objects` — in `Objects to
Show` / `Objects to Hide` (Collection Object Name Ref List). (The PDF also
lists a second export ref-list arg under `Show Objects` — it belongs to a
neighbouring step; the real step takes only `Objects to Show`.) `Show/Hide
Points` — in `Point Names`, `Show? (Hide = FALSE)` (always succeeds).
`Show/Hide by Object Type` — in `All Collections?`, `Specific Collection`
(**NOT** "Collection Name" — that PDF column is the arg's TYPE; using that
name → SdkError -1), `Object Type To Show/Hide` (enum), `Hide? (Show =
FALSE)`. No scope = current/active collection.

## Fit quality & robust rebuild (live on `6.01.25 — обработка.xit`,
collection "A", group «т контур», 376 pts, SMR 19.05)
- **Reflector compensation.** SA measures to the reflector CENTRE; `Get Point
  Properties` → `Radial Offset` stores the reflector radius (19.05 mm =
  3/4" SMR; all 376 pts), so raw points sit ~19.05 mm OUTSIDE the fitted
  surface and fits apply it. Reduce reported deviations by the stored
  per-point offset — or by constant `probe_offset_mm` when none is stored
  (`_compensation_meta`: stored_point_offsets | constant | none). Plain
  compensated cylinder RMS = 0.661255575 mm (raw ≈ 19.06).
- **Deviation sign** (`_compute_deviations`): + = outside nominal
  (cyl/sphere/circle radial, plane along its normal), − = inside; a line has
  magnitude only. `_dev_stats` → RMS ("СКО"), average, std dev,
  min/max/max-abs.
- **`sa_fit_clean`** = the GUI robust fit as an explicit pass loop: fit →
  read geometry → compensated deviations → refit ONLY in-threshold points
  (`Fit Geometry to Points`) → repeat until no outliers / excluded set stops
  changing / `max_iterations`. Per-iteration stats over all points AND the
  kept core (`stats_kept`). Deletes its previous-pass object before every
  refit and any same-named object up front (`replaced_object`) — the job ends
  with exactly one object. Threshold: fixed `tolerance_mm` or `sigma` ×
  robust spread `1.4826 * MAD` (`_robust_sigma`; plain σ excludes nothing
  when outliers inflate it). Deviations are measured around the fit's MEDIAN
  (a strong outlier can pull the LSQ fit so far the inliers read
  out-of-tolerance on the wrong side); fixed-tolerance runs with median >
  0.25·tol get one automatic debias pass (`debias_pass`). `delete_outliers=
  True` PHYSICALLY deletes them (`Delete Points`) and refits — destructive,
  explicit opt-in. Honest `converged: False` when the surface FORM error
  exceeds the tolerance (т контур at 0.2 mm vs 0.66 RMS form: excluded set
  churns, ends at max_iterations with the tightest-core geometry, kept-core
  RMS ≈ 0.095 vs 0.661 over all) — that is correct output, not a bug.
- Tools: `sa_geometry_props` (parameters of an existing object),
  `sa_fit_quality` (+ per-point deviations/stats/outliers beyond
  `tolerance_mm`), `sa_best_fit_report` (fit + quality), `sa_fit_clean`.

## Offline shape & fixed fits (sa_fitmath.py — pure math, nothing in SA)
- **Identify** (`sa_identify_geometry`, decision rules in
  `server._classify_cloud`): fit all six primitives, rank; gross cloud shape
  (eigen `cloud_stats`: flat/linear/volumetric) breaks the circle-vs-cylinder
  tie (free 3D circle fit ≡ cylinder radial fit). Collinear → line (radial
  only if ≥3× better — a real tube); flat → circle if `_ring_like` (points
  hug one radius: ring/rim/arc) else plane; volumetric → solid when circle
  ties; two parallel near-equal rings → cylinder (bore/shaft). Genuine
  synonyms (coplanar ring IS a plane; bore IS a sphere; flat patch IS an
  osculating huge sphere) keep `confidence` low + a note instead of a false
  high verdict; on flat/collinear clouds the degenerated curved rivals drop
  out of the confidence pool. `recognized: False` when the winning residual
  is large vs cloud size; a pile of coincident points is rejected ("no
  extent"). Regression `_t_identify.py`.
- **Fixed fits** (`sa_fit_fixed*`): no SA step constrains a parameter →
  constrained LSQ offline (Levenberg-Marquardt on compensated surface
  residual) then create the object via `Construct <Type>` so it carries the
  exact fixed nominal. `compensation` side: outside/+ / inside/− / none /
  both (circles). **Key semantics:** with stored offsets the free-fit radius
  Rf is the reflector-CENTRE radius — pass the TRUE-surface radius (≈ Rf ∓
  stored offset) to a fixed fit; the side picks the sign. Regression
  `_t_fitmath.py`.

## Attach-first job open (live semantics)
SA's SDK cannot report the loaded file (verified against the SDK headers +
PDF), and `Open SA File` always discards. `sa_ensure_file(path)` detects the
loaded job via two signals: the session tracker `_OPEN` (set by
`sa_open_file` / `sa_ensure_running` / `sa_launch`) and the SA main-window
caption (`sa_app.sa_main_window_title` — catches hand-opened or leftover
jobs; case-insensitive match; a caption naming a DIFFERENT .xit marks the
tracker stale). Flow: bootstrap (`sa_ensure_running(file_path=…)`) → attach
if evidence says the file is loaded (`already_open: True`, skip SDK open —
in-memory state preserved) → best-effort `Save` of the current job before it
is discarded (unnamed job pops a Save-As dialog the watchdog cancels → step
fails → skipped) → SDK `Open SA File` → on failed open OR failed Connect
while SA runs (wedged listener): force-restart (`sa_app.kill_sa` + bridge
reset + relaunch SA with the file) — a restart discards the previous job's
unsaved changes; that is the sanctioned last resort. `sa_current_file()`
reports the evidence. Regression `_t_openlogic.py`.

## Status / next steps
Live-confirmed: connect/launch/open/import/inspect, all 6 best fits, the
whole fit-quality chain (props readback, compensated deviations, outliers,
`sa_fit_clean`), fixed fits end-to-end (synthetic + real «т контур»: fixed
radius read back exactly; side argument discriminates ~57× in RMS), view
show/hide, deletion, frames/СК basics (frame on fitted plane + replace,
activate + read-back + reset WORLD, two-point СК, point/object deletion),
projection (`_live_proj.py` ALL OK), attach-first open, modal watchdog.
PDF-named, NOT yet live: compare/vector-group trio + vector style
(`_live_vectors.py` pending) and the frame/working-frame steps
(`_live_frames_delete.py` pending). `sa_point_coordinates` is not yet
exercised live. Compare min point counts per fit type: `_LIST_FIT_MIN_POINTS`
(line 2 … cone 6).
To extend: exports/reports, USMN, point/group manipulation beyond
delete/construct — copy step names from the PDF / SDK examples and confirm
live. Live check on a real job: `6.01.25 — обработка.xit` (collection "A",
group «т контур» 376 pts).
