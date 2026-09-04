"""Offline self-test for sa_app dialog dismissal (no SA, no COM).

Pops real MessageBoxes owned by THIS process on a helper thread and checks
that sa_app.dismiss_sa_dialogs closes them (WM_CLOSE and/or button click),
exactly the path the MCP watchdog uses on SA's blocking modals. A MessageBox
is a real #32770 dialog, so this exercises the same Win32 code.

Run: python _t_dialogs.py
"""

from __future__ import annotations

import ctypes
import os
import threading
import time

import sa_app

# (MessageBox style, caption, WM_COMMAND id that force-closes it as cleanup).
# MB_OK (0, no Cancel: WM_CLOSE works), MB_OKCANCEL (1), MB_YESNO (4, no
# Cancel: only a button click / WM_COMMAND IDYES|IDNO ends it).
CASES = [(0, "SA MCP test MB_OK", 1),
         (1, "SA MCP test MB_OKCANCEL", 2),
         (4, "SA MCP test MB_YESNO", 7)]


def _pop_box(flags: int, title: str, result: list) -> None:
    result.append(ctypes.windll.user32.MessageBoxW(0, "dialog self-test",
                                                   title, flags))


def run_case(flags: int, title: str, cleanup_id: int) -> bool:
    pid = os.getpid()
    result: list = []
    t = threading.Thread(target=_pop_box, args=(flags, title, result),
                         daemon=True)
    t.start()
    deadline = time.time() + 5.0
    while time.time() < deadline:
        dialogs = [w for w in sa_app._top_windows({pid})
                   if w[1] == "#32770" and w[2] == title]
        if dialogs:
            break
        time.sleep(0.05)
    if not dialogs:
        print(f"[{title}] FAIL: dialog never appeared")
        return False
    methods: set[str] = set()
    while time.time() < deadline and t.is_alive():
        res = sa_app.dismiss_sa_dialogs(pids=[pid], include_engine=False)
        methods.update(c["method"] for c in res.get("closed", []))
        time.sleep(0.1)
    still_open = []
    if t.is_alive():
        # Cleanup so the process can exit: force the dialog's default button.
        for hwnd, cls, cap in sa_app._top_windows({pid}):
            if cls == "#32770" and cap == title:
                ctypes.windll.user32.SendMessageW(hwnd, sa_app.WM_COMMAND,
                                                  cleanup_id, 0)
        still_open = [c["title"] for c in
                      sa_app.dismiss_sa_dialogs(
                          pids=[pid], include_engine=False)["still_open"]]
    closed = not t.is_alive()
    print(f"[{title}] closed={closed} methods={sorted(methods)} "
          f"still_open={still_open} box_returned={bool(result)}")
    return closed and not still_open


def main() -> int:
    ok = True
    for flags, title, cleanup_id in CASES:
        try:
            ok = run_case(flags, title, cleanup_id) and ok
        except Exception as exc:  # noqa: BLE001
            print(f"[{title}] ERROR: {exc}")
            ok = False
        time.sleep(0.3)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
