# -*- coding: utf-8 -*-
"""Live check: best-fit creation must NOT leave auto 'cardinal points' groups.

SA 2015's default geometry fit profile auto-creates a point group named
"<geometry name>Кардинальные точки" beside every best-fit geometry; there is
no fit-step argument to disable it. _purge_auto_cardinal_groups() deletes it
after each successful fit - this harness confirms no such group survives any
of the creation paths, and cleans the MCP_* harness artifacts (incl. the
stale MCP_PROJ_*Кардинальные точки groups an earlier projection harness run
left in the open job) afterwards.

IMPORTANT: SA's SDK listener serves ONE engine at a time. Run this only when
no other SDK engine is connected (i.e. BEFORE any MCP sa_* tool call in a
session, or right after the MCP server was reloaded), or the connect times
out.

Run with SA open on any job:  python _t_cardinal.py
"""
import math
import sys
import time

import sa_app
import sa_sdk
import server

_orig_submit = sa_sdk.SABridge._submit


def _fast_submit(self, func, *args, timeout=60.0, **kwargs):
    return _orig_submit(self, func, *args, timeout=12.0, **kwargs)


sa_sdk.SABridge._submit = _fast_submit

PREFIX = "MCP_CC"
ALL = []
FAILURES = []
COLL = ""


def check(label, cond, extra=""):
    print(f"  CHECK {label}: {'PASS' if cond else 'FAIL'} {extra}", flush=True)
    if not cond:
        FAILURES.append(label)


def pgs(coll):
    return server._objects_in_collection_by_type(coll, "Point Group")


def leftover_cardinals(coll, prefix):
    out = []
    for n in pgs(coll):
        local = str(n).split("::")[-1]
        if local.lower().startswith(prefix.lower()) and any(
                k in local.lower() for k in ("кардинальн", "cardinal")):
            out.append(n)
    return out


def cleanup(names):
    if not names:
        return
    try:
        server._delete_geometry_objects(COLL, names)
        print(f"  cleanup: deleted {len(names)} objects", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  cleanup note: {exc}", flush=True)


def main():
    run = server.sa_ensure_running()
    print(f"ensure: running={run.get('running')} connected={run.get('connected')} "
          f"err={run.get('error')}", flush=True)
    if not run.get("connected"):
        print("Cannot connect - is another SDK engine (e.g. the MCP bridge) "
              "already connected? Retry after reloading MCP.", flush=True)
        sys.exit(2)
    colls = server._list_collections()
    global COLL
    COLL = colls[0] if colls else ""
    print(f"collections: {colls} -> working in '{COLL}'", flush=True)
    time.sleep(1)

    # First remove the stale cardinal groups the earlier projection harness
    # left (its cleanup deleted only the geometry, not the auto cardinal
    # groups): they are MCP_PROJ_* test artifacts.
    stale = leftover_cardinals(COLL, "MCP_PROJ_")
    if stale:
        cleanup([n.split("::")[-1] for n in stale])
        print(f"  removed {len(stale)} stale MCP_PROJ_* cardinal groups",
              flush=True)

    def mk(group, coords):
        r = server._create_point_group(COLL, group, coords, "P")
        ALL.append(group)
        return r

    try:
        pln = [[x, y, 0.0] for x in (-100.0, 0.0, 100.0)
               for y in (-80.0, 0.0, 80.0)]
        circ = [[400 * math.cos(2 * math.pi * i / 12),
                 400 * math.sin(2 * math.pi * i / 12), 0.0]
                for i in range(12)]
        cyl = [[400 * math.cos(2 * math.pi * i / 12),
                400 * math.sin(2 * math.pi * i / 12), -100.0 + i * 20.0]
               for i in range(12)]

        # --- 1) sa_best_fit on an existing group (plane) -------------------
        mk(f"{PREFIX}_SRC", pln)
        f = server.sa_best_fit("plane", f"{PREFIX}_PLN",
                               point_group=f"{PREFIX}_SRC", collection=COLL)
        ALL += [f"{PREFIX}_PLN"]
        print(f"best_fit plane: constructed={f.get('constructed')} "
              f"cardinal_removed={f.get('cardinal_points_removed')} "
              f"err={f.get('error')}", flush=True)
        check("T1 best_fit(plane) constructed", f.get("constructed"),
              str(f.get("error")))
        check("T1 no cardinal group left",
              not leftover_cardinals(COLL, f"{PREFIX}_PLN"),
              str(leftover_cardinals(COLL, f"{PREFIX}_PLN")))

        # --- 2) sa_best_fit from raw coordinates (circle, temp group) ------
        f = server.sa_best_fit("circle", f"{PREFIX}_CIR", coordinates=circ,
                               point_group=f"{PREFIX}_TMP", collection=COLL)
        ALL += [f"{PREFIX}_CIR", f"{PREFIX}_TMP"]
        print(f"best_fit circle(coords): constructed={f.get('constructed')} "
              f"cardinal_removed={f.get('cardinal_points_removed')} "
              f"err={f.get('error')}", flush=True)
        check("T2 best_fit(circle) constructed", f.get("constructed"),
              str(f.get("error")))
        check("T2 no cardinal group left",
              not leftover_cardinals(COLL, f"{PREFIX}_CIR"),
              str(leftover_cardinals(COLL, f"{PREFIX}_CIR")))

        # --- 3) sa_best_fit_report (fit + quality) -------------------------
        f = server.sa_best_fit_report("cylinder", f"{PREFIX}_CYL_R",
                                      coordinates=cyl,
                                      point_group=f"{PREFIX}_TMP2",
                                      collection=COLL)
        ALL += [f"{PREFIX}_CYL_R", f"{PREFIX}_TMP2"]
        print(f"best_fit_report cylinder: constructed={f.get('constructed')} "
              f"cardinal_removed={f.get('cardinal_points_removed')} "
              f"err={f.get('error')}", flush=True)
        check("T3 best_fit_report constructed", f.get("constructed"),
              str(f.get("error")))
        check("T3 no cardinal group left",
              not leftover_cardinals(COLL, f"{PREFIX}_CYL_R"),
              str(leftover_cardinals(COLL, f"{PREFIX}_CYL_R")))

        # --- 4) sa_fit_clean, multi-pass: 8 in-plane + 1 high outlier -------
        outl = pln + [[250.0, 0.0, 25.0]]
        mk(f"{PREFIX}_SRC2", outl)
        f = server.sa_fit_clean("plane", f"{PREFIX}_PLN_CLEAN",
                                point_group=f"{PREFIX}_SRC2", collection=COLL,
                                tolerance_mm=0.5, max_iterations=3)
        ALL += [f"{PREFIX}_PLN_CLEAN"]
        npass = len(f.get("iterations") or [])
        print(f"fit_clean plane: constructed={f.get('constructed')} "
              f"passes={npass} converged={f.get('converged')} "
              f"cardinal_removed={f.get('cardinal_points_removed')} "
              f"err={f.get('error')}", flush=True)
        check("T4 fit_clean constructed", f.get("constructed"),
              str(f.get("error")))
        check("T4 fit_clean did multiple passes",
              npass >= 2, f"passes={npass}")
        check("T4 no cardinal group left",
              not leftover_cardinals(COLL, f"{PREFIX}_PLN_CLEAN"),
              str(leftover_cardinals(COLL, f"{PREFIX}_PLN_CLEAN")))

        # --- 5) sa_fit_fixed -> Construct <Type> (creates none by design) --
        f = server.sa_fit_fixed("cylinder", f"{PREFIX}_CYL_FIX",
                                radius_mm=400.0, coordinates=cyl,
                                collection=COLL)
        ALL += [f"{PREFIX}_CYL_FIX"]
        print(f"fit_fixed cylinder: ok={f.get('ok')} "
              f"err={f.get('error')}", flush=True)
        check("T5 fit_fixed ok", f.get("ok"), str(f.get("error")))
        check("T5 no cardinal group left",
              not leftover_cardinals(COLL, f"{PREFIX}_CYL_FIX"),
              str(leftover_cardinals(COLL, f"{PREFIX}_CYL_FIX")))
    finally:
        cleanup(sorted(set(ALL)))

    stale = leftover_cardinals(COLL, PREFIX)
    check("no MCP_CC cardinal groups remain", not stale, str(stale))
    print("DONE", flush=True)
    print("FAILED: " + "; ".join(FAILURES) if FAILURES else "ALL OK",
          flush=True)
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
