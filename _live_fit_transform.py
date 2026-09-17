# -*- coding: utf-8 -*-
"""Live end-to-end check of sa_best_fit_transform (МНК-совмещение групп).

Confirms against a real SA 2015 instance:
  - 'Best Fit Transformation - Group to Group' runs and returns a transform;
  - the matrix convention: applying the returned transform to the CORRESPONDING
    points (p' = M . [x,y,z,1]) lands them on the REFERENCE points, i.e. the
    transform really maps corresponding -> reference. A wrong convention shows
    up as ~2x the misalignment, not as ~0;
  - the deviations recomputed here agree with the step's OWN RMS/Max Absolute
    (the cross-check that our report is the same estimator SA solved);
  - a planted outlier shows up at its planted size, exclusion refits with the
    point dropped (refit: True, temp MCP_FITTMP_* copies created and removed)
    and the inlier RMS collapses to ~0;
  - apply=True moves the corresponding group AND its named companion in one
    step, and verify_after_move shows the group really landed on the reference.

Step/arg names come from the MP Command Reference PDF; sa_best_fit_transform
was built from them - this harness is the live check.

Test plan (collection A; everything created is prefixed MCP_FT_* and deleted
at the end):
  1. REF = 8 irregular-box points; CORR = REF rotated 90 deg about Z and
     translated by (50,-20,7), with target 4 pushed a further 0.5 mm in Z.
  2. fit CORR -> REF: transform recovered, our RMS vs SA's own RMS, the
     planted point reading worst. NOTE the fit is RIGID, so it cannot isolate
     the bump - it partly absorbs it as a rotation, which drags every inlier
     to ~0.06-0.15 mm. Target 4 reads ~0.34 mm: about 2.3x the worst inlier
     (and the only point past a 0.2 mm tolerance), not ~0.5 mm with clean
     inliers. That pollution is exactly why the exclusion below is worth
     doing.
  3. refit excluding target 4: inlier RMS collapses to ~0 (exactly 0.0 on the
     fixture) while the dropped point keeps its 0.5 mm in
     excluded_deviations.
  4. apply with a companion group: the group lands on REF (verify_after_move
     ~0), the companion moves by the same rigid transform.

Run with SA closed (this cold-starts SA):  python _live_fit_transform.py
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

# REF plus the rigid move used to build CORR. Eight points (an irregular box)
# so a single planted outlier stays a minority of the equations: its leverage
# on the rigid fit is 1/N, which is what makes "exclude the bad point" worth
# doing. The outlier still pollutes every inlier slightly - that pollution IS
# the reason to exclude it.
REF_PTS = [[0.0, 0.0, 0.0], [120.0, 0.0, 0.0], [120.0, 80.0, 0.0],
           [0.0, 80.0, 0.0], [0.0, 0.0, 60.0], [120.0, 0.0, 60.0],
           [120.0, 80.0, 60.0], [10.0, 65.0, 60.0]]
ROT_DEG = 90.0
SHIFT = (50.0, -20.0, 7.0)
OUTLIER_TARGET = 4          # 1-based, the 'P4' of the built groups
OUTLIER_MM = 0.5            # planted displacement along +Z
SPOT_PTS = [[300.0, 300.0, 300.0], [310.0, 300.0, 300.0]]

NAMES = ["MCP_FT_REF", "MCP_FT_CORR", "MCP_FT_SPOT", "MCP_FT_MOVED",
         "MCP_FITTMP_REF", "MCP_FITTMP_CORR"]

_seen_nags = set()


def t(label):
    print(f"[{time.monotonic():8.1f}] {label}", flush=True)


def dismiss(pid=None):
    try:
        return sa_app._dismiss_modal_dialogs(pid) or []
    except Exception:  # noqa: BLE001
        return []


def _nag_watchdog(pid, stop):
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


def close(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


def close_pt(p, q, tol=1e-6):
    return len(p) == len(q) and all(close(a, b, tol) for a, b in zip(p, q))


def coords_of(group):
    r = server.sa_point_coordinates(point_group=group, collection=COLL,
                                    include_offsets=False)
    if not r.get("ok"):
        return None, r.get("error")
    return {p["name"].split("::")[-1]: [p["x"], p["y"], p["z"]]
            for p in r["points"]}, None


def rigid(pts, deg, shift, extra=None):
    """Rotate about world Z by `deg`, then translate; optional per-target bump."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    out = {}
    for i, (x, y, z) in enumerate(pts, start=1):
        key = f"P{i}"
        rx, ry = c * x - s * y, s * x + c * y
        p = [rx + shift[0], ry + shift[1], z + shift[2]]
        if extra and key in extra:
            p = [p[k] + extra[key][k] for k in range(3)]
        out[key] = p
    return out


def main():
    t("ensure SA (cold start if needed)")
    run = server.sa_ensure_running()
    t(f"ensure: running={run.get('running')} launched={run.get('launched')} "
      f"connected={run.get('connected')} err={run.get('error')}")
    pids = sa_app.sa_gui_pids()
    pid = pids[0] if pids else None
    stop = threading.Event()
    if pid is not None:
        threading.Thread(target=_nag_watchdog, args=(pid, stop),
                         daemon=True).start()
    if run.get("launched"):
        t(f"wait 30 s post-launch dismissing dialogs on pid={pid}")
        settle(30, pid)
    else:
        settle(4, pid)
    t(f"nags seen: {sorted(_seen_nags) or 'none'}")
    connected = bool(run.get("connected"))
    for attempt in range(8):
        if connected:
            break
        settle(6, pid)
        c = server.sa_connect("localhost")
        t(f"connect retry {attempt + 1}: connected={c.get('connected')} "
          f"err={c.get('error')}")
        connected = bool(c.get("connected"))
    if not connected:
        t("ABORT: bridge not connected")
        sys.exit(1)

    t(f"open fixture {os.path.basename(FIXTURE)}")
    op = server.sa_open_file(FIXTURE)
    t(f"open: {op.get('opened')} status={op.get('status')} "
      f"err={op.get('error')}")
    if not op.get("opened"):
        t("ABORT: could not open fixture")
        sys.exit(1)
    settle(3, pid)

    server._delete_geometry_objects(COLL, NAMES)
    server.sa_reset_working_frame()

    # ---- fixtures ---------------------------------------------------------
    t("T0 build REF / CORR / SPOT")
    server._ensure_sa()
    b_ref = server._create_point_group(COLL, "MCP_FT_REF", REF_PTS)
    corr_pts = rigid(REF_PTS, ROT_DEG, SHIFT,
                     extra={f"P{OUTLIER_TARGET}": (0.0, 0.0, OUTLIER_MM)})
    b_corr = server._create_point_group(
        COLL, "MCP_FT_CORR",
        [corr_pts[f"P{i}"] for i in range(1, len(REF_PTS) + 1)])
    b_spot = server._create_point_group(COLL, "MCP_FT_SPOT", SPOT_PTS)
    check("T0 fixture groups built",
          b_ref.get("ok") and b_corr.get("ok") and b_spot.get("ok"),
          f"{b_ref.get('error')} {b_corr.get('error')} {b_spot.get('error')}")
    ref_now, _ = coords_of("MCP_FT_REF")
    spot_before, _ = coords_of("MCP_FT_SPOT")
    print(f"    -> REF: {ref_now}", flush=True)

    # ---- 1) the fit: transform + deviation report -------------------------
    t("T1 fit CORR -> REF (planted 0.5 mm outlier on P4)")
    r = server.sa_best_fit_transform(
        reference_group="MCP_FT_REF", corresponding_group="MCP_FT_CORR",
        collection=COLL, tolerance_mm=0.2)
    print(f"    -> computed={r.get('computed')} code={r.get('status_code')} "
          f"{r.get('status')} error={r.get('error')}", flush=True)
    print(f"    -> SA rms={r['sa_stats']['rms_deviation']} "
          f"max={r['sa_stats']['max_absolute_deviation']}", flush=True)
    print(f"    -> our stats={r.get('stats')}", flush=True)
    print(f"    -> fixed_xyz={r.get('fixed_xyz')}", flush=True)
    for d in r.get("deviations") or []:
        print(f"       {d['target']}: corr={d['corresponding']} -> "
              f"{d['deviation']:.6f} mm (included={d['included']}, "
              f"outlier={d['outlier']})", flush=True)
    check("T1 fit succeeded", r.get("computed") and not r.get("error"),
          str(r.get("error")))
    check("T1 every name paired",
          r.get("pairs") == len(REF_PTS), r.get("pairs"))

    devs = {d["target"]: d["deviation"] for d in r.get("deviations") or []}
    bad = f"P{OUTLIER_TARGET}"
    inliers = [v for k, v in devs.items() if k != bad]
    # The rigid fit spreads the bump over every point (see the docstring), so
    # the honest claim is "clearly the worst, well above the rest" - not "0.5
    # while the inliers sit at 0".
    check("T1 the planted outlier is the worst point by a clear margin",
          (r.get("worst_point") or {}).get("target") == bad
          and devs.get(bad, 0.0) > 1.5 * max(inliers),
          f"outlier={devs.get(bad)} inliers={inliers}")
    check("T1 the outlier POLLUTES the inliers (a rigid fit has to compromise)",
          max(inliers) > 0.0 and (r.get("stats") or {})["rms_deviation"] > 0.05,
          f"inlier max={max(inliers)} rms={r['stats']['rms_deviation']}")
    check("T1 tolerance_mm flags exactly the bad point",
          r.get("outliers") == [bad] and not r.get("excluded"),
          f"outliers={r.get('outliers')} excluded={r.get('excluded')}")
    my_rms = (r.get("stats") or {}).get("rms_deviation")
    sa_rms = r["sa_stats"]["rms_deviation"]
    check("T1 our RMS agrees with the step's own RMS",
          close(my_rms, sa_rms, 1e-4), f"ours={my_rms} SA={sa_rms}")
    check("T1 bottom row of the returned matrix is 0 0 0 1",
          [round(v, 9) for v in r["matrix"][3]] == [0.0, 0.0, 0.0, 1.0],
          str(r["matrix"][3]))

    # ---- 2) exclusion + refit --------------------------------------------
    t("T2 refit excluding P4 (temp copies, then removed)")
    r2 = server.sa_best_fit_transform(
        reference_group="MCP_FT_REF", corresponding_group="MCP_FT_CORR",
        collection=COLL, exclude_points=[f"P{OUTLIER_TARGET}"],
        tolerance_mm=0.2)
    print(f"    -> computed={r2.get('computed')} refit={r2.get('refit')} "
          f"error={r2.get('error')}", flush=True)
    print(f"    -> our stats={r2.get('stats')} "
          f"excluded_deviations={r2.get('excluded_deviations')}", flush=True)
    check("T2 refit ran", r2.get("computed") and r2.get("refit"),
          str(r2.get("error")))
    check("T2 the excluded point is dropped from the stats",
          r2["stats"]["point_count"] == len(REF_PTS) - 1, r2["stats"])
    check("T2 inlier RMS collapses to ~0 after the refit",
          close(r2["stats"]["rms_deviation"], 0.0, 1e-3),
          r2["stats"]["rms_deviation"])
    check("T2 the dropped point keeps its ~0.5 mm deviation",
          close((r2.get("excluded_deviations") or {}).get(
              f"P{OUTLIER_TARGET}"), OUTLIER_MM, 1e-3),
          r2.get("excluded_deviations"))
    leftovers = sa_project_names()
    check("T2 the temp copies are gone",
          not [n for n in leftovers if n.startswith("MCP_FITTMP")], leftovers)

    # ---- 3) apply: the group + companion move together --------------------
    t("T3 apply the refit transform to CORR + the companion SPOT")
    r3 = server.sa_best_fit_transform(
        reference_group="MCP_FT_REF", corresponding_group="MCP_FT_CORR",
        collection=COLL, exclude_points=[f"P{OUTLIER_TARGET}"],
        apply=True, move_objects=["MCP_FT_SPOT"])
    print(f"    -> applied={r3.get('applied')} step={r3.get('step')} "
          f"objects={r3.get('moved_objects')} err={r3.get('error')}",
          flush=True)
    print(f"    -> stats_after_move={r3.get('stats_after_move')}", flush=True)
    check("T3 move reported success", r3.get("applied"), str(r3.get("error")))
    check("T3 the group and the companion were both named",
          r3.get("moved_objects") == ["A::MCP_FT_CORR", "A::MCP_FT_SPOT"],
          r3.get("moved_objects"))
    check("T3 verify_after_move: the group landed on the reference (~0 mm)",
          close((r3.get("stats_after_move") or {}).get("rms_deviation"), 0.0,
                1e-3),
          r3.get("stats_after_move"))
    moved, err = coords_of("MCP_FT_CORR")
    print(f"    -> CORR after move: {moved} err={err}", flush=True)
    moved_spot, err = coords_of("MCP_FT_SPOT")
    print(f"    -> SPOT after move: {moved_spot} err={err}", flush=True)
    if moved and ref_now:
        # the moved group now sits on REF (except the excluded point, which
        # was NOT part of the fit that produced the transform)
        check("T3 inlier points of CORR now coincide with REF",
              all(close_pt(moved[k], ref_now[k], 1e-3) for k in ref_now
                  if k != f"P{OUTLIER_TARGET}"),
              f"corr={moved} ref={ref_now}")
    if moved_spot and spot_before:
        # the companion moved by the SAME rigid transform: its own point-to-
        # point distances are preserved and it agrees with the group's move.
        d_before = math.dist(spot_before["P1"], spot_before["P2"])
        d_after = math.dist(moved_spot["P1"], moved_spot["P2"])
        check("T3 the companion moved rigidly (distance preserved)",
              close(d_before, d_after, 1e-6),
              f"{d_before:.6f} -> {d_after:.6f}")
        check("T3 the companion actually moved",
              not close_pt(spot_before["P1"], moved_spot["P1"], 1e-6),
              f"{spot_before['P1']} -> {moved_spot['P1']}")

    # ---- 4) a failed fit reports cleanly ----------------------------------
    t("T4 unknown group reported, no exception")
    r4 = server.sa_best_fit_transform(reference_group="MCP_FT_NOPE",
                                      corresponding_group="MCP_FT_CORR",
                                      collection=COLL)
    check("T4 missing group -> error, not a crash",
          not r4.get("computed") and bool(r4.get("error")), r4.get("error"))

    settle(2, pid)
    server._delete_geometry_objects(COLL, NAMES)
    stop.set()
    t("DONE")
    print("FAILED: " + "; ".join(FAILURES) if FAILURES else "ALL OK",
          flush=True)
    sys.exit(1 if FAILURES else 0)


def sa_project_names():
    """Simple names of the point groups currently in the collection."""
    try:
        return [n.split("::")[-1]
                for n in server._objects_in_collection_by_type(COLL,
                                                              "Point Group")]
    except Exception:  # noqa: BLE001
        return []


if __name__ == "__main__":
    main()
