# -*- coding: utf-8 -*-
"""Live end-to-end check of sa_project_points (closest-point projection).

Confirms the Query-engine projection mechanism against a real SA 2015
instance: 'Query Points to Objects' + Projection Options output "Points on
Object" must create a NEW point group holding the closest points of the
source points on the target object(s).

Key live finding (2026-09-07): the step STATUS is not a reliable success
signal on SA 2015 - identical inputs return DoneSuccess or DoneFatalError
across sessions while creating the same points either way, and points SA
cannot project are silently SKIPPED. The tool therefore decides success by
what the result group actually contains; this harness checks GEOMETRY of the
created points (they must lie exactly on the object) and tolerates a partial
result only on the real measured group.

Test plan (all objects are created under MCP_PROJ_* names in collection A of
the fixture and deleted at the end):
  1. plane: 9 source points 25 mm above a fitted plane -> projected z ~ 0,
     x/y preserved.
  2. same plane, projection_type "Points on Offset Object",
     probe_offset_mm=10 -> created points 10 mm off the surface (the GUI
     Probe Offset / back-away semantics).
  3. cylinder r=400: 5 source points at r=430 -> projected radial ~ 400.
  4. explicit-points path (no point_groups).
  5. auto result_group name (single group, no result_group given).
  6. re-run into the same result_group -> old group replaced, still correct.
  7. real measured group "т контур" (376 pts, stored radial offset 19.05):
     fit the cylinder free, project 5 points onto it -> every created point
     lies ON the fitted cylinder (radial distance to axis == fitted radius);
     SA may skip some points, that is reported, not a failure.

Run with SA closed (this cold-starts SA):  python _live_proj.py
"""
import math
import os
import sys
import threading
import time

import sa_app
import sa_sdk
import server

# Cap every COM call at 12 s so a wedged SA costs seconds, not a minute.
_orig_submit = sa_sdk.SABridge._submit


def _fast_submit(self, func, *args, timeout=60.0, **kwargs):
    return _orig_submit(self, func, *args, timeout=12.0, **kwargs)


sa_sdk.SABridge._submit = _fast_submit

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "6.01.25 — обработка.xit")
COLL = "A"
FAILURES = []

ALL_NAMES = [
    "MCP_PROJ_PLN_PTS", "MCP_PROJ_PLN", "MCP_PROJ_SRC_PLN",
    "MCP_PROJ_OUT_PLN", "MCP_PROJ_OUT_PLN_OFF", "MCP_PROJ_SRC_PLN_proj",
    "MCP_PROJ_CYL_PTS", "MCP_PROJ_CYL", "MCP_PROJ_SRC_CYL",
    "MCP_PROJ_OUT_CYL", "MCP_PROJ_OUT_PTS", "MCP_PROJ_OUT_AGAIN",
    "MCP_PROJ_FIT_CYL", "MCP_PROJ_OUT_TK",
]


def t(label):
    print(f"[{time.monotonic():8.1f}] {label}", flush=True)


def dismiss(pid=None):
    try:
        return sa_app._dismiss_modal_dialogs(pid) or []
    except Exception:  # noqa: BLE001
        return []


_seen_nags = set()


def _nag_watchdog(pid, stop):
    """Close modal dialogs on the launched SA every 2 s; log any titles."""
    while not stop.is_set():
        for x in dismiss(pid):
            if x not in _seen_nags:
                _seen_nags.add(x)
                t(f"NAG-DIALOG dismissed: '{x}'")
        stop.wait(2.0)


def settle(sec, pid=None):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        time.sleep(2.0)
        dismiss(pid)


def check(label, cond, extra=""):
    print(f"    CHECK {label}: {'PASS' if cond else 'FAIL'} {extra}",
          flush=True)
    if not cond:
        FAILURES.append(label)


def make_group(group, coords, prefix="P"):
    """Create a point group in COLL from raw coordinates."""
    return server._create_point_group(COLL, group, coords, prefix)


def read_points(group):
    r = server._read_group_points(COLL, group)
    if not r.get("ok"):
        return []
    return r["points"]


def dist_to_axis(pt, begin, axis):
    """Perpendicular distance from pt to the infinite line (begin, axis)."""
    u = [axis[0] - begin[0], axis[1] - begin[1], axis[2] - begin[2]]
    lu = math.sqrt(u[0] ** 2 + u[1] ** 2 + u[2] ** 2)
    dx = pt["x"] - begin[0]
    dy = pt["y"] - begin[1]
    dz = pt["z"] - begin[2]
    cross = [dy * u[2] - dz * u[1], dz * u[0] - dx * u[2],
             dx * u[1] - dy * u[0]]
    return math.sqrt(cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2) / lu


def cleanup(names):
    """Best-effort Delete Objects of our MCP_PROJ_* names."""
    try:
        server._delete_geometry_objects(COLL, names)
        print(f"    cleanup deleted {len(names)} objects", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"    cleanup note: {exc}", flush=True)


def main():
    t("ensure SA (cold start if needed)")
    run = server.sa_ensure_running()
    t(f"ensure: running={run.get('running')} launched={run.get('launched')} "
      f"connected={run.get('connected')} err={run.get('error')}")
    if not run.get("connected"):
        t("ABORT: bridge not connected")
        sys.exit(1)
    pids = sa_app.sa_gui_pids()
    pid = pids[0] if pids else None
    stop = threading.Event()
    if pid is not None:
        threading.Thread(target=_nag_watchdog, args=(pid, stop),
                         daemon=True).start()
    if run.get("launched"):
        t(f"wait 45 s post-launch dismissing dialogs on pid={pid}")
        settle(45, pid)
    else:
        settle(4, pid)
    t(f"nags seen: {sorted(_seen_nags) or 'none'}")

    t(f"open fixture {os.path.basename(FIXTURE)}")
    op = server.sa_open_file(FIXTURE)
    t(f"open: {op.get('opened')} status={op.get('status')} "
      f"err={op.get('error')}")
    if not op.get("opened"):
        t("ABORT: could not open fixture")
        sys.exit(1)
    settle(3, pid)

    # Idempotent: drop leftovers of a previous (partial) run, if any.
    cleanup(ALL_NAMES)

    # ---- 1) plane: vertical projection -> z=0, x/y preserved -------------
    t("T1 plane projection")
    pln_xy = [(x, y) for x in (-100.0, 0.0, 100.0)
              for y in (-80.0, 0.0, 80.0)]
    b = make_group("MCP_PROJ_PLN_PTS", [[x, y, 0.0] for x, y in pln_xy], "P")
    check("T1 plane points built", b.get("ok") and b["count"] == 9,
          str(b.get("error")))
    src = [[x, y, 25.0] for x, y in pln_xy]
    b = make_group("MCP_PROJ_SRC_PLN", src, "S")
    check("T1 source group built", b.get("ok") and b["count"] == 9,
          str(b.get("error")))
    f = server.sa_best_fit("plane", "MCP_PROJ_PLN",
                           point_group="MCP_PROJ_PLN_PTS", collection=COLL)
    check("T1 plane fit", f.get("constructed"), f.get("error"))
    p = server.sa_project_points(
        objects=["MCP_PROJ_PLN"], point_groups=["MCP_PROJ_SRC_PLN"],
        result_group="MCP_PROJ_OUT_PLN", collection=COLL)
    print(f"    -> projected={p.get('projected')} code={p.get('status_code')} "
          f"{p.get('status')} res={p.get('result_count')} of "
          f"{p.get('requested')} err={p.get('error')}", flush=True)
    check("T1 query ran", p.get("projected") and not p.get("error"),
          str(p.get("minor_error")))
    pts = read_points("MCP_PROJ_OUT_PLN")
    check("T1 result group has 9 points", len(pts) == 9,
          f"got {len(pts)}")
    if pts:
        dz = max(abs(pt["z"]) for pt in pts)
        dx = max(abs(pt["x"] - s[0]) for pt, s in zip(pts, src))
        dy = max(abs(pt["y"] - s[1]) for pt, s in zip(pts, src))
        print(f"    -> max |z|={dz:.6f} max |dx|={dx:.6f} "
              f"max |dy|={dy:.6f}", flush=True)
        check("T1 projected points ON the plane (z ~ 0)", dz < 1e-3,
              f"max|z|={dz}")
        check("T1 x/y preserved by closest-point projection",
              dx < 1e-3 and dy < 1e-3, f"dx={dx} dy={dy}")

    # ---- 2) back-away: "Points on Offset Object" + probe_offset_mm --------
    t("T2 back-away probe offset (Offset Object output, +10 mm)")
    p = server.sa_project_points(
        objects=["MCP_PROJ_PLN"], point_groups=["MCP_PROJ_SRC_PLN"],
        result_group="MCP_PROJ_OUT_PLN_OFF", collection=COLL,
        projection_type="Points on Offset Object", probe_offset_mm=10.0)
    print(f"    -> projected={p.get('projected')} code={p.get('status_code')} "
          f"res={p.get('result_count')} err={p.get('error')}", flush=True)
    check("T2 query ran", p.get("projected") and not p.get("error"))
    pts = read_points("MCP_PROJ_OUT_PLN_OFF")
    if pts:
        dz = [pt["z"] for pt in pts]
        print(f"    -> z={['%.4f' % v for v in dz]}", flush=True)
        check("T2 points backed off 10 mm from the surface",
              all(abs(abs(v) - 10.0) < 1e-2 for v in dz),
              "|z| must be ~10")

    # ---- 3) cylinder r=400: closest point at r=400 ------------------------
    t("T3 cylinder projection")
    cyl_pts = []
    for i in range(12):
        ang = 2 * math.pi * i / 12
        cyl_pts.append([400 * math.cos(ang), 400 * math.sin(ang),
                        -150.0 + i * 30.0])
    f = server.sa_best_fit("cylinder", "MCP_PROJ_CYL",
                           point_group="MCP_PROJ_CYL_PTS",
                           coordinates=cyl_pts, collection=COLL)
    check("T3 cylinder fit", f.get("constructed"), f.get("error"))
    props = server._read_geometry_props("cylinder", COLL, "MCP_PROJ_CYL")
    rfit = props["properties"].get("radius")
    begin = props["properties"].get("begin") or [0, 0, 0]
    axis = props["properties"].get("axis") or [0, 0, 1]
    print(f"    -> fitted radius={rfit}", flush=True)
    src_c = [[430 * math.cos(a), 430 * math.sin(a), z]
             for a, z in ((0.0, 0.0), (1.2, 50.0), (2.5, -40.0),
                          (3.9, 20.0), (5.1, -90.0))]
    make_group("MCP_PROJ_SRC_CYL", src_c, "S")
    p = server.sa_project_points(
        objects=["MCP_PROJ_CYL"], point_groups=["MCP_PROJ_SRC_CYL"],
        result_group="MCP_PROJ_OUT_CYL", collection=COLL)
    print(f"    -> projected={p.get('projected')} code={p.get('status_code')} "
          f"res={p.get('result_count')} of {p.get('requested')} "
          f"err={p.get('error')}", flush=True)
    check("T3 query ran", p.get("projected") and not p.get("error"))
    pts = read_points("MCP_PROJ_OUT_CYL")
    if pts and rfit:
        rads = [dist_to_axis(pt, begin, axis) for pt in pts]
        print(f"    -> radii={['%.4f' % r for r in rads]}", flush=True)
        check("T3 closest points lie ON the cylinder (r ~ 400)",
              all(abs(r - rfit) < 1e-3 for r in rads),
              f"max|r-rfit|={max(abs(r - rfit) for r in rads):.6f}")

    # ---- 4) explicit points list (no point_groups) ------------------------
    t("T4 explicit point names")
    p = server.sa_project_points(
        objects=["MCP_PROJ_PLN"],
        points=["MCP_PROJ_SRC_PLN::S1", "MCP_PROJ_SRC_PLN::S5"],
        result_group="MCP_PROJ_OUT_PTS", collection=COLL)
    print(f"    -> projected={p.get('projected')} code={p.get('status_code')} "
          f"res={p.get('result_count')} err={p.get('error')}", flush=True)
    check("T4 explicit points ran", p.get("projected") and not p.get("error"))
    pts = read_points("MCP_PROJ_OUT_PTS")
    check("T4 two points projected", len(pts) == 2, f"got {len(pts)}")
    if pts:
        check("T4 points on plane", all(abs(pt["z"]) < 1e-3 for pt in pts))

    # ---- 5) auto result_group name (single group, none given) -------------
    t("T5 auto result_group name")
    p = server.sa_project_points(
        objects=["MCP_PROJ_PLN"], point_groups=["MCP_PROJ_SRC_PLN"],
        collection=COLL)
    print(f"    -> projected={p.get('projected')} "
          f"result_group={p.get('result_group')} res={p.get('result_count')} "
          f"err={p.get('error')}", flush=True)
    check("T5 auto name ran", p.get("projected") and not p.get("error")
          and p.get("result_group") == "MCP_PROJ_SRC_PLN_proj",
          str(p.get("result_group")))

    # ---- 6) re-run into the same result group (idempotent replace) --------
    t("T6 replace existing result group")
    p2 = server.sa_project_points(
        objects=["MCP_PROJ_PLN"], point_groups=["MCP_PROJ_SRC_PLN"],
        result_group="MCP_PROJ_OUT_PLN", collection=COLL)
    print(f"    -> projected={p2.get('projected')} replaced={p2.get('replaced')} "
          f"res={p2.get('result_count')} err={p2.get('error')}", flush=True)
    check("T6 re-run replaced the old group", p2.get("projected")
          and p2.get("replaced") is True and not p2.get("error"))
    pts = read_points("MCP_PROJ_OUT_PLN")
    check("T6 re-run still produced 9 clean points", len(pts) == 9,
          f"got {len(pts)}")

    # ---- 7) real measured group onto its own free fit ---------------------
    t("T7 real group 'т контур' (376 pts) -> 5 points onto free cylinder")
    tk_pts = server._points_in_group("A::т контур")
    check("T7 group present", len(tk_pts) == 376, f"got {len(tk_pts)}")
    if len(tk_pts) >= 5:
        f = server.sa_best_fit("cylinder", "MCP_PROJ_FIT_CYL",
                               point_group="т контур", collection=COLL)
        check("T7 free cylinder fit", f.get("constructed"), f.get("error"))
        props = server._read_geometry_props("cylinder", COLL,
                                            "MCP_PROJ_FIT_CYL")
        rfit = props["properties"].get("radius")
        begin = props["properties"].get("begin") or [0, 0, 0]
        axis = props["properties"].get("axis") or [0, 0, 1]
        print(f"    -> free radius={rfit}", flush=True)
        p = server.sa_project_points(
            objects=["MCP_PROJ_FIT_CYL"],
            points=tk_pts[:5], collection=COLL,
            result_group="MCP_PROJ_OUT_TK")
        print(f"    -> projected={p.get('projected')} code={p.get('status_code')} "
              f"res={p.get('result_count')} of {p.get('requested')} "
              f"skipped={p.get('skipped_points')} err={p.get('error')}",
              flush=True)
        check("T7 query ran on real points", p.get("projected")
              and not p.get("error"), str(p.get("minor_error")))
        pts = read_points("MCP_PROJ_OUT_TK")
        if pts and rfit:
            rads = [dist_to_axis(pt, begin, axis) for pt in pts]
            print(f"    -> radii={['%.4f' % r for r in rads]} vs {rfit}",
                  flush=True)
            check("T7 created points lie ON the fitted cylinder",
                  all(abs(r - rfit) < 0.05 for r in rads),
                  f"max|r-rfit|={max(abs(r - rfit) for r in rads):.6f}")
        if pts:
            check("T7 per-point offsets of created points are 0",
                  all(abs(pt["radial_offset"]) < 1e-6 for pt in pts))

    settle(2, pid)
    cleanup(ALL_NAMES)
    stop.set()
    t("DONE")
    print("FAILED: " + "; ".join(FAILURES) if FAILURES else "ALL OK",
          flush=True)
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
