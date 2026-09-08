"""Non-COM layer for managing the SpatialAnalyzer GUI process.

Everything here runs on the caller's thread (no COM, no apartment affinity):
finding the SA executable, checking whether the GUI is running, and launching
it (optionally with a .sa file) so the SDK bridge can then Connect() to it.

This intentionally does NOT use psutil (extra dependency). Process enumeration
is done with the Win32 Toolhelp32 snapshot via ctypes.
"""

from __future__ import annotations

import ctypes
import glob
import os
import subprocess
import time
from ctypes import wintypes

# The GUI executable. Note the space in the name. This is distinct from
# SpatialAnalyzerSDK.exe, which is the out-of-process COM engine spawned by
# Dispatch(SA_PROG_ID) inside sa_sdk.py.
SA_GUI_EXE_NAME = "Spatial Analyzer.exe"

# Where NRK installs SA. Globbed so we pick up any installed version.
_INSTALL_ROOTS = (
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 "New River Kinematics"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "New River Kinematics"),
)

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
BM_CLICK = 0x00F5
# Standard Windows dialog / message-box window class. SA's blocking error
# boxes ("Object Not Found", subscription nags, ...) are instances of it.
DIALOG_CLASS = "#32770"

# The out-of-process COM engine (spawned by Dispatch() in sa_sdk.py). It shows
# no interactive UI except its own modal error dialogs (e.g. the "NRK
# socketinterface 10061" box of an engine born before the GUI listener), which
# block just like the GUI's dialogs, so dismissal targets it too.
SA_ENGINE_EXE_NAME = "SpatialAnalyzerSDK.exe"


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _enum_processes() -> list[tuple[int, str]]:
    """Return (pid, exe-name) pairs of all running processes."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return []
    procs: list[tuple[int, str]] = []
    pe = _PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    try:
        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                procs.append((int(pe.th32ProcessID), pe.szExeFile))
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return procs


def _enum_process_names() -> list[str]:
    """Return exe names (as printed by the OS) of all running processes."""
    return [name for _, name in _enum_processes()]


def find_sa_exe() -> str | None:
    """Locate the SA GUI executable under the standard NRK install dirs.

    Returns the first match (newest-ish by glob ordering) or None.
    """
    for root in _INSTALL_ROOTS:
        if not root or not os.path.isdir(root):
            continue
        pattern = os.path.join(root, "SpatialAnalyzer*", SA_GUI_EXE_NAME)
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    return None


def is_sa_running() -> bool:
    """True if a process named 'Spatial Analyzer.exe' is running."""
    return bool(sa_gui_pids())


def _pids_by_exe(exe_name: str) -> list[int]:
    """PIDs of every running process whose exe name equals `exe_name`."""
    target = exe_name.lower()
    return [pid for pid, name in _enum_processes() if name.lower() == target]


def sa_gui_pids() -> list[int]:
    """PIDs of every running 'Spatial Analyzer.exe' process."""
    return _pids_by_exe(SA_GUI_EXE_NAME)


def sa_engine_pids() -> list[int]:
    """PIDs of every running 'SpatialAnalyzerSDK.exe' process."""
    return _pids_by_exe(SA_ENGINE_EXE_NAME)


def dialog_target_pids(include_engine: bool = True) -> list[int]:
    """PIDs whose modal dialogs can block SA automation: the SA GUI process
    (any running instance) plus, optionally, the SDK engine process."""
    pids = set(sa_gui_pids())
    if include_engine:
        pids.update(sa_engine_pids())
    return sorted(pids)


def sa_has_visible_window(pid: int | None = None) -> bool:
    """True if the SA GUI has at least one visible top-level window.

    A freshly launched SA can linger as a BARE STUCK process: no window, no
    SDK listener, ~30 MB idle (seen live, usually after a bad previous
    shutdown or a license prompt). Spawning an SDK engine against such an
    instance pops the modal "NRK socketinterface 10061" dialog, so engine
    birth must be gated on this check. EnumWindows via ctypes, no COM, no
    psutil - cheap enough to poll. With pid=None matches any SA process.
    """
    wanted = {pid} if pid is not None else set(sa_gui_pids())
    if not wanted:
        return False
    user32 = ctypes.windll.user32
    found: list = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):  # noqa: ANN001 - ctypes callback signature
        if user32.IsWindowVisible(hwnd):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value in wanted:
                found.append(hwnd)
                return False  # stop early
        return True

    user32.EnumWindows(_cb, 0)
    return bool(found)


def sa_main_window_title(pid: int | None = None) -> str:
    """Title of the SA GUI's main window ('' if none can be found).

    Used to detect which job file SA currently has open: when the caption
    carries the file name (e.g. "SpatialAnalyzer - C:\\job\\file.xit" or a
    bare "... - file.xit"), automation can tell that the requested file is
    already loaded and skip the discard-and-reload. The main window is taken
    as the largest visible top-level window of the SA GUI process(es) that is
    not a #32770 dialog and has a non-empty title. Pure user32, no COM.
    """
    wanted = {pid} if pid is not None else set(sa_gui_pids())
    if not wanted:
        return ""
    user32 = ctypes.windll.user32
    best = ["", -1]  # (title, window area in px^2)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):  # noqa: ANN001 - ctypes callback signature
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value not in wanted or not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, cls, 128)
        if cls.value == DIALOG_CLASS:  # a dialog, not the main frame
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        if not title.value:
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        area = (rect.right - rect.left) * (rect.bottom - rect.top)
        if area > best[1]:
            best[0], best[1] = title.value, area
        return True

    user32.EnumWindows(_cb, 0)
    return best[0]


def kill_sa(kill_engine: bool = True) -> dict:
    """Force-terminate the SA GUI process(es) and, optionally, the SDK engine.

    Last-resort recovery for a wedged SA that cannot be driven (SDK listener
    dead, every MP step times out): kill the GUI and any leaked engine, then
    relaunch with the target file. Force-kill discards unsaved in-memory job
    state, so callers should save the current job first when its file is
    known. taskkill via subprocess, no COM.

    Returns:
        {"killed": [{pid, ok, detail}], "still_running": [pid, ...], "error"?}
    """
    targets = sa_gui_pids()
    if kill_engine:
        targets += sa_engine_pids()
    if not targets:
        return {"killed": [], "still_running": [], "error": None}
    killed = []
    for pid in targets:
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=30,
            )
            killed.append({"pid": pid, "ok": r.returncode == 0,
                           "detail": (r.stdout or r.stderr or "").strip()})
        except Exception as exc:  # noqa: BLE001 - one failure must not stop us
            killed.append({"pid": pid, "ok": False, "detail": str(exc)})
    deadline = time.time() + 15
    while time.time() < deadline:  # let the OS reap the processes
        if not sa_gui_pids() and not (kill_engine and sa_engine_pids()):
            break
        time.sleep(0.5)
    still = sa_gui_pids() + (sa_engine_pids() if kill_engine else [])
    return {"killed": killed, "still_running": still, "error": None}


def _top_windows(pids: set[int]) -> list[tuple[int, str, str]]:
    """(hwnd, class-name, title) of every top-level window owned by `pids`."""
    user32 = ctypes.windll.user32
    out: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):  # noqa: ANN001 - ctypes callback signature
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids:
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, cls, 128)
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            out.append((int(hwnd), cls.value, title.value))
        return True

    user32.EnumWindows(_cb, 0)
    return out


_CANCEL_BUTTONS = {"cancel", "отмена", "no", "нет"}
_ACCEPT_BUTTONS = {"ok", "ок", "yes", "да", "close", "закрыть", "retry",
                   "повторить"}


def _child_buttons(dialog_hwnd: int) -> list[tuple[int, str]]:
    """(button-hwnd, text) of every 'Button' child control of `dialog_hwnd`."""
    user32 = ctypes.windll.user32
    out: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(child, _lparam):  # noqa: ANN001 - ctypes callback signature
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(child, cls, 64)
        if cls.value == "Button":
            text = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(child, text, 256)
            out.append((int(child), text.value))
        return True

    user32.EnumChildWindows(dialog_hwnd, _cb, 0)
    return out


def _click_safe_button(dialog_hwnd: int) -> bool:
    """Click the button that dismisses `dialog_hwnd` without extra damage.

    Preference: Cancel/No/Нет (a plain dismissal) → the only button → an
    OK/Yes/Да/Retry button → any button. A plain MB_OK error box ("Object Not
    Found") has no Cancel and no close button, so WM_CLOSE alone never ends it
    — clicking its only OK button is what a human does. BM_CLICK is posted
    (never SendMessage: if the dialog's thread is wedged, a synchronous send
    would block the caller — the watchdog must never block). Returns True if a
    click was posted.
    """
    buttons = _child_buttons(dialog_hwnd)
    if not buttons:
        return False
    by_priority: dict[int, list[tuple[int, str]]] = {}
    for hwnd, text in buttons:
        t = text.strip().lower()
        if t in _CANCEL_BUTTONS:
            by_priority.setdefault(0, []).append((hwnd, text))
        elif t in _ACCEPT_BUTTONS:
            by_priority.setdefault(1, []).append((hwnd, text))
        else:
            by_priority.setdefault(2, []).append((hwnd, text))
    if 0 in by_priority:
        pick = by_priority[0][0]
    elif len(buttons) == 1:
        pick = buttons[0][0]
    elif 1 in by_priority:
        pick = by_priority[1][0]
    elif 2 in by_priority:
        pick = by_priority[2][0]
    else:
        return False
    ctypes.windll.user32.PostMessageW(pick, BM_CLICK, 0, 0)
    return True


def dismiss_sa_dialogs(
    pids: set[int] | list[int] | None = None,
    include_engine: bool = True,
    classes: tuple[str, ...] = (DIALOG_CLASS,),
    title_contains: str | None = None,
    max_close: int = 0,
    escalate: bool = True,
    require_action: bool = True,
    settle: float = 0.08,
) -> dict:
    """Close SA-owned modal dialogs that block automation, without COM.

    SA pops a modal dialog mid-step (an "Object Not Found" error box, a
    subscription nag, ...) that blocks the MP step until a human clicks it
    away — every MCP call then looks hung. Each matching top-level window gets
    WM_CLOSE (== Cancel/X); survivors (plain MB_OK boxes have no close button)
    get a button click via `_click_safe_button`, then a WM_COMMAND IDCANCEL as
    a last resort. Pure user32, so it is safe to call from any thread while a
    COM call is stuck. Works on the GUI and (optionally) the SDK engine.

    Args:
        pids: Process IDs to scan. None = every SA GUI + engine process.
        include_engine: Also scan SpatialAnalyzerSDK.exe (its own 10061 modal).
        classes: Window classes to treat as dismissible dialogs.
        title_contains: Only close dialogs whose title contains this text
                        (case-insensitive). None = all matching classes.
        max_close: Close at most this many dialogs (0 = unlimited).
        escalate: Try button clicks / IDCANCEL on dialogs that survive WM_CLOSE.
        require_action: Only treat windows that ask the user something as
                        dialogs (visible, or carrying a Button child). The SDK
                        engine keeps an INVISIBLE #32770 splash/about window
                        (only Static children) — not a blocker, and closing it
                        is pointless noise. The startup path passes False so
                        even hidden launch-blocking nags are dismissed.
        settle: Seconds to wait between close attempts.

    Returns:
        {"closed": [{"class_name", "title", "method"}], "still_open": [...],
         "scanned_pids": [...], "error": None}
    """
    try:
        if pids is None:
            targets = dialog_target_pids(include_engine=include_engine)
        else:
            targets = sorted({int(p) for p in pids})
        if not targets:
            return {"closed": [], "still_open": [], "scanned_pids": [],
                    "error": None}
        user32 = ctypes.windll.user32

        def _wanted():
            out = []
            for hwnd, cls, title in _top_windows(set(targets)):
                if cls not in classes:
                    continue
                if (title_contains is not None
                        and title_contains.lower() not in title.lower()):
                    continue
                if require_action and not (
                        user32.IsWindowVisible(hwnd)
                        or _child_buttons(hwnd)):
                    # Invisible window with no buttons: not asking for input
                    # (the engine's hidden splash/about dialog), skip.
                    continue
                out.append((hwnd, cls, title))
            return out

        closed: list[dict] = []
        seen: set[int] = set()

        def _record(hwnd: int, cls: str, title: str, how: str) -> None:
            if hwnd in seen:
                return
            seen.add(hwnd)
            closed.append({"class_name": cls, "title": title, "method": how})

        windows = _wanted()
        if max_close and max_close > 0:
            windows = windows[:max_close]
        for hwnd, cls, title in windows:
            _record(hwnd, cls, title, "WM_CLOSE")
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

        if escalate and windows:
            for _ in range(3):
                time.sleep(settle)
                survivors = _wanted()
                if not survivors:
                    break
                for hwnd, cls, title in survivors:
                    _record(hwnd, cls, title, "button-click")
                    if not _click_safe_button(hwnd):
                        # No clickable buttons: Esc/Cancel semantics.
                        user32.PostMessageW(hwnd, WM_COMMAND, 2, 0)

        still_open = [{"class_name": w[1], "title": w[2]} for w in _wanted()]
        return {"closed": closed, "still_open": still_open,
                "scanned_pids": targets, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"closed": [], "still_open": [], "scanned_pids": [],
                "error": str(exc)}


def _dismiss_modal_dialogs(pid: int | None = None) -> list[str]:
    """Close modal (#32770) dialogs that block the SA GUI's startup.

    SA 2015 can park its launch behind a dialog such as "SpatialAnalyzer
    Maintenance and Support Subscription": no main window appears and the SDK
    listener never binds, so no engine may be born against it. Called ONLY
    while waiting for an instance this module launched (launch_sa /
    sa_wait_for_window), never on the user's own running instance mid-work.
    WM_CLOSE equals clicking the dialog's Cancel/X. Returns dialog titles.
    """
    res = dismiss_sa_dialogs(
        pids=({pid} if pid is not None else None),
        include_engine=False,  # no engine exists during startup
        escalate=False,
        require_action=False,  # also close hidden launch-blocking nags
    )
    return [d["title"] for d in res["closed"]]


def sa_wait_for_window(timeout: float, pid: int | None = None,
                       poll_interval: float = 1.0) -> bool:
    """Poll until a visible SA GUI window appears (or timeout elapses).

    If `pid` is given but that instance exited (single-instance handoff to a
    concurrently started SA), any other SA instance with a visible window
    counts as success. While polling, modal startup dialogs on the tracked
    instance are closed automatically so the main window can appear.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pid is not None:
            _dismiss_modal_dialogs(pid)
        if sa_has_visible_window(pid):
            if pid is not None:
                # A nag can still be up behind the main frame; close it too so
                # the MP thread / SDK listener are not blocked afterwards.
                _dismiss_modal_dialogs(pid)
            return True
        if (pid is not None and pid not in sa_gui_pids()
                and sa_has_visible_window()):
            return True
        time.sleep(poll_interval)
    return False


def launch_sa(
    file_path: str | None = None,
    exe_path: str | None = None,
    timeout: float = 60.0,
    poll_interval: float = 1.0,
) -> dict:
    """Start the SA GUI (optionally opening a .sa file) and wait for it.

    Launch is detached (CREATE_NEW_PROCESS_GROUP) so SA keeps running after the
    MCP server exits. We then poll until 'Spatial Analyzer.exe' appears in the
    process list (or until timeout). Modal startup dialogs on the launched
    instance (e.g. "Maintenance and Support Subscription") are closed
    automatically so SA does not park behind them.

    Args:
        file_path: Optional .sa file to open. Passed as a command-line arg.
        exe_path: Override the discovered exe path.
        timeout: Seconds to wait for the process to appear.
        poll_interval: Seconds between process-list polls.

    Returns:
        {launched, already_running, exe, file, pid?, appeared, error?}
    """
    result = {
        "launched": False,
        "already_running": False,
        "exe": None,
        "file": file_path,
        "appeared": False,
        "pid": None,
        "error": None,
    }

    if is_sa_running():
        result["already_running"] = True
        result["appeared"] = True
        return result

    exe = exe_path or find_sa_exe()
    if not exe or not os.path.isfile(exe):
        result["error"] = (
            "Could not find the SA GUI executable ('Spatial Analyzer.exe'). "
            "Pass exe_path explicitly."
        )
        return result
    result["exe"] = exe

    if file_path:
        file_path = os.path.abspath(file_path)
        if not os.path.isfile(file_path):
            result["error"] = f"File not found: {file_path}"
            return result

    cmd = [exe]
    if file_path:
        cmd.append(file_path)

    # DETACHED_PROCESS (0x00000008) | CREATE_NEW_PROCESS_GROUP (0x00000200):
    # SA runs independently of this server's lifetime / console. wShowWindow is
    # set explicitly to SW_SHOWNORMAL (1): with STARTF_USESHOWWINDOW the OS
    # default is SW_HIDE, which can leave SA's startup dialogs (and window)
    # invisible while still blocking.
    flags = 0x00000008 | 0x00000200
    dismissed: list[str] = []
    try:
        creationflags = flags
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL
        proc = subprocess.Popen(
            cmd,
            creationflags=creationflags,
            startupinfo=startupinfo,
            close_fds=True,
        )
        result["pid"] = proc.pid
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"Failed to launch SA: {exc}"
        return result

    # Wait for the GUI process to register in the process list. The exe we
    # spawned may present under 'Spatial Analyzer.exe' (with space). SA was
    # NOT running before this launch (checked above), so any SA process now is
    # ours: close its modal startup dialogs (subscription nags, ...) so the
    # main window can appear instead of SA parking behind them.
    deadline = time.time() + timeout
    while time.time() < deadline:
        for p in sa_gui_pids():
            dismissed += _dismiss_modal_dialogs(p)
        if is_sa_running():
            result["appeared"] = True
            result["launched"] = True
            for p in sa_gui_pids():
                dismissed += _dismiss_modal_dialogs(p)
            result["dismissed_dialogs"] = dismissed
            return result
        time.sleep(poll_interval)

    result["dismissed_dialogs"] = dismissed
    result["error"] = (
        f"SA was launched (pid={result['pid']}) but did not appear in the "
        f"process list within {timeout}s. It may still be starting."
    )
    return result


def ensure_sa_running(
    file_path: str | None = None,
    exe_path: str | None = None,
    timeout: float = 60.0,
) -> dict:
    """Convenience: return running state; launch if needed."""
    if is_sa_running():
        return {
            "running": True,
            "already_running": True,
            "launched": False,
            "error": None,
        }
    res = launch_sa(file_path=file_path, exe_path=exe_path, timeout=timeout)
    res["running"] = res.get("appeared", False)
    return res
