# AGENTS.md — Spatial Analyzer MCP Server

Guidance for AI coding agents (opencode, Claude Code, etc.) working in this repo.

## Project goal
A Model Context Protocol (MCP) server that lets LLM clients drive New River
Kinematics **SpatialAnalyzer (SA)** — a 3D metrology package — through its COM
automation SDK. There is no existing MCP for SA; this is built from scratch.

## Architecture (read this first)
- `sa_sdk.py` — `SABridge`: the ONLY thing that touches the SA COM object.
  All COM calls run on one dedicated worker thread (COM objects are
  thread-affine). Public methods are blocking wrappers. **Never** call
  `win32com` directly elsewhere.
- `sa_app.py` — non-COM layer for the SA GUI process: discovers
  `Spatial Analyzer.exe`, checks if it's running (Toolhelp32 via ctypes, no
  psutil dependency), and launches it (optionally with a `.sa` file). No COM,
  no apartment affinity — runs on the caller's thread.
- `server.py` — `FastMCP` server exposing SA operations as MCP tools over
  stdio. Tools call `SABridge` (COM) and `sa_app` (process) methods.
- `test_sdk.py` — standalone smoke test (no MCP), checks COM registration +
  optional live connect. Run before touching `sa_sdk.py`.

## The SA SDK model (critical)
SA automation is NOT a flat function API. It is **step-based**:
1. `SetStep("<exact step name>")`  — picks an operation from SA's Measurement
   Plan tree. Names are case/space sensitive (e.g.
   `"Construct a Point in Working Coordinates"`).
2. `Set<Type>Arg("<arg name>", value)` — set each input by name + type
   (string/double/int/bool/vector/point/object/transform...).
3. `ExecuteStep()` — runs it.
4. `Get<Type>Arg("<arg name>")` / `GetMPStepResult()` — read outputs + status.

Step names and argument names live in SA's MP tree (see SA SDK examples at
`C:\Program Files (x86)\New River Kinematics\SpatialAnalyzer <ver>\SA SDK\Examples`).
When adding a tool for a new step, **copy the exact step + arg names** from
those examples or from SA itself.

## pywin32 gotcha
SA COM methods return a BOOL plus `[out]` values. Late-bound Dispatch may
return the value directly OR a `(bool, value)` tuple. Always route results
through `_unwrap()` in `sa_sdk.py`. If a getter returns garbage, suspect the
unwrap and test against a live SA instance.

**Bigger gotcha — the SA COM object has NO type library**
(`SpatialAnalyzerSDK.exe`, ProgID `SpatialAnalyzerSDK.Application`,
`GetTypeInfoCount()==0`). So pywin32 dynamic dispatch cannot return
non-`[retval]` `[out]` parameters — calling e.g. `GetMPStepResult()` raises
`DISP_E_PARAMNOTOPTIONAL`, and any getter with byref out-params
(`GetVectorArg`, `GetPointNameArg`, ...) is similarly broken through
`sdk.<method>(...)`. The fix is to bypass dynamic dispatch and call
`IDispatch::Invoke` directly via `_oleobj_.InvokeTypes(...)` with explicit
type descriptors. Descriptor format (from pywin32 build.py): element 0 is the
**combined** vt (`VT_I4 | VT_BYREF` for an out long), element 1 is `0`;
`retType = (VT_BOOL, 0)` for these NRK methods (they return BOOL). Example:
`sa_sdk.SABridge.get_step_result()`. There is now a general helper,
`SABridge._invoke_method(name, ret_desc, arg_descs, *args)`, that all
out-param getters (`get_string_arg`, `get_*_name_arg`, the `get_*_ref_list_arg`
SAFEARRAY getters, ...) route through. When adding any new getter that has
out-params, follow that pattern — do NOT use `_call()`/`_unwrap()` for it.

**Same gotcha hits enum INPUT args too — `SetObjectTypeArg`.**
`Object Type` is an enum argument type used by steps like
`Make a Collection Object Name Ref List - By Type` and
`... - WildCard Selection`. Through dynamic dispatch the enum value is mangled
exactly like the out-param case: SA's MP runtime receives no valid Object Type
and **falls back to popping the interactive type/selection picker**, which
makes `ExecuteStep()` block forever (no dialog the user can see, just a hang).
`SABridge.set_object_type_arg(name, value)` therefore routes through
`_invoke_method("SetObjectTypeArg", (VT_BOOL,0), ((VT_BSTR,0),(VT_BSTR,0)), ...)`
with explicit `VT_BSTR` descriptors — after that the by-type / wildcard steps
run fully non-interactively. **Lesson: any SA "Type" enum arg (Object Type,
Coordinate System Type, Geometry Type, ...) must go through `InvokeTypes`, not
`_call()`.** Confirmed live on SA 2015: with the fix, every Object Type value
returns `DoneSuccess`; without it, the step hangs >60s.

**Engine process leak**
`Dispatch(SA_PROG_ID)` spawns a separate out-of-process SDK Engine. It only
terminates when its COM reference is Released on the creating thread. The
bridge registers `atexit.register(self.shutdown)` to do this; never remove it,
or every script/server run leaks an engine window (and can wedge SA's SDK
listener, which then refuses connections with socket error 10061).

**Ordering is CRITICAL — the engine must NEVER be born before the SA GUI.**
If `SpatialAnalyzerSDK.exe` starts while the SA GUI's SDK listener is not up
yet (e.g. SA not running at all), the engine pops a MODAL error dialog
("NRK socketinterface 10061 connection refused") on the desktop and wedges
EVERY later `Connect()` — the listener refuses all future connections. Seen
twice live on SA 2015. Consequences baked into the code:
- `import server` is now COM-free. The bridge is created lazily by
  `server._ensure_sa()`, which raises if the SA GUI has no VISIBLE WINDOW and
  Connect()s the engine right after creating it — so ANY tool call
  self-bootstraps (no separate sa_connect()/sa_ensure_running() needed).
  A bare process is NOT enough: an auto-launched SA can linger as a stuck
  zombie — no window, no SDK listener, ~30 MB idle (seen live twice; usually
  after a bad previous shutdown or a license prompt) — and an engine born
  against it pops the SAME modal 10061. `_ensure_bridge()` therefore gates
  engine birth on `sa_app.sa_has_visible_window()` (EnumWindows via ctypes,
  no COM). `sa_app.sa_wait_for_window(timeout)` polls for the window.
  Why the connect is mandatory: an UNCONNECTED engine does not error — every
  MP step silently no-ops and returns empty results, which looks exactly like
  an empty project (`sa_inspect_project` → `{"collections": [], "errors": []}`
  while the GUI has data). If a tool ever returns empty-success on a loaded
  job, suspect the engine was never connected. Only `sa_status` deliberately
  does NOT create the bridge.
- `sa_ensure_running()` is GUI-first: launch SA → poll for a visible window
  (none appears within `timeout` = stuck zombie: it returns a clear error and
  spawns NO engine) → wait out the remaining cold-start window (~20 s total
  since launch, for the listener to bind) → create the bridge → Connect.
- **Startup nags are auto-closed — an SA that shows no window may be parked
  behind a modal dialog, not dead.** Seen live on SA 2015: the "SpatialAnalyzer
  Maintenance and Support Subscription" dialog (hidden or visible) blocked
  launch — no main window, no SDK listener, ~34 MB, for 90+ s. `sa_app`
  dismisses any `#32770` dialog on an instance WE launched: in `launch_sa`
  while the process registers and in `sa_wait_for_window` until the main
  window appears (WM_CLOSE, non-blocking PostMessage; titles surface as
  `dismissed_dialogs`). It never touches a user's own running instance.
  `launch_sa` also sets `wShowWindow = SW_SHOWNORMAL` — with
  `STARTF_USESHOWWINDOW` and no value, the OS default SW_HIDE can leave SA's
  windows invisible while still blocking.
- If the modal dialog already popped: `taskkill //F //IM
  SpatialAnalyzerSDK.exe` (or restart SA), then retry in the right order.

**One long-lived bridge per SA session — do NOT spawn probe scripts.**
Even with the lazy bridge, every python process that actually touches SA (a
real `_ensure_sa()`) spawns its own SDK Engine. Rapidly creating/releasing
several engines wedges SA's listener (socket error 10061 "connection refused"
on `Connect()`). When testing by hand or from another agent, run **one**
persistent `python -i`, call `server.sa_ensure_running()` once, and keep
reusing it. Seen live again on 2026-09-04: after ~6 short-lived test
processes in 30 min, `tasklist` showed FOUR leaked `SpatialAnalyzerSDK.exe`
engines and the listener silently stopped answering (every MP step timed out
at the 60 s client cap, even `SetStep`, on an otherwise healthy GUI). Cleanup
that restores the listener: kill every `SpatialAnalyzerSDK.exe`, close SA,
relaunch. Abrupt process deaths (crash, `| tail` pipe break) do NOT reliably
run the atexit release, so the engine leaks and later ones wedge the listener.

**Cold-start nags can also appear LATE (tens of seconds after launch) and
wedge the listener mid-work.** Seen live: a dialog titled "Spatial Analyzer"
popped ~50 s after a cold launch — well after `sa_wait_for_window` had
returned and several MP steps (Open SA File) had already succeeded. The very
next step hung (60 s client timeout) and poisoned every later step until the
dialog was closed. `sa_app._dismiss_modal_dialogs(pid)` closes it, but only if
called AFTER it appears: a test harness must run a watchdog thread that calls
it every ~2 s for the whole session (and wait ~45 s post-cold-launch before
the real steps), not just during launch. `_live_fixed2.py` / `_live_fixed3.py`
show the pattern (watchdog + pid-scoped dismiss + 12 s COM timeout cap via
monkey-patched `SABridge._submit`).

**Modal dialogs mid-work are auto-closed (2026-09-04) — no more babysitting.**
The operator-facing failure was: SA pops a modal dialog DURING an MP step
(an "Object Not Found" error box for a bad object name, a nag, ...), the step
waits on it, and every MCP call looks hung until a human clicks the dialog
away. Two mechanisms now handle it, both pure `sa_app`/ctypes (no COM, safe
on any thread, do not touch the COM worker):
- `sa_app.dismiss_sa_dialogs(...)` — one-shot scan+close of the SA GUI and
  SDK engine processes. Each target window gets `WM_CLOSE` (== Cancel/X);
  survivors get a button click (`BM_CLICK` posted, never SendMessage — a
  wedged dialog thread must not block the caller), Cancel/No/Нет preferred,
  then the single/OK/Да/Yes/Retry button (a plain MB_OK "Object Not Found"
  box has NO close button — only clicking its OK ends it), then a
  `WM_COMMAND IDCANCEL` as last resort. `require_action=True` (public path)
  skips windows that cannot be waiting on the user: invisible windows with no
  Button children — the SDK engine keeps an INVISIBLE `#32770` splash/about
  window ("Spatial Analyzer SDK Engine", only Static children, seen live) that
  is NOT a blocker; closing it every tick was noise. The launch path's
  `_dismiss_modal_dialogs` still passes `require_action=False` (unchanged
  behavior: also close hidden launch-blocking nags, no engine exists yet).
- Server watchdog (`_WATCH_*` in `server.py`): a daemon thread armed by
  `_ensure_bridge()` on the first COM tool call (before any step runs), polls
  `interval_s` (default 0.5 s, env `SA_MCP_WATCHDOG_INTERVAL`) and dismisses
  whatever pops, so a mid-step modal self-heals in ≤ ~0.5 s instead of
  hanging the session. It also retries forever against dialogs that cannot be
  closed by messages (a wedged engine thread) — harmless, and the real cure
  for a wedged ENGINE is still the documented taskkill (the watchdog never
  kills anything).
- Tools: `sa_dismiss_dialogs` (one-shot; call it, then retry the stuck step)
  and `sa_dialog_watchdog` (`start`/`stop`/`status`, optional `title_contains`
  filter). The watchdog auto-starts on connect; an explicit `stop` disarms it
  for the session (`auto: false`) — use that when an operator is working the
  SA GUI by hand and must not have dialogs yanked. Offline regression:
  `python _t_dialogs.py` (pops real MessageBoxes, no SA/COM needed).

**Connect speed (measured):** with the SA GUI already running,
`sa_ensure_running()` connects in ~0.2 s — no artificial delay anywhere. The
only slow path is a COLD GUI launch: `sa_ensure_running()` waits for a
visible window (bails with a clear error and NO engine if none appears),
then sleeps only the remaining time to ~20 s since launch so the SDK
listener can bind (a Connect inside that window fails; ~15-20 s measured),
then creates the engine and Connect()s with retries. A 10061 right after a
cold start usually just means "listener not bound yet" — retry, don't assume
a wedge. A 10061 when the GUI has been up for a while means an engine was
born against a windowless/stuck instance or a leaked engine wedged the
listener: kill leaked `SpatialAnalyzerSDK.exe`, close the stuck SA, relaunch.

**Non-ASCII paths — never type them inline into the REPL.**
SA paths often contain Cyrillic (e.g. `...\Скрипты\...обечайка....xit`). When a
non-ASCII literal is *typed* into `python -i` over a PowerShell stdin pipe, the
console codepage (CP866/1251) mangles the bytes and SA receives gibberish, so
`Open SA File` times out / "file not found". Always read such paths from a
UTF-8 `.py` module/file (Python decodes source as UTF-8), or pass them as
programmatic values — never as inline REPL literals.

## Conventions
- Keep tools synchronous (`def`, not `async def`) — the bridge blocks and COM
  must stay on one thread. FastMCP handles sync tools fine.
- Every tool returns a **dict** with an `error` key on failure (never raises).
- Add new convenience tools in `server.py` on top of `sa_run_step`, mirroring
  the SA SDK example file patterns.
- No comments in code unless asked; explain design in this file / README.

## Commands
- Install: `pip install -r requirements.txt`
- Build + deploy to ZCode: `python build_mcp.py` — byte-compiles every *.py,
  probes the MCP server over stdio (tools/list; COM-free, engine stays
  unborn), and writes the `spatial-analyzer` entry into the USER config
  `~/.zcode/cli/config.json` (default: tools in every project, whatever
  folder is open). `--scope workspace`/`--workspace` targets
  `<repo>/.zcode/config.json` instead (only auto-connects while that folder
  is the opened project). `deploy.cmd` = double-click wrapper. After
  code changes rerun it and reload MCP in ZCode (Settings → MCP → Reload /
  new session) — the server is a fresh process per launch.
- Run server foreground (debug): `python build_mcp.py start` (== `python server.py`)
- Test bridge (no SA needed): `python test_sdk.py`
- Test bridge (live SA): open SA, then `python test_sdk.py localhost`
- Start server: `python server.py`
- Debug via Inspector: `npx -y @modelcontextprotocol/inspector python server.py`
- Syntax check: `python -m py_compile sa_sdk.py sa_app.py server.py test_sdk.py`
- Dialog-dismissal regression (no SA, no COM): `python _t_dialogs.py`
- Geometry-identification regression (no SA, no COM): `python _t_identify.py`
- Compare/vector-group live harness (needs SA free): `python _live_vectors.py`

## Tools (server.py)
- Process: `sa_is_running`, `sa_launch`, `sa_ensure_running`, `sa_open_file`
  (opens/imports a `.xit` via `Open SA File`/`Import SA File`).
- Dialogs: `sa_dismiss_dialogs`, `sa_dialog_watchdog` (see "Modal dialogs
  mid-work are auto-closed" above).
- Connect: `sa_status`, `sa_connect`.
- Generic: `sa_run_step`, `sa_construct_point`.
- Inspection: `sa_inspect_project` — confirmed live on SA 2015 against a real
  job (`6.01.25 — обработка.xit`, collection "A"): 6 point groups (Опорная
  сеть, т контур, БО, LocateInstMeas1, LocateInstMeas1*, ц.ц), frames WORLD +
  New Frame, 2 lines, 2 cylinders — counts matched the operator's ground truth
  exactly (376 points in т контур). With no `collection`: lists top-level
  collections. With a `collection`: lists its objects grouped by `Object Type`
  (Point Group, Vector Group, Frame, Cylinder, Plane, Line, ... via
  `Make a Collection Object Name Ref List - By Type`), each entry a FULL
  hierarchical name ("A::т контур"); optionally the points in each point group
  (names like "т контур::1"; a 376-point group adds ~0.1 s).
- Best fit: `sa_best_fit` + `sa_best_fit_{plane,sphere,cylinder,cone,circle,line}`.
- Best fit from an explicit point list: `sa_best_fit_from_points` — fits
  exactly the named points wherever they live (subset of one group, or points
  across several groups/collections), via the `Fit Geometry to Points` list
  variant (same live-confirmed step `sa_fit_clean`'s passes use). Names may be
  full `C::G::T`, group-relative `G::T` (in `collection`), or bare targets
  (in `group`); each point keeps its stored reflector offset. A pre-existing
  object with the same name is deleted first (`replaced_object`), unreadable
  points are dropped under `unresolved`, min counts per type are
  `_LIST_FIT_MIN_POINTS` (line 2 … cone 6).
- Geometry identification: `sa_identify_geometry` — fits all six primitives
  to a cloud (point_group / explicit `points` / raw `coordinates`; fully
  offline for coordinates) and reports a ranked `candidates` list + a `best`
  pick, `confidence` (high/medium/low separation from a qualitatively
  different shape family), `recognized` (False = nothing fits well enough to
  be one primitive) and `notes`. Pure math in `sa_fitmath.py` (`cloud_stats`
  eigen decomposition, free-cone-angle fit, line fit) — nothing is created
  in SA. Decision rules live in `server._classify_cloud`: collinear clouds →
  line (a radial fit only wins if it is ~3x better — a real tube), flat
  clouds → circle when the points form a ring/rim (`_ring_like`, radial
  spread << radius) else plane, volumetric clouds → the solid primitive when
  the free circle fit (≡ cylinder radial fit) ties, and two parallel
  near-equal rings → cylinder (bore/shaft; the osculating sphere through the
  rims fits equally well — noted). Genuinely ambiguous synonyms keep
  `confidence` low with an explanatory note instead of a wrong high verdict.
  Offline regression: `python _t_identify.py` (10 shape cases, all expected
  picks + noise checks).
- Fixed fits: `sa_fit_fixed` + `sa_fit_fixed_{cylinder,sphere,circle,cone}` —
  constrained LSQ in `sa_fitmath.py` + `Construct <Type>` in SA; fixed
  radius/diameter or cone apex angle, compensation side outside/inside/none
  (circle also `both`). Offline math regression: `python _t_fitmath.py`;
  live end-to-end harness: `_live_fixed2.py`, side-semantics check:
  `_live_fixed3.py`.
- Fit quality & robust rebuild: `sa_geometry_props`, `sa_fit_quality`,
  `sa_best_fit_report`, `sa_fit_clean` — see "Fit quality + robust clean"
  below.
- **No auto "cardinal points" from fits (2026-09-07):** SA's default geometry
  fit profile creates a point group of cardinal points (center/axis/... of
  the fitted shape) named `<geometry name>Кардинальные точки` (English SA:
  `...Cardinal Points`) beside EVERY best-fit geometry — `Fit Geometry to
  Point Group` / `Fit Geometry to Points` expose no argument to disable it
  (live leftovers seen: `A::MCP_PROJ_PLNКардинальные точки`, ...). Every
  successful fit therefore deletes that group right away
  (`server._purge_auto_cardinal_groups`: enumerates the collection's Point
  Groups, removes exactly the group(s) whose name = object name + a
  cardinal keyword; `sa_best_fit`/`sa_best_fit_report` via
  `_fit_geometry_report`, each `sa_fit_clean` pass via `_fit_into`; the
  `Construct <Type>` path of `sa_fit_fixed` creates none). A fit response may
  carry `cardinal_points_removed` (full names deleted). Offline logic
  regression: `python _t_cardinal_logic.py` (no SA); live regression
  `python _t_cardinal.py` (needs SA free — no other SDK engine connected).
- View control (show/hide in the graphics window): `sa_show_objects`,
  `sa_hide_objects` (collection objects — point groups, frames, geometry,
  etc.; steps `Show Objects`/`Hide Objects`), `sa_show_points`,
  `sa_hide_points` (individual points of a group; step `Show/Hide Points`),
  `sa_show_hide_by_type` (a whole Object Type at once; step `Show/Hide by
  Object Type`). All accept the joined full names `sa_inspect_project`
  returns. Live harness: `_live_showhide.py` (every hide is paired with the
  matching show, so the fixture's graphics state is restored).
- Project points onto objects: `sa_project_points` — mirrors SA's
  Construct > Points > Project Points to > Objects > Closest Point
  (step `Query Points to Objects`, see "Projection" below). Whole point
  groups and/or individual points onto one or more objects, output as a NEW
  point group. Live harness: `python _live_proj.py`.
- Compare points to objects (deviation whiskers): `sa_compare_points_objects`
  — the GUI Compare > Points > Objects ("Сравнить > Точки > Объекты") as a
  typed tool: same `Query Points to Objects` engine, but with a VECTOR-group
  Projection Option output, so it creates a NEW vector group of deviation
  arrows (one per source point) instead of a point group. Vector groups are
  read back with `sa_vector_group_props` (group statistics + optional
  per-vector dump) and styled with `sa_vector_group_style` (arrows/colours/
  tolerance/colour bar, incl. auto-range). Live harness:
  `python _live_vectors.py`. Step/arg names are from the PDF and NOT yet
  confirmed live — see "Compare > Points > Objects" below.

## SA 2015 step + arg names — CONFIRMED LIVE (against SA 2015.07.28, "MP Command Reference")
SA step/arg names live in SA's Measurement Plan tree and are NOT in a type
library. The authoritative source is `Documentation\MP Command Reference.pdf`
in the SA install (printed page N ≈ PDF page N+25). The names below were all
verified live (return `DoneSuccess`/code 2) on SA 2015; the older guesses
(`"Get the Names of all ..."`, `"Open a File"`, arg `"File Path"`) **do not
exist** in this build — don't reintroduce them. A plain-text extraction of the
PDF is kept at `mp_ref.txt` in the repo root for quick greps.

**File loading (.xit is SA's native exchange/project format; a .xit holds a
whole job: collections, points, geometry, frames, instruments, MP tasks):**
- `Open SA File` — discards current job, opens the .xit.
  in: `SA File Name` (File Path or Embedded File Name). **(NOT "File Path".)**
- `.xit64` files open through the same `Open SA File` step (confirmed live on
  SA 2015: "14.02.2024.xit64" → DoneSuccess). No special handling needed.
- `Import SA File` — imports .xit INTO the current job (merges, renames on
  collision). in: `SA File Name`, `Allow Operator Selections` (Boolean; False
  = import everything silently, True pops a picker).
- (Note: there is no `.sa` step; SA's on-disk job file IS the .xit.)

**Project enumeration (SA 2015 has NO "Get the Names of all ..." steps — it
uses a count + i-th pattern, and type-filtered ref-list builders):**
- Collections: `Get Number of Collections` (out `Total Count` Integer) + loop
  `Get i-th Collection Name` (in `Collection Index` Integer,
  out `Resultant Name` String). Collections are 0-indexed; index order matches
  the tree. → `server._list_collections()`.
- Objects of a type in a collection:
  `Make a Collection Object Name Ref List - By Type`
  (in `Collection` String + `Object Type` enum via `set_object_type_arg` /
  `InvokeTypes`; out `Resultant Collection Object Name List`, read with
  `get_collection_object_name_ref_list_arg`). →
  `server._objects_in_collection_by_type()`.
- The only dedicated non-interactive builder for ONE type is
  `Make a Collection Object Name Ref List from all Groups in a Collection`
  (in `Collection Name`; out `Collection Object Name List`) → point groups only.
- Points in a point group:
  `Make a Point Name Ref List From a Group`
  (in `Group Name` via `set_object_name_arg`; out `Resultant Point Name List`,
  read with `get_point_name_ref_list_arg`). → `server._points_in_group()`.
- Vectors in a vector group:
  `Make a Vector Name Ref List From a Vector Group`
  (in `Vector Group Name`; out `Resultant Vector Name List`).

**Object Type enum values (all return DoneSuccess; "Any" = every type):**
`Any`, `Point Group`, `Vector Group`, `Frame`, `Circle`, `Cylinder`, `Plane`,
`Sphere`, `Cone`, `Line`, `Perimeter`, `B-Spline`, `Surface`, `Scale Bar`.
(Held in `server.SA_OBJECT_TYPES`. Note: cloud type is NOT "Point Cloud" here —
that value returns code 3 / minor error; no cloud builder was confirmed.)

**Ref-list outputs — FLAT full names, never chunk them.**
Every SA "Name Ref List" getter (Collection Object Name List, Point Name List,
Vector Name List, ...) returns a flat 1-D array with ONE hierarchical full name
per element — e.g. `"A::т контур"`, `"A::WORLD"`, or `"::т контур::1"` (the
leading `::` = empty collection part; SA returns that when the step input had
no collection prefix). Confirmed live on SA 2015: a 376-point group returned
376 elements. The old parsers that chunked such arrays every 2 or 3 elements
into `{collection, object}` pairs / `{collection, group, name}` triples were
BOGUS — 6 point groups looked like 3 fake "group in group" pairs, and every
3rd point of 376 was silently dropped. The `sa_sdk.py` getters now return
plain `list[str]` of full names. Never reintroduce pair/triple chunking.

**One object can legitimately appear under several Object Types.**
SA reports a name in every type-filtered list it belongs to: group "т контур"
shows up under BOTH "Point Group" and "Cylinder", because the cylinder
best-fit geometry constructed from that group keeps the group's name (the
file's report "Рез-ты впис. геом. (т контур)1" is the same story). That is
correct output, not a duplicate bug.

**Group-name step inputs must be BARE (no collection prefix).**
`Make a Point Name Ref List From a Group` with `"A::т контур"` → code 3
(minor error); with bare `"т контур"` → code 2 and the 376 points (SA resolves
bare names in the current collection). `_points_in_group()` strips everything
up to the last `"::"` for exactly this reason; the returned point names carry
the group prefix with a possible leading `"::"`, which the tool strips so
clients see e.g. `"т контур::1"`.

**Ref-list INPUTS — the same flat joined full names, ONE per element.**
The `Set<...>RefListArg` setters accept the SAME layout the getters emit
(`A::Cyl`, `C::G::T`, or `::G::T` for the current collection) — one joined
hierarchical name per element. The old (collection, group, target) triple
flattening returned `DoneFatalError` on `Fit Geometry to Points` /
`Delete Points`; joined full names return `DoneSuccess` (confirmed live on SA
2015). The VARIANT*-carried SAFEARRAY must be passed as a PLAIN Python list
under an explicit `VT_VARIANT|VT_BYREF` descriptor (`sa_sdk._set_variant_list`);
wrapping it in a win32com `VARIANT` raises DISP_E_TYPEMISMATCH. Never
reintroduce pair/triple chunking in the setters either.

**SA never OVERWRITES a constructed object whose name already exists** — a
second fit under the same `Resulting Object Name` silently creates a
SUFFIXED duplicate ("X", then "X1"; confirmed live on SA 2015: two fits of the
same group/name → cylinders `A::X` AND `A::X1`). Reading geometry back under
the plain name then returns the FIRST object's stale parameters — a silent
wrong-result trap for any loop that re-fits the same output name.

- Delete constructed geometry / point groups: `Delete Objects`
  (in `Object Names` — Collection Object Name Ref List, joined full names).
  SUCCESS = code 2. Removes ANY collection object, including a point group
  with all its points.
- Delete points from a group: `Delete Points` (in `Point Names` — Point Name
  Ref List, joined full names).
- `Fit Geometry to Points` (the list variant of the fit) fits EXACTLY the
  named points: in `Points to Fit` (Point Name Ref List) instead of
  `Group to Fit`; all other args identical to `Fit Geometry to Point Group`.
  Confirmed live: a line over exactly 2 points leaves a 3rd point's deviation
  at 100.0 (untouched); a plane over 2 points → DoneFatalError
  (underdetermined); SA still applies each point's stored reflector offset.

**Best-fit geometry — ONE confirmed step, NOT "Construct a Best Fit <Shape>".**
SA 2015 (MP Command Reference, ch. 6 Analysis Operations, p. 414) fits any
primitive via the single step `Fit Geometry to Point Group` (list variant:
`Fit Geometry to Points`). The three "Construct a Best Fit Plane"-style names
in the original code DO NOT exist here — every `SetStep` returned `SdkError`
(code -1) live. Args (all confirmed): `Geometry Type` (enum — plane, sphere,
cylinder, cone, circle, line, ...; MUST be set via `sa_sdk.set_geometry_type_arg`,
the same `InvokeTypes`-with-`VT_BSTR` trick as Object Type, or the enum never
reaches SA), `Group to Fit` (Collection Object Name: full `A::плоскость`),
`Resulting Object Name` (Collection Object Name), `Fit Profile Name` (blank =
default profile), `Report Deviations` (Boolean; set False so SA never pops the
results dialog), `Fit Interface Tolerance (-1.0 use profile)` (Double; 0.0 = no
tolerance limit — the arg label carries its usage hint, cf.
"Resultant Point Name List(A+B)"), `Ignore Out of Tolerance Points` (Boolean),
`Starting Condition Geometry` (optional). Return Arguments: None — success is
`DoneSuccess` (code 2) + the object appearing in the tree under `Resulting
Object Name` (verified live: fit of group "плоскость" → "A::MCP_BF_плоскость"
shows up in the collection's Plane list).

`_try_steps(...)` runs candidate step names until one returns `DoneSuccess`
(code 2). Remaining candidate lists: only `sa_open_file` (import mode probes
`Import SA File` then `Open SA File`; plain open is the single confirmed step).
`socket`-style probing of wrong names is gone from best fit. When adding a
step, copy the exact name from the PDF or SA's MP tree and confirm live.

**Fit quality + robust clean — CONFIRMED LIVE on `6.01.25 — обработка.xit`
(collection "A", group "т контур", 376 pts, cylinder).**

- **Reflector compensation.** SA measures to the reflector CENTRE. Each
  point's `Get Point Properties` → `Radial Offset` stores the reflector radius
  (19.05 mm = 3/4" SMR in the test job, all 376 points) and fits apply it, so
  raw point coordinates sit ~19.05 mm OUTSIDE the fitted surface. Reported
  deviations must be reduced by the stored per-point offset — or by a constant
  `probe_offset_mm` when the group stores none. Compensated RMS of the plain
  cylinder fit = 0.661255575 mm (raw ≈ 19.06). The response says which one ran
  (`server._compensation_meta`: stored_point_offsets | constant | none).
- **Geometry parameters:** `Get <Type> Properties` — `Get Cylinder Properties`
  (radius/diameter/length), `Get Plane Properties`, `Get Sphere Properties`,
  `Get Circle Properties`, `Get Line Properties`, `Get Cone Properties`.
  Read via `get_double_arg` / `get_vector_arg` with the per-type label sets in
  `server._GEOMETRY_PROP_SPECS` (SA labels differ per shape; each label is
  tried in order). Point readback: `Get Point Coordinate` (in `Point Name`,
  out `X Value`/`Y Value`/`Z Value`), `Get Point Properties` (out
  `Radial Offset`/`Planar Offset`), `Get Number of Points in Group`.
- **Deviation convention** (`server._compute_deviations`): signed; + = point
  outside the nominal surface (cylinder/sphere/circle radial, plane along its
  normal), − = inside; a line has magnitude only. `server._dev_stats` →
  RMS ("СКО"), average, standard deviation, min/max/max-abs signed deviation.
- Tools: `sa_geometry_props` (parameters of an existing object),
  `sa_fit_quality` (parameters + per-point deviations + stats + outliers
  beyond `tolerance_mm`), `sa_best_fit_report` (fit + quality in one call),
  `sa_fit_clean` (robust rebuild — next).
- **`sa_fit_clean`** replicates the GUI robust best-fit as an explicit pass
  loop: fit → read geometry → measure compensated deviations → refit ONLY the
  in-threshold points via `Fit Geometry to Points` → repeat until no outliers
  or the excluded set stops changing (or `max_iterations`). Each iteration
  reports RMS/stats over all points AND over the kept core (`stats_kept`).
  `delete_outliers=True` afterwards PHYSICALLY deletes the outliers
  (`Delete Points`) and refits — destructive, explicit opt-in only.
  - Because SA never overwrites, the tool deletes its previous-pass object
    before every refit and any pre-existing same-named object up front
    (`replaced_object` in the response): the job ends with exactly ONE object
    under `object_name`.
  - Threshold: fixed `tolerance_mm` (physical, job units) or `sigma` × robust
    spread `1.4826 * MAD` of the deviations (`server._robust_sigma`; plain σ
    of all points excludes nothing when a few bad points inflate it — seen
    live). Deviations are measured around the fit's MEDIAN: a strong outlier
    can pull the first LSQ fit so far that the inliers read out-of-tolerance
    on the wrong side (seen live: 5 pts at ~0.02 noise + one at +10 mm →
    inliers come back at ~ −1.65 mm). A fixed-tolerance run whose median sits
    > 0.25·tol from zero gets one automatic "debias" pass first
    (`debias_pass: True` in that iteration).
  - Convergence caveat (seen live): when the surface FORM error exceeds the
    tolerance (т контур at 0.2 mm vs ~0.66 mm RMS form error), the excluded
    set keeps churning and the run ends at `max_iterations` with
    `converged: False` and the tightest-core geometry (RMS over kept ≈ 0.095
    vs 0.661 over all points). That is honest output, not a bug: the points
    really are that far off the best 0.2 mm cylinder. `delete_outliers` still
    deletes against the final geometry, like the SA GUI 'Delete Outliers'.
  - Live numbers (2026-09-04): «т контур» + tolerance 0.2 → kept-core RMS
    0.095 after 6 passes, 254 points beyond 0.2 on the plain fit; sigma 3 →
    13 worst points beyond ~1.6 mm excluded; planted +10 mm outlier in a 6-pt
    plane group → excluded set {P6} only, kept-core RMS 0.018,
    `delete_outliers=True` removed exactly P6 (group 6 → 5).

**Projection — "Проецировать точки на объекты / ближайшая точка"
(2026-09-07, confirmed live on SA 2015).** The GUI command
Construct > Points > Project Points to > Objects > Closest Point maps onto the
MP step `Query Points to Objects` (MP Command Reference ch. 6, a geometry
QUERY whose engine can "create projected points on the object" instead of a
deviation group):
- in: `Point Names` (Point Name Ref List, joined "C::G::T" per point),
  `Object Name List (Objects to Project to)` (Collection Object Name Ref
  List, joined "C::O" per object), `Resulting Object Name` (Collection Object
  Name — the NEW point group that gets the projected points),
  `Projection Options` (via `sa_sdk.set_projection_options_arg`,
  projection-type string + booleans — see below),
  `RMS Tolerance (0.0 for none)`, `Maximum Absolute Tolerance (0.0 for none)`
  (Double), `Show Results Dialog?` (Boolean; always False). out: `RMS
  Deviation`, `Max Absolute Deviation`, `Average Deviation`,
  `Standard Deviation` (Double; read best-effort).
- **Projection Options** (`SetProjectionOptionsArg`):
  projectionType ∈ {`Points on Object` (closest point ON the object — the
  default + 99% real use), `Points on Offset Object`, `Points on Probe
  Surface`, `Offset Object To Target Vectors`, `Target To Offset Object
  Vectors`, `Object To Probe Vectors`, `Probe To Object Vectors`} +
  ignoreEdgeProjections, bOverrideTargetOffsets + overrideTargetOffsetsValue,
  bAddExtraMaterialThickness + extraMaterialThicknessValue. All plain values
  → goes through dynamic dispatch, no `InvokeTypes` needed (unlike the Object
  Type enums).
- **Geometry is exact, status is NOT** (the tool's semantics): projected
  points land exactly ON the object (plane z=25 → z≈0 to 1e-9; source at
  r=430 onto cylinder r=400 → r=400 exactly; real «т контур» points → r =
  r_fit ±1e-6). But SA 2015 intermittently returns code 3 DoneFatalError for
  identical inputs while STILL creating the points, and silently SKIPS
  points it cannot project. `sa_project_points` therefore decides
  `projected`/`result_count` by what the result point group actually
  contains (`Make a Point Name Ref List From a Group` after the step),
  reports `status_code`/`status` as advisory, and lists the missing targets
  under `skipped`/`skipped_points` (target names compared after stripping
  the "C::G::" prefix). A same-named result group is deleted first
  (`replaced: True`) because SA never overwrites — it would merge.
- **probe_offset / extra_material semantics (live-verified):** with
  projection_type `Points on Object` the created points ALWAYS stay on the
  object — `probe_offset_mm` and `extra_material_mm` are ignored. The GUI's
  Probe Offset back-away is projection_type `Points on Offset Object` +
  probe_offset_mm (points move along the outward surface normal by the
  offset; live: +10 → z 25→10 exactly). Extra material thickness applies to
  the offset/vector outputs, not to the on-object points. The per-point
  stored reflector offsets on created points read back as 0.
- Offsets: `use_stored_offsets=False` (default) overrides stored per-point
  probe/reflector offsets to 0 and projects the raw coordinates
  (deterministic — avoids SA 2015 fatals on groups carrying stored offsets,
  e.g. «т контур» with 19.05 mm on every point). `True` lets SA apply the
  stored values (measured-surface semantics). Either way the created points
  land ON the object.
- Live harness: `python _live_proj.py` (T1 plane 9 pts; T2 Offset Object
  back-away +10; T3 cylinder; T4 explicit points; T5 auto `_proj` name; T6
  re-run into an existing group → replaced + full count; T7 real «т контур»
  5 of 376 points onto its free cylinder fit, offsets of created points = 0;
  cleanup deletes all MCP_PROJ_* objects).

**Compare > Points > Objects — point-vs-object deviation vector groups
(2026-09-07, step names from the PDF — NOT yet confirmed live on SA 2015).**
The GUI Compare > Points > Objects ("Сравнить > Точки > Объекты") is the
SAME `Query Points to Objects` engine as the projection, with the Projection
Options output set to one of the four VECTOR-group types instead of "Points
on Object": every source point is compared to the closest of the target
objects and SA creates a NEW VECTOR GROUP of deviation whiskers (the primary
graphical way SA shows deviations). `sa_compare_points_objects` wraps it —
whole point groups and/or explicit points vs one or more objects, source +
result resolved exactly like `sa_project_points` (joined full names,
delete-before-create of a same-named vector group under `replaced`, default
result name `<group>_dev` for a single source group). It returns the group
properties read back (`total_vectors`, in/out-of-tolerance counts,
magnitude stats) and lists skipped points.
- Direction of the whiskers = the Projection Option output:
  `Object To Probe Vectors` (default — the GUI "inspect" view: arrow from
  the object to the measured point, i.e. how far the point is off nominal),
  `Probe To Object Vectors` (same magnitudes, reversed — the "build" view),
  `Target To Offset Object Vectors` / `Offset Object To Target Vectors` (the
  "Offset Surface" pair, vectors anchored at the point centre with all
  offsets applied on the object — for thin parts). The three point-group
  outputs of the same enum belong to `sa_project_points`.
- Offsets follow the same semantics as the projection tool, with the
  opposite default: `use_stored_offsets=True` lets SA apply each point's
  stored probe/reflector offset (the SA User Manual says all query commands
  account for target offset unless overridden), `False` overrides to raw
  coordinates. `probe_offset_mm` / `extra_material_mm` map onto the same
  Projection Options fields as the projection path.
- Same flaky-query caveat as `sa_project_points` (status is advisory;
  SA can return code 3 while still creating the group and silently skips
  points it cannot compare) — success is decided by what the created vector
  group contains, `status_code`/`status` are kept for reference, skipped
  count reported.
- Vector-group read-back — `sa_vector_group_props` wraps
  `Get Vector Group Properties` (in `Vector Group Name`, a Collection Object
  Name; out Integer `Total Vectors`, `Vectors In Tolerance`, `Vectors Out Of
  Tolerance`; Double `% Vectors In Tolerance`, `% Vectors Out Of Tolerance`,
  `Absolute Max Magnitude`, `Absolute Min Magnitude`, `Max Magnitude`,
  `Min Magnitude`, `Standard Deviation`, `Standard Deviation Mean Zero`,
  `Average Magnitude`, `Avg of Abs Magnitude`, `High Tolerance Value`, `Low
  Tolerance Value` — MP Command Reference p. 360) and `include_vectors=True`
  also dumps each whisker via `Get i-th Vector From Vector Group` (in
  `Vector Group Name` + Integer `Vector Index`; out String `Vector Name`,
  Vector `Begin in Working` / `End in Working` / `Total Delta in Working` /
  `ijk Unit Vector in Working`, Double `Magnitude`; 0-based index — the
  vector-index base is taken from the collection/point convention, not yet
  verified live). Group count: `Get Number of Vectors in Vector Group` (out
  Integer `Total Count`).
- Style — `sa_vector_group_style` wraps the SA 2015 colorization steps
  `Set Vector Group Colorization Options (Selected)` / `Auto-Range and Set
  Vector Group Colorization (Selected)` (p. 362-365): in
  `Vector Groups to be Set` (Collection Vector Group Name Ref List, joined
  full names), `Treat Individually?` (auto-range only) and the whole
  `Colorization Options` / `Colorization Options (Uses Mode Only)` compound,
  set via `sa_sdk.set_colorization_options_arg` (a 20-param
  `SetColorizationOptionsArg` — colour-range method string + three base
  colours + draw/arrowhead/tube/blotch/colour-bar flags + magnification +
  width + saturation + tolerance values — routed through `InvokeTypes` with
  explicit descriptors, same no-type-library reason as the Object Type
  enums: the compound embeds two enum strings). The tolerance values feed
  the in/out-of-tolerance statistics that `Get Vector Group Properties`
  reads back. The exact enum spellings (colour-range method presets "Go/
  No-Go", "Continuous", ..., colour names) come from the SA docs/GUI and are
  NOT yet verified live; the docstring lists the known presets. There is no
  getter for the options compound (`Get Vector Group Colorization Options`
  exists but its out-arg is the same opaque compound; the SDK exposes no
  per-field reader) — so every field you omit takes the defaults above, no
  partial updates.
- Live harness: `python _live_vectors.py` (T1 plane +25 mm → 9 whiskers of
  ~25 mm pointing probe-ward; T2 raw compare with probe_offset_mm → same
  magnitudes; T3 cylinder r=400/r=430 → ~30 mm outward; T4 reversed
  direction → same magnitudes, opposite ijk; T5 explicit points across two
  objects; T6 re-run → replaced; T7 style round-trip (tolerance values
  read back by the props step, in+out-of-tolerance == total); T8 auto-range;
  T9 real «т контур» vs its free cylinder fit → compensated magnitudes ≪
  19.05; cleanup deletes all MCP_VEC_* objects).

## Status / next steps
Done: connect, generic step runner, construct-point, process launch,
file open (`.xit` via `Open SA File`/`Import SA File`), and full project
inspection (`sa_inspect_project` — collections, objects-by-type, points per
group) — all confirmed live on SA 2015. Plus all 6 best-fit primitives
(`sa_best_fit` + typed wrappers) through the single `Fit Geometry to Point
Group` step, and the full fit-quality chain confirmed live on `6.01.25 —
обработка.xit`: geometry parameter readback, reflector-compensated deviations
(СКО/RMS), outlier detection vs a tolerance, and the robust rebuild
(`sa_fit_clean`: tolerance or sigma×MAD threshold, delete-before-refit pass
loop, optional physical `Delete Points`, replaced-object semantics).

Fixed-parameter fits (2026-09-04): `sa_fit_fixed` + typed wrappers
`sa_fit_fixed_{cylinder,sphere,circle,cone}` — best fit with a FIXED nominal
(radius/diameter for cylinder/sphere/circle, full apex angle "угол раствора"
for cone) and an explicit reflector-offset compensation side (outside/+,
inside/−, none, or per-point-sign "both" for circles). SA 2015 has NO fit step
that constrains a parameter, so the primitive is fitted by constrained
least squares in `sa_fitmath.py` (pure Python, no numpy; Levenberg-Marquardt
on the per-point compensated surface residual) and the result is then CREATED
in SA via the `Construct <Type>` step, so the SA object carries exactly the
fixed diameter / included angle (verified live by reading it back). The math
is covered offline by `python _t_fitmath.py` (25 checks: clean/noisy ×
outside/inside/none for all five primitives, radius/angle recovery, free
radius regression). Live-verified end-to-end on the fixture 2026-09-04:
- Synthetic clouds, all four types: SA object constructed, `Get <Type>
  Properties` read-back radius/diameter/included_angle EQUALS the nominal
  (d=1000 cylinder, r=800 sphere, r=400 circle, 50° cone), RMS ≈ the 0.02 mm
  injected noise.
- Real group «т контур» (376 pts, stored Radial Offset 19.05 for every
  point — SMR measurements): free fit → Rf=2068.26, RMS 0.661; re-fit at the
  true-surface radius Rf−19.05 with compensation `outside` → RMS 0.661
  (correct side, matches the free fit), `inside` → RMS 38.09 (≈2× probe,
  wrong side). Symmetric at Rf+19.05. The side argument therefore
  discriminates the measured side on real data by ~57× in RMS.
- Key semantics: with per-point stored offsets the raw free-fit radius Rf is
  the reflector-CENTRE radius; to fit a FIXED radius you must pass the
  TRUE-surface radius (≈ Rf ∓ stored offset), not Rf. The `compensation`
  side then picks the correct sign (reflector on outside of a shaft →
  `outside`).

Fit-from-point-list + geometry identification (2026-09-07): two new
`@mcp.tool()`s — `sa_best_fit_from_points` (best fit over an EXPLICIT list of
points across any groups/collections; the live-confirmed `Fit Geometry to
Points` list step, same path `sa_fit_clean` already uses per-pass) and
`sa_identify_geometry` (classify a cloud's shape offline: fit all six
primitives in `sa_fitmath.py` — incl. NEW free-cone-angle and line fits — and
rank them). Decision + ambiguity handling is in `server._classify_cloud` and
was tuned against synthetic clouds (regression `python _t_identify.py`, 10
cases + noise + degenerate inputs, all expected):
- The free 3D circle fit is mathematically the cylinder's radial fit, so a
  clean long cylinder, a ring and a short bore all give the circle fit RMS ≈
  the cylinder's — the cloud's gross shape (eigen-decomposition `cloud_stats`:
  flat / linear / volumetric) breaks those ties: collinear → line (any radial
  fit only wins if ≥3× better — a real tube), flat → circle when `_ring_like`
  (points hug one radius in the fitted plane; ring/rim/arc) else plane,
  volumetric → solid when a circle "best" ties the cylinder, and two parallel
  near-equal rings → cylinder (bore/shaft) even though an osculating sphere
  through the rims fits exactly.
- Genuinely ambiguous synonyms keep `confidence` low with a note instead of a
  false-high verdict: a coplanar ring IS also a plane and a two-ring bore IS
  also a sphere; a flat patch IS also an osculating huge sphere/cylinder.
  On flat clouds those curved rivals are excluded from the confidence pool
  (they only match by degenerating), and on collinear clouds so are the
  collapsed radial fits and the containing plane.
- `recognized: False` when the winning residual is large relative to the
  cloud size (free-form surface / mixed shapes — e.g. a box corner reads
  best-fit sphere with recognized False + a note), and a pile of coincident
  points is rejected outright ("no extent").
- `sa_best_fit_from_points` mirrors `sa_fit_clean`'s live semantics: joined
  full names in the ref list, per-point stored reflector offsets applied,
  pre-existing same-name object deleted first, auto cardinal-point group
  purged. Live end-to-end still to do: run it against `6.01.25 — обработка.xit`
  (subset of «т контур» + a cross-group pick) the next time SA is free.

Newly confirmed SA 2015 step names (all in "MP Command Reference.pdf", this
time taken from the PDF text, not live-probed):
- `Delete Objects` — in: `Object Names` (Collection Object Name Ref List).
  Non-interactive, SUCCESS/PARTIAL SUCCESS. (Used by the fixed-fit tools to
  replace an existing object — SA never overwrites, it would auto-suffix.)
- `Construct Cylinder` — in: `Cylinder Name`, `Cylinder End Point (in working
  coordinates)`, `Cylinder Axis (in working coordinates)`, `Cylinder
  Diameter`, `Cylinder Length`. Diameter = 2× the fitted radius
  (`_CONSTRUCT_SPECS["cylinder"]["radius_from"]`).
- `Construct Sphere` — in: `Sphere Name`, `Sphere Center`, `Sphere Radius`.
- `Construct Circle` — in: `Circle Name`, `Circle Center`, `Circle Normal`,
  `Circle Radius`.
- `Construct Cone` — in: `Cone Name`, `Cone End Point (in working
  coordinates)`, `Cone Axis (in working coordinates)`, `Cone Length`, `Cone
  Theta Start`, `Cone Theta Span`, `Cone Included Angle`. The fit params
  carry no sweep angles → `_CONSTRUCT_SPECS` defaults theta_start=0 /
  theta_span=360 (full cone).
- `Get <Type> Properties` return-arg names (verified against the PDF, used by
  `_read_geometry_props`): Sphere — `Center Coordinate`, `Radius`,
  `Diameter`; Circle — `Center Coordinate`, `Normal Direction`, `Radius`,
  `Diameter`; Cylinder — `Begin Coordinate`, `End Coordinate`, `Axis
  Direction`, `Length`, `Radius`, `Diameter`; Cone — `Cone End Point (in
  working coordinates)`, `Cone Axis (in working coordinates)`, `Cone Length`,
  `Cone Theta Start`, `Cone Theta Span`, `Cone Included Angle`; Plane —
  `Normal Direction`, `Point on Plane`, `D Parameter`.

Live-verified 2026-09-04 (unlike the above, these were probed against the
running SA, not just the PDF):
- `Show Objects` — in: `Objects to Show` (Collection Object Name Ref List).
- `Hide Objects` — in: `Objects to Hide` (Collection Object Name Ref List).
  Both take one joined "C::O" full name per object ("A::т контур"); SUCCESS =
  code 2, PARTIAL SUCCESS = code 4 (DoneMinorError) when some names were not
  found, FAILURE otherwise. NOTE: the PDF arg table also lists a second
  ref-list arg under `Show Objects` ("Object Name List ... export to the
  file") — that belongs to a neighbouring step; the real step takes only
  `Objects to Show` (setting only it returns DoneSuccess live).
- `Show/Hide Points` — in: `Point Names` (Point Name Ref List, one joined
  "C::G::T" name per point), `Show? (Hide = FALSE)` (Boolean). SUCCESS even
  for an empty selection; PDF says "This command always succeeds".
- `Show/Hide by Object Type` — in: `All Collections?` (Boolean),
  `Specific Collection` (Collection Name), `Object Type To Show/Hide`
  (Object Type enum via `set_object_type_arg`), `Hide? (Show = FALSE)`
  (Boolean). WATCH OUT: the PDF table's "Collection Name" column is the
  arg's TYPE, not its name — setting an arg literally named "Collection
  Name" makes ExecuteStep return SdkError (code -1); the real name is
  `Specific Collection` (confirmed live). With no collection scope set the
  step acts on the current/active collection.

Modal-dialog auto-close (2026-09-04): `sa_dismiss_dialogs` (one-shot) +
background watchdog `sa_dialog_watchdog` (armed automatically by
`_ensure_bridge` before any step runs; stop/start/status; interval + optional
title filter). Pure sa_app/ctypes — never touches COM. Verified offline
against real MessageBoxes (`python _t_dialogs.py`: MB_OK / MB_OKCANCEL /
MB_YESNO all close); the engine's INVISIBLE #32770 splash/about window (no
buttons) is deliberately not treated as a blocking dialog.

Graphics view control (2026-09-04): `sa_show_objects` / `sa_hide_objects` /
`sa_show_points` / `sa_hide_points` / `sa_show_hide_by_type` set the
show/hide state of collection objects, of individual points inside a group,
or of every object of one type (MP Command Reference ch. 4 "View Control").
Live-verified on `6.01.25 — обработка.xit`: hide/show of point group
«Опорная сеть» and of points of «LocateInstMeas1*» by joined full names →
code 2; hiding [valid group, bogus name] → PARTIAL SUCCESS code 4;
type-level hide/show of Frame / Point Group scoped to collection "A"
(`Specific Collection`) and across all collections → code 2. Every tool
normalizes simple names into the ref-list full form (`_object_full_name` /
`_point_full_name`) and the harness restores visibility after each check.
State changes are in-memory; nothing is written back to the .xit.

Project points onto objects (2026-09-07): `sa_project_points` — the SA GUI's
"Проецировать точки на объекты / ближайшая точка" as a typed tool, driven by
the `Query Points to Objects` step + Projection Options "Points on Object"
(see "Projection" in the step-names section). Live-verified end-to-end on
`6.01.25 — обработка.xit` (harness `_live_proj.py`, ALL OK 2026-09-07): plane
9 pts z=25 → z≈0 (x/y preserved to 1e-12); cylinder source r=430 → r=400
exact; `Points on Offset Object` +10 mm → points backed off 10 mm; explicit
point lists; auto `_proj` result name; re-run into an existing result group
(delete + re-create, `replaced: True`) reproduces the full requested count;
real «т контур» (376 pts, stored 19.05 mm offsets) → 5 of 5 onto its free
cylinder fit, radii = r_fit ±1e-6, created points' offsets = 0. Known SA 2015
quirk baked into the tool: the step status is flaky (code 3 while still
creating points; silent partial skips) — success is decided by the created
result group, status is advisory, skipped targets are listed.

Point-vs-object compare + vector groups (2026-09-07): `sa_compare_points_objects`
(the GUI "Сравнить > Точки > Объекты" as a typed tool — the same
`Query Points to Objects` engine with a VECTOR-group Projection Options
output, creating a deviation-whisker vector group), `sa_vector_group_props`
(`Get Vector Group Properties` stats + `Get i-th Vector From Vector Group`
per-vector dump) and `sa_vector_group_style` (the `Set Vector Group
Colorization Options (Selected)` / `Auto-Range and Set ... (Selected)`
steps via the new `sa_sdk.set_colorization_options_arg` +
`set_collection_vector_group_name_ref_list_arg` + `set_projection_options_arg`
primitive). Step + arg names are taken from the MP Command Reference PDF
(p. 358-365, 391, 404) and are NOT yet confirmed live; the harness
`python _live_vectors.py` (synthetic plane/cylinder vectors, directions,
style round-trip, auto-range, real «т контур» compensation) is the pending
live check — run it the next time SA is free.

To extend: exports/reports, USMN, point/group manipulation beyond
delete/construct. See the C++/C# SDK examples for exact step+arg names and
the "MP Command Reference.pdf" sections referenced above (extract the text
with pymupdf if you need a grep-able copy).
