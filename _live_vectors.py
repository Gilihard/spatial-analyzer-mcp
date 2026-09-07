# -*- coding: utf-8 -*-
"""Live end-to-end check of the point-vs-object compare + vector-group tools.

Confirms against a real SA 2015 instance the "Сравнить > Точки > Объекты"
mechanism: 'Query Points to Objects' with a VECTOR-group Projection Option
output must create a NEW vector group holding one deviation vector per
source point, and 'Get Vector Group Properties' / 'Get i-th Vector From
Vector Group' must read the group's statistics + whiskers back.

Expected live caveat (same flaky-query semantics as sa_project_points): the
step status is advisory on SA 2015 - success is decided by what the created
vector group contains, and SA may silently skip points it cannot project
(the real measured group). Synthetic groups must compare 1:1.

Test plan (collection A of the fixture; every created object is prefixed
MCP_VEC_* and deleted at the end):
  1. plane: 9 source points 25 mm above a best-fit plane -> compare
     (default "Object To Probe Vectors"): vector group of 9 whiskers, every
     magnitude ~ 25 mm.
  2. same plane, probe_offset_mm=10 + use_stored_offsets=False (raw) ->
     magnitudes still ~ 25 (the offset output back-away does not change the
     deviation on the object: the whisker start moves with the probe).
  3. cylinder r=400 with 5 source points at r=430 -> magnitudes ~ 30.
  4. "Probe To Object Vectors" direction: same magnitudes, opposite ijk.
  5. explicit points list + several target objects (result_group required).
  6. re-run into the same result_group -> old group replaced.
  7. vector group props round-trip: style the group (Go/No-Go, tolerance
     band) then read 'Get Vector Group Properties' back: high/low tolerance
     values match, in+out-of-tolerance counts == total vectors.
  8. auto-range colorization on the group (auto_range=True).
  9. real measured group "т контур" (376 pts, stored radial offset 19.05)
     onto its free cylinder fit: every created vector magnitude ~= the
     compensated point deviation (per-point stored offset applied).

Run with SA closed (this cold-starts SA):  python _live_vectors.py
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
    "MCP_VEC_PLN_PTS", "MCP_VEC_PLN", "MCP_VEC_SRC_PLN",
    "MCP_VEC_OUT_PLN", "MCP_VEC_OUT_PLN_RE", "MCP_VEC_OUT_PLN_P2O",
    "MCP_VEC_CYL_PTS", "MCP_VEC_CYL", "MCP_VEC_SRC_CYL", "MCP_VEC_OUT_CYL",
    "MCP_VEC_OUT_MULTI", "MCP_VEC_FIT_CYL", "MCP_VEC_OUT_TK",
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


def vg_full(result):
    """Vector group full name from a compare response (may be None)."""
    return result.get("vector_group") or result.get("result_group")


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

    # ---- 1) plane, 25 mm above: whisker magnitude ~ 25 --------------------
    t("T1 plane compare (Object To Probe Vectors)")
    pln_xy = [(x, y) for x in (-100.0, 0.0, 100.0)
              for y in (-80.0, 0.0, 80.0)]
    b = make_group("MCP_VEC_PLN_PTS", [[x, y, 0.0] for x, y in pln_xy], "P")
    check("T1 plane points built", b.get("ok") and b["count"] == 9,
          str(b.get("error")))
    src = [[x, y, 25.0] for x, y in pln_xy]
    b = make_group("MCP_VEC_SRC_PLN", src, "S")
    check("T1 source group built", b.get("ok") and b["count"] == 9,
          str(b.get("error")))
    f = server.sa_best_fit("plane", "MCP_VEC_PLN",
                           point_group="MCP_VEC_PLN_PTS", collection=COLL)
    check("T1 plane fit", f.get("constructed"), f.get("error"))
    c = server.sa_compare_points_objects(
        objects=["MCP_VEC_PLN"], point_groups=["MCP_VEC_SRC_PLN"],
        result_group="MCP_VEC_OUT_PLN", collection=COLL)
    print(f"    -> created={c.get('created')} code={c.get('status_code')} "
          f"{c.get('status')} vec={c.get('vector_count')} of "
          f"{c.get('requested')} err={c.get('error')} "
          f"{c.get('minor_error') or ''}", flush=True)
    check("T1 compare ran", c.get("created") and not c.get("error"),
          str(c.get("minor_error")))
    props = c.get("properties") or {}
    mags = [v.get("magnitude") for v in _read_vectors(c)]
    if mags:
        print(f"    -> magnitudes={['%.4f' % m for m in mags]}", flush=True)
        check("T1 every whisker magnitude ~ 25 mm",
              all(abs(m - 25.0) < 0.01 for m in mags))
        check("T1 vectors all point probe-ward (object -> probe)",
              all(_same_dir(v, [0, 0, 1]) for v in _read_vectors(c)))
    check("T1 props read back", bool(props)
          and props.get("total_vectors") == 9,
          str(props))

    # ---- 2) probe_offset_mm + raw coordinates: magnitudes unchanged --------
    t("T2 probe_offset_mm=10, use_stored_offsets=False (raw compare)")
    c = server.sa_compare_points_objects(
        objects=["MCP_VEC_PLN"], point_groups=["MCP_VEC_SRC_PLN"],
        result_group="MCP_VEC_OUT_PLN_RE", collection=COLL,
        probe_offset_mm=10.0, use_stored_offsets=False)
    print(f"    -> created={c.get('created')} vec={c.get('vector_count')} "
          f"of {c.get('requested')} err={c.get('error')}", flush=True)
    check("T2 ran", c.get("created") and not c.get("error"))
    mags = [v.get("magnitude") for v in _read_vectors(c)]
    if mags:
        print(f"    -> magnitudes={['%.4f' % m for m in mags]}", flush=True)
        check("T2 raw coordinates -> whiskers still ~ 25 mm",
              all(abs(m - 25.0) < 0.01 for m in mags),
              "probe offset moves the probe point, not the deviation")

    # ---- 3) cylinder r=400, source at r=430: magnitude ~ 30 ----------------
    t("T3 cylinder compare")
    cyl_pts = []
    for i in range(12):
        ang = 2 * math.pi * i / 12
        cyl_pts.append([400 * math.cos(ang), 400 * math.sin(ang),
                        -150.0 + i * 30.0])
    f = server.sa_best_fit("cylinder", "MCP_VEC_CYL",
                           point_group="MCP_VEC_CYL_PTS",
                           coordinates=cyl_pts, collection=COLL)
    check("T3 cylinder fit", f.get("constructed"), f.get("error"))
    src_c = [[430 * math.cos(a), 430 * math.sin(a), z]
             for a, z in ((0.0, 0.0), (1.2, 50.0), (2.5, -40.0),
                          (3.9, 20.0), (5.1, -90.0))]
    make_group("MCP_VEC_SRC_CYL", src_c, "S")
    c = server.sa_compare_points_objects(
        objects=["MCP_VEC_CYL"], point_groups=["MCP_VEC_SRC_CYL"],
        result_group="MCP_VEC_OUT_CYL", collection=COLL)
    print(f"    -> created={c.get('created')} code={c.get('status_code')} "
          f"vec={c.get('vector_count')} of {c.get('requested')} "
          f"err={c.get('error')} {c.get('minor_error') or ''}", flush=True)
    check("T3 compare ran", c.get("created") and not c.get("error"))
    vecs = _read_vectors(c)
    mags = [v.get("magnitude") for v in vecs]
    if mags:
        print(f"    -> magnitudes={['%.4f' % m for m in mags]}", flush=True)
        check("T3 whisker magnitudes ~ radial difference 30 mm",
              all(abs(m - 30.0) < 0.01 for m in mags))
    if vecs:
        # object -> probe: whisker points radially outward (away from axis).
        rad = [v["begin"][0] * v["delta"][0] + v["begin"][1] * v["delta"][1]
               for v in vecs]
        check("T3 vectors point outward from the cylinder axis",
              all(r > 0 for r in rad))

    # ---- 4) reversed direction: same magnitudes, opposite sense ------------
    t("T4 Probe To Object Vectors (reversed whiskers)")
    c = server.sa_compare_points_objects(
        objects=["MCP_VEC_PLN"], point_groups=["MCP_VEC_SRC_PLN"],
        result_group="MCP_VEC_OUT_PLN_P2O", collection=COLL,
        projection_type="Probe To Object Vectors")
    print(f"    -> created={c.get('created')} vec={c.get('vector_count')} "
          f"err={c.get('error')}", flush=True)
    check("T4 ran", c.get("created") and not c.get("error"))
    vecs = _read_vectors(c)
    mags = [v.get("magnitude") for v in vecs]
    if mags:
        check("T4 magnitudes unchanged (~ 25)",
              all(abs(m - 25.0) < 0.01 for m in mags))
    if vecs:
        check("T4 whiskers reversed (probe -> object, -z)",
              all(_same_dir(v, [0, 0, -1]) for v in vecs))

    # ---- 5) explicit points + two objects (result_group required) ----------
    t("T5 explicit points, several objects")
    c = server.sa_compare_points_objects(
        objects=["MCP_VEC_PLN", "MCP_VEC_CYL"],
        points=["MCP_VEC_SRC_PLN::S1", "MCP_VEC_SRC_PLN::S9",
                "MCP_VEC_SRC_CYL::S1"],
        result_group="MCP_VEC_OUT_MULTI", collection=COLL)
    print(f"    -> created={c.get('created')} vec={c.get('vector_count')} "
          f"of {c.get('requested')} err={c.get('error')}", flush=True)
    check("T5 explicit points compared", c.get("created")
          and c.get("vector_count") == 3 and not c.get("error"))
    mags = [v.get("magnitude") for v in _read_vectors(c)]
    if mags:
        print(f"    -> magnitudes={['%.4f' % m for m in mags]}", flush=True)
        check("T5 magnitudes ~ 25 / 25 / 30",
              len(mags) == 3
              and abs(mags[0] - 25.0) < 0.01
              and abs(mags[1] - 25.0) < 0.01
              and abs(mags[2] - 30.0) < 0.01)

    # ---- 6) re-run into the same result group (replaced) -------------------
    t("T6 re-run into existing vector group")
    c2 = server.sa_compare_points_objects(
        objects=["MCP_VEC_PLN"], point_groups=["MCP_VEC_SRC_PLN"],
        result_group="MCP_VEC_OUT_PLN", collection=COLL)
    print(f"    -> created={c2.get('created')} replaced={c2.get('replaced')} "
          f"vec={c2.get('vector_count')} err={c2.get('error')}", flush=True)
    check("T6 re-run replaced the old group", c2.get("created")
          and c2.get("replaced") is True and not c2.get("error"))
    mags = [v.get("magnitude") for v in _read_vectors(c2)]
    check("T6 re-run produced 9 clean vectors again",
          bool(mags) and len(mags) == 9
          and all(abs(m - 25.0) < 0.01 for m in mags))

    # ---- 7) style round-trip: tolerance band reflects in props -------------
    t("T7 vector group style + props round-trip")
    vg = vg_full(c2)
    st = server.sa_vector_group_style(
        vector_groups=[vg], collection=COLL,
        color_range_method="Go/No-Go",
        high_tolerance=10.0, low_tolerance=-10.0,
        show_out_of_tolerance_only=False)
    print(f"    -> applied={st.get('applied')} code={st.get('status_code')} "
          f"{st.get('status')} step={st.get('step')} err={st.get('error')}",
          flush=True)
    check("T7 style applied", st.get("applied") and not st.get("error"),
          str(st.get("error")))
    pr = server.sa_vector_group_props(vector_group=vg, collection=COLL)
    props = pr.get("properties") or {}
    print(f"    -> props: in={props.get('vectors_in_tolerance')} "
          f"out={props.get('vectors_out_of_tolerance')} "
          f"hi={props.get('high_tolerance_value')} "
          f"lo={props.get('low_tolerance_value')}", flush=True)
    check("T7 props read", pr.get("ok") and props.get("total_vectors") == 9,
          str(pr.get("error")))
    check("T7 tolerance values round-trip",
          abs((props.get("high_tolerance_value") or 0.0) - 10.0) < 1e-6
          and abs((props.get("low_tolerance_value") or 0.0) + 10.0) < 1e-6,
          f"hi={props.get('high_tolerance_value')} "
          f"lo={props.get('low_tolerance_value')}")
    in_t = props.get("vectors_in_tolerance")
    out_t = props.get("vectors_out_of_tolerance")
    if in_t is not None and out_t is not None:
        check("T7 in+out-of-tolerance counts == total",
              in_t + out_t == props.get("total_vectors"),
              f"{in_t}+{out_t} vs {props.get('total_vectors')}")

    # ---- 8) auto-range colorization ----------------------------------------
    t("T8 auto-range style")
    st = server.sa_vector_group_style(
        vector_groups=[vg], collection=COLL,
        auto_range=True, color_range_method="Continuous",
        draw_tubes=True)
    print(f"    -> applied={st.get('applied')} code={st.get('status_code')} "
          f"step={st.get('step')} err={st.get('error')}", flush=True)
    check("T8 auto-range applied", st.get("applied")
          and st.get("step", "").startswith("Auto-Range")
          and not st.get("error"))

    # ---- 9) real measured group onto its own free fit ----------------------
    t("T9 real group 'т контур' (376 pts, stored offset 19.05)")
    tk_pts = server._points_in_group("A::т контур")
    check("T9 group present", len(tk_pts) == 376, f"got {len(tk_pts)}")
    if len(tk_pts) >= 5:
        f = server.sa_best_fit("cylinder", "MCP_VEC_FIT_CYL",
                               point_group="т контур", collection=COLL)
        check("T9 free cylinder fit", f.get("constructed"), f.get("error"))
        props = server._read_geometry_props("cylinder", COLL,
                                            "MCP_VEC_FIT_CYL")
        rfit = props["properties"].get("radius")
        print(f"    -> free radius={rfit} (raw centres ~ 19.05 off surface)",
              flush=True)
        c = server.sa_compare_points_objects(
            objects=["MCP_VEC_FIT_CYL"],
            points=tk_pts[:5], collection=COLL,
            result_group="MCP_VEC_OUT_TK")
        print(f"    -> created={c.get('created')} code={c.get('status_code')} "
              f"vec={c.get('vector_count')} of {c.get('requested')} "
              f"skipped={c.get('skipped')} err={c.get('error')} "
              f"{c.get('minor_error') or ''}", flush=True)
        check("T9 compare ran on real points", c.get("created")
              and not c.get("error"), str(c.get("minor_error")))
        mags = [v.get("magnitude") for v in _read_vectors(c)]
        if mags:
            # Per-point stored offset 19.05 applied by the query -> the
            # whiskers measure the SURFACE deviation, ~0 not ~19.
            print(f"    -> magnitudes={['%.4f' % m for m in mags]}",
                  flush=True)
            check("T9 stored reflector offset compensated (mag << 19.05)",
                  all(abs(m) < 2.0 for m in mags),
                  f"max={max(mags) if mags else None}")

    settle(2, pid)
    server._delete_geometry_objects(COLL, ALL_NAMES)
    stop.set()
    t("DONE")
    print("FAILED: " + "; ".join(FAILURES) if FAILURES else "ALL OK",
          flush=True)
    sys.exit(1 if FAILURES else 0)


def _read_vectors(result):
    """Per-vector dump of the vector group a compare created (best-effort)."""
    vg = vg_full(result)
    if not vg:
        return []
    r = server.sa_vector_group_props(vector_group=vg, collection=COLL,
                                     include_vectors=True)
    return r.get("vectors") or []


def _same_dir(v, want):
    """True when the vector's ijk unit points along `want` (~ within 1e-3)."""
    ijk = v.get("ijk") or []
    if len(ijk) != 3:
        return False
    return all(abs(a - b) < 1e-3 for a, b in zip(ijk, want))


if __name__ == "__main__":
    main()
