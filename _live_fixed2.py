# -*- coding: utf-8 -*-
"""Live end-to-end check of sa_fit_fixed_* against SA 2015.

Synthetic clouds exercise the whole SA pipeline for the four fixed-parameter
geometries (delete stale object -> Construct <Type> with the exact fixed
diameter / included angle -> Get <Type> Properties read-back). One real point
group of the fixture is then cylinder-fitted free and re-fitted at that radius
with inside/outside compensation to compare RMS.  Run with SA closed (this
cold-starts SA):  python _live_fixed2.py
"""
import math
import os
import random
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

RNG = random.Random(7)
PROBE = 19.05  # reflector radius used to build the synthetic measurements
HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "6.01.25 — обработка.xit")


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
        titles = dismiss(pid)
        for x in titles:
            if x not in _seen_nags:
                _seen_nags.add(x)
                t(f"NAG-DIALOG dismissed: '{x}'")
        stop.wait(2.0)


def settle(sec, pid=None):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        time.sleep(2.0)
        dismiss(pid)


def unit(v):
    m = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / m for x in v]


def add(a, b):
    return [a[i] + b[i] for i in range(3)]


def scale(a, s):
    return [x * s for x in a]


def cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def ortho(n0):
    n0 = unit(n0)
    ref = [0.0, 0.0, 1.0] if abs(n0[2]) < 0.9 else [1.0, 0.0, 0.0]
    e1 = unit(cross(n0, ref))
    e2 = cross(n0, e1)
    return e1, e2


def rad_dir(axis, th):
    e1, e2 = ortho(axis)
    return [e1[i] * math.cos(th) + e2[i] * math.sin(th) for i in range(3)]


def noisy(v):
    return [v[i] + RNG.gauss(0, 0.02) for i in range(3)]


def synth_cylinder():
    """Reflector centres on the OUTSIDE of a r=500 cylinder."""
    base = [1000.0, -500.0, 300.0]
    u = unit([0.2, -0.1, 0.97])
    pts = []
    for it in range(5):
        zz = 80.0 * it - 160.0
        for k in range(8):
            rho = rad_dir(u, 2 * math.pi * k / 8)
            surf = add(add(base, scale(u, zz)), scale(rho, 500.0))
            pts.append(noisy(add(surf, scale(rho, PROBE))))
    return pts


def synth_sphere():
    """Reflector centres on the OUTSIDE of a r=800 sphere."""
    c0 = [200.0, 3000.0, -400.0]
    pts = []
    for _ in range(12):
        th = RNG.uniform(0, 2 * math.pi)
        ph = math.acos(RNG.uniform(-0.7, 0.7))
        d = [math.sin(ph) * math.cos(th), math.sin(ph) * math.sin(th),
             math.cos(ph)]
        surf = add(c0, scale(d, 800.0))
        pts.append(noisy(add(surf, scale(d, PROBE))))
    return pts


def synth_circle():
    """Reflector centres on the OUTSIDE of a r=400 circle."""
    c0 = [500.0, 120.0, 800.0]
    n0 = unit([0.3, -0.2, 0.93])
    pts = []
    for k in range(16):
        rho = rad_dir(n0, 2 * math.pi * k / 16)
        surf = add(c0, scale(rho, 400.0))
        pts.append(noisy(add(surf, scale(rho, PROBE))))
    return pts


def synth_cone():
    """Reflector centres on the OUTSIDE of a 50 deg cone (axis = Z)."""
    alpha = math.radians(25.0)
    pts = []
    for it in range(5):
        zz = 150.0 + 100.0 * it
        rr = zz * math.tan(alpha)
        for k in range(8):
            th = 2 * math.pi * k / 8
            rho = [math.cos(th), math.sin(th), 0.0]
            surf = [rr * rho[0], rr * rho[1], zz]
            nhat = [math.cos(alpha) * rho[i] - math.sin(alpha) * unit([0, 0, 1])[i]
                    for i in range(3)]
            pts.append(noisy(add(surf, scale(nhat, PROBE))))
    return pts


def run_fixed(gtype, name, coords, expect, **kw):
    t(f"  sa_fit_fixed_{gtype} -> {name} {kw}")
    st = time.monotonic()
    res = server.sa_fit_fixed(gtype, name, collection="A", coordinates=coords,
                              probe_offset_mm=PROBE, compensation="outside",
                              include_deviations=True, **kw)
    dt = time.monotonic() - st
    stats = res.get("stats") or {}
    rms = stats.get("rms_deviation")
    print(f"    ok={res.get('ok')} constructed={res.get('constructed')} "
          f"dt={dt:4.1f}s err={res.get('error')}", flush=True)
    if res.get("error"):
        print(f"    error: {res['error']}", flush=True)
    else:
        print(f"    readback: radius={res.get('radius')} "
              f"diameter={res.get('diameter')} "
              f"included_angle={res.get('included_angle')} "
              f"rms_dev={rms} devs={len(res.get('deviations', []))}",
              flush=True)
        if expect is not None:
            ok = abs(float(res[expect[0]]) - expect[1]) < 1e-3
            print(f"    CHECK read {expect[0]} == {expect[1]}: "
                  f"{'PASS' if ok else 'FAIL'}", flush=True)
    return res, dt


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
    # ride out the delayed-startup nag window (SA 2015 can pop a maintenance
    # dialog tens of seconds after launch, wedging the SDK listener) - only a
    # cold start by us needs that; a warm instance is already past it
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

    # 1) synthetic pipeline tests, all four fixed-parameter geometries
    cases = [
        ("cylinder", "MCP_LIVE_cyl", synth_cylinder(),
         ("diameter", 1000.0), dict(radius_mm=500.0)),
        ("sphere", "MCP_LIVE_sph", synth_sphere(),
         ("radius", 800.0), dict(diameter_mm=1600.0)),
        ("circle", "MCP_LIVE_cir", synth_circle(),
         ("radius", 400.0), dict(diameter_mm=800.0)),
        ("cone", "MCP_LIVE_con", synth_cone(),
         ("included_angle", 50.0), dict(apex_angle_deg=50.0)),
    ]
    wedged = False
    failed = False
    for gtype, name, coords, expect, kw in cases:
        dismiss(pid)
        res, dt = run_fixed(gtype, name, coords, expect, **kw)
        if dt >= 11.0:
            wedged = True
            t(f"wedge suspected at {gtype} (call took {dt:.1f}s); stopping "
              f"SA-side tests")
            break
        if not res.get("ok") or not res.get("constructed"):
            failed = True
    if not wedged:
        t("synthetic pipeline DONE - cleaning synthetic objects")
        server._delete_geometry_objects("A", [c[1] for c in cases])
        settle(2, pid)

    # 2) real group from the fixture: free cylinder fit, then fixed re-fit
    if not wedged:
        groups = server.sa_inspect_project(
            collection="A", object_types=["Point Group"]).get("types", {}).get(
            "Point Group", [])
        chosen = None
        for g in groups or []:
            nm = str(g).split("::")[-1]
            pts = server._read_group_points("A", nm)
            if pts.get("ok") and pts["count"] >= 10 and \
                    (chosen is None or pts["count"] > chosen[1]):
                chosen = (nm, pts["count"])
        t(f"real groups: {[(str(g).split('::')[-1]) for g in (groups or [])]}")
        if chosen:
            gname, gcount = chosen
            t(f"fit real group '{gname}' ({gcount} pts): free radius first")
            t("  free-fit cylinder on group")
            st = time.monotonic()
            res1 = server.sa_fit_fixed(
                "cylinder", "MCP_LIVE_A", point_group=gname,
                collection="A", compensation="none",
                include_deviations=True)
            dt1 = time.monotonic() - st
            print(f"    free ok={res1.get('ok')} dt={dt1:4.1f}s "
                  f"err={res1.get('error')}", flush=True)
            print(f"    free radius={res1.get('radius')} "
                  f"rms={((res1.get('stats') or {}).get('rms_deviation'))}",
                  flush=True)
            if res1.get("ok") and res1.get("radius"):
                Rfix = float(res1["radius"])
                side_rms = {}
                for side in ("inside", "outside"):
                    dismiss(pid)
                    t(f"  fixed cylinder r={Rfix:.6f} comp={side}")
                    st = time.monotonic()
                    res2 = server.sa_fit_fixed(
                        "cylinder", "MCP_LIVE_A", radius_mm=Rfix,
                        point_group=gname, collection="A",
                        compensation=side, include_deviations=True)
                    dt2 = time.monotonic() - st
                    stats2 = res2.get("stats") or {}
                    side_rms[side] = stats2.get("rms_deviation")
                    print(f"    fixed ok={res2.get('ok')} dt={dt2:4.1f}s "
                          f"rms={stats2.get('rms_deviation')} "
                          f"read_radius={res2.get('radius')} "
                          f"err={res2.get('error')}", flush=True)
                    if dt2 >= 11.0:
                        wedged = True
                        break
                if not wedged:
                    print(f"    side RMS compare: {side_rms}", flush=True)
            if not wedged:
                server._delete_geometry_objects("A", ["MCP_LIVE_A"])
                t("  cleaned MCP_LIVE_A")

    settle(2, pid)
    stop.set()
    t("DONE")
    print("WEDGED" if wedged else ("FAILED" if failed else "ALL OK"),
          flush=True)


if __name__ == "__main__":
    main()
