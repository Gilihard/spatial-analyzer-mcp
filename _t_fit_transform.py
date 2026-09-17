# -*- coding: utf-8 -*-
"""Offline regression for sa_best_fit_transform (no SA, no COM).

Pins the contract a live run cannot cheaply re-check:
  - the step/arg names of 'Best Fit Transformation - Group to Group' and the
    Allow-<DOF> mapping, with NOTHING running between the last Set*Arg and
    ExecuteStep;
  - corresponding points are matched BY NAME (unmatched names are reported,
    not silently paired);
  - per-point deviations are recomputed from the returned transform, worst
    first, with `included`/`outlier` flags and the stats taken over the
    INCLUDED points only;
  - excluding a point refits on temp copies: both groups copied, the excluded
    targets deleted from each, the fit run on the COPIES, the copies removed
    again - and a delete that silently did nothing is caught, not reported as
    a successful refit;
  - apply=True moves the corresponding group AND its named companions in one
    'Transform Objects by Delta (About Working Frame)', never the reference
    group, and verify_after_move measures where the group actually landed;
  - validation returns before any COM call.
Run: python _t_fit_transform.py
"""

import server

fails = []


def check(desc, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + desc + " " + str(extra))
    if not cond:
        fails.append(desc)


# A translation that maps CORR back onto REF (CORR = REF + 10 in X).
TX = [[1.0, 0.0, 0.0, -10.0],
      [0.0, 1.0, 0.0, 0.0],
      [0.0, 0.0, 1.0, 0.0],
      [0.0, 0.0, 0.0, 1.0]]

REF = {"1": (0.0, 0.0, 0.0), "2": (100.0, 0.0, 0.0),
       "3": (100.0, 50.0, 0.0), "4": (0.0, 50.0, 0.0)}
# same shape shifted by +10 X, and target 3 deformed a further 0.5 in Y.
CORR = {"1": (10.0, 0.0, 0.0), "2": (110.0, 0.0, 0.0),
        "3": (110.0, 50.5, 0.0), "4": (10.0, 50.0, 0.0)}


class _FakeBridge:
    """Models just enough of a job: an object store and the mutating steps."""

    def __init__(self, store, fit_code=2, break_delete=False):
        self.store = store
        self.fit_code = fit_code
        self.break_delete = break_delete
        self.trace = []
        self.executed = []
        self.step = None
        self.cur = {}

    # -- recording ---------------------------------------------------------
    def set_step(self, s):
        self.trace.append(("set_step", (s,)))
        self.step = s
        self.cur = {}

    def execute_step(self):
        self.trace.append(("execute_step", (self.step,)))
        self.executed.append((self.step, dict(self.cur)))
        if self.step == "Copy Object":
            src = self._full(self.cur["Source Object"])
            dst = self._full(self.cur["New Object Name"])
            self.store[dst] = dict(self.store.get(src, {}))
        elif self.step == "Delete Points":
            if self.break_delete:
                return
            for full in self.cur.get("Point Names", []):
                parts = str(full).split("::")
                target, obj = parts[-1], parts[-2]
                coll = "::".join(parts[:-2])
                key = f"{coll}::{obj}" if coll else f"::{obj}"
                self.store.get(key, {}).pop(target, None)
        elif self.step == "Delete Objects":
            for full in self.cur.get("Object Names", []):
                self.store.pop(str(full), None)
        elif self.step == "Transform Objects by Delta (About Working Frame)":
            matrix = self.cur["Delta Transform"]
            for full in self.cur.get("Objects to Transform", []):
                pts = self.store.get(str(full))
                if not pts:
                    continue
                for t, (x, y, z) in list(pts.items()):
                    pts[t] = (matrix[0][0] * x + matrix[0][1] * y
                              + matrix[0][2] * z + matrix[0][3],
                              matrix[1][0] * x + matrix[1][1] * y
                              + matrix[1][2] * z + matrix[1][3],
                              matrix[2][0] * x + matrix[2][1] * y
                              + matrix[2][2] * z + matrix[2][3])

    @staticmethod
    def _full(dc):
        coll, obj = dc
        return f"{coll}::{obj}" if coll else f"::{obj}"

    def _rec(self, name, *args):
        self.trace.append((name, args))
        return True

    def set_collection_object_name_arg(self, n, coll, obj):
        self.cur[n] = (coll, obj)
        return self._rec("set_collection_object_name_arg", n, coll, obj)

    def set_collection_object_name_ref_list_arg(self, n, v):
        self.cur[n] = list(v)
        return self._rec("set_collection_object_name_ref_list_arg", n, list(v))

    def set_point_name_ref_list_arg(self, n, v):
        self.cur[n] = list(v)
        return self._rec("set_point_name_ref_list_arg", n, list(v))

    def set_bool_arg(self, n, v):
        self.cur[n] = v
        return self._rec("set_bool_arg", n, v)

    def set_double_arg(self, n, v):
        self.cur[n] = v
        return self._rec("set_double_arg", n, v)

    def set_transform_arg(self, n, m):
        self.cur[n] = m
        return self._rec("set_transform_arg", n, m)

    def get_step_result(self):
        if self.step == "Best Fit Transformation - Group to Group":
            return self.fit_code
        return 2

    def get_step_messages(self):
        return []

    def get_double_arg(self, n):
        return {"RMS Deviation": 0.25,
                "Maximum Absolute Deviation": 0.5}.get(n, 0.0)

    def get_transform_arg(self, n):
        return TX

    # -- helpers -----------------------------------------------------------
    def idx(self, name, pred=lambda a: True):
        return [i for i, (n, a) in enumerate(self.trace)
                if n == name and pred(a)]

    def sets_before_execute(self):
        exec_i = self.idx("execute_step")[-1]
        sets = [i for i, (n, _) in enumerate(self.trace)
                if n.startswith("set_") and n != "set_step" and i < exec_i]
        last_set = max(sets)
        between = [a for n, a in self.trace[last_set + 1:exec_i]
                   if n.startswith("set_")]
        return last_set, between


def _install(fit_code=2, break_delete=False, extra_ref=None, extra_corr=None):
    store = {"A::REF": dict(REF), "A::CORR": dict(CORR)}
    if extra_ref:
        store["A::REF"].update(extra_ref)
    if extra_corr:
        store["A::CORR"].update(extra_corr)
    fake = _FakeBridge(store, fit_code=fit_code, break_delete=break_delete)
    orig = (server.sa, server._ensure_sa, server._points_in_group,
            server._read_selected_point_records)
    server.sa = fake
    server._ensure_sa = lambda *a, **k: None

    def points_in_group(full):
        pts = store.get(str(full))
        if not pts:
            return []
        obj = str(full).split("::", 1)[-1]
        return [f"{obj}::{t}" for t in pts]

    def read_records(fulls, include_offsets=True):
        recs, unres = [], []
        for full in fulls:
            parts = str(full).split("::")
            target, obj = parts[-1], parts[-2]
            coll = "::".join(parts[:-2])
            key = f"{coll}::{obj}" if coll else f"::{obj}"
            pts = store.get(key)
            if not pts or target not in pts:
                unres.append(full)
                continue
            x, y, z = pts[target]
            recs.append({"name": f"{obj}::{target}", "full": full, "x": x,
                         "y": y, "z": z, "planar_offset": 0.0,
                         "radial_offset": 0.0})
        return recs, unres

    server._points_in_group = points_in_group
    server._read_selected_point_records = read_records
    return fake, store, orig


def _restore(orig):
    (server.sa, server._ensure_sa, server._points_in_group,
     server._read_selected_point_records) = orig


# --- pure helper: matrix application ---------------------------------------
p = server._apply_matrix_to_point(TX, (10.0, 5.0, 0.0))
check("apply_matrix_to_point: translation is column 3",
      p == (0.0, 5.0, 0.0), p)

# --- validation returns before any COM call --------------------------------
fake, store, orig = _install()
try:
    r = server.sa_best_fit_transform(reference_group="", corresponding_group="C")
    check("validation: both groups required", bool(r["error"]) and not fake.trace)
    r = server.sa_best_fit_transform(reference_group="A::REF",
                                     corresponding_group="A::CORR",
                                     allow_x=False, allow_y=False, allow_z=False,
                                     allow_rx=False, allow_ry=False,
                                     allow_rz=False)
    check("validation: at least one DOF required",
          "degree of freedom" in (r["error"] or "") and not fake.trace)
    r = server.sa_best_fit_transform(reference_group="A::REF",
                                     corresponding_group="A::CORR",
                                     max_deviations=-1)
    check("validation: max_deviations >= 0",
          "max_deviations" in (r["error"] or "") and not fake.trace)
finally:
    _restore(orig)

# --- the fit itself: step, args, DOF flags, ordering -----------------------
fake, store, orig = _install()
try:
    r = server.sa_best_fit_transform(reference_group="A::REF",
                                     corresponding_group="A::CORR",
                                     allow_rz=False, allow_scale=True,
                                     rms_tolerance=0.05,
                                     max_abs_tolerance=0.1)
    check("fit: computed", r["computed"], r["error"])
    check("fit: step name",
          bool(fake.idx("set_step",
                        lambda a: a[0] == "Best Fit Transformation - "
                                          "Group to Group")))
    check("fit: reference/corresponding groups set",
          fake.idx("set_collection_object_name_arg",
                   lambda a: a[0] == "Reference Group"
                   and a[1:] == ("A", "REF"))
          and fake.idx("set_collection_object_name_arg",
                       lambda a: a[0] == "Corresponding Group"
                       and a[1:] == ("A", "CORR")))
    check("fit: no results dialog",
          fake.idx("set_bool_arg",
                   lambda a: a[0] == "Show Interface" and a[1] is False))
    check("fit: tolerances passed through",
          fake.idx("set_double_arg",
                   lambda a: a[0] == "RMS Tolerance (0.0 for none)"
                   and a[1] == 0.05)
          and fake.idx("set_double_arg",
                       lambda a: a[0] == "Maximum Absolute Tolerance "
                                         "(0.0 for none)" and a[1] == 0.1))
    check("fit: allow_scale honoured",
          fake.idx("set_bool_arg",
                   lambda a: a[0] == "Allow Scale" and a[1] is True))
    check("fit: disabled DOF is off, the rest on",
          fake.idx("set_bool_arg", lambda a: a[0] == "Allow Rz"
                   and a[1] is False)
          and fake.idx("set_bool_arg", lambda a: a[0] == "Allow Rx"
                       and a[1] is True))
    check("fit: all six DOF args are set",
          all(fake.idx("set_bool_arg", lambda a, d=d: a[0] == d)
              for d in server._FIT_DOF_ARGS))
    last_set, between = fake.sets_before_execute()
    check("fit: nothing runs between the arg setters and ExecuteStep",
          not between, between)
    check("fit: sa stats surfaced",
          r["sa_stats"] == {"rms_deviation": 0.25,
                            "max_absolute_deviation": 0.5}, r["sa_stats"])
    check("fit: no temp groups without exclusions",
          r["refit"] is False and r["temp_objects"] == [])
    check("fit: matrix + fixed xyz returned",
          r["matrix"] == TX and r["fixed_xyz"] is not None)
    check("fit: pairs matched by name", r["pairs"] == 4)
finally:
    _restore(orig)

# --- deviations: recomputed from the transform, worst first ----------------
fake, store, orig = _install()
try:
    r = server.sa_best_fit_transform(reference_group="A::REF",
                                     corresponding_group="A::CORR",
                                     tolerance_mm=0.1)
    devs = {d["target"]: d["deviation"] for d in r["deviations"]}
    check("deviations: inliers are zero, the deformed point is 0.5",
          devs == {"1": 0.0, "2": 0.0, "3": 0.5, "4": 0.0}, devs)
    check("deviations: sorted worst first",
          [d["target"] for d in r["deviations"]][0] == "3", r["deviations"])
    check("deviations: worst_point is the same entry",
          r["worst_point"]["target"] == "3", r["worst_point"])
    check("deviations: outlier flagged, nothing excluded",
          r["outliers"] == ["3"] and r["excluded"] == []
          and all(d["included"] for d in r["deviations"]))
    check("stats: rms over all four points",
          abs(r["stats"]["rms_deviation"] - 0.25) < 1e-9, r["stats"])
    check("stats: max_abs is the deformed point",
          abs(r["stats"]["max_abs_deviation"] - 0.5) < 1e-9, r["stats"])
    check("deviations: cap honoured",
          len(server.sa_best_fit_transform(
              reference_group="A::REF", corresponding_group="A::CORR",
              max_deviations=2)["deviations"]) == 2)
finally:
    _restore(orig)

# --- unmatched names are reported, never paired ----------------------------
fake, store, orig = _install(extra_ref={"9": (200.0, 0.0, 0.0)},
                             extra_corr={"5": (10.0, 90.0, 0.0)})
try:
    r = server.sa_best_fit_transform(reference_group="A::REF",
                                     corresponding_group="A::CORR")
    check("unmatched: reference-only name listed",
          r["unmatched_reference"] == ["9"], r["unmatched_reference"])
    check("unmatched: corresponding-only name listed",
          r["unmatched_corresponding"] == ["5"], r["unmatched_corresponding"])
    check("unmatched: pairs exclude them", r["pairs"] == 4, r["pairs"])
finally:
    _restore(orig)

# no shared names at all
fake, store, orig = _install()
try:
    store["A::CORR"] = {"x1": (0.0, 0.0, 0.0), "x2": (1.0, 0.0, 0.0)}
    r = server.sa_best_fit_transform(reference_group="A::REF",
                                     corresponding_group="A::CORR")
    check("no shared names -> error, no fit",
          "share no point names" in (r["error"] or "") and not r["computed"])
finally:
    _restore(orig)

# --- excluding a point: refit on temp copies -------------------------------
fake, store, orig = _install()
try:
    r = server.sa_best_fit_transform(reference_group="A::REF",
                                     corresponding_group="A::CORR",
                                     exclude_points=["3"])
    check("exclude: refit on temps", r["refit"] is True
          and r["temp_objects"] == ["A::MCP_FITTMP_REF", "A::MCP_FITTMP_CORR"],
          r["temp_objects"])
    copies = fake.idx("set_collection_object_name_arg",
                      lambda a: a[0] == "Source Object")
    check("exclude: BOTH groups copied", len(copies) == 2, copies)
    dels = fake.idx("set_point_name_ref_list_arg",
                    lambda a: a[0] == "Point Names")
    check("exclude: the target deleted from both copies",
          [fake.trace[i][1][1] for i in dels]
          == [["A::MCP_FITTMP_REF::3"], ["A::MCP_FITTMP_CORR::3"]],
          [fake.trace[i][1][1] for i in dels])
    check("exclude: the fit ran on the COPIES, not the sources",
          fake.idx("set_collection_object_name_arg",
                   lambda a: a[0] == "Reference Group"
                   and a[1:] == ("A", "MCP_FITTMP_REF"))
          and fake.idx("set_collection_object_name_arg",
                       lambda a: a[0] == "Corresponding Group"
                       and a[1:] == ("A", "MCP_FITTMP_CORR")))
    check("exclude: temps deleted again",
          store.get("A::MCP_FITTMP_REF") is None
          and store.get("A::MCP_FITTMP_CORR") is None, store.keys())
    check("exclude: the source groups are untouched",
          set(store["A::REF"]) == {"1", "2", "3", "4"}
          and set(store["A::CORR"]) == {"1", "2", "3", "4"})
    check("exclude: dropped point keeps its deviation, flagged out",
          r["excluded"] == ["3"] and r["excluded_deviations"] == {"3": 0.5}
          and not [d for d in r["deviations"]
                   if d["target"] == "3"][0]["included"])
    check("exclude: stats over the INCLUDED points only",
          abs(r["stats"]["rms_deviation"]) < 1e-9
          and r["stats"]["point_count"] == 3, r["stats"])
    check("exclude: worst_point still reports the bad point",
          r["worst_point"]["target"] == "3", r["worst_point"])
finally:
    _restore(orig)

# an exclusion that silently did nothing must NOT pass as a refit
fake, store, orig = _install(break_delete=True)
try:
    r = server.sa_best_fit_transform(reference_group="A::REF",
                                     corresponding_group="A::CORR",
                                     exclude_points=["3"])
    check("exclude: an ineffective delete is caught",
          not r["computed"] and "were not removed" in (r["error"] or ""),
          r["error"])
    check("exclude: temps cleaned even on that failure",
          store.get("A::MCP_FITTMP_REF") is None
          and store.get("A::MCP_FITTMP_CORR") is None, store.keys())
finally:
    _restore(orig)

# unknown exclusion names and excluding everything
fake, store, orig = _install()
try:
    r = server.sa_best_fit_transform(reference_group="A::REF",
                                     corresponding_group="A::CORR",
                                     exclude_points=["1", "77"])
    check("exclude: unknown names reported, not applied",
          r["excluded"] == ["1"] and r["excluded_unknown"] == ["77"], r)
    r = server.sa_best_fit_transform(reference_group="A::REF",
                                     corresponding_group="A::CORR",
                                     exclude_points=["1", "2", "3", "4"])
    check("exclude: excluding everything -> error",
          "nothing is left to fit" in (r["error"] or "") and not r["computed"])
finally:
    _restore(orig)

# a failing fit reports no matrix and cleans its temps
fake, store, orig = _install(fit_code=3)
try:
    r = server.sa_best_fit_transform(reference_group="A::REF",
                                     corresponding_group="A::CORR",
                                     exclude_points=["3"])
    check("failed fit: not computed, status reported",
          not r["computed"] and r["status_code"] == 3
          and r["sa_stats"] == {"rms_deviation": None,
                                "max_absolute_deviation": None}, r["status"])
    check("failed fit: temps still cleaned",
          store.get("A::MCP_FITTMP_REF") is None
          and store.get("A::MCP_FITTMP_CORR") is None)
finally:
    _restore(orig)

# --- apply: the group + companions move, the reference never -----------------
fake, store, orig = _install()
try:
    r = server.sa_best_fit_transform(reference_group="A::REF",
                                     corresponding_group="A::CORR",
                                     apply=True,
                                     move_objects=["A::FRAME1", "A::REF"])
    check("apply: moved group first, then the companion",
          r["moved_objects"] == ["A::CORR", "A::FRAME1"], r["moved_objects"])
    check("apply: the reference group is refused, with a reason",
          r["move_objects_skipped"][0]["object"] == "A::REF",
          r["move_objects_skipped"])
    check("apply: move step + object list",
          fake.idx("set_collection_object_name_ref_list_arg",
                   lambda a: a[0] == "Objects to Transform"
                   and a[1] == ["A::CORR", "A::FRAME1"])
          and fake.idx("set_step",
                       lambda a: a[0] == "Transform Objects by Delta "
                                         "(About Working Frame)"))
    check("apply: the transform really moved the group onto the reference",
          abs(r["stats_after_move"]["rms_deviation"] - 0.25) < 1e-9,
          r["stats_after_move"])
    check("apply: applied flag set", r["applied"] is True, r["error"])
    check("apply: the reference group did not move",
          store["A::REF"] == REF, store["A::REF"])
finally:
    _restore(orig)

# apply without verification skips the re-read
fake, store, orig = _install()
try:
    r = server.sa_best_fit_transform(reference_group="A::REF",
                                     corresponding_group="A::CORR",
                                     apply=True, verify_after_move=False)
    check("apply: verify_after_move=False skips the re-read",
          r["stats_after_move"] is None and r["applied"], r["stats_after_move"])
finally:
    _restore(orig)

print()
if fails:
    print(f"{len(fails)} FAILURES:")
    for f in fails:
        print(" -", f)
    raise SystemExit(1)
print("ALL OK")
