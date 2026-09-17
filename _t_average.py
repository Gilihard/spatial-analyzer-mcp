# -*- coding: utf-8 -*-
"""Offline regression for sa_average_groups' pure logic ('Average a set of
Groups'). No SA, no COM: a recording fake bridge pins the arg names, the
setter TYPES, the delete-before-create ordering and the "success is decided by
the result group's content" rule. The live half (the mean really is the
arithmetic mean, matching is by target name) is covered by _live_average.py.

Run: python _t_average.py
"""

import server

fails = []


def check(desc, cond, extra=""):
    tag = "PASS" if cond else "FAIL"
    print(f"{tag} {desc} {extra}")
    if not cond:
        fails.append(desc)


class _FakeBridge:
    """Records every Set*Arg / step call; get_double_arg returns a marker."""

    def __init__(self):
        self.trace = []
        self.step = None

    def _rec(self, name, *args):
        self.trace.append((name, args))
        return True

    def set_step(self, step):
        self.trace.append(("set_step", (step,)))
        self.step = step

    def execute_step(self):
        self.trace.append(("execute_step", (self.step,)))
        return True

    def get_step_result(self):
        return 2

    def set_collection_object_name_ref_list_arg(self, name, value):
        return self._rec("set_co_ref_list", name, value)

    def set_collection_object_name_arg(self, name, *rest):
        return self._rec("set_co_name", name, *rest)

    def set_double_arg(self, name, value):
        return self._rec("set_double", name, value)

    def get_double_arg(self, name):
        self.trace.append(("get_double", (name,)))
        return 0.5

    def idx(self, name, pred=lambda a: True):
        return [i for i, (n, a) in enumerate(self.trace)
                if n == name and pred(a)]


def _run(points=("1", "2"), **kwargs):
    fake = _FakeBridge()
    orig = (server.sa, server._ensure_sa, server._safe_messages,
            server._points_in_group)
    server.sa = fake
    server._ensure_sa = lambda *a, **k: None
    server._safe_messages = lambda: []
    server._points_in_group = lambda full: list(points)
    try:
        return server.sa_average_groups(**kwargs), fake
    finally:
        (server.sa, server._ensure_sa, server._safe_messages,
         server._points_in_group) = orig


# --- validation (returns before the bridge) --------------------------------
called = []
_orig_ensure = server._ensure_sa
server._ensure_sa = lambda *a, **k: called.append(1) or None
try:
    r = server.sa_average_groups(source_groups=["A::G1", "A::G2"],
                                 result_group="")
    check("empty result_group -> error before the bridge",
          bool(r.get("error")) and not r["averaged"] and not called, str(r))
    r = server.sa_average_groups(source_groups=["A::G1"],
                                 result_group="Средняя")
    check("one source group -> error before the bridge",
          bool(r.get("error")) and not r["averaged"] and not called, str(r))
    r = server.sa_average_groups(source_groups=[], result_group="Средняя")
    check("no source groups -> error", bool(r.get("error")) and not called,
          str(r))
finally:
    server._ensure_sa = _orig_ensure

# --- happy path: arg names, setter types, ordering -------------------------
res, fake = _run(source_groups=["G1", "G2"], result_group="Средняя",
                 collection="A", rms_tolerance=0.05, max_abs_tolerance=0.1,
                 max_average_tolerance=0.07)
check("full names resolved from bare names",
      res["source_groups"] == ["A::G1", "A::G2"] and
      res["result_group"] == "A::Средняя", str(res))
check("source groups go through the REF LIST setter under 'Group Names'",
      fake.idx("set_co_ref_list",
               lambda a: a[0] == "Group Names" and a[1] == ["A::G1", "A::G2"]),
      str(fake.trace))
check("result name goes through the COLLECTION OBJECT NAME setter",
      fake.idx("set_co_name",
               lambda a: a[0] == "Resulting Group Name" and a[1] == "A"
               and a[2] == "Средняя"),
      str(fake.trace))
for arg, val in (("RMS Tolerance (0.0 for none)", 0.05),
                 ("Maximum Absolute Tolerance (0.0 for none)", 0.1),
                 ("Maximum Average Tolerance (0.0 for none)", 0.07)):
    check(f"tolerance '{arg}' set as a Double",
          fake.idx("set_double", lambda a, n=arg, v=val: a[0] == n and a[1] == v),
          str(fake.trace))

check("step armed as 'Average a set of Groups'",
      fake.idx("set_step", lambda a: a[0] == "Average a set of Groups"),
      str(fake.trace))
last_set = max(fake.idx("set_double"))
exec_i = fake.idx("execute_step", lambda a: a[0] == "Average a set of Groups")[0]
check("nothing runs between the arg setters and ExecuteStep",
      not [a[0] for n, a in fake.trace[last_set:exec_i] if n == "set_step"],
      str(fake.trace[last_set:exec_i]))
check("delete-before-create runs BEFORE the step is armed",
      max(fake.idx("set_step", lambda a: a[0] == "Delete Objects")) <
      fake.idx("set_step", lambda a: a[0] == "Average a set of Groups")[0],
      str(fake.trace))

check("success decided by content -> averaged + result_count",
      res["averaged"] is True and res["result_count"] == 2, str(res))
check("stats read back under the PDF output names",
      [a[0] for a in [t[1] for t in fake.trace if t[0] == "get_double"]] ==
      ["RMS Deviation", "Max Absolute Deviation", "Average Deviation"] and
      res["stats"] == {"rms": 0.5, "max_absolute": 0.5, "average": 0.5},
      str(res["stats"]))

# --- an empty result group is a FAILURE even with DoneSuccess -------------
res, fake = _run(points=(), source_groups=["A::G1", "A::G2"],
                 result_group="Средняя")
check("empty result group -> not averaged, with an explanatory error",
      res["averaged"] is False and res["result_count"] == 0 and
      "no group" in (res.get("error") or ""), str(res))

# --- delete_existing=False leaves the same-named group alone --------------
res, fake = _run(source_groups=["A::G1", "A::G2"], result_group="Средняя",
                 collection="A", delete_existing=False)
check("delete_existing=False -> no 'Delete Objects' step",
      not fake.idx("set_step", lambda a: a[0] == "Delete Objects"), str(fake.trace))
check("... and replaced stays False", res["replaced"] is False, str(res))

print()
if fails:
    print(f"{len(fails)} FAILURES:")
    for f in fails:
        print(" -", f)
    raise SystemExit(1)
print("ALL OK")
