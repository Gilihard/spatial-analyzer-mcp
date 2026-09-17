# -*- coding: utf-8 -*-
"""Offline regression for sa_unify_groups (no SA, no COM).

Pins the contract that was hard-won live:
  - argument names of 'Locate Instruments (USMN)' and their order, with
    NOTHING running between the last Set*Arg and ExecuteStep;
  - 'Groups to be Excluded' gets every OTHER point group of the collection
    (a group reusing target names is what made USMN fail silently);
  - the solve is retried: a code-3 attempt is not accepted, a code-2 attempt
    that created no points is not accepted either;
  - method='fit' copies every source, fits each copy onto the reference,
    applies the transform, averages the COPIES, then deletes them;
  - validation returns before any COM call.
Run: python _t_unify.py
"""
import server

fails = []


def check(desc, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + desc + " " + str(extra))
    if not cond:
        fails.append(desc)


class _FakeBridge:
    """Records every call; 'Locate Instruments (USMN)' returns scripted codes."""

    def __init__(self, usmn_codes=(3, 2), usmn_points=(0, 7)):
        self.trace = []
        self.step = None
        self.usmn_codes = list(usmn_codes)
        self.usmn_points = list(usmn_points)
        self.calls = 0

    # -- recording ---------------------------------------------------------
    def set_step(self, s):
        self.trace.append(("set_step", (s,)))
        self.step = s

    def execute_step(self):
        self.trace.append(("execute_step", (self.step,)))

    def get_step_result(self):
        if self.step == "Locate Instruments (USMN)":
            code = self.usmn_codes[min(self.calls,
                                       len(self.usmn_codes) - 1)]
            self.calls += 1
            return code
        return 2

    def _rec(self, name, *args):
        self.trace.append((name, args))
        return True

    def set_col_inst_id_ref_list_arg(self, n, v):
        return self._rec("set_col_inst_id_ref_list_arg", n, list(v))

    def set_collection_object_name_ref_list_arg(self, n, v):
        return self._rec("set_collection_object_name_ref_list_arg", n, list(v))

    def set_collection_object_name_arg(self, n, *rest):
        return self._rec("set_collection_object_name_arg", n, *rest)

    def set_bool_arg(self, n, v):
        return self._rec("set_bool_arg", n, v)

    def set_double_arg(self, n, v):
        return self._rec("set_double_arg", n, v)

    def set_transform_arg(self, n, m):
        return self._rec("set_transform_arg", n, m)

    def get_double_arg(self, n):
        return 0.0123

    def get_transform_arg(self, n):
        return [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]

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


def _install(result_points=7, **kwargs):
    fake = _FakeBridge(*kwargs.get("codes", ((3, 2), (0, 7))))
    seen = {"points": [], "deleted": []}
    orig = (server.sa, server._ensure_sa, server.sa_delete,
            server._instruments_on_group, server._objects_in_collection_by_type,
            server._points_in_group, server._group_agreement)
    server.sa = fake
    server._ensure_sa = lambda *a, **k: None
    server.sa_delete = lambda objects=None, points=None: (
        seen["deleted"].extend(objects or []) or {"objects_deleted": True})
    server._instruments_on_group = (
        lambda full, limit=10: ["A::0", "A::2", "A::3"])
    server._objects_in_collection_by_type = lambda coll, t: [
        "A::т контур", "A::БО", "A::Опорная сеть", "A::LocateInstMeas1",
        "A::LocateInstMeas1*"]
    server._points_in_group = lambda full: (
        seen["points"].append(full)
        or (["x"] * result_points if "Средняя" in full else []))
    server._group_agreement = lambda src, res: {
        "overall": {"rms": 0.02, "max_absolute": 0.03, "pairs": 17}}
    return fake, seen, orig


def _restore(orig):
    (server.sa, server._ensure_sa, server.sa_delete,
     server._instruments_on_group, server._objects_in_collection_by_type,
     server._points_in_group, server._group_agreement) = orig


SRC = ["A::Опорная сеть", "A::LocateInstMeas1", "A::LocateInstMeas1*"]

# --- validation returns before any COM call --------------------------------
fake = _FakeBridge()
orig = (server.sa, server._ensure_sa, server.sa_delete,
        server._instruments_on_group, server._objects_in_collection_by_type,
        server._points_in_group, server._group_agreement)
server.sa = fake
try:
    r = server.sa_unify_groups(source_groups=SRC, result_group="")
    check("empty result_group -> error, no COM", bool(r["error"]) and not fake.trace, r)
    r = server.sa_unify_groups(source_groups=[SRC[0]], result_group="A::X")
    check("one source group -> error", "At least two" in (r["error"] or ""), r)
    r = server.sa_unify_groups(source_groups=SRC, result_group="A::X",
                               method="bogus")
    check("unknown method -> error", "Unknown method" in (r["error"] or ""), r)
    check("no COM before validation errors", not fake.trace, fake.trace[:3])
finally:
    _restore(orig)

# --- USMN argument contract + retry ---------------------------------------
fake, seen, orig = _install(codes=((3, 2), (0, 7)))
try:
    r = server.sa_unify_groups(source_groups=SRC,
                               result_group="A::Средняя", method="usmn")
    check("usmn: unified after retry", r["unified"] and r["attempts"] == 2, r)
    check("usmn: instruments derived from the groups",
          r["instruments"] == ["A::0", "A::2", "A::3"], r["instruments"])
    check("usmn: other point groups excluded",
          r["groups_excluded"] == ["A::т контур", "A::БО"], r["groups_excluded"])
    check("usmn: instruments set as a Col-Inst-Id ref list",
          fake.idx("set_col_inst_id_ref_list_arg",
                   lambda a: a[0] == "Instruments to Locate"), fake.trace[:6])
    check("usmn: excluded groups set as a Collection-Object ref list",
          fake.idx("set_collection_object_name_ref_list_arg",
                   lambda a: a[0] == "Groups to be Excluded"))
    check("usmn: output group set as a Collection Object Name",
          fake.idx("set_collection_object_name_arg",
                   lambda a: a[0] == "Output Group Name (to be established)"
                   and a[1:] == ("A", "Средняя")))
    check("usmn: nominals passed blank",
          fake.idx("set_collection_object_name_arg",
                   lambda a: a[0] == "Nominals Group Name (blank for none)"
                   and a[1:] == ("", "")))
    check("usmn: AutoReject bool named exactly",
          fake.idx("set_bool_arg",
                   lambda a: a[0] == "AutoReject Outliers and Resolve"
                   and a[1] is True))
    last_set, between = fake.sets_before_execute()
    check("usmn: nothing runs between the arg setters and ExecuteStep",
          not between, between)
    check("usmn: delete-before-create of the result group",
          "A::Средняя" in seen["deleted"], seen["deleted"])
    check("usmn: agreement computed over every source pair",
          (r["agreement"] or {}).get("overall", {}).get("pairs") == 17,
          r["agreement"])
    check("usmn: the step's own RMS wins over the agreement fallback",
          r["stats"]["rms"] == 0.0123, r["stats"])
finally:
    _restore(orig)

# code 2 but an empty result group is NOT a success
fake, seen, orig = _install(result_points=0, codes=((2, 2), (0, 0)))
try:
    r = server.sa_unify_groups(source_groups=SRC, result_group="A::Средняя")
    check("usmn: code 2 with no points -> not unified, all attempts spent",
          not r["unified"] and r["attempts"] == 4 and bool(r["error"]), r)
    check("usmn: failure names the target-name collision as the suspect",
          "reusing target names" in r["error"], r["error"])
finally:
    _restore(orig)

# --- fit method: copy -> fit -> apply -> average the copies ----------------
fake, seen, orig = _install(codes=((2, 2), (7, 7)))
try:
    r = server.sa_unify_groups(source_groups=SRC,
                               result_group="A::Средняя", method="fit")
    copies = fake.idx("set_collection_object_name_arg",
                      lambda a: a[0] == "New Object Name")
    check("fit: every source copied", len(copies) == 3, copies)
    fits = fake.idx("set_step",
                    lambda a: a[0] == "Best Fit Transformation - Group to Group")
    check("fit: one fit per non-reference source", len(fits) == 2, fits)
    check("fit: reference group defaults to the first source",
          r["reference_group"] == "A::Опорная сеть", r["reference_group"])
    check("fit: transform applied with the right step",
          fake.idx("set_step",
                   lambda a: a[0] == "Transform Objects by Delta "
                                     "(About Working Frame)"))
    avg = fake.idx("set_step", lambda a: a[0] == "Average a set of Groups")
    check("fit: averaged the aligned copies", bool(avg), fake.trace[-4:])
    grp = fake.idx("set_collection_object_name_ref_list_arg",
                   lambda a: a[0] == "Group Names")
    used = fake.trace[grp[-1]][1][1] if grp else []
    check("fit: average runs on the temp copies, not the sources",
          len(used) == 3 and all("MCP_UNIFY_" in u for u in used), used)
    check("fit: temps deleted afterwards",
          sum("MCP_UNIFY_" in d for d in seen["deleted"]) == 3,
          seen["deleted"])
    check("fit: alignment reported per group",
          r["alignment"]["A::LocateInstMeas1"]["rms"] == 0.0123, r["alignment"])
    check("fit: unified", r["unified"], r)
finally:
    _restore(orig)

print()
if fails:
    print(f"{len(fails)} FAILURES:")
    for f in fails:
        print(" -", f)
    raise SystemExit(1)
print("ALL OK")
