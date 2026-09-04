# -*- coding: utf-8 -*-
"""Final live check: side semantics of sa_fit_fixed on a REAL fixture group.

Cold-starts SA (must be closed), opens the fixture, fits group 'т контур'
free (no compensation), then re-fits at the true-surface radius candidates
Rf +/- probe with compensation outside/inside: the physically correct side
must come back with an RMS close to the free fit, the wrong side with
~2x probe. Single process, nag watchdog on, cleans up after itself.
"""
import os
import sys
import threading
import time

import sa_app
import sa_sdk
import server

_orig = sa_sdk.SABridge._submit


def _fast(self, func, *args, timeout=60.0, **kwargs):
    return _orig(self, func, *args, timeout=12.0, **kwargs)


sa_sdk.SABridge._submit = _fast
PROBE = 19.05
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "6.01.25 — обработка.xit")


def t(msg):
    print(f"[{time.monotonic():8.1f}] {msg}", flush=True)


def dismiss(pid=None):
    try:
        return sa_app._dismiss_modal_dialogs(pid) or []
    except Exception:  # noqa: BLE001
        return []


def main():
    t("ensure SA (cold)")
    run = server.sa_ensure_running()
    t(f"connected={run.get('connected')} launched={run.get('launched')} "
      f"err={run.get('error')}")
    if not run.get("connected"):
        t("ABORT: no bridge")
        sys.exit(1)
    pids = sa_app.sa_gui_pids()
    pid = pids[0] if pids else None
    seen = set()
    stop = threading.Event()

    def watchdog():
        while not stop.is_set():
            for x in dismiss(pid):
                if x not in seen:
                    seen.add(x)
                    t(f"NAG dismissed: '{x}'")
            stop.wait(2.0)

    threading.Thread(target=watchdog, daemon=True).start()
    if run.get("launched"):
        t("wait 45 s for delayed-startup dialogs")
        end = time.monotonic() + 45
        while time.monotonic() < end:
            time.sleep(2.0)
            dismiss(pid)
    t("open fixture")
    op = server.sa_open_file(FIXTURE)
    t(f"open: {op.get('opened')} err={op.get('error')}")
    if not op.get("opened"):
        sys.exit(1)

    def fit(radius_mm, comp, name):
        r = server.sa_fit_fixed("cylinder", name, radius_mm=radius_mm,
                                point_group="т контур", collection="A",
                                compensation=comp, include_deviations=True)
        s = r.get("stats") or {}
        print(f"    {comp:8s} R_read={r.get('radius')} "
              f"rms={s.get('rms_deviation')} note={r.get('note')} "
              f"err={r.get('error')}", flush=True)
        return r

    t("free fit (no compensation) on 'т контур'")
    r0 = server.sa_fit_fixed("cylinder", "MCP_SIDE0", point_group="т контур",
                             collection="A", compensation="none",
                             include_deviations=True)
    s0 = r0.get("stats") or {}
    Rf = r0.get("radius")
    print(f"    free none: Rf={Rf} rms={s0.get('rms_deviation')} "
          f"err={r0.get('error')}", flush=True)
    if Rf:
        for label, Rt in (("Rf-19.05", Rf - PROBE), ("Rf+19.05", Rf + PROBE)):
            t(f"fixed at true-surface candidate {label} = {Rt:.4f}")
            fit(Rt, "outside", "MCP_SIDE1")
            fit(Rt, "inside", "MCP_SIDE2")
    t("cleanup")
    server._delete_geometry_objects("A",
                                    ["MCP_SIDE0", "MCP_SIDE1", "MCP_SIDE2"])
    stop.set()
    t("DONE")


if __name__ == "__main__":
    main()
