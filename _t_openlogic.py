"""Offline regression for the attach-first current-file detection.

No SA, no COM: exercises server._current_open_evidence / _track_open /
sa_current_file with a mocked SA main-window caption. Run: python _t_openlogic.py
"""

import os

import sa_app
import server

P = os.path.abspath(r"C:\jobs\6.01.25 — обработка.xit")
P64 = os.path.abspath(r"C:\jobs\14.02.2024.xit64")
OTHER = os.path.abspath(r"C:\jobs\other.xit")

fails = []


def check(desc, cond, extra=""):
    tag = "PASS" if cond else "FAIL"
    print(f"{tag} {desc} {extra}")
    if not cond:
        fails.append(desc)


def title(t):
    sa_app.sa_main_window_title = lambda *a, **k: t


server._track_open(None, None)

# 1. Fresh process, generic caption -> cannot confirm -> must (re)open.
title("SpatialAnalyzer")
ev = server._current_open_evidence(P)
check("fresh process + generic caption -> not loaded",
      ev["loaded"] is False and ev["how"] is None, str(ev))

# 2. This session opened the file, generic caption -> attach via tracker.
server._track_open(P, "Open SA File")
title("SpatialAnalyzer")
ev = server._current_open_evidence(P)
check("tracked same + generic caption -> attach via 'tracked'",
      ev["loaded"] and ev["how"] == "tracked", str(ev))

# 3. Caption names the requested file (manual open / earlier session).
server._track_open(None, None)
title(f"SpatialAnalyzer - {os.path.basename(P)}")
ev = server._current_open_evidence(P)
check("caption names requested file -> attach via 'window-title'",
      ev["loaded"] and ev["how"] == "window-title", str(ev))

# 4. Full path in the caption.
title(f"SpatialAnalyzer - {P}")
ev = server._current_open_evidence(P)
check("caption carries full path -> attach via 'window-title'",
      ev["loaded"] and ev["how"] == "window-title", str(ev))

# 5. .xit64 caption.
title(f"SpatialAnalyzer - {os.path.basename(P64)}")
ev = server._current_open_evidence(P64)
check("xit64 caption -> attach", ev["loaded"] and ev["how"] == "window-title",
      str(ev))

# 6. Tracker says requested, but the caption names ANOTHER file: the job was
#    switched behind us -> tracker is stale, must NOT attach.
server._track_open(P, "Open SA File")
title(f"SpatialAnalyzer - {os.path.basename(OTHER)}")
ev = server._current_open_evidence(P)
check("tracker stale (caption names other file) -> not loaded",
      ev["loaded"] is False and ev["how"] == "tracked-stale", str(ev))

# 7. sa_current_file picks the caption token over a stale tracker.
server._track_open(P, "Open SA File")
title(f"SpatialAnalyzer - {os.path.basename(P64)}")
cur = server.sa_current_file()
check("sa_current_file prefers the caption token",
      cur["current_file"] == os.path.basename(P64),
      f"current={cur['current_file']!r}")
check("sa_current_file still reports the tracked file",
      cur["tracked_file"] == server._normalize_job_path(P))

# 8. Case-insensitivity (upper-case extension / drive in the caption).
title("SPATIALANALYZER - C:\\JOBS\\14.02.2024.XIT64")
ev = server._current_open_evidence(P64)
check("caption matching is case-insensitive",
      ev["loaded"] and ev["how"] == "window-title", str(ev))

# 9. Watchdog gate: dismissals only while a COM task is in flight.
class _FakeBridge:
    def __init__(self, busy):
        self._busy = busy
    def is_busy(self):
        return self._busy

server.sa = None
check("watchdog gate: no bridge -> no scan", server._watchdog_gate() is False)
server.sa = _FakeBridge(False)
check("watchdog gate: idle bridge -> no scan", server._watchdog_gate() is False)
server.sa = _FakeBridge(True)
check("watchdog gate: busy bridge -> scan", server._watchdog_gate() is True)

print("\nALL-OK" if not fails else f"\nFAILED: {len(fails)}")
raise SystemExit(1 if fails else 0)
