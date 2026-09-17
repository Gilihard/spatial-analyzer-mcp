# -*- coding: utf-8 -*-
"""Live end-to-end check of the move / transform tools.

Confirms against a real SA 2015 instance:
  - the Transform transport itself: 'Make a Transform from Doubles (Fixed XYZ)'
    -> SABridge.get_transform_arg's 4x4 SAFEARRAY (VARIANT*) round-trip, and
    'Decompose Transform into Doubles (Fixed XYZ)' back to Fixed XYZ (proves
    the matrix layout + the angle unit).
  - sa_move_objects mode='about_working_frame' translation: an object moves by
    exactly (dx, dy, dz) along the active frame axes.
  - sa_move_objects mode='about_working_frame' rotation: rotation is applied
    about the working frame origin (SA User Manual: transforms are expressed
    in the active coordinate frame) - verified from a displaced position.
  - sa_move_objects mode='translate' and mode='world' run and agree with
    about_working_frame when the working frame is WORLD.
  - sa_move_objects mode='frame_to_frame': an object is moved by the delta
    between two constructed frames.
  - sa_object_transform: reads the object's placement matrix + Fixed XYZ.

Step/arg names come from the MP Command Reference PDF and are NOT confirmed
live on SA 2015 - this harness is that live check.

Test plan (collection A of the fixture; everything created is prefixed
MCP_MV_* and deleted at the end):
  1. compose a transform (10,30,50,40,-30,-60) -> matrix, decompose back.
  2. point group (0,0,0),(10,0,0),(0,20,0) -> translate (100,-5,3).
  3. rotate the same group rz=90 -> record where the points land.
  4. translate mode / world mode -> agree with (2).
  5. frame-to-frame: two frames 200 mm apart on X -> object moved +200 X.
  6. sa_object_transform on a point group.

Run with SA closed (this cold-starts SA):  python _live_transform.py
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
    "MCP_MV_A", "MCP_MV_B", "MCP_MV_O", "MCP_MV_X",
    "MCP_MV_FR0", "MCP_MV_FR1",
]

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


def make_group(group, coords, prefix="P"):
    return server._create_point_group(COLL, group, coords, prefix)


def coords_of(group):
    r = server.sa_point_coordinates(point_group=group, collection=COLL,
                                    include_offsets=False)
    if not r.get("ok"):
        return None, r.get("error")
    return {p["name"].split("::")[-1]: [p["x"], p["y"], p["z"]]
            for p in r["points"]}, None


def close(a, b, tol=1e-6):
    return a is not None and b is not None and \
        abs(a - b) <= tol


def close_pt(p, q, tol=1e-6):
    return len(p) == len(q) and all(close(a, b, tol) for a, b in zip(p, q))


def translate(pts, d):
    return {k: [v[i] + d[i] for i in range(3)] for k, v in pts.items()}


def rotz(pts, deg, origin=(0.0, 0.0, 0.0)):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    out = {}
    for k, (x, y, z) in pts.items():
        dx, dy = x - origin[0], y - origin[1]
        out[k] = [origin[0] + c * dx - s * dy, origin[1] + s * dx + c * dy, z]
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
    # A freshly launched SA's SDK listener can take a bit longer than
    # sa_ensure_running's post-launch wait - retry Connect (the engine is
    # already born and stays connected to THIS one SA instance).
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

    server._delete_geometry_objects(COLL, ALL_NAMES)
    server.sa_reset_working_frame()

    # ---- 1) Fixed XYZ -> Transform matrix -> Fixed XYZ --------------------
    t("T1 Make/Get/Decompose Transform (the VARIANT* 4x4 SAFEARRAY)")
    server._ensure_sa()
    dx, dy, dz, rx, ry, rz = 10.0, 30.0, 50.0, 40.0, -30.0, -60.0
    matrix, code, status, msgs = server._make_transform_fixed_xyz(
        dx, dy, dz, rx, ry, rz)
    print(f"    -> code={code} {status} msgs={msgs}", flush=True)
    check("T1 'Make a Transform from Doubles' succeeded",
          code == 2 and len(matrix) == 4 and
          all(len(r) == 4 for r in matrix),
          f"matrix={matrix}")
    if matrix:
        for row in matrix:
            print("       " + "  ".join(f"{v:12.6f}" for v in row), flush=True)
        # Translation lives in the last column of a row-major 4x4 (the SDK
        # helper indexes [row][col]); verify against the typed deltas.
        check("T1 translation column = (X,Y,Z)",
              close(matrix[0][3], dx, 1e-6) and
              close(matrix[1][3], dy, 1e-6) and
              close(matrix[2][3], dz, 1e-6),
              f"col3={[matrix[r][3] for r in range(3)]}")
        check("T1 bottom row is 0 0 0 1",
              matrix[3] == [0.0, 0.0, 0.0, 1.0] or
              (close(matrix[3][0], 0) and close(matrix[3][1], 0) and
               close(matrix[3][2], 0) and close(matrix[3][3], 1)),
              str(matrix[3]))
        fixed = server._decompose_transform(matrix)
        print(f"    -> decomposed: {fixed}", flush=True)
        check("T1 decompose round-trips Fixed XYZ (degrees)",
              fixed is not None and
              close(fixed["x"], dx, 1e-6) and close(fixed["y"], dy, 1e-6) and
              close(fixed["z"], dz, 1e-6) and close(fixed["rx"], rx, 1e-6) and
              close(fixed["ry"], ry, 1e-6) and close(fixed["rz"], rz, 1e-6),
              str(fixed))

    # ---- 2) translation relative to the working frame --------------------
    t("T2 about_working_frame translation")
    b = make_group("MCP_MV_A", [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0],
                                [0.0, 20.0, 0.0]])
    check("T2 group built", b.get("ok") and b["count"] == 3, str(b.get("error")))
    before, err = coords_of("MCP_MV_A")
    print(f"    -> before: {before} err={err}", flush=True)
    d = (100.0, -5.0, 3.0)
    mv = server.sa_move_objects(["MCP_MV_A"], collection=COLL,
                                dx=d[0], dy=d[1], dz=d[2])
    print(f"    -> moved={mv.get('moved')} step={mv.get('step')} "
          f"code={mv.get('status_code')} {mv.get('status')} "
          f"err={mv.get('error')}", flush=True)
    check("T2 move reported success", mv.get("moved") and not mv.get("error"),
          str(mv.get("error")))
    after, err = coords_of("MCP_MV_A")
    print(f"    -> after: {after} err={err}", flush=True)
    check("T2 points shifted by exactly (dx,dy,dz)",
          before is not None and after is not None and
          all(close_pt(after[k], translate(before, d)[k], 1e-6)
              for k in before),
          f"after={after}")
    # read-back reports the object's working transform
    rb = (mv.get("transforms") or {}).get("A::MCP_MV_A", {})
    print(f"    -> read_back after={rb.get('after', {}).get('fixed_xyz')}",
          flush=True)

    # ---- 3) rotation relative to the working frame -----------------------
    t("T3 about_working_frame rotation (rz=90)")
    rot = server.sa_move_objects(["MCP_MV_A"], collection=COLL, rz=90.0)
    print(f"    -> moved={rot.get('moved')} code={rot.get('status_code')} "
          f"{rot.get('status')} err={rot.get('error')}", flush=True)
    check("T3 rotation reported success", rot.get("moved"), str(rot.get("error")))
    rotated, err = coords_of("MCP_MV_A")
    print(f"    -> after rz=90: {rotated} err={err}", flush=True)
    if rotated is not None and after is not None:
        # Rigid body: pairwise distances and z are preserved either way.
        keys = sorted(after)
        d0 = math.dist(after[keys[0]], after[keys[1]])
        d1 = math.dist(rotated[keys[0]], rotated[keys[1]])
        check("T3 rigid (distance preserved) + z unchanged",
              close(d0, d1, 1e-6) and
              all(close(rotated[k][2], after[k][2]) for k in after),
              f"d {d0:.6f}->{d1:.6f}")
        # Decide the pivot: about the WORKING origin vs about the object.
        exp_work = rotz(after, 90.0)
        exp_obj = rotz(after, 90.0, origin=tuple(after[keys[0]]))
        match_work = all(close_pt(rotated[k], exp_work[k], 1e-4)
                         for k in after)
        match_obj = all(close_pt(rotated[k], exp_obj[k], 1e-4)
                        for k in after)
        t(f"    PIVOT: about_working_origin={match_work} "
          f"about_object_origin={match_obj}")
        print(f"       expected (working origin): {exp_work}", flush=True)
        print(f"       expected (object  origin): {exp_obj}", flush=True)
        check("T3 rotation pivots about the working frame origin",
              match_work, f"work={match_work} obj={match_obj}")

    # ---- 4) translate mode + world mode ----------------------------------
    t("T4 translate mode + world mode")
    make_group("MCP_MV_X", [[0.0, 0.0, 0.0]])
    tx = server.sa_move_objects(["MCP_MV_X"], collection=COLL,
                                mode="translate", dx=7.0, dy=8.0, dz=9.0)
    print(f"    -> translate mode: moved={tx.get('moved')} "
          f"code={tx.get('status_code')} {tx.get('status')} "
          f"err={tx.get('error')}", flush=True)
    check("T4 translate mode success", tx.get("moved"), str(tx.get("error")))
    p, _ = coords_of("MCP_MV_X")
    check("T4 translate mode moved the point by (7,8,9)",
          p is not None and close_pt(p["P1"], [7.0, 8.0, 9.0], 1e-6), str(p))

    w = server.sa_move_objects(["MCP_MV_X"], collection=COLL, mode="world",
                               dx=-7.0, dy=-8.0, dz=-9.0)
    print(f"    -> world mode: moved={w.get('moved')} "
          f"code={w.get('status_code')} {w.get('status')} "
          f"err={w.get('error')}", flush=True)
    check("T4 world mode success", w.get("moved"), str(w.get("error")))
    p, _ = coords_of("MCP_MV_X")
    check("T4 world mode moved the point back to the origin",
          p is not None and close_pt(p["P1"], [0.0, 0.0, 0.0], 1e-6), str(p))

    # ---- 5) frame to frame ------------------------------------------------
    t("T5 frame_to_frame (two frames 200 mm apart on X)")
    make_group("MCP_MV_O", [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
    f0 = server.sa_create_frame(frame_name="MCP_MV_FR0",
                                origin_point="MCP_MV_O::P1",
                                point_on_x_axis="MCP_MV_O::P2",
                                group="MCP_MV_O", collection=COLL)
    make_group("MCP_MV_B", [[200.0, 0.0, 0.0], [210.0, 0.0, 0.0]])
    f1 = server.sa_create_frame(frame_name="MCP_MV_FR1",
                                origin_point="MCP_MV_B::P1",
                                point_on_x_axis="MCP_MV_B::P2",
                                group="MCP_MV_B", collection=COLL)
    check("T5 both frames created",
          f0.get("created") and f1.get("created"),
          f"f0={f0.get('error')} f1={f1.get('error')}")
    mk = server.sa_move_objects(["MCP_MV_X"], collection=COLL,
                                mode="frame_to_frame",
                                source_frame="MCP_MV_FR0",
                                destination_frame="MCP_MV_FR1")
    print(f"    -> moved={mk.get('moved')} step={mk.get('step')} "
          f"code={mk.get('status_code')} {mk.get('status')} "
          f"err={mk.get('error')}", flush=True)
    check("T5 frame_to_frame success", mk.get("moved"), str(mk.get("error")))
    p, _ = coords_of("MCP_MV_X")
    check("T5 point moved by the frame delta (+200 X)",
          p is not None and close_pt(p["P1"], [200.0, 0.0, 0.0], 1e-4),
          str(p))

    # ---- 6) sa_object_transform read-back --------------------------------
    t("T6 sa_object_transform")
    ot = server.sa_object_transform(object="MCP_MV_FR1", collection=COLL)
    print(f"    -> ok={ot.get('ok')} fixed={ot.get('fixed_xyz')} "
          f"code={ot.get('status_code')} {ot.get('status')} "
          f"err={ot.get('error')}", flush=True)
    check("T6 object transform read for a frame",
          ot.get("ok") and ot.get("fixed_xyz") and
          close(ot["fixed_xyz"]["x"], 200.0, 1e-4),
          str(ot.get("fixed_xyz")))
    ot2 = server.sa_object_transform(object="MCP_MV_NOPE", collection=COLL)
    check("T6 missing object reported, no exception",
          ot2.get("ok") is False and ot2.get("error"),
          f"{ot2.get('status_code')} {ot2.get('status')}")

    settle(2, pid)
    server._delete_geometry_objects(COLL, ALL_NAMES)
    stop.set()
    t("DONE")
    print("FAILED: " + "; ".join(FAILURES) if FAILURES else "ALL OK",
          flush=True)
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
