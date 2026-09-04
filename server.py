"""Minimal MCP server for Spatial Analyzer (SA).

Exposes SA SDK operations as MCP tools so an LLM client can drive
SpatialAnalyzer. Transport: stdio (standard for MCP servers launched by a
client such as Claude Desktop, opencode, or the MCP Inspector).

Run directly to start the server:
    python server.py

Connect from a client via stdio. See AGENTS.md / README.md for config.
"""

import logging
import math
import os
import threading
import time

from mcp.server.fastmcp import FastMCP

import sa_app
import sa_fitmath
from sa_sdk import MP_STATUS, SAError, SABridge

logging.basicConfig(
    level=os.environ.get("SA_MCP_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sa-mcp")

# Shared bridge, created LAZILY on the first tool call that needs COM - never
# at import. Spawning the SDK engine (SpatialAnalyzerSDK.exe) while the SA GUI
# is not yet up makes the engine pop a modal "NRK socketinterface 10061" error
# and wedge every later Connect() (see AGENTS.md). Import must stay COM-free;
# sa_ensure_running() launches the GUI first and only then creates the bridge.
sa = None


def _ensure_bridge():
    """Create the shared bridge on first use (only if the GUI is really up).

    The SDK engine (SpatialAnalyzerSDK.exe) must NEVER be born before the SA
    GUI's SDK listener is up - an engine born without a GUI pops a modal
    "NRK socketinterface 10061" dialog and wedges every later Connect(). A
    bare process is NOT enough: an auto-launched SA can linger as a stuck
    zombie (no window, no listener, ~30 MB idle - usually after a bad
    previous shutdown), and an engine born against that pops the same modal.
    Engine birth is therefore gated on the GUI having a VISIBLE WINDOW
    (sa_app.sa_has_visible_window, EnumWindows - no COM). We also keep
    checking every call so a tool after the GUI died raises instead of
    silently running steps against a stale connection.
    """
    global sa
    if not sa_app.is_sa_running():
        raise SAError(
            "SpatialAnalyzer GUI is not running. Start it first via "
            "sa_ensure_running().")
    _ensure_watchdog()  # auto-close modals that would otherwise hang steps
    if sa is None:
        if not sa_app.sa_has_visible_window():
            raise SAError(
                "SpatialAnalyzer is running but shows no window - the "
                "instance is stuck (bad previous shutdown / license prompt?) "
                "and its SDK listener is not up. The engine was NOT spawned "
                "(it would pop a modal 10061 error). Close SA and retry, or "
                "start SA manually.")
        log.info("Creating SA bridge (engine spawns after GUI is up).")
        sa = SABridge()
    return sa


def _ensure_sa(host: str = "localhost"):
    """Return the bridge, creating it AND connecting it (self-bootstrap).

    An unconnected engine silently no-ops every MP step (empty results, no
    error), so this is what every tool goes through: bridge is born only when
    the GUI is up, then Connect() runs - instant when SA is already running,
    so the first tool call needs no separate sa_connect()/sa_ensure_running().
    Raises SAError with a retry hint when Connect fails (e.g. SA was just
    launched and its SDK listener is still binding, ~15-20 s on a cold start).
    """
    sa = _ensure_bridge()
    if not sa.is_connected():
        ok = sa.connect(host)
        if not ok:
            raise SAError(
                f"SDK Connect('{host}') failed. If SA was just launched its "
                f"SDK listener needs ~20 s to bind - simply retry the call. "
                f"If it keeps failing, restart SA (Utilities > SDK Settings).")
    return sa


# ---------------------------------------------------------------------------
# Background modal-dialog watchdog
#
# SA can pop a modal dialog mid-step (an "Object Not Found" error box, a nag)
# that blocks the MP step until a human clicks it away - every MCP call then
# looks hung. The watchdog (a daemon thread, pure sa_app/ctypes - no COM, so
# it is safe on any thread and does not disturb the COM worker) scans the SA
# GUI + SDK engine processes every interval_s seconds and closes their #32770
# dialogs. It is armed lazily by _ensure_bridge() on the first tool call that
# needs COM, so every SA-driving session is protected from step 1; a manual
# sa_dialog_watchdog "stop" disarms it for the rest of the session.
# ---------------------------------------------------------------------------
_WATCH_LOCK = threading.Lock()
_WATCH = {
    "thread": None,
    "stop": threading.Event(),
    "interval_s": float(os.environ.get("SA_MCP_WATCHDOG_INTERVAL", "0.5")),
    "title_contains": None,
    "auto": os.environ.get("SA_MCP_WATCHDOG", "1") != "0",
    "scans": 0,
    "closed_total": 0,
    "last_closed": [],
}


def _watchdog_status() -> dict:
    with _WATCH_LOCK:
        t = _WATCH["thread"]
        return {
            "running": bool(t and t.is_alive()),
            "interval_s": _WATCH["interval_s"],
            "title_contains": _WATCH["title_contains"] or "",
            "auto": _WATCH["auto"],
            "scans": _WATCH["scans"],
            "closed_total": _WATCH["closed_total"],
            "last_closed": list(_WATCH["last_closed"]),
        }


def _watchdog_loop() -> None:
    while True:
        if _WATCH["stop"].wait(_WATCH["interval_s"]):
            return
        try:
            res = sa_app.dismiss_sa_dialogs(
                title_contains=_WATCH["title_contains"] or None)
            with _WATCH_LOCK:
                _WATCH["scans"] += 1
                closed = res.get("closed") or []
                if closed:
                    _WATCH["closed_total"] += len(closed)
                    _WATCH["last_closed"] = closed
        except Exception:  # noqa: BLE001 - the watchdog must never die
            log.warning("dialog watchdog scan failed", exc_info=True)


def _watchdog_start(interval_s: float | None = None,
                    title_contains: str = "") -> dict:
    """Start (or reconfigure) the auto-closer. Returns a status dict."""
    with _WATCH_LOCK:
        if interval_s is not None and interval_s > 0:
            _WATCH["interval_s"] = float(interval_s)
        _WATCH["title_contains"] = title_contains or None
        _WATCH["auto"] = True
        t = _WATCH["thread"]
        if not (t and t.is_alive()):
            _WATCH["stop"].clear()
            _WATCH["thread"] = threading.Thread(
                target=_watchdog_loop, name="sa-dialog-watchdog",
                daemon=True)
            _WATCH["thread"].start()
    return _watchdog_status()


def _watchdog_stop() -> dict:
    with _WATCH_LOCK:
        _WATCH["auto"] = False
        t = _WATCH["thread"]
        if t and t.is_alive():
            _WATCH["stop"].set()
            t.join(timeout=2.0)
        _WATCH["thread"] = None
    return _watchdog_status()


def _ensure_watchdog() -> None:
    """Arm the auto-closer (idempotent). A manual 'stop' disarms it."""
    if _WATCH["auto"]:
        _watchdog_start()


mcp = FastMCP("spatial-analyzer")


# ---------------------------------------------------------------------------
# Tool: connection status / ping
# ---------------------------------------------------------------------------
@mcp.tool()
def sa_status() -> dict:
    """Report whether the MCP server is alive and connected to SA.

    Use this first to confirm the server started and the COM bridge works.
    Returns connection state and the SA host (if connected).
    """
    if sa is None:
        return {"server_alive": True, "bridge_ok": False,
                "connected": False,
                "error": "Bridge not created yet. Call sa_ensure_running() to "
                         "start SA (if needed) and connect."}
    return {
        "server_alive": True,
        "bridge_ok": True,
        "connected": sa.is_connected(),
        "host": sa.host,
    }


# ---------------------------------------------------------------------------
# Tool: connect to a running SA instance
# ---------------------------------------------------------------------------
@mcp.tool()
def sa_connect(host: str = "localhost") -> dict:
    """Connect the bridge to a running SpatialAnalyzer process.

    SpatialAnalyzer MUST be open and its SDK listener enabled (SA menu:
    Utilities > SDK Settings, or it listens by default). If SA is not running
    this returns an error instead of spawning the SDK engine - an engine born
    without a GUI listener pops a modal 10061 error and wedges; use
    sa_ensure_running() to start SA first.

    Args:
        host: Hostname or IP of the machine running SA. Use "localhost" if SA
              runs on this machine.

    Returns:
        {connected, host, error?}
    """
    try:
        _ensure_bridge()
    except Exception as exc:  # noqa: BLE001
        return {"connected": False, "host": host, "error": str(exc)}
    try:
        ok = sa.connect(host)
        return {"connected": ok, "host": host,
                "error": None if ok else "Connect() returned False"}
    except Exception as exc:  # noqa: BLE001
        sa.connected = False
        return {"connected": False, "host": host, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: is the SA GUI process running?
# ---------------------------------------------------------------------------
@mcp.tool()
def sa_is_running() -> dict:
    """Check whether the SpatialAnalyzer GUI process is running.

    Looks for 'Spatial Analyzer.exe' in the OS process list (no COM call, so
    it is safe and fast even if SA's SDK listener is wedged). Use this before
    sa_launch / sa_connect to decide what to do.

    Returns:
        {running, exe_path?}  (exe_path is the discovered GUI executable)
    """
    return {"running": sa_app.is_sa_running(),
            "exe_path": sa_app.find_sa_exe()}


# ---------------------------------------------------------------------------
# Tool: one-shot modal-dialog dismissal
# ---------------------------------------------------------------------------
@mcp.tool()
def sa_dismiss_dialogs(title_contains: str = "") -> dict:
    """Close SA modal dialogs that are blocking MCP steps, right now.

    SA pops a modal dialog mid-step (e.g. "Object Not Found" when an object
    name is wrong) that blocks the MP step until a human clicks it away -
    every MCP call then looks hung. This scans the SA GUI (and SDK engine)
    processes and closes their dialog windows: WM_CLOSE first (Cancel/X), then
    a button click on survivors (Cancel/No if present, else the single or OK
    button - a plain MB_OK error box has no close button, only OK ends it).
    It needs no COM, so it works even while a step is stuck; call it and then
    retry the step. The background auto-closer (sa_dialog_watchdog) does the
    same continuously once a bridge exists.

    Args:
        title_contains: Optional; only close dialogs whose title contains
                        this text (case-insensitive). Empty = all #32770
                        dialogs owned by SA.

    Returns:
        {closed: [{class_name, title, method}], still_open: [...],
         scanned_pids: [...], error?}
    """
    return sa_app.dismiss_sa_dialogs(title_contains=title_contains or None)


# ---------------------------------------------------------------------------
# Tool: background modal-dialog watchdog (auto start/stop/status)
# ---------------------------------------------------------------------------
@mcp.tool()
def sa_dialog_watchdog(action: str = "status",
                       interval_s: float = 0.5,
                       title_contains: str = "") -> dict:
    """Manage the background auto-closer of SA modal dialogs.

    The watchdog is armed automatically when the first COM tool call connects
    the bridge: every interval_s seconds it scans the SA GUI + SDK engine
    processes and closes blocking #32770 dialogs, so a dialog that pops
    mid-step stops hanging the session (the stuck COM call completes as soon
    as the dialog is closed). Pure sa_app/ctypes - it never touches COM, so it
    cannot wedge the bridge.

    Use 'stop' if you are operating SA's GUI by hand and do not want dialogs
    auto-closed (this disarms it for the rest of the session); 'start' re-arms
    it.

    Args:
        action: "status" (default) | "start" | "stop".
        interval_s: Poll interval when (re)starting (default 0.5 s).
        title_contains: Optional title filter (see sa_dismiss_dialogs).

    Returns:
        {action, running, interval_s, title_contains, auto, scans,
         closed_total, last_closed?}
    """
    action = (action or "status").lower()
    if action == "start":
        return {"action": action, **_watchdog_start(interval_s, title_contains)}
    if action == "stop":
        return {"action": action, **_watchdog_stop()}
    return {"action": "status", **_watchdog_status()}


# ---------------------------------------------------------------------------
# Tool: launch the SA GUI (optionally with a .sa file)
# ---------------------------------------------------------------------------
@mcp.tool()
def sa_launch(
    file_path: str = "",
    exe_path: str = "",
    timeout: float = 60.0,
    connect_host: str = "",
) -> dict:
    """Start the SpatialAnalyzer GUI, optionally opening a project file.

    If SA is already running, does nothing (reports already_running). After
    launch, optionally connects the SDK bridge to it.

    Args:
        file_path: Optional path to a .sa project file to open on launch.
        exe_path: Override the auto-discovered 'Spatial Analyzer.exe' path.
        timeout: Seconds to wait for the GUI process to appear.
        connect_host: If non-empty (e.g. 'localhost'), connect the bridge to
                      SA after launch and return the connection result.

    Returns:
        {launched, already_running, appeared, exe, file, pid?, connected?, error?}
    """
    res = sa_app.launch_sa(
        file_path=file_path or None,
        exe_path=exe_path or None,
        timeout=timeout,
    )
    if connect_host and res.get("appeared"):
        try:
            _ensure_sa(connect_host)
            res["connected"] = True
        except Exception as exc:  # noqa: BLE001
            res["connected"] = False
            res["error"] = (res.get("error") or "") + f" connect failed: {exc}"
    return res


# ---------------------------------------------------------------------------
# Tool: ensure SA is running + connected (the all-in-one bootstrap)
# ---------------------------------------------------------------------------
@mcp.tool()
def sa_ensure_running(
    file_path: str = "",
    host: str = "localhost",
    timeout: float = 60.0,
) -> dict:
    """Make sure SA is running AND the SDK bridge is connected to it.

    Order matters (see AGENTS.md): the SDK engine must NEVER be born before
    the SA GUI's SDK listener is up - an early engine pops a modal
    "NRK socketinterface 10061" error and then wedges. So: launch the GUI if
    needed, wait for the listener to bind (only on a cold start), create the
    bridge, then Connect() with fast retries.

    When SA is already running this returns in milliseconds.

    Args:
        file_path: Optional .sa file to open if SA must be launched.
        host: SA SDK host (default 'localhost').
        timeout: Seconds to wait for the GUI process to appear.

    Returns:
        {running, launched, connected, host, error?}
    """
    out: dict = {"running": False, "launched": False, "connected": False,
                 "host": host, "error": None}
    if sa_app.is_sa_running():
        out["running"] = True
    else:
        lr = sa_app.launch_sa(file_path=file_path or None, timeout=timeout)
        out["launched"] = lr.get("launched", False)
        if lr.get("dismissed_dialogs"):
            # Startup nags (e.g. "Maintenance and Support Subscription") that
            # SA parked behind were auto-closed so the launch could proceed.
            out["dismissed_dialogs"] = lr.get("dismissed_dialogs")
        if not lr.get("appeared"):
            out["error"] = lr.get("error") or "SA did not appear after launch"
            return out
        out["running"] = True
        # Cold start, engine-birth discipline (see AGENTS.md): the engine must
        # not be born until the GUI's SDK listener is up. First wait for a
        # VISIBLE WINDOW - an auto-launched SA can linger as a bare stuck
        # process (no window, no listener) and spawning the engine against it
        # pops the modal 10061 error. Then let the listener finish binding
        # (~20 s total after launch) before the engine is born. Once SA is
        # warm, connects are instant - no wait here.
        t_launch = time.time()
        if not sa_app.sa_wait_for_window(timeout=timeout, pid=lr.get("pid")):
            out["error"] = (
                "SA process started but showed no window within "
                f"{int(timeout)}s - it is stuck (bad previous shutdown / "
                "license prompt?). No engine was spawned (a birth here would "
                "pop a modal 10061 error). Close SA and retry, or start SA "
                "manually.")
            return out
        settle = 20.0 - (time.time() - t_launch)
        if settle > 0:
            time.sleep(settle)
    # Create the bridge (engine birth happens on the first _ensure_sa call,
    # i.e. AFTER the 20 s settle sleep on a cold start) and Connect with fast
    # retries until the deadline.
    deadline = time.time() + timeout
    last_err = None
    while not out["connected"] and time.time() < deadline:
        if not sa_app.is_sa_running():
            out["error"] = "SA exited during connect. Call sa_ensure_running() " \
                           "again to relaunch it."
            return out
        try:
            _ensure_sa(host)
            out["connected"] = True
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
        if not out["connected"]:
            time.sleep(1)
    if out["connected"]:
        out["error"] = None
    else:
        out["error"] = (f"{last_err} " if last_err else "") + \
            "Connect() did not succeed in time. Check SA menu: " \
            "Utilities > SDK Settings (SDK server enabled?)."
    return out


# ---------------------------------------------------------------------------
# Tool: open a project file in an already-running SA (via SDK step)
# ---------------------------------------------------------------------------
@mcp.tool()
def sa_open_file(file_path: str, import_mode: bool = False,
                 embedded: bool = False) -> dict:
    """Open a SpatialAnalyzer .xit exchange file in the connected SA project.

    SA's native project/exchange format is .xit (a .xit carries a whole job:
    collections, points, geometry, frames, instruments, even MP tasks - see
    the Samples folder). Two MP steps drive file loading (confirmed against
    the SA 2015 "MP Command Reference"):

      * "Open SA File"  - DISCARDS the current job and opens the .xit. Arg
        "SA File Name" (File Path or Embedded File Name).
      * "Import SA File" - imports the .xit INTO the current job (merging
        collections, renaming on collision). Args "SA File Name" plus a
        Boolean "Allow Operator Selections" (True pops a picker; False
        imports everything silently).

    Args:
        file_path: Absolute path to the .xit file (or embedded-file name if
                   `embedded`).
        import_mode: False = replace the current job ("Open SA File", the
                     default); True = merge into the current job
                     ("Import SA File").
        embedded: True when file_path is an embedded-file name, not a disk
                  path.

    Returns:
        {opened, step, status_code, status, messages, error?}
    """
    try:
        _ensure_sa()
    except Exception as exc:  # noqa: BLE001
        return {"opened": False, "error": str(exc)}
    arg_name = "SA File Name"

    def set_args(step):
        sa.set_file_path_arg(arg_name, file_path, embedded)
        if step == "Import SA File":
            sa.set_bool_arg("Allow Operator Selections", False)

    return _try_steps(
        candidates=("Import SA File", "Open SA File") if import_mode
                 else ("Open SA File",),
        set_args=set_args,
        read_outputs=lambda: {},
        result_key="opened",
    )


# ---------------------------------------------------------------------------
# Tool: generic step (the powerful escape hatch)
# ---------------------------------------------------------------------------
@mcp.tool()
def sa_run_step(
    step_name: str,
    string_args: dict | None = None,
    double_args: dict | None = None,
    int_args: dict | None = None,
    bool_args: dict | None = None,
    vector_args: dict | None = None,
    output_args: list[str] | None = None,
) -> dict:
    """Execute an arbitrary SA "Measurement Plan step" by name.

    Every SA operation (Construct Point, Construct Sphere, Make Transform,
    reports, exports, ...) is a named step with typed arguments. This generic
    tool drives ANY step, so the model can explore SA's full capability set
    without a dedicated tool per step.

    Pattern (matches the SA SDK):
      1. SetStep(step_name)
      2. set each input argument by name + type
      3. ExecuteStep()
      4. read requested output arguments back

    Args:
        step_name: Exact SA step name, e.g. "Construct a Point in Working
                   Coordinates" (case/space sensitive - see SA's MP tree).
        string_args: {arg_name: value} string inputs.
        double_args: {arg_name: value} numeric inputs.
        int_args: {arg_name: value} integer inputs.
        bool_args: {arg_name: value} boolean inputs.
        vector_args: {arg_name: [x, y, z]} 3D vector inputs.
        output_args: list of argument names to read back after execution.
                     Their type is auto-detected from the SA step definition.

    Returns:
        {executed, status_code, status, messages, outputs, error?}
    """
    try:
        _ensure_sa()
    except Exception as exc:  # noqa: BLE001
        return {"executed": False, "error": str(exc)}

    result = {
        "step": step_name,
        "executed": False,
        "status_code": None,
        "status": None,
        "messages": [],
        "outputs": {},
        "error": None,
    }
    try:
        sa.set_step(step_name)
        for k, v in (string_args or {}).items():
            sa.set_string_arg(k, v)
        for k, v in (double_args or {}).items():
            sa.set_double_arg(k, v)
        for k, v in (int_args or {}).items():
            sa.set_integer_arg(k, v)
        for k, v in (bool_args or {}).items():
            sa.set_bool_arg(k, v)
        for k, v in (vector_args or {}).items():
            sa.set_vector_arg(k, v[0], v[1], v[2])

        ok = sa.execute_step()
        result["executed"] = ok
        try:
            code = sa.get_step_result()
            result["status_code"] = code
            result["status"] = MP_STATUS.get(code, f"Unknown({code})")
        except Exception as exc:  # noqa: BLE001
            result["status"] = f"result-unavailable: {exc}"

        # Best-effort read-back of requested outputs, trying each getter type.
        for name in output_args or []:
            result["outputs"][name] = _read_output(name)

    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result



def _read_output(name: str):
    """Try reading an output arg, attempting every getter type.

    SA output arguments can be string/double/int/bool/vector/point. We probe
    each and return the first that yields a value without error. Returns a dict
    like {"type": ..., "value": ...} on success, or {"error": ...}.
    """
    probes = [
        ("string", lambda: sa.get_string_arg(name)),
        ("double", lambda: sa.get_double_arg(name)),
        ("integer", lambda: sa.get_integer_arg(name)),
        ("bool", lambda: sa.get_bool_arg(name)),
        ("vector", lambda: sa.get_vector_arg(name)),
    ]
    for kind, getter in probes:
        try:
            val = getter()
            if val is None:
                continue
            return {"type": kind, "value": val}
        except Exception:  # noqa: BLE001 - wrong type for this arg, try next
            continue
    return {"error": f"could not read output arg '{name}'"}


# ---------------------------------------------------------------------------
# Helpers for step-name discovery.
#
# SA step names and their argument names are not in a type library; they live
# in SA's Measurement Plan tree and vary slightly between versions. Each tool
# below declares CANDIDATE names (in priority order) and the helper runs them
# until one returns DoneSuccess. The registries are the single place to fix a
# name once confirmed against a live SA instance.
# ---------------------------------------------------------------------------

# Object Type enum values accepted by the "Make a Collection Object Name Ref
# List - By Type" step. Confirmed live against SA 2015 (all return code 2 /
# DoneSuccess). "Any" returns every type; the rest filter to one type. The
# value is routed through IDispatch::Invoke by sa.set_object_type_arg (the
# no-type-lib enum gotcha), so these steps run NON-interactively.
SA_OBJECT_TYPES = (
    "Point Group", "Vector Group", "Frame", "Circle", "Cylinder",
    "Plane", "Sphere", "Cone", "Line", "Perimeter", "B-Spline",
    "Surface", "Scale Bar",
)


def _list_collections():
    """Top-level collection names: 'Get Number of Collections' + loop
    'Get i-th Collection Name'. (SA 2015 has no 'Get the Names of all ...'.)"""
    sa.set_step("Get Number of Collections")
    sa.execute_step()
    if sa.get_step_result() != 2:
        return []
    count = sa.get_integer_arg("Total Count")
    names = []
    for i in range(int(count)):
        sa.set_step("Get i-th Collection Name")
        sa.set_integer_arg("Collection Index", i)
        sa.execute_step()
        if sa.get_step_result() == 2:
            names.append(sa.get_string_arg("Resultant Name"))
    return names


def _objects_in_collection_by_type(collection, object_type):
    """Full hierarchical names of one object type in a collection.

    The step output is a flat list, ONE element per object (e.g. the 6 point
    groups of collection A come back as 6 names "A::<group>"). Values are
    strings, not {collection, object} dicts.
    """
    res = _try_steps(
        candidates=("Make a Collection Object Name Ref List - By Type",),
        set_args=lambda step: (
            sa.set_string_arg("Collection", collection),
            sa.set_object_type_arg("Object Type", object_type),
        ),
        read_outputs=lambda: {
            "objects": sa.get_collection_object_name_ref_list_arg(
                "Resultant Collection Object Name List")},
        result_key="built",
    )
    return (res.get("outputs") or {}).get("objects", []) if res.get("built") else []


def _points_in_group(group_full_name):
    """Point names of a point group (ONE element per point).

    Verified live: the step's "Group Name" arg wants the name WITHOUT its
    collection prefix - passing "A::т контур" returns code 3 (fails), passing
    "т контур" works. The returned names are relative ("::т контур::1",
    leading empty collection segment): we drop the leading "::".
    """
    bare = group_full_name.split("::", 1)[-1]
    res = _try_steps(
        candidates=("Make a Point Name Ref List From a Group",),
        set_args=lambda step: sa.set_object_name_arg("Group Name", bare),
        read_outputs=lambda: {
            "points": [p[2:] if p.startswith("::") else p
                       for p in sa.get_point_name_ref_list_arg(
                           "Resultant Point Name List")]},
        result_key="built",
    )
    return (res.get("outputs") or {}).get("points", []) if res.get("built") else []


def _safe_messages() -> list:
    try:
        raw = sa.get_step_messages()
    except Exception:  # noqa: BLE001
        return []
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(m) for m in raw]
    return [str(raw)]


def _try_steps(candidates, set_args, read_outputs, result_key="executed"):
    """Try candidate step names in order; first DoneSuccess wins.

    Returns a report including every attempt (for diagnostics) and the winning
    step's outputs. `set_args(step)` sets the inputs after SetStep;
    `read_outputs()` returns the parsed outputs dict.
    """
    report = {result_key: False, "step": None, "status": None,
              "messages": [], "outputs": {}, "error": None, "tried": []}
    last_err = None
    for step in candidates:
        attempt = {"step": step, "ok": False}
        try:
            sa.set_step(step)
            set_args(step)
            sa.execute_step()
            code = sa.get_step_result()
            attempt["status_code"] = code
            attempt["status"] = MP_STATUS.get(code, f"Unknown({code})")
            attempt["messages"] = _safe_messages()
            outs = read_outputs()
            attempt["outputs"] = outs
            attempt["ok"] = (code == 2)
            report["tried"].append(attempt)
            if code == 2:  # DoneSuccess
                report[result_key] = True
                report["step"] = step
                report["status"] = attempt["status"]
                report["outputs"] = outs
                report["messages"] = attempt["messages"]
                return report
        except Exception as exc:  # noqa: BLE001
            attempt["error"] = str(exc)
            last_err = exc
            report["tried"].append(attempt)
    report["error"] = (
        f"No candidate step succeeded (tried {list(candidates)}). "
        f"Last error: {last_err}. Confirm the step/arg names in SA's MP tree."
    )
    return report


def _set_query_input(arg_name, value):
    """Set a query input arg, tolerating string-vs-collection-name variants."""
    for setter in (sa.set_collection_name_arg, sa.set_string_arg):
        try:
            setter(arg_name, value)
            return
        except Exception:  # noqa: BLE001
            continue



# ---------------------------------------------------------------------------
# Tool: inspect the SA project tree (collections + all object types)
# ---------------------------------------------------------------------------
@mcp.tool()
def sa_inspect_project(collection: str = "",
                       object_types: list[str] | None = None,
                       include_points: bool = False) -> dict:
    """Enumerate the SA project tree using the SA 2015 step model.

    IMPORTANT: SA 2015 has NO "Get the Names of all ..." steps (those names in
    older docs do not exist in this build). Enumeration uses a two-step model
    confirmed live against SA 2015 ("MP Command Reference"):

      * collections (collection == ""): "Get Number of Collections"
        (out "Total Count") + loop "Get i-th Collection Name" (in
        "Collection Index", out "Resultant Name").
      * objects grouped by type (collection set): "Make a Collection Object
        Name Ref List - By Type" (in "Collection" + "Object Type" enum; out
        "Resultant Collection Object Name List"). The Object Type enum is
        routed through IDispatch::Invoke (sa_sdk.set_object_type_arg) - without
        that it mangles the enum and SA pops an interactive picker that blocks
        ExecuteStep forever. Each output element is ONE object, returned as its
        full hierarchical name ("A::т контур"). NOTE: SA may list the SAME full
        name under several types (in this project the point group "т контур"
        also comes back as a Cylinder - SA reuses the group name for the
        fitted geometry), so names are grouped per type as SA reports them.
      * points in a point group (include_points): "Make a Point Name Ref List
        From a Group" (in "Group Name", out "Resultant Point Name List").
        Verified live: the step wants the group name WITHOUT its collection
        prefix ("т контур", not "A::т контур"), and returns one element per
        point ("::т контур::1" - relative, empty collection segment).

    Args:
        collection: If empty, list only the top-level collections. Otherwise
                    enumerate the objects of this collection, grouped by type.
        object_types: Which SA Object Type values to enumerate for the
                      collection. Defaults to the most useful ones (Point Group,
                      Vector Group, Frame and the geometry primitives).
        include_points: If True (and collection is set), also list the points
                        in each Point Group. Can be slow for large groups.

    Returns:
        With no collection: {collections: [...], errors: [...]}.
        With a collection:  {collection, types: {ObjectType: [full name, ...]},
                            points_by_group?, errors: [...]}.
    """
    try:
        _ensure_sa()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    types = object_types or ["Point Group", "Vector Group", "Frame",
                             "Circle", "Cylinder", "Plane", "Sphere",
                             "Cone", "Line"]
    errors: list[str] = []

    if not collection:
        try:
            collections = _list_collections()
        except Exception as exc:  # noqa: BLE001
            collections = []
            errors.append(f"collections: {exc}")
        return {"collections": collections, "errors": errors}

    report: dict = {"collection": collection, "types": {}, "errors": errors}
    for ot in types:
        try:
            objs = _objects_in_collection_by_type(collection, ot)
            if objs:
                report["types"][ot] = objs
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{ot}: {exc}")

    if include_points:
        groups = report["types"].get("Point Group", [])
        points_by_group: dict = {}
        for g in groups:
            name = str(g)
            try:
                points_by_group[name] = _points_in_group(name)
            except Exception as exc:  # noqa: BLE001
                points_by_group[name] = {"error": str(exc)}
        report["points_by_group"] = points_by_group

    return report


# ---------------------------------------------------------------------------
# Tools: graphics-window visibility.
#
# SA 2015 (MP Command Reference ch. 4 "View Control") exposes show/hide as
# four one-shot steps. All of them take the SAME joined hierarchical full
# names that the ref-list getters emit, so a client can feed
# sa_inspect_project output straight back in:
#   - "Show Objects"   / "Hide Objects"        -> Collection Object Name Ref
#     List, one "C::O" name per object (e.g. "A::т контур").
#   - "Show/Hide Points"                       -> Point Name Ref List, one
#     "C::G::T" name per point ("A::т контур::1").
#   - "Show/Hide by Object Type"               -> a whole type at once,
#     scoped to one collection (arg "Specific Collection") or all
#     collections.
# SUCCESS = code 2; PARTIAL SUCCESS (some names not found) comes back as
# DoneMinorError code 4, the same pairing 'Delete Objects' uses.
# ---------------------------------------------------------------------------


def _object_full_name(name, collection):
    """Normalize one object name to the ref-list "C::O" full form.

    Accepts full names exactly as sa_inspect_project returns ("A::т контур").
    A simple name is resolved inside `collection`; with no collection it gets
    the canonical leading "::" (empty collection part = current collection),
    matching the layout SA's ref lists use.
    """
    s = str(name)
    if "::" in s:
        return s
    return "::".join([collection or "", s])


def _point_full_name(name, group, collection):
    """Normalize one point name to the ref-list "C::G::T" full form.

    SA Point Name Ref Lists carry ONE joined hierarchical name per element.
    Accepts a full name ("A::т контур::1", "::т контур::1"), the
    group-relative shape sa_inspect_project's include_points returns
    ("т контур::1" - split at the FIRST "::"), or a bare target ("1")
    resolved against (collection, group). A 2-segment name must be the
    relative group::target form, never a bare collection::object pair.
    """
    s = str(name)
    if s.startswith("::") or s.count("::") >= 2:
        return s
    if "::" in s:
        g, target = s.split("::", 1)
        return "::".join([collection or "", g, target])
    g = str(group or "")
    if "::" in g:
        # Full "C::G" group name: the group segment must be bare in the joined
        # form, and its collection fills in when none was given explicitly.
        c0, g = g.split("::", 1)
        if not collection:
            collection = c0
    return "::".join([collection or "", g, s])


def _run_objects_visibility(full_names, show):
    """Execute 'Show Objects' / 'Hide Objects' over full object names."""
    step = "Show Objects" if show else "Hide Objects"
    res = {"shown" if show else "hidden": False, "step": step,
           "objects": list(full_names), "status_code": None, "status": None,
           "messages": [], "error": None}
    try:
        _ensure_sa()
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)
        return res
    try:
        sa.set_step(step)
        sa.set_collection_object_name_ref_list_arg(
            "Objects to Show" if show else "Objects to Hide", full_names)
        sa.execute_step()
        code = sa.get_step_result()
        res["status_code"] = code
        res["status"] = MP_STATUS.get(code, f"Unknown({code})")
        res["messages"] = _safe_messages()
        # code 4 = PARTIAL SUCCESS: at least one object was not found.
        res["shown" if show else "hidden"] = code in (2, 4)
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)
    return res


def _run_points_visibility(full_names, show):
    """Execute 'Show/Hide Points' over full "C::G::T" point names."""
    step = "Show/Hide Points"
    res = {"shown" if show else "hidden": False, "step": step,
           "points": list(full_names), "status_code": None, "status": None,
           "messages": [], "error": None}
    try:
        _ensure_sa()
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)
        return res
    try:
        sa.set_step(step)
        sa.set_point_name_ref_list_arg("Point Names", full_names)
        sa.set_bool_arg("Show? (Hide = FALSE)", bool(show))
        sa.execute_step()
        code = sa.get_step_result()
        res["status_code"] = code
        res["status"] = MP_STATUS.get(code, f"Unknown({code})")
        res["messages"] = _safe_messages()
        res["shown" if show else "hidden"] = code in (2, 4)
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)
    return res


@mcp.tool()
def sa_show_objects(objects: list[str], collection: str = "") -> dict:
    """Show objects in SA's graphics window ('Show Objects').

    Un-hides collection objects (point groups, frames, fitted geometry, ...)
    that were previously hidden. Each entry may be a full hierarchical name as
    returned by sa_inspect_project ("A::т контур"), or a simple name resolved
    inside `collection` (pass the collection when the job has several).

    Args:
        objects: Object names to show.
        collection: Collection the simple names belong to ("" = the names are
                   already full, or belong to the current collection).

    Returns:
        {shown, step, objects (normalized full names), status_code, status,
         messages, error?}
    """
    full = [_object_full_name(o, collection) for o in (objects or [])]
    return _run_objects_visibility(full, True)


@mcp.tool()
def sa_hide_objects(objects: list[str], collection: str = "") -> dict:
    """Hide objects in SA's graphics window ('Hide Objects').

    Hides collection objects (point groups, frames, fitted geometry, ...) so
    they no longer appear in the graphical view. Each entry may be a full
    hierarchical name as returned by sa_inspect_project ("A::т контур"), or a
    simple name resolved inside `collection`.

    Args:
        objects: Object names to hide.
        collection: Collection the simple names belong to ("" = the names are
                   already full, or belong to the current collection).

    Returns:
        {hidden, step, objects (normalized full names), status_code, status,
         messages, error?}
    """
    full = [_object_full_name(o, collection) for o in (objects or [])]
    return _run_objects_visibility(full, False)


@mcp.tool()
def sa_show_points(points: list[str], group: str = "",
                   collection: str = "") -> dict:
    """Show points in SA's graphics window ('Show/Hide Points').

    Un-hides individual points of a point group (the tree shows each point
    separately; hiding a group hides its points, and showing any point of a
    hidden group shows the group again). Each entry may be a full
    "C::G::T" name ("A::т контур::1"), the group-relative shape
    sa_inspect_project's include_points returns ("т контур::1"), or a bare
    target ("1") resolved against (collection, group).

    Args:
        points: Point names to show.
        group: Point group for bare target names (full or bare name).
        collection: Collection for bare/relative names ("" = current
                    collection).

    Returns:
        {shown, step, points (normalized full names), status_code, status,
         messages, error?}
    """
    full = [_point_full_name(p, group, collection) for p in (points or [])]
    return _run_points_visibility(full, True)


@mcp.tool()
def sa_hide_points(points: list[str], group: str = "",
                   collection: str = "") -> dict:
    """Hide points in SA's graphics window ('Show/Hide Points').

    Hides individual points of a point group so they no longer appear in the
    graphical view. Each entry may be a full "C::G::T" name
    ("A::т контур::1"), the group-relative shape sa_inspect_project's
    include_points returns ("т контур::1"), or a bare target ("1") resolved
    against (collection, group).

    Args:
        points: Point names to hide.
        group: Point group for bare target names (full or bare name).
        collection: Collection for bare/relative names ("" = current
                    collection).

    Returns:
        {hidden, step, points (normalized full names), status_code, status,
         messages, error?}
    """
    full = [_point_full_name(p, group, collection) for p in (points or [])]
    return _run_points_visibility(full, False)


@mcp.tool()
def sa_show_hide_by_type(object_type: str, visible: bool,
                         collection: str = "",
                         all_collections: bool = False) -> dict:
    """Show or hide every object of one type ('Show/Hide by Object Type').

    Bulk show/hide for an object type across a collection or the whole job,
    e.g. hide all "Point Group" entries to declutter the view before a fit.
    The Object Type enum is routed through IDispatch::Invoke, so the step runs
    non-interactively.

    Args:
        object_type: One of the SA object types ("Point Group", "Vector
                     Group", "Frame", "Circle", "Cylinder", "Plane", "Sphere",
                     "Cone", "Line", ... - "Any" matches every type).
        visible: True = show, False = hide.
        collection: Collection to scope to ("" = current collection). Ignored
                   when all_collections is True.
        all_collections: True = apply in every collection.

    Returns:
        {applied, object_type, visible, collection, all_collections, step,
         status_code, status, messages, error?}
    """
    res = {"applied": False, "object_type": object_type,
           "visible": bool(visible), "collection": collection,
           "all_collections": bool(all_collections),
           "step": "Show/Hide by Object Type", "status_code": None,
           "status": None, "messages": [], "error": None}
    try:
        _ensure_sa()
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)
        return res
    try:
        sa.set_step("Show/Hide by Object Type")
        sa.set_bool_arg("All Collections?", bool(all_collections))
        # The scoping arg is "Specific Collection" (Collection Name type) on
        # SA 2015 - the PDF table's "Collection Name" is the arg's TYPE, not
        # its name; setting an arg literally named "Collection Name" makes
        # ExecuteStep return SdkError. Confirmed live. When no scope is given
        # the step runs against the current/active collection.
        if not all_collections and collection:
            sa.set_collection_name_arg("Specific Collection", collection)
        sa.set_object_type_arg("Object Type To Show/Hide", object_type)
        sa.set_bool_arg("Hide? (Show = FALSE)", not bool(visible))
        sa.execute_step()
        code = sa.get_step_result()
        res["status_code"] = code
        res["status"] = MP_STATUS.get(code, f"Unknown({code})")
        res["messages"] = _safe_messages()
        res["applied"] = code == 2
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)
    return res


# ---------------------------------------------------------------------------
# Tool: a concrete convenience example (proves the full pipeline)
# ---------------------------------------------------------------------------
@mcp.tool()
def sa_construct_point(group: str, name: str, x: float, y: float,
                       z: float) -> dict:
    """Construct a point at working coordinates (x, y, z).

    Convenience wrapper around the "Construct a Point in Working Coordinates"
    SA step. Useful as a quick end-to-end test of the pipeline.

    Args:
        group: Point group name (created if missing).
        name: Point target name.
        x, y, z: Coordinates in the current working frame.

    Returns:
        {executed, status, group, name, error?}
    """
    try:
        _ensure_sa()
    except Exception as exc:  # noqa: BLE001
        return {"executed": False, "error": str(exc)}
    # The Point Name argument needs a dedicated setter (collection, group,
    # target) that the generic sa_run_step does not cover, so drive the step
    # directly. (NB: the point name MUST be set before ExecuteStep, otherwise
    # SA may pop a modal prompt and block the call.)
    try:
        sa.set_step("Construct a Point in Working Coordinates")
        sa.set_vector_arg("Working Coordinates", x, y, z)
        sa.set_point_name_arg("Point Name", "", group, name)
        ok = sa.execute_step()
        code = sa.get_step_result()
        return {
            "executed": ok,
            "status": MP_STATUS.get(code, f"Unknown({code})"),
            "group": group,
            "name": name,
            "error": None if ok else "ExecuteStep returned False",
        }
    except Exception as exc:  # noqa: BLE001
        return {"executed": False, "error": str(exc), "group": group,
                "name": name}


# ---------------------------------------------------------------------------
# Best-fit geometry construction from points.
#
# CONFIRMED live against SA 2015 ("MP Command Reference", ch. 6 Analysis
# Operations, p. 414): this build has NO "Construct a Best Fit <Shape>" steps.
# Fitting any primitive (line, plane, circle, sphere, cylinder, cone, ...) is
# a single MP step, "Fit Geometry to Point Group", driven by the "Geometry
# Type" enum arg. Geometry Type is an enum arg: it MUST be set via
# IDispatch::Invoke (sa_sdk.set_geometry_type_arg), exactly like the Object
# Type enum - dynamic dispatch mangles the value. Inputs: Geometry Type, Group
# to Fit, Resulting Object Name, Fit Profile Name (blank = default profile),
# Report Deviations (False = no modal results dialog), Fit Interface Tolerance
# (-1.0 use profile / 0.0 = no tolerance), Ignore Out of Tolerance Points,
# Starting Condition Geometry (optional). No return arguments - the geometry
# appears under Resulting Object Name.
# ---------------------------------------------------------------------------

# geometry_type (lowercase) -> "Geometry Type" enum value string.
BESTFIT_TYPES = {
    "plane": "Plane", "sphere": "Sphere", "cylinder": "Cylinder",
    "cone": "Cone", "circle": "Circle", "line": "Line",
}

FIT_GEOMETRY_STEP = "Fit Geometry to Point Group"

# The tolerance arg's MP label carries its usage hint in the name (SDK arg
# labels include such parentheticals - cf. "Resultant Point Name List(A+B)"),
# so probe the exact name; the setter returns False for an unknown arg.
_TOLERANCE_ARG_NAMES = (
    "Fit Interface Tolerance (-1.0 use profile)",
    "Fit Interface Tolerance",
)


def _create_point_group(collection, group, coordinates, name_prefix="P"):
    """Build a point group from raw [x,y,z] coordinates.

    Uses the 'Construct a Point in Working Coordinates' step per point. Returns
    {ok, group, count, error}. Used when best-fit input is a coord list rather
    than an existing point group.
    """
    res = {"ok": False, "group": group, "count": 0, "error": None}
    try:
        for i, xyz in enumerate(coordinates):
            sa.set_step("Construct a Point in Working Coordinates")
            sa.set_vector_arg("Working Coordinates", float(xyz[0]),
                              float(xyz[1]), float(xyz[2]))
            sa.set_point_name_arg("Point Name", collection, group,
                                  f"{name_prefix}{i + 1}")
            sa.execute_step()
            res["count"] += 1
        res["ok"] = True
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)
    return res


def _resolve_point_source(point_group, collection, coordinates):
    """Return (collection, group, build_info) for best-fit input.

    If coordinates are given, build a temporary point group; else use the named
    point group as-is.
    """
    build_info = None
    if coordinates:
        group = point_group or "MCP_BF_Input"
        build_info = _create_point_group(collection, group, coordinates)
        return collection, group, build_info
    return collection, point_group, build_info


def _fit_geometry_report(geometry_type, object_name, collection, group,
                         build_info, fit_tolerance_mm=None,
                         ignore_out_of_tolerance=False):
    """Run 'Fit Geometry to Point Group' and return the tool response dict.

    fit_tolerance_mm: 'Fit Interface Tolerance' (0.0 = no limit). When > 0 AND
    ignore_out_of_tolerance=True, SA excludes points farther than the
    tolerance from the fit (points stay in the group) - the GUI best-fit
    behaviour. SA then returns DoneMinorError (code 4) as long as points fall
    outside the tolerance, even though the geometry IS created from the
    in-tolerance points (confirmed live on SA 2015) - so an exclusion pass is
    treated as constructed on code 4 too.
    """
    report = {
        "constructed": False,
        "geometry_type": geometry_type,
        "object_name": object_name,
        "step": FIT_GEOMETRY_STEP,
        "status": None,
        "parameters": {},
        "point_source": {"collection": collection, "group": group,
                         "from_coordinates": bool(build_info)},
        "build": build_info,
        "messages": [],
        "error": None,
        "tried": [],
        "fit_tolerance_mm": fit_tolerance_mm,
        "ignore_out_of_tolerance": ignore_out_of_tolerance,
    }
    try:
        sa.set_step(FIT_GEOMETRY_STEP)
        if not sa.set_geometry_type_arg("Geometry Type",
                                        BESTFIT_TYPES[geometry_type]):
            report["error"] = ("SetGeometryTypeArg('Geometry Type', "
                               f"{BESTFIT_TYPES[geometry_type]!r}) returned "
                               "False - wrong enum value or arg name?")
            return report
        if not sa.set_collection_object_name_arg("Group to Fit", collection,
                                                 group):
            report["error"] = "Could not set 'Group to Fit'."
            return report
        if not sa.set_collection_object_name_arg("Resulting Object Name",
                                                 collection, object_name):
            report["error"] = "Could not set 'Resulting Object Name'."
            return report
        sa.set_string_arg("Fit Profile Name", "")
        sa.set_bool_arg("Report Deviations", False)  # never pop the dialog
        tol = float(fit_tolerance_mm) if fit_tolerance_mm is not None else 0.0
        for arg in _TOLERANCE_ARG_NAMES:
            if sa.set_double_arg(arg, tol):  # 0.0 = no tolerance limit
                break
        if ignore_out_of_tolerance:
            sa.set_bool_arg("Ignore Out of Tolerance Points", True)
        sa.execute_step()
        code = sa.get_step_result()
        report["status_code"] = code
        report["status"] = MP_STATUS.get(code, f"Unknown({code})")
        report["messages"] = _safe_messages()
        report["tried"].append({"step": FIT_GEOMETRY_STEP,
                                "status_code": code,
                                "status": report["status"]})
        if code == 2:
            report["constructed"] = True
        elif code == 4 and ignore_out_of_tolerance:
            # DoneMinorError: SA built the geometry from the in-tolerance
            # points but some points fell outside the tolerance. Expected on
            # every sa_fit_clean exclusion pass until the run converges.
            report["constructed"] = True
            report["minor_error"] = True
        else:
            report["error"] = ("'Fit Geometry to Point Group' returned "
                               f"{report['status']} (code {code}). The "
                               "geometry was NOT created.")
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
    return report


@mcp.tool()
def sa_best_fit(
    geometry_type: str,
    object_name: str,
    point_group: str = "",
    collection: str = "",
    coordinates: list | None = None,
) -> dict:
    """Construct a best-fit geometric primitive from points.

    Point source (one of):
      - point_group: name of an existing point group (in `collection`), or
      - coordinates: list of [x, y, z] triples; a temporary point group is
        built first, then the fit is computed from it.

    geometry_type must be one of: plane, sphere, cylinder, cone, circle, line.

    Confirmed against live SA 2015: the fit runs through the single MP step
    "Fit Geometry to Point Group" (the older "Construct a Best Fit <Shape>"
    step names do not exist in this build). Status DoneSuccess means the
    object was created under object_name.

    Args:
        geometry_type: One of plane|sphere|cylinder|cone|circle|line.
        object_name: Name for the constructed geometry object.
        point_group: Existing point group to fit to (mutually exclusive with
                     coordinates, but may be used as the temp group name).
        collection: Collection holding the point group / output object.
        coordinates: Optional list of [x, y, z] point coordinates.

    Returns:
        {constructed, geometry_type, object_name, step, status, parameters,
         point_source, build?, messages, error?}
    """
    try:
        _ensure_sa()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    gtype = geometry_type.lower()
    if gtype not in BESTFIT_TYPES:
        return {"constructed": False, "error": (
            f"Unknown geometry_type '{geometry_type}'. Choose one of: "
            f"{sorted(BESTFIT_TYPES)}")}

    collection, group, build_info = _resolve_point_source(
        point_group, collection, coordinates)
    if coordinates and not (build_info and build_info["ok"]):
        return {"constructed": False, "geometry_type": geometry_type,
                "object_name": object_name, "build": build_info,
                "error": "Failed to build input point group from coordinates."}
    if not group and not coordinates:
        return {"constructed": False, "error": (
            "Provide either 'point_group' (an existing group) or 'coordinates'.")}

    report = _fit_geometry_report(gtype, object_name, collection, group,
                                  build_info)
    report["geometry_type"] = geometry_type
    return report


def _best_fit_wrapper(gtype):
    """Build a typed sa_best_fit_<gtype> tool bound to one geometry_type."""
    @mcp.tool(name=f"sa_best_fit_{gtype}")
    def _tool(object_name: str, point_group: str = "",
              collection: str = "",
              coordinates: list | None = None) -> dict:
        return sa_best_fit(gtype, object_name, point_group, collection,
                           coordinates)
    _tool.__doc__ = (
        f"Construct a best-fit {gtype} from points. Thin wrapper over "
        f"sa_best_fit(geometry_type='{gtype}'). See sa_best_fit for details.")
    return _tool


for _g in ("plane", "sphere", "cylinder", "cone", "circle", "line"):
    _best_fit_wrapper(_g)


# ---------------------------------------------------------------------------
# Fit results: geometry parameters + per-point deviations ("quality").
#
# "Fit Geometry to Point Group" returns no numbers (Return Arguments: None), so
# a finished geometry is evaluated with plain MP steps afterwards:
#   * parameters  - "Get <Type> Properties" (ch. 6 Analysis) reads the
#     primitive definition: cylinder begin/end/axis/radius/diameter/length,
#     sphere center/radius, circle center/normal/radius, plane
#     normal/point/D, line begin/end/length, cone apex/axis/included angle.
#   * deviations  - group points are read back one by one ("Get Point
#     Coordinate", active coordinate frame) and the signed deviation to the
#     fitted primitive is computed here. This yields RMS ("СКО"), mean, sigma,
#     min/max and an outlier list without touching the job, and lets us refit
#     excluding outliers through the fit step's own "Fit Interface Tolerance"
#     + "Ignore Out of Tolerance Points" arguments (what the SA GUI does).
# All lengths are in the job's distance unit (mm for the test file).
# ---------------------------------------------------------------------------

_GEOMETRY_PROP_SPECS = {
    "plane": {
        "step": "Get Plane Properties", "name_arg": "Plane Name",
        "vectors": [("normal", ["Normal Direction", "Normal"]),
                    ("point", ["Point on Plane", "Point"])],
        "doubles": [("d", ["D Parameter", "D"])],
    },
    "line": {
        "step": "Get Line Properties", "name_arg": "Line Name",
        "vectors": [("begin", ["Begin Coordinate"]),
                    ("end", ["End Coordinate"]),
                    ("delta", ["Delta Components", "Delta"])],
        "doubles": [("length", ["Length"])],
    },
    "sphere": {
        "step": "Get Sphere Properties", "name_arg": "Sphere Name",
        "vectors": [("center", ["Center Coordinate", "Center"])],
        "doubles": [("radius", ["Radius"]), ("diameter", ["Diameter"])],
    },
    "circle": {
        "step": "Get Circle Properties", "name_arg": "Circle Name",
        "vectors": [("center", ["Center Coordinate"]),
                    ("normal", ["Normal Direction", "Normal"])],
        "doubles": [("radius", ["Radius"]), ("diameter", ["Diameter"])],
    },
    "cylinder": {
        "step": "Get Cylinder Properties", "name_arg": "Cylinder Name",
        "vectors": [("begin", ["Begin Coordinate", "Begin"]),
                    ("end", ["End Coordinate", "End"]),
                    ("axis", ["Axis Direction", "Axis"])],
        "doubles": [("length", ["Length"]), ("radius", ["Radius"]),
                    ("diameter", ["Diameter"])],
    },
    "cone": {
        "step": "Get Cone Properties", "name_arg": "Cone Name",
        "vectors": [("apex", ["Cone End Point (in working coordinates)",
                              "Cone End Point", "End Point", "Apex"]),
                    ("axis", ["Cone Axis (in working coordinates)",
                              "Cone Axis", "Axis Direction", "Axis"])],
        "doubles": [("length", ["Cone Length", "Length"]),
                    ("included_angle", ["Cone Included Angle",
                                        "Included Angle"])],
    },
}


def _read_geometry_props(geometry_type, collection, object_name):
    """Read the built primitive's parameters via 'Get <Type> Properties'."""
    spec = _GEOMETRY_PROP_SPECS[geometry_type]
    res = {"step": spec["step"], "status": None, "status_code": None,
           "properties": {}, "error": None}
    try:
        sa.set_step(spec["step"])
        if not sa.set_collection_object_name_arg(spec["name_arg"],
                                                 collection, object_name):
            res["error"] = f"Could not set '{spec['name_arg']}'."
            return res
        sa.execute_step()
        code = sa.get_step_result()
        res["status_code"] = code
        res["status"] = MP_STATUS.get(code, f"Unknown({code})")
        if code != 2:
            res["error"] = (f"{spec['step']} returned {res['status']} "
                            f"(code {code}) - geometry '{object_name}' not "
                            "found?")
            return res
        for key, labels in spec["vectors"]:
            for lab in labels:
                try:
                    res["properties"][key] = sa.get_vector_arg(lab)
                    break
                except Exception:  # noqa: BLE001 - wrong label, try next
                    continue
        for key, labels in spec["doubles"]:
            for lab in labels:
                try:
                    res["properties"][key] = sa.get_double_arg(lab)
                    break
                except Exception:  # noqa: BLE001 - wrong label, try next
                    continue
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)
    return res


def _read_group_points(collection, group):
    """Read the working coordinates + stored offsets of group points.

    Returns {ok, points: [{name, x, y, z, planar_offset, radial_offset}],
    count, error?}. 'name' is the relative "group::target" name, matching
    _points_in_group() output. Offsets come from 'Get Point Properties' -
    SA stores the reflector/target offset there and applies it during the
    fit, so the reported deviations can be compensated by it.
    """
    full = f"{collection}::{group}" if collection else group
    names = _points_in_group(full)
    if not names:
        return {"ok": False, "points": [], "count": 0,
                "error": f"Point group '{full}' not found or empty."}
    points = []
    failures = 0
    for rel in names:
        target = rel.split("::")[-1]
        try:
            sa.set_step("Get Point Coordinate")
            sa.set_point_name_arg("Point Name", collection, group, target)
            if not sa.execute_step() or sa.get_step_result() != 2:
                failures += 1
                continue
            pt = {
                "name": rel,
                "x": float(sa.get_double_arg("X Value")),
                "y": float(sa.get_double_arg("Y Value")),
                "z": float(sa.get_double_arg("Z Value")),
                "planar_offset": 0.0,
                "radial_offset": 0.0,
            }
            try:  # stored probe/reflector offsets (may be absent: keep 0)
                sa.set_step("Get Point Properties")
                sa.set_point_name_arg("Point Name", collection, group, target)
                if sa.execute_step() and sa.get_step_result() == 2:
                    pt["planar_offset"] = float(
                        sa.get_double_arg("Planar Offset"))
                    pt["radial_offset"] = float(
                        sa.get_double_arg("Radial Offset"))
            except Exception:  # noqa: BLE001 - offsets are optional
                pass
            points.append(pt)
        except Exception:  # noqa: BLE001 - one bad point must not kill the run
            failures += 1
    if not points:
        return {"ok": False, "points": [], "count": 0,
                "error": f"Could not read coordinates of group '{full}'."}
    return {"ok": True, "points": points, "count": len(points),
            "failures": failures, "error": None}


def _v3(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a):
    return math.sqrt(_dot(a, a))


def _unit(a):
    n = _norm(a)
    if n == 0:
        return None
    return [a[0] / n, a[1] / n, a[2] / n]


def _point_deviation(geometry_type, props, p):
    """Signed deviation of point p to a fitted primitive (job length units).

    + = point outside the nominal surface (radial types), - = inside.
    Plane: signed distance along the normal. Line: unsigned (magnitude only).
    Cone: signed distance along the surface normal, apex half-angle from
    'included_angle' (degrees). Returns None when props are insufficient.
    """
    x, y, z = p["x"], p["y"], p["z"]
    pt = [x, y, z]
    if geometry_type == "cylinder":
        a, u = props.get("begin"), _unit(props.get("axis", [0, 0, 0]))
        r = props.get("radius")
        if a is None or u is None or r is None:
            return None
        w = _v3(pt, a)
        s = _dot(w, u)
        radial = _norm(_v3(w, [s * u[0], s * u[1], s * u[2]]))
        return radial - r
    if geometry_type == "sphere":
        c, r = props.get("center"), props.get("radius")
        if c is None or r is None:
            return None
        return _norm(_v3(pt, c)) - r
    if geometry_type == "circle":
        c, n, r = props.get("center"), props.get("normal"), props.get("radius")
        u = _unit(n or [0, 0, 0])
        if c is None or u is None or r is None:
            return None
        w = _v3(pt, c)
        s = _dot(w, u)
        radial = _norm(_v3(w, [s * u[0], s * u[1], s * u[2]]))
        return radial - r
    if geometry_type == "plane":
        n, p0 = props.get("normal"), props.get("point")
        u = _unit(n or [0, 0, 0])
        if p0 is None or u is None:
            return None
        return _dot(_v3(pt, p0), u)
    if geometry_type == "line":
        a, b = props.get("begin"), props.get("end")
        u = _unit(_v3(b, a)) if a is not None and b is not None else None
        if a is None or u is None:
            return None
        return _norm(_cross(_v3(pt, a), u))  # unsigned distance to axis
    if geometry_type == "cone":
        apex, axis = props.get("apex"), props.get("axis")
        incl = props.get("included_angle")
        u = _unit(axis or [0, 0, 0])
        if apex is None or u is None or incl is None or not incl:
            return None
        alpha = math.radians(float(incl)) / 2.0  # included angle -> half angle
        w = _v3(pt, apex)
        s = _dot(w, u)
        radial = _norm(_v3(w, [s * u[0], s * u[1], s * u[2]]))
        return radial * math.cos(alpha) - s * math.sin(alpha)
    return None


def _compute_deviations(geometry_type, props, points, probe_offset_mm=None):
    """Signed deviations of points to a fitted primitive, compensated by the
    reflector offset.

    SA measures to the centre of the reflector and stores the offset per point
    ('Get Point Properties' -> Radial Offset), applying it during the fit. The
    raw point coordinates therefore sit one reflector radius outside the fitted
    surface. Each deviation is reduced by the point's stored radial offset when
    present, else by the constant probe_offset_mm argument. Returns dicts with
    {name, deviation (compensated), raw_deviation, offset_mm, abs_deviation}.
    """
    out = []
    for p in points:
        dev = _point_deviation(geometry_type, props, p)
        if dev is None:
            continue
        stored = p.get("radial_offset")
        if stored not in (None, 0):
            offset = stored
        elif probe_offset_mm not in (None, 0):
            offset = float(probe_offset_mm)
        else:
            offset = 0.0
        comp = dev - offset
        out.append({"name": p["name"], "deviation": round(comp, 9),
                    "raw_deviation": round(dev, 9),
                    "offset_mm": round(offset, 9),
                    "abs_deviation": round(abs(comp), 9)})
    return out


def _compensation_meta(points, probe_offset_mm):
    """How the reported deviations were compensated (for the response)."""
    stored = [p["radial_offset"] for p in points
              if p.get("radial_offset") not in (None, 0)]
    if stored:
        return {"mode": "stored_point_offsets",
                "points_with_offset": len(stored),
                "min_offset_mm": round(min(stored), 9),
                "max_offset_mm": round(max(stored), 9),
                "constant_fallback_mm": probe_offset_mm}
    if probe_offset_mm not in (None, 0):
        return {"mode": "constant", "constant_mm": probe_offset_mm,
                "points_with_offset": 0}
    return {"mode": "none", "points_with_offset": 0,
            "note": ("no stored point offsets and probe_offset_mm not given - "
                     "deviations are relative to the raw point coordinates")}


def _dev_stats(deviations):
    """RMS/СКО, mean, sigma, min/max over signed deviations."""
    if not deviations:
        return {"point_count": 0, "error": "no deviations computed"}
    n = float(len(deviations))
    mean = sum(d["deviation"] for d in deviations) / n
    rms = math.sqrt(sum(d["deviation"] ** 2 for d in deviations) / n)
    var = sum((d["deviation"] - mean) ** 2 for d in deviations) / n
    vals = [d["deviation"] for d in deviations]
    abs_vals = [abs(v) for v in vals]
    return {
        "point_count": int(n),
        "rms_deviation": round(rms, 9),      # СКО / RMS
        "average_deviation": round(mean, 9),
        "standard_deviation": round(math.sqrt(var), 9),
        "min_deviation": round(min(vals), 9),
        "max_deviation": round(max(vals), 9),
        "max_abs_deviation": round(max(abs_vals), 9),
        "error": None,
    }


def _robust_sigma(deviations):
    """Robust spread of signed deviations for sa_fit_clean's sigma mode.

    A few genuine outliers inflate the plain standard deviation so much that
    a sigma * std clip excludes nothing; the median/MAD scale stays near the
    inlier spread (e.g. an outlier planted at +10 mm over ~0.05 mm noise ->
    std ~4 mm but MAD-based sigma ~0.07 mm). Returns 1.4826 * MAD, or - when
    MAD is zero (more than half the points share one deviation, e.g. a
    noise-free synthetic group) - the median |deviation|, so a lone planted
    outlier still gets excluded. 0.0 only for fully degenerate input (the
    caller then falls back to the plain standard deviation).
    """
    vals = sorted(d["deviation"] for d in deviations)
    if not vals:
        return 0.0
    n = len(vals)
    if n % 2:
        med = vals[n // 2]
    else:
        med = 0.5 * (vals[n // 2 - 1] + vals[n // 2])
    mad = sorted(abs(v - med) for v in vals)
    m = len(mad)
    if m % 2:
        med_mad = mad[m // 2]
    else:
        med_mad = 0.5 * (mad[m // 2 - 1] + mad[m // 2])
    sigma_mad = 1.4826 * med_mad
    if sigma_mad > 0:
        return sigma_mad
    abs_vals = sorted(abs(v) for v in vals)
    k = len(abs_vals)
    if k % 2:
        return abs_vals[k // 2]
    return 0.5 * (abs_vals[k // 2 - 1] + abs_vals[k // 2])


def _quality_result(geometry_type, collection, object_name, group, points,
                    tolerance_mm=None, max_outliers=200,
                    probe_offset_mm=None):
    """Geometry parameters + deviation stats/outliers for one fit."""
    props_res = _read_geometry_props(geometry_type, collection, object_name)
    if props_res["error"]:
        return {"ok": False, "error": props_res["error"]}
    props = props_res["properties"]
    devs = _compute_deviations(geometry_type, props, points,
                               probe_offset_mm=probe_offset_mm)
    stats = _dev_stats(devs)
    stats["tolerance_mm"] = tolerance_mm
    outliers = []
    if tolerance_mm is not None:
        tol = float(tolerance_mm)
        outliers = [d for d in devs if d["abs_deviation"] > tol]
        outliers.sort(key=lambda d: d["abs_deviation"], reverse=True)
        stats["n_outliers"] = len(outliers)
        stats["pct_outliers"] = round(
            100.0 * len(outliers) / stats["point_count"], 3) \
            if stats["point_count"] else 0.0
    else:
        stats["n_outliers"] = None
        stats["pct_outliers"] = None
    return {
        "ok": True,
        "geometry_type": geometry_type,
        "object_name": object_name,
        "collection": collection,
        "geometry": props,
        "props_step": props_res["step"],
        "stats": stats,
        "outliers": outliers[:max_outliers],
        "compensation": _compensation_meta(points, probe_offset_mm),
        "error": None,
    }


def _quality_from_source(geometry_type, collection, object_name, group,
                         coordinates, tolerance_mm=None,
                         probe_offset_mm=None):
    """Shared body of the report tools: resolves the point source, computes
    quality. Returns (report_dict, points_list)."""
    if coordinates is not None:
        points = [{"name": f"P{i + 1}", "x": float(xyz[0]),
                   "y": float(xyz[1]), "z": float(xyz[2]),
                   "planar_offset": 0.0, "radial_offset": 0.0}
                  for i, xyz in enumerate(coordinates)]
    else:
        rd = _read_group_points(collection, group)
        if not rd["ok"]:
            return {"ok": False, "error": rd["error"]}, None
        points = rd["points"]
    q = _quality_result(geometry_type, collection, object_name, group, points,
                        tolerance_mm=tolerance_mm,
                        probe_offset_mm=probe_offset_mm)
    q["point_source"] = {
        "collection": collection, "group": group,
        "from_coordinates": coordinates is not None,
    }
    return q, points


@mcp.tool()
def sa_geometry_props(geometry_type: str, object_name: str,
                      collection: str = "") -> dict:
    """Read the parameters of an existing fitted geometry object.

    The best-fit step itself returns no numbers ("Return Arguments: None"), so
    the built geometry is read back with SA's 'Get <Type> Properties' step:
    cylinder begin/end/axis/length/radius/diameter, sphere center/radius,
    circle center/normal/radius, plane normal/point/D, line begin/end/length,
    cone apex/axis/included angle. All lengths are in the job's distance unit.

    Args:
        geometry_type: One of plane|sphere|cylinder|cone|circle|line.
        object_name: Name of the geometry object (as created by sa_best_fit).
        collection: Collection holding the object ("" if none).

    Returns:
        {geometry_type, object_name, collection, step, status, properties,
         error?}
    """
    try:
        _ensure_sa()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    gtype = geometry_type.lower()
    if gtype not in _GEOMETRY_PROP_SPECS:
        return {"error": (f"Unknown geometry_type '{geometry_type}'. Choose "
                          f"one of: {sorted(_GEOMETRY_PROP_SPECS)}")}
    res = _read_geometry_props(gtype, collection, object_name)
    res["geometry_type"] = gtype
    res["object_name"] = object_name
    res["collection"] = collection
    return res


@mcp.tool()
def sa_fit_quality(
    geometry_type: str,
    object_name: str,
    point_group: str,
    collection: str = "",
    tolerance_mm: float | None = None,
    probe_offset_mm: float | None = None,
) -> dict:
    """Check how well a fitted geometry matches its point group.

    Computes the signed deviation of every group point from the fitted
    primitive and reports the fit quality: RMS deviation ("СКО"), mean,
    sigma, min/max signed deviation, max absolute deviation - plus, when
    tolerance_mm is given, how many points are out of tolerance ("вылеты")
    with their names and deviations.

    Reflector compensation: SA measures to the reflector centre and stores the
    offset per point ('Get Point Properties' -> Radial Offset), applying it
    during the fit - so raw point coordinates sit one reflector radius outside
    the fitted surface. Deviations are reduced by the stored per-point radial
    offset automatically; when the group stores none (or you evaluate your own
    coordinates), pass the constant offset as probe_offset_mm (e.g. 19.05 for
    a 3/4" SMR). What "compensation" did is reported in the response.

    Deviation sign convention: + = point outside the nominal surface
    (cylinder/sphere/circle radial, plane along its normal), - = inside; a
    line has no meaningful sign (magnitude only). All values are in the job's
    distance unit (mm in the test file).

    Args:
        geometry_type: One of plane|sphere|cylinder|cone|circle|line.
        object_name: Fitted geometry to check (created by sa_best_fit).
        point_group: Point group the geometry was fit from.
        collection: Collection holding both ("" if none).
        tolerance_mm: Optional tolerance; points with |deviation| above it are
                      reported as outliers.
        probe_offset_mm: Constant reflector offset to compensate when the
                         points store none (default: use stored offsets).

    Returns:
        {ok, geometry_type, object_name, geometry, stats, outliers,
         compensation, error?}
    """
    try:
        _ensure_sa()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    gtype = geometry_type.lower()
    if gtype not in _GEOMETRY_PROP_SPECS:
        return {"error": (f"Unknown geometry_type '{geometry_type}'. Choose "
                          f"one of: {sorted(_GEOMETRY_PROP_SPECS)}")}
    if not point_group:
        return {"error": "Provide 'point_group' (the fitted group)."}
    q, _ = _quality_from_source(gtype, collection, object_name, point_group,
                                None, tolerance_mm,
                                probe_offset_mm=probe_offset_mm)
    return q


@mcp.tool()
def sa_best_fit_report(
    geometry_type: str,
    object_name: str,
    point_group: str = "",
    collection: str = "",
    coordinates: list | None = None,
    tolerance_mm: float | None = None,
    probe_offset_mm: float | None = None,
) -> dict:
    """Best-fit geometry AND report the construction results.

    One call for: fit the primitive ("Fit Geometry to Point Group"), read its
    parameters back ("Get <Type> Properties") and evaluate how well it was
    built (RMS/СКО, mean, sigma, min/max, outliers vs tolerance_mm). Same
    point sources as sa_best_fit: existing point_group, or raw coordinates.
    Reflector compensation follows sa_fit_quality (stored point offsets, or
    the constant probe_offset_mm).

    Args:
        geometry_type: One of plane|sphere|cylinder|cone|circle|line.
        object_name: Name for the constructed geometry object.
        point_group: Existing point group to fit to (or temp group name when
                     coordinates are given).
        collection: Collection holding the group / output object.
        coordinates: Optional list of [x, y, z] point coordinates.
        tolerance_mm: Optional tolerance; points with |deviation| above it are
                      listed as outliers ("вылеты").
        probe_offset_mm: Constant reflector offset to compensate when the
                         points store none.

    Returns:
        sa_best_fit result plus {geometry, stats, outliers, compensation}.
    """
    try:
        _ensure_sa()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    gtype = geometry_type.lower()
    if gtype not in BESTFIT_TYPES:
        return {"constructed": False, "error": (
            f"Unknown geometry_type '{geometry_type}'. Choose one of: "
            f"{sorted(BESTFIT_TYPES)}")}
    if not point_group and not coordinates:
        return {"constructed": False, "error": (
            "Provide either 'point_group' (an existing group) or "
            "'coordinates'.")}
    collection, group, build_info = _resolve_point_source(
        point_group, collection, coordinates)
    if coordinates and not (build_info and build_info["ok"]):
        return {"constructed": False, "geometry_type": geometry_type,
                "object_name": object_name, "build": build_info,
                "error": "Failed to build input point group from coordinates."}
    report = _fit_geometry_report(gtype, object_name, collection, group,
                                  build_info)
    report["geometry_type"] = geometry_type
    if not report.get("constructed"):
        return report
    q, _ = _quality_from_source(gtype, collection, object_name, group,
                                coordinates, tolerance_mm,
                                probe_offset_mm=probe_offset_mm)
    report.update(q)
    return report


@mcp.tool()
def sa_fit_clean(
    geometry_type: str,
    object_name: str,
    point_group: str,
    collection: str = "",
    tolerance_mm: float | None = None,
    sigma: float = 3.0,
    max_iterations: int = 6,
    delete_outliers: bool = False,
    probe_offset_mm: float | None = None,
) -> dict:
    """Robust best fit: iteratively exclude ("delete") outliers and refit.

    Replicates the SA GUI robust best-fit: fit the primitive to the point
    group, measure every point's deviation, then refit to ONLY the points
    within tolerance ('Fit Geometry to Points' over the kept subset), repeat
    until no outliers remain or the excluded set stops changing between
    passes (a genuinely out-of-tolerance population never reaches zero
    outliers - the robust fit stabilises instead), or max_iterations is
    reached. Points farther than the tolerance never influence the geometry
    and stay in the group. Reports RMS ("СКО"), sigma and the excluded count
    of every iteration; 'stats' covers all group points, 'stats_kept' only
    the in-tolerance points the geometry was fit to.

    Note: SA's own 'Fit Geometry to Point Group' does NOT exclude when
    'Ignore Out of Tolerance Points' is set (confirmed live on SA 2015: it
    returns DoneMinorError "tolerance exceeded" but the geometry is identical
    to an unconstrained fit) - exclusion is done here explicitly, pass by
    pass, through the list variant of the fit step.

    Note: SA never OVERWRITES a constructed object when the name already
    exists - a second fit under the same name creates a suffixed duplicate
    ("X", then "X1"; confirmed live on SA 2015), so reading the geometry back
    under the plain name would return the FIRST pass' stale result.
    sa_fit_clean therefore deletes any pre-existing object named object_name
    before starting and deletes its own previous-pass object before every
    refit ('Delete Objects', joined full name - confirmed live): the job ends
    up with exactly one geometry object, named object_name.

    Threshold: tolerance_mm (fixed, physical) when given, otherwise a
    sigma-clip of sigma * robust_sigma, where robust_sigma is 1.4826 * MAD of
    the deviations (a few genuine outliers inflate the plain standard
    deviation so much that sigma * std excludes nothing - seen live). Falls
    back to the median |deviation| (then the plain standard deviation) when
    MAD is zero. Both modes measure around the fit's MEDIAN deviation, and a
    first pass whose median sits far from zero (a strong outlier pulled the
    LSQ geometry, so every good point reads out-of-tolerance on the wrong
    side) gets one automatic 'debias' pass first - such iterations are marked
    debias_pass=True. A run whose excluded set keeps changing until
    max_iterations (a genuine form deviation larger than the tolerance, e.g.
    tolerance 0.2 on a surface with ~0.66 RMS form error) returns converged
    False with the best (tightest-core) geometry; delete_outliers still
    deletes against that final geometry, exactly like the SA GUI 'Delete
    Outliers' action.
    Deviations are reflector-compensated like in sa_fit_quality (stored point
    offsets, or the constant probe_offset_mm); lengths are in the job's unit.

    WARNING: delete_outliers=True additionally PHYSICALLY deletes the outlier
    points from the point group ('Delete Points') after convergence and refits
    on the remaining points - destructive to the group in the current job.

    Args:
        geometry_type: One of plane|sphere|cylinder|cone|circle|line.
        object_name: Name for the constructed geometry object. A pre-existing
                     object with this same name is deleted first (replaced).
        point_group: Point group to fit.
        collection: Collection holding both ("" if none).
        tolerance_mm: Fixed tolerance for outlier exclusion (job length unit).
        sigma: Sigma multiplier for automatic threshold when tolerance_mm is
               None (default 3.0; <=0 disables exclusion).
        max_iterations: Max fit passes (default 6).
        delete_outliers: Also physically delete outliers from the group.
        probe_offset_mm: Constant reflector offset to compensate when the
                         points store none.

    Returns:
        {constructed, object_name, iterations: [...], converged, geometry,
         stats, outliers, error?}
    """
    try:
        _ensure_sa()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    gtype = geometry_type.lower()
    if gtype not in BESTFIT_TYPES:
        return {"constructed": False, "error": (
            f"Unknown geometry_type '{geometry_type}'. Choose one of: "
            f"{sorted(BESTFIT_TYPES)}")}
    if not point_group:
        return {"constructed": False, "error": (
            "Provide 'point_group' (the group to fit).")}
    rd = _read_group_points(collection, point_group)
    if not rd["ok"]:
        return {"constructed": False, "error": rd["error"]}
    points = rd["points"]
    # Minimum points a fit of the type needs to stay determined.
    min_points = {"line": 2, "plane": 3, "circle": 3, "sphere": 4,
                  "cylinder": 5, "cone": 6}.get(gtype, 3)

    def _point_full(p):
        # Joined hierarchical full name ("C::G::T"; leading "::" when the job
        # has no collection) - the layout SA's Point Name Ref Lists use.
        return "::".join([collection or "", point_group,
                          p["name"].split("::")[-1]])

    def _devs(props):
        return _compute_deviations(gtype, props, points,
                                   probe_offset_mm=probe_offset_mm)

    def _object_full():
        return "::".join([collection or "", object_name])

    def _del_object():
        # 'Delete Objects' (joined full name) - removes only geometry this run
        # created (or replaced). SUCCESS/PARTIAL come back as code 2/4.
        try:
            sa.set_step("Delete Objects")
            sa.set_collection_object_name_ref_list_arg("Object Names",
                                                       [_object_full()])
            sa.execute_step()
            return sa.get_step_result() in (2, 4)
        except Exception:  # noqa: BLE001
            return False

    def _fit_into(full_names):
        # Fit the WHOLE group (None) or exactly the given points (list of
        # joined full names). The list variant 'Fit Geometry to Points' never
        # pops dialogs and touches no point group; SA still applies each
        # point's stored reflector offset (all confirmed live on SA 2015).
        try:
            if full_names is None:
                sa.set_step("Fit Geometry to Point Group")
                sa.set_geometry_type_arg("Geometry Type",
                                         BESTFIT_TYPES[gtype])
                sa.set_collection_object_name_arg("Group to Fit", collection,
                                                  point_group)
            else:
                sa.set_step("Fit Geometry to Points")
                sa.set_geometry_type_arg("Geometry Type",
                                         BESTFIT_TYPES[gtype])
                sa.set_point_name_ref_list_arg("Points to Fit", full_names)
            sa.set_collection_object_name_arg("Resulting Object Name",
                                              collection, object_name)
            sa.set_string_arg("Fit Profile Name", "")
            sa.set_bool_arg("Report Deviations", False)
            for arg in _TOLERANCE_ARG_NAMES:
                if sa.set_double_arg(arg, 0.0):  # no limit; exclusion manual
                    break
            sa.set_bool_arg("Ignore Out of Tolerance Points", False)
            sa.execute_step()
            code = sa.get_step_result()
            return {"constructed": code in (2, 4),
                    "status_code": code,
                    "status": MP_STATUS.get(code, f"Unknown({code})"),
                    "messages": _safe_messages()}
        except Exception as exc:  # noqa: BLE001
            return {"constructed": False, "error": str(exc)}

    # Clean slate: SA duplicates instead of overwriting, so a pre-existing
    # object under object_name would shadow every readback below.
    replaced = False
    try:
        hits = [n for n in _objects_in_collection_by_type(
                    collection, BESTFIT_TYPES[gtype])
                if n == _object_full()]
    except Exception:  # noqa: BLE001 - enumeration not available; skip
        hits = []
    if hits:
        sa.set_step("Delete Objects")
        sa.set_collection_object_name_ref_list_arg("Object Names", hits)
        sa.execute_step()
        if sa.get_step_result() not in (2, 4):
            return {"constructed": False, "object_name": object_name,
                    "geometry_type": geometry_type,
                    "error": f"could not replace existing '{_object_full()}'"}
        replaced = True

    fit = _fit_into(None)
    if not fit.get("constructed"):
        return {"constructed": False, "object_name": object_name,
                "geometry_type": geometry_type,
                "error": f"initial fit failed: {fit.get('error')}"}

    history = []
    last = None
    tol = None  # active tolerance (None = exclusion disabled)
    center = 0.0  # deviation center the threshold is measured from
    debias_done = False
    prev_excluded = None  # excluded target set of the previous iteration
    for it in range(max(1, int(max_iterations))):
        props_res = _read_geometry_props(gtype, collection, object_name)
        if props_res["error"]:
            return {"constructed": False, "object_name": object_name,
                    "geometry_type": geometry_type,
                    "iterations": history, "error": props_res["error"]}
        props = props_res["properties"]
        devs = _devs(props)
        stats = _dev_stats(devs)
        n = len(devs)
        svals = sorted(d["deviation"] for d in devs)
        med = (svals[n // 2] if n % 2
               else 0.5 * (svals[n // 2 - 1] + svals[n // 2])) if n else 0.0
        center = 0.0
        debias_now = False
        if tolerance_mm is not None:
            tol = float(tolerance_mm)
            # A lone strong outlier pulls the FIRST LSQ fit so far that every
            # good point reads as out-of-tolerance on the wrong side (seen
            # live: 5 points at 0.02 mm noise + one at +10 mm -> inliers come
            # back at ~-1.65 mm). Peel it once with the robust centered clip,
            # then apply the user's physical tolerance to the de-biased fit.
            if (it == 0 and sigma and sigma > 0 and not debias_done
                    and abs(med) > 0.25 * tol):
                debias_done = True
                debias_now = True
                tol = float(sigma) * (_robust_sigma(devs) or tol)
                center = med
        elif sigma and sigma > 0:
            # sigma mode: threshold = sigma * robust spread (1.4826 * MAD,
            # see _robust_sigma), measured around the median deviation so a
            # biased first fit does not flag its own inliers.
            robust = _robust_sigma(devs)
            scale = robust or stats["standard_deviation"] \
                or stats["rms_deviation"] or 0.0
            tol = float(sigma) * scale
            center = med
        else:
            tol = None
        outs = ([d for d in devs
                 if abs(d["deviation"] - center) > tol]
                if (tol and tol > 0) else [])
        # Convergence: no outliers at all, or the excluded set stopped
        # changing between passes (a genuinely out-of-tolerance population
        # never reaches zero outliers - the robust fit stabilises instead).
        excluded_set = {d["name"].split("::")[-1] for d in outs}
        stable = (prev_excluded is not None and excluded_set == prev_excluded)
        kept = ([d for d in devs
                 if abs(d["deviation"] - center) <= tol]
                if (tol and tol > 0) else list(devs))
        stats_kept = _dev_stats(kept) if kept else None
        step = {
            "iteration": it,
            "tolerance_mm": tol,
            "deviation_center_mm": center,
            "debias_pass": debias_now,
            "geometry": props,
            "stats": stats,
            "stats_kept": stats_kept,
            "compensation": _compensation_meta(points, probe_offset_mm),
            "n_excluded": len(outs),
            "excluded": outs[:200],
            "converged": bool(not outs or stable),
        }
        history.append(step)
        last = step
        if not outs or stable or not (tol and tol > 0):
            break
        if len(kept) < min_points:
            step["note"] = (f"only {len(kept)} point(s) within tolerance - "
                            f"too few to refit a {gtype}; stopping")
            break
        # SA never overwrites: drop this pass' object first so the refit lands
        # exactly under object_name and the next readback is the new geometry.
        if not _del_object():
            step["note"] = ("could not delete previous geometry before the "
                            "refit; stopping with the fit of all points")
            break
        refit = _fit_into([_point_full(d) for d in kept])
        if not refit.get("constructed"):
            _fit_into(None)  # recover: leave a plain all-points fit behind
            step["refit_error"] = (refit.get("error")
                                   or f"fit of kept points returned "
                                   f"{refit.get('status')}")
            break
        prev_excluded = excluded_set
    converged = bool(last and last["converged"])

    deleted = []
    if (delete_outliers and tol and tol > 0 and last
            and not last.get("refit_error")):
        # Physical delete matches the SA GUI 'Delete Outliers' action: drop
        # every point beyond the FINAL pass' threshold from the group and
        # refit the rest. History "excluded" lists are capped at 200 entries,
        # so the outlier set is recomputed in full here.
        cut = last.get("deviation_center_mm") or 0.0
        outs = [d for d in _devs(last["geometry"])
                if abs(d["deviation"] - cut) > tol]
        if outs:
            targets = [d["name"].split("::")[-1] for d in outs]
            try:
                sa.set_step("Delete Points")
                sa.set_point_name_ref_list_arg(
                    "Point Names", [_point_full(d) for d in outs])
                sa.execute_step()
                code = sa.get_step_result()
                deleted = [d["name"] for d in outs]
                if code not in (2, 4):
                    return {"constructed": True, "object_name": object_name,
                            "geometry_type": geometry_type,
                            "iterations": history, "deleted": deleted,
                            "error": ("'Delete Points' returned "
                                      f"{MP_STATUS.get(code, code)} - points "
                                      "may still be in the group")}
                # Final plain fit of the reduced group (no tolerance),
                # replacing this run's object so the name stays unique.
                removed = set(targets)
                points = [p for p in points if p["name"].split("::")[-1]
                          not in removed]
                _del_object()
                fit = _fit_into(None)
                if fit.get("constructed"):
                    props_res = _read_geometry_props(gtype, collection,
                                                     object_name)
                    devs = _devs(props_res["properties"])
                    stats = _dev_stats(devs)
                    last = {
                        "iteration": len(history),
                        "tolerance_mm": None,
                        "geometry": props_res["properties"],
                        "stats": stats,
                        "stats_kept": stats,
                        "compensation": _compensation_meta(
                            points, probe_offset_mm),
                        "n_excluded": 0,
                        "excluded": [],
                        "converged": True,
                        "after_deletion": True,
                    }
                    history.append(last)
            except Exception as exc:  # noqa: BLE001
                return {"constructed": True, "object_name": object_name,
                        "geometry_type": geometry_type,
                        "iterations": history, "deleted": deleted,
                        "error": f"delete_outliers failed: {exc}"}

    if not last:
        return {"constructed": False, "object_name": object_name,
                "geometry_type": geometry_type, "iterations": history,
                "error": "no fit iterations completed"}
    return {
        "constructed": True,
        "geometry_type": geometry_type,
        "object_name": object_name,
        "collection": collection,
        "iterations": history,
        "converged": converged,
        "final_geometry": last["geometry"],
        "final_stats": last["stats"],
        "final_stats_kept": last.get("stats_kept"),
        "deleted_outliers": deleted,
        "replaced_object": replaced,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Best fit with FIXED parameters + reflector-offset compensation side.
#
# SA 2015 has no MP step that fits a primitive with a fixed radius / cone
# included angle ("Fit Geometry to Point Group" takes a geometry type, a group
# and a tolerance only), and the compensation direction of the reflector
# offset cannot be chosen either. So these tools fit the primitive OURSELVES
# (sa_fitmath.fit_geometry, pure constrained least squares, no COM) and then
# create the geometry object in SA with the plain constructors ("Construct
# Cylinder/Sphere/Circle/Cone" - confirmed names in the MP Command Reference,
# ch. 5 Construction Operations). Constructors accept any size/vector, so the
# fixed radius shows up verbatim in the created object.
#
# Compensation ("в какую сторону компенсировать смещение отражателя"):
#   the measured points are reflector/target CENTRE coordinates, one probe
#   radius away from the true surface along the local normal. side = +1 means
#   the reflector stood on the OUTSIDE of the feature (3D: shaft / outer
#   surface; plane: above), side = -1 INSIDE (bore / below), side = 0 means no
#   compensation. The per-point offset magnitudes come from the group's stored
#   'Radial Offset' point properties when present, else from probe_offset_mm.
#   Circle accepts compensation="both": each point is compensated by its own
#   stored offset sign (a measured circle can have points standing on both
#   sides of the surface).
# ---------------------------------------------------------------------------

# geometry_type -> Construct step + names of its vector/double/name arguments
# (arg labels from the MP Command Reference; vector labels may carry a
# "(in working coordinates)" suffix in SA's MP tree, so candidates are probed).
_CONSTRUCT_SPECS = {
    "cylinder": {
        "step": "Construct Cylinder",
        "name_arg": "Cylinder Name",
        "vectors": [("begin", ["Cylinder End Point (in working coordinates)",
                               "Cylinder End Point"]),
                    ("axis", ["Cylinder Axis (in working coordinates)",
                              "Cylinder Axis"])],
        "doubles": [("diameter", ["Cylinder Diameter"]),
                    ("length", ["Cylinder Length"])],
        "radius_from": "radius",
    },
    "sphere": {
        "step": "Construct Sphere",
        "name_arg": "Sphere Name",
        "vectors": [("center", ["Sphere Center (in working coordinates)",
                                "Sphere Center"])],
        "doubles": [("radius", ["Sphere Radius"])],
        "radius_from": "radius",
    },
    "circle": {
        "step": "Construct Circle",
        "name_arg": "Circle Name",
        "vectors": [("center", ["Circle Center (in working coordinates)",
                                "Circle Center"]),
                    ("normal", ["Circle Normal (in working coordinates)",
                                "Circle Normal"])],
        "doubles": [("radius", ["Circle Radius"])],
        "radius_from": "radius",
    },
    "cone": {
        "step": "Construct Cone",
        "name_arg": "Cone Name",
        "vectors": [("apex", ["Cone End Point (in working coordinates)",
                              "Cone End Point"]),
                    ("axis", ["Cone Axis (in working coordinates)",
                              "Cone Axis"])],
        "doubles": [("length", ["Cone Length"]),
                    ("theta_start", ["Cone Theta Start"]),
                    ("theta_span", ["Cone Theta Span"]),
                    ("included_angle", ["Cone Included Angle"])],
        "defaults": {"theta_start": 0.0, "theta_span": 360.0},
        "radius_from": None,
    },
}

_FIXED_MIN_POINTS = {"cylinder": 5, "sphere": 4, "circle": 3, "cone": 6}


def _set_first_arg(setter, candidates, *args):
    """Set the first arg candidate that the bridge accepts; return its name."""
    for cand in candidates:
        try:
            if setter(cand, *args):
                return cand
        except Exception:  # noqa: BLE001 - unknown arg name, try next
            continue
    return None


def _construct_geometry(geometry_type, collection, object_name, params):
    """Create the primitive in SA via its Construct step from fitted params.

    params carries the same keys sa_fitmath returns (begin/end/axis/radius/...
    for a cylinder, center/radius, apex/axis/length/included_angle, ...).
    Returns {constructed, step, status_code, status, messages, error?}.
    """
    spec = _CONSTRUCT_SPECS[geometry_type]
    res = {"constructed": False, "step": spec["step"], "status_code": None,
           "status": None, "error": None}
    try:
        sa.set_step(spec["step"])
        if not sa.set_collection_object_name_arg(spec["name_arg"],
                                                 collection, object_name):
            res["error"] = (f"Could not set '{spec['name_arg']}' on "
                            f"{spec['step']}.")
            return res
        for key, cands in spec["vectors"]:
            vec = params.get(key)
            if vec is None:
                res["error"] = f"Fitted params lack vector '{key}'."
                return res
            if _set_first_arg(sa.set_vector_arg, cands,
                              float(vec[0]), float(vec[1]), float(vec[2])) \
                    is None:
                res["error"] = (f"None of {cands} accepted the vector - "
                                f"check {spec['step']} arg names.")
                return res
        for key, cands in spec["doubles"]:
            val = params.get(key)
            if val is None:
                # Construct <Type> may take the diameter while the fit yields
                # the radius (cylinder) - derive it, or use the spec default.
                rf = spec.get("radius_from")
                if key == "diameter" and rf and params.get(rf) is not None:
                    val = 2.0 * float(params[rf])
                else:
                    val = spec.get("defaults", {}).get(key)
            if val is None:
                res["error"] = f"Fitted params lack double '{key}'."
                return res
            if _set_first_arg(sa.set_double_arg, cands, float(val)) is None:
                res["error"] = (f"None of {cands} accepted a double - "
                                f"check {spec['step']} arg names.")
                return res
        sa.execute_step()
        code = sa.get_step_result()
        res["status_code"] = code
        res["status"] = MP_STATUS.get(code, f"Unknown({code})")
        if code not in (2, 4):
            res["error"] = (f"{spec['step']} returned {res['status']} "
                            f"(code {code}).")
            return res
        res["constructed"] = True
        res["messages"] = _safe_messages()
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)
    return res


def _delete_geometry_objects(collection, names):
    """Delete geometry objects by simple name ('Delete Objects')."""
    full = [("::".join([collection or "", n])) for n in names]
    try:
        sa.set_step("Delete Objects")
        sa.set_collection_object_name_ref_list_arg("Object Names", full)
        sa.execute_step()
        return sa.get_step_result() in (2, 4)
    except Exception:  # noqa: BLE001
        return False


def _fixed_signed_offsets(geometry_type, compensation, points, probe_offset_mm):
    """Per-point SIGNED offsets applied by the fit, from the chosen side.

    Returns (signed_offsets, side_label, note). Magnitude per point = |stored
    Radial Offset| when the group stores one, else probe_offset_mm. side
    semantics: outside/+1 (shaft, outer surface, above), inside/-1 (bore,
    below), none/0. Circle 'both' compensates each point by its own stored
    offset sign (a measured circle can have points on both sides of the
    surface).
    """
    mags = []
    for p in points:
        ro = p.get("radial_offset")
        if ro not in (None, 0):
            mags.append(abs(float(ro)))
        elif probe_offset_mm not in (None, 0):
            mags.append(abs(float(probe_offset_mm)))
        else:
            mags.append(0.0)
    have_data = any(mags)
    comp = (compensation or "outside").lower()
    note = None
    if not have_data and comp not in ("none", "no", "0", "off", ""):
        note = ("no stored point offsets and no probe_offset_mm - "
                "compensation has no effect, fitting the raw centres")
    if comp in ("none", "no", "0", "off", ""):
        return [0.0] * len(points), "none", note
    if comp in ("outside", "out", "above", "+", "outer"):
        return [m for m in mags], "outside", note
    if comp in ("inside", "in", "below", "-", "inner"):
        return [-m for m in mags], "inside", note
    if comp in ("both", "mixed") and geometry_type == "circle":
        signed = []
        missing_dir = False
        for p, m in zip(points, mags):
            ro = p.get("radial_offset")
            sgn = 1.0
            if ro not in (None, 0):
                sgn = 1.0 if float(ro) >= 0 else -1.0
            elif probe_offset_mm not in (None, 0):
                sgn = 1.0  # probe_offset_mm carries no direction
                missing_dir = True
            signed.append(sgn * m)
        if missing_dir:
            note = (note + "; " if note else "") + \
                "points without a stored offset are compensated + (outside)"
        return signed, "both", note
    return None, comp, f"unknown compensation '{compensation}'"


def _fixed_result(geometry_type, collection, object_name, points, signed_off,
                  math_res, read_res, tolerance_mm=None, include_deviations=False,
                  note=None):
    """Assemble the tool response dict (flat, with stats on top level)."""
    props = read_res["properties"] if read_res.get("error") is None else {}
    gtype = geometry_type
    resp = {
        "ok": math_res.get("ok") and read_res.get("error") is None,
        "constructed": read_res.get("error") is None,
        "geometry_type": gtype,
        "object_name": object_name,
        "collection": collection or "",
        "point_count": len(points),
        "compensation": None,
        "error": read_res.get("error"),
    }
    # flatten the read-back parameters to top level, rounded for readability
    rnd = lambda v: [round(x, 6) for x in v] if isinstance(v, list) \
        else (round(v, 9) if v is not None else None)
    for key, val in (props or {}).items():
        resp[key] = rnd(val)
    # deviations of every point to the CREATED geometry, compensated by the
    # signed offsets actually used by the fit
    devs = []
    for p, so in zip(points, signed_off):
        dev = _point_deviation(gtype, props, p)
        if dev is None:
            continue
        devs.append({"name": p["name"],
                     "deviation": round(dev - so, 9),
                     "raw_deviation": round(dev, 9),
                     "offset_used_mm": round(so, 9)})
    if not devs:
        resp["stats"] = _dev_stats([])
    else:
        stats = _dev_stats(devs)
        resp["stats"] = stats
        if tolerance_mm not in (None, 0) and tolerance_mm > 0:
            n_out = sum(1 for d in devs if abs(d["deviation"]) > tolerance_mm)
            resp["outliers_over_mm"] = {"tolerance_mm": round(tolerance_mm, 9),
                                        "count": n_out}
        if include_deviations:
            resp["deviations"] = devs[:2000]
    resp["fit"] = {
        "iterations": math_res.get("iterations"),
        "sse": round(math_res.get("sse") or 0.0, 9),
        "compensated_rms_mm": (stats if devs else {"rms_deviation": None})
        .get("rms_deviation"),
    }
    if note:
        resp["note"] = note
    return resp


@mcp.tool()
def sa_fit_fixed(
    geometry_type: str,
    object_name: str,
    radius_mm: float | None = None,
    diameter_mm: float | None = None,
    apex_angle_deg: float | None = None,
    compensation: str = "outside",
    point_group: str = "",
    collection: str = "",
    coordinates: list | None = None,
    probe_offset_mm: float | None = None,
    tolerance_mm: float | None = None,
    include_deviations: bool = False,
) -> dict:
    """Best fit with a FIXED parameter, plus reflector-offset compensation side.

    SA 2015 has no fit step that constrains the radius / cone angle, so the
    primitive is fitted here by least squares (sa_fitmath) and then created in
    SA via its Construct step - the resulting object carries exactly the fixed
    radius / included angle.

    Fixed parameters (one set per geometry_type):
      cylinder | circle | sphere: radius_mm or diameter_mm (the nominal value
        in the job length unit). If neither is given the radius is left free.
      cone: apex_angle_deg - full cone apex angle ("угол раствора", 0..180).

    compensation ("в какую сторону компенсировать смещение отражателя"):
      The measured points are reflector/target centre coordinates, standing one
      probe radius off the true surface. Offset magnitudes come from the
      group's stored Radial Offset point properties, else probe_offset_mm.
        - "outside" (+): 3D - reflector on the OUTSIDE (shaft / outer surface);
                         2D - above the surface.
        - "inside" (-):  3D - reflector on the INSIDE (bore); 2D - below.
        - "none":        no compensation (fit the raw centres).
        - "both":        circle only - each point is compensated by its own
                         stored offset sign (a measured circle can have points
                         standing on both sides of the surface).
      With no stored offsets and no probe_offset_mm, compensation is a no-op
      (a note is returned) - the fit degenerates to the raw centres.

    Point source: point_group (an existing group, keeps per-point stored
    offsets) or coordinates ([[x,y,z],...]; offsets then come from
    probe_offset_mm only). A pre-existing object with object_name is deleted
    first (SA never overwrites - it would create a suffixed duplicate).

    Returns a flat dict: ok, constructed, geometry_type, object_name,
    collection, point_count, the read-back parameters (begin/end/axis/radius/
    ... per type), stats (RMS "СКО" of the compensated deviations, average,
    standard deviation, min/max), optional outliers_over_mm, deviations,
    fit info, error?
    """
    try:
        _ensure_sa()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    gtype = geometry_type.lower()
    if gtype not in _CONSTRUCT_SPECS:
        return {"ok": False, "error": (
            f"Fixed fits support: {sorted(_CONSTRUCT_SPECS)}. "
            f"Got '{geometry_type}'.")}
    if not object_name:
        return {"ok": False, "error": "Provide 'object_name'."}
    if not point_group and not coordinates:
        return {"ok": False, "error": (
            "Provide 'point_group' (existing group) or 'coordinates'.")}

    if coordinates:
        pts_raw = [list(map(float, c)) for c in coordinates]
        points = [{"name": f"P{i + 1}", "x": p[0], "y": p[1], "z": p[2],
                   "radial_offset": 0.0} for i, p in enumerate(pts_raw)]
    else:
        rd = _read_group_points(collection, point_group)
        if not rd["ok"]:
            return {"ok": False, "error": rd["error"]}
        points = rd["points"]
        pts_raw = [[p["x"], p["y"], p["z"]] for p in points]
    n = len(points)
    if n < _FIXED_MIN_POINTS.get(gtype, 3):
        return {"ok": False, "error": (
            f"{gtype} fit needs >= {_FIXED_MIN_POINTS.get(gtype, 3)} points, "
            f"got {n}.")}

    if radius_mm is not None and diameter_mm is not None \
            and abs(2.0 * float(radius_mm) - float(diameter_mm)) > 1e-9:
        return {"ok": False, "error": (
            "radius_mm and diameter_mm disagree - pass only one.")}
    fixed_radius = float(radius_mm) if radius_mm is not None \
        else (float(diameter_mm) / 2.0 if diameter_mm is not None else None)

    # compensation: per-point signed offsets actually applied by the fit
    signed, side_label, note = _fixed_signed_offsets(
        gtype, compensation, points, probe_offset_mm)
    if signed is None:
        return {"ok": False, "error": note}

    math_res = sa_fitmath.fit_geometry(
        gtype, pts_raw, radius=fixed_radius, apex_angle_deg=apex_angle_deg,
        side=1, offsets=signed)
    if not math_res.get("ok"):
        return {"ok": False, "error": math_res.get("error"),
                "geometry_type": gtype, "object_name": object_name}

    # SA never overwrites: delete any pre-existing object of that name first.
    replaced = False
    try:
        obj_type = BESTFIT_TYPES[gtype]
        prev = _objects_in_collection_by_type(collection, obj_type)
        suffix = f"::{object_name}"
        replaced = any(n == object_name or str(n).endswith(suffix)
                       for n in prev)
    except Exception:  # noqa: BLE001 - existence check is best-effort
        pass
    _delete_geometry_objects(collection, [object_name])

    construct_res = _construct_geometry(gtype, collection, object_name,
                                        math_res["params"])
    if not construct_res.get("constructed"):
        return {"ok": False, "error": construct_res.get("error"),
                "geometry_type": gtype, "object_name": object_name,
                "status": construct_res.get("status")}
    read_res = _read_geometry_props(gtype, collection, object_name)
    resp = _fixed_result(gtype, collection, object_name, points, signed,
                         math_res, read_res, tolerance_mm=tolerance_mm,
                         include_deviations=include_deviations, note=note)
    resp["replaced_object"] = replaced
    resp["point_source"] = {"point_group": point_group or None,
                            "coordinates_count": len(coordinates) if coordinates else None}
    if fixed_radius is not None:
        resp["fixed_radius_mm"] = round(fixed_radius, 9)
        resp["fixed_diameter_mm"] = round(2.0 * fixed_radius, 9)
    elif gtype == "cone":
        resp["fixed_apex_angle_deg"] = apex_angle_deg
    resp["compensation"] = side_label
    return resp


def _fixed_wrapper(gtype, extra_doc=""):
    """Typed sa_fit_fixed_<gtype> tool bound to one geometry_type."""
    @mcp.tool(name=f"sa_fit_fixed_{gtype}")
    def _tool(object_name: str, radius_mm: float | None = None,
              diameter_mm: float | None = None,
              apex_angle_deg: float | None = None,
              compensation: str = "outside", point_group: str = "",
              collection: str = "", coordinates: list | None = None,
              probe_offset_mm: float | None = None,
              tolerance_mm: float | None = None,
              include_deviations: bool = False) -> dict:
        return sa_fit_fixed(gtype, object_name, radius_mm=radius_mm,
                            diameter_mm=diameter_mm,
                            apex_angle_deg=apex_angle_deg,
                            compensation=compensation,
                            point_group=point_group, collection=collection,
                            coordinates=coordinates,
                            probe_offset_mm=probe_offset_mm,
                            tolerance_mm=tolerance_mm,
                            include_deviations=include_deviations)
    _tool.__doc__ = (
        f"Best fit a {gtype} with a FIXED parameter + compensation side. "
        f"{extra_doc}Thin wrapper over sa_fit_fixed(geometry_type='{gtype}'); "
        "see sa_fit_fixed for the full description.")
    return _tool


_wrap_specs = {
    "cylinder": "Fixed radius_mm/diameter_mm; compensation outside (вал) / "
                "inside (отверстие) / none.",
    "circle": "Fixed radius_mm/diameter_mm; compensation outside / inside / "
              "none / both (per-point stored offset sign).",
    "sphere": "Fixed radius_mm/diameter_mm; compensation outside / inside / "
              "none.",
    "cone": "Fixed apex_angle_deg (full included angle, угол раствора); "
            "compensation outside / inside / none.",
}
for _g, _doc in _wrap_specs.items():
    _fixed_wrapper(_g, _doc)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")

