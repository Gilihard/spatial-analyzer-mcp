# -*- coding: utf-8 -*-
"""Live end-to-end check of the delete + frame (СК) tools.

Confirms against a real SA 2015 instance:
  - sa_delete: 'Delete Objects' (whole objects, a point group with its
    points) and 'Delete Points' (individual points), including a combined
    points+objects call (points deleted before their emptied group) and the
    not-found status (code 3 FAILURE / code 4 PARTIAL SUCCESS).
  - sa_create_frame: 'Construct Frame On Object' (СК на объекте - frame on a
    fitted plane's local coordinate system) and 'Construct Frame, Pick origin
    and point on X axis - clock Z along working Z' (СК through two measured
    points, Z along the working frame), with replace=True deleting a
    same-named frame first.
  - sa_set_working_frame / sa_reset_working_frame / sa_current_working_frame:
    activate a СК, read the working frame back, and return it to WORLD.

The frame step + arg names are taken from the MP Command Reference PDF and
are NOT yet confirmed live on SA 2015 - this harness is that live check (the
delete steps ARE confirmed live: sa_fit_clean already drives both).

Test plan (collection A of the fixture; every created object is prefixed
MCP_FR_* / MCP_DEL_* and deleted at the end):
  1. plane: 9 points -> best-fit plane -> construct frame ON the plane
     (origin/orientation = plane's local system) -> created + listed under
     collection A's frames; re-create under the same name -> replaced, still
     exactly one frame.
  2. activate the frame (sa_set_working_frame) -> activated; the
     sa_current_working_frame read-back names it; sa_reset_working_frame ->
     back to WORLD (activated, no error).
  3. frame through two constructed points (origin at P1, X axis through P2,
     Z parallel to the working Z) -> created, then deleted again.
  4. delete 2 of 5 points of a point group by group-relative names -> the
     group keeps the remaining 3.
  5. combined call: points of a group AND the group itself -> both deleted.
  6. deleting an object that does not exist -> objects_deleted False, code 3.

Run with SA closed (this cold-starts SA):  python _live_frames_delete.py
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
    "MCP_DEL_PLN_PTS", "MCP_DEL_PLN", "MCP_DEL_SRC", "MCP_DEL_5P",
    "MCP_DEL_GRP", "MCP_DEL_2P",
    "MCP_FR_PLN", "MCP_FR_2P",
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


def frames_of(collection=COLL):
    """Frame full names currently listed in `collection`."""
    return server._objects_in_collection_by_type(collection, "Frame")


def has_frame(name, collection=COLL):
    full = f"{collection}::{name}" if collection else name
    return any(n == full or str(n).endswith("::" + name) for n in frames_of(collection))


def pts_of(group):
    full = f"{COLL}::{group}"
    return server._points_in_group(full)


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
    server._delete_geometry_objects(COLL, ALL_NAMES)

    # ---- 1) construct a СК on a fitted plane, then replace it -------------
    t("T1 frame on object (plane)")
    b = make_group("MCP_DEL_PLN_PTS",
                   [[x, y, 0.0] for x in (-100.0, 0.0, 100.0)
                    for y in (-80.0, 0.0, 80.0)])
    check("T1 plane points built", b.get("ok") and b["count"] == 9,
          str(b.get("error")))
    f = server.sa_best_fit("plane", "MCP_DEL_PLN",
                           point_group="MCP_DEL_PLN_PTS", collection=COLL)
    check("T1 plane fit", f.get("constructed"), f.get("error"))
    cr = server.sa_create_frame(frame_name="MCP_FR_PLN",
                                reference_object="MCP_DEL_PLN",
                                collection=COLL)
    print(f"    -> created={cr.get('created')} step={cr.get('step')} "
          f"code={cr.get('status_code')} {cr.get('status')} "
          f"replaced={cr.get('replaced')} err={cr.get('error')}", flush=True)
    check("T1 frame created", cr.get("created") and not cr.get("error"),
          str(cr.get("error")))
    check("T1 frame listed in collection frames",
          has_frame("MCP_FR_PLN"), str(frames_of(COLL)))

    cr2 = server.sa_create_frame(frame_name="MCP_FR_PLN",
                                 reference_object="MCP_DEL_PLN",
                                 collection=COLL)
    print(f"    -> re-created={cr2.get('created')} "
          f"replaced={cr2.get('replaced')} err={cr2.get('error')}",
          flush=True)
    n_after = sum(1 for n in frames_of(COLL)
                  if str(n).endswith("::MCP_FR_PLN"))
    check("T1 re-create replaced the old frame (exactly one left)",
          cr2.get("created") and cr2.get("replaced") is True
          and n_after == 1, f"frames={n_after}")

    # ---- 2) activate / read back / reset the working frame ----------------
    t("T2 activate + read back + reset")
    act = server.sa_set_working_frame(frame_name="MCP_FR_PLN",
                                      collection=COLL)
    print(f"    -> activated={act.get('activated')} "
          f"code={act.get('status_code')} {act.get('status')} "
          f"err={act.get('error')}", flush=True)
    check("T2 frame activated", act.get("activated"), str(act.get("error")))
    cur = server.sa_current_working_frame()
    print(f"    -> current: frame={cur.get('frame_name')} "
          f"collection={cur.get('collection')} ok={cur.get('ok')}",
          flush=True)
    check("T2 current working frame is the created СК",
          cur.get("ok") and cur.get("frame_name") == "MCP_FR_PLN",
          f"{cur.get('frame_name')} in {cur.get('collection')}")

    reset = server.sa_reset_working_frame()
    print(f"    -> reset activated={reset.get('activated')} "
          f"code={reset.get('status_code')} {reset.get('status')} "
          f"err={reset.get('error')}", flush=True)
    check("T2 reset to WORLD activated", reset.get("activated"),
          str(reset.get("error")))
    cur = server.sa_current_working_frame()
    print(f"    -> current after reset: frame={cur.get('frame_name')} "
          f"collection={cur.get('collection')}", flush=True)

    # ---- 3) СК through two points (origin + X axis, Z along working Z) ----
    t("T3 frame through two points")
    b = make_group("MCP_DEL_2P", [[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]])
    check("T3 two-point group built", b.get("ok") and b["count"] == 2,
          str(b.get("error")))
    cr = server.sa_create_frame(frame_name="MCP_FR_2P",
                                origin_point="MCP_DEL_2P::P1",
                                point_on_x_axis="MCP_DEL_2P::P2",
                                group="MCP_DEL_2P", collection=COLL)
    print(f"    -> created={cr.get('created')} step={cr.get('step')} "
          f"code={cr.get('status_code')} {cr.get('status')} "
          f"err={cr.get('error')}", flush=True)
    check("T3 frame created through two points",
          cr.get("created") and cr.get("method") == "origin_x_axis",
          str(cr.get("error")))
    check("T3 frame listed in collection frames",
          has_frame("MCP_FR_2P"), str(frames_of(COLL)))

    # ---- 4) delete individual points --------------------------------------
    t("T4 delete individual points")
    b = make_group("MCP_DEL_5P",
                   [[float(i), float(i), 0.0] for i in range(1, 6)])
    check("T4 five-point group built", b.get("ok") and b["count"] == 5,
          str(b.get("error")))
    d = server.sa_delete(points=["MCP_DEL_5P::P2", "MCP_DEL_5P::P4"],
                         group="MCP_DEL_5P", collection=COLL)
    print(f"    -> deleted={d.get('points_deleted')} "
          f"code={d.get('points_status_code')} {d.get('points_status')} "
          f"err={d.get('error')}", flush=True)
    left = pts_of("MCP_DEL_5P")
    check("T4 exactly two points deleted",
          d.get("points_deleted") and len(left) == 3,
          f"remaining={left}")

    # ---- 5) combined: points of a group AND the group itself --------------
    t("T5 combined delete (points first, then their group)")
    b = make_group("MCP_DEL_GRP",
                   [[float(i) * 10.0, 0.0, 0.0] for i in range(1, 5)])
    check("T5 four-point group built", b.get("ok") and b["count"] == 4,
          str(b.get("error")))
    d = server.sa_delete(objects=["MCP_DEL_GRP"],
                         points=["MCP_DEL_GRP::P1", "MCP_DEL_GRP::P2"],
                         group="MCP_DEL_GRP", collection=COLL)
    print(f"    -> points={d.get('points_deleted')} "
          f"({d.get('points_status')}) objects={d.get('objects_deleted')} "
          f"({d.get('objects_status')}) err={d.get('error')}", flush=True)
    groups = server._objects_in_collection_by_type(COLL, "Point Group")
    check("T5 points and object both deleted",
          d.get("points_deleted") and d.get("objects_deleted")
          and pts_of("MCP_DEL_GRP") == []
          and not any(str(n).endswith("::MCP_DEL_GRP")
                      for n in groups),
          f"remaining={pts_of('MCP_DEL_GRP')}")

    # ---- 6) deleting a missing object -> FAILURE code 3, no crash ---------
    t("T6 delete a nonexistent object")
    d = server.sa_delete(objects=["MCP_DEL_NOPE"], collection=COLL)
    print(f"    -> deleted={d.get('objects_deleted')} "
          f"code={d.get('objects_status_code')} {d.get('objects_status')} "
          f"err={d.get('error')}", flush=True)
    check("T6 missing object reported, no exception",
          d.get("objects_deleted") is False
          and d.get("objects_status_code") == 3,
          f"{d.get('objects_status_code')} {d.get('objects_status')}")

    settle(2, pid)
    server._delete_geometry_objects(COLL, ALL_NAMES)
    stop.set()
    t("DONE")
    print("FAILED: " + "; ".join(FAILURES) if FAILURES else "ALL OK",
          flush=True)
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
