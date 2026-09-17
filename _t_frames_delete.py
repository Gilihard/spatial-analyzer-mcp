# -*- coding: utf-8 -*-
"""Offline regression for the delete + frame (СК) tools' pure logic.

No SA, no COM: exercises the name normalization and input validation of
sa_delete / sa_create_frame / sa_set_working_frame / sa_current_working_frame
(the COM half is covered by _live_frames_delete.py, pending live SA).
Run: python _t_frames_delete.py
"""

import server

fails = []


def check(desc, cond, extra=""):
    tag = "PASS" if cond else "FAIL"
    print(f"{tag} {desc} {extra}")
    if not cond:
        fails.append(desc)


class no_sa:
    """Force the 'SA GUI is not running' path for a block.

    Without this the suite would call the real bridge whenever an SA instance
    happens to be up (e.g. right after a live harness) - the assertions below
    about the offline error would fail AND the run would quietly drive the
    loaded job (it really did activate WORLD once).
    """

    def __enter__(self):
        self.orig = server._ensure_sa

        def _raise(*a, **k):
            raise server.SAError("SpatialAnalyzer GUI is not running. "
                                 "Start it first via sa_launch.")

        server._ensure_sa = _raise
        return self

    def __exit__(self, *exc):
        server._ensure_sa = self.orig
        return False


# --- _co_name_parts: "C::O" full-name splitting ----------------------------
check("co parts: full A::name", server._co_name_parts("A::т контур")
      == ["A", "т контур"])
check("co parts: leading :: (current collection)", server._co_name_parts("::X")
      == ("", "X"))
check("co parts: bare name", server._co_name_parts("WORLD") == ("", "WORLD"))
check("co parts: deep name keeps rest", server._co_name_parts("A::B::C")
      == ["A", "B::C"])

# --- sa_delete validation (returns before any bridge/COM call) ------------
called = []
_orig_ensure = server._ensure_sa
server._ensure_sa = lambda *a, **k: called.append(1) or None
try:
    r = server.sa_delete()
    check("sa_delete no args -> error before bridge",
          bool(r.get("error")) and r["deleted"] is False and not called,
          str(r))
    r = server.sa_delete(objects=["A::Cyl", "A::плоскость"], points=[])
    check("sa_delete resolves object names offline",
          r["objects"] == ["A::Cyl", "A::плоскость"] and
          r["points"] == [] and called, str(r))
    r = server.sa_delete(points=["1", "т контур::2"],
                         group="т контур", collection="A")
    check("sa_delete resolves point names offline",
          r["points"] == ["A::т контур::1", "A::т контур::2"], str(r))
finally:
    server._ensure_sa = _orig_ensure

# --- sa_create_frame validation -------------------------------------------
with no_sa():
    r = server.sa_create_frame(frame_name="")
    check("create frame: empty name -> error", bool(r.get("error")), str(r))

    r = server.sa_create_frame(frame_name="СК1", method="bogus")
    check("create frame: unknown method -> error", bool(r.get("error")), str(r))

    r = server.sa_create_frame(frame_name="СК1", method="on_object")
    check("create frame: on_object without reference -> error",
          bool(r.get("error")), str(r))

    r = server.sa_create_frame(frame_name="СК1", origin_point="1")
    check("create frame: origin_x_axis without x point -> error",
          bool(r.get("error")), str(r))

    r = server.sa_create_frame(frame_name="СК1",
                               reference_object="A::MCP_BF_плоскость")
    check("create frame: on_object accepted offline (bridge skipped on error?)",
          r["step"] == "Construct Frame On Object" and
          r["full_name"] == "::СК1", str(r))
    # The COM half then failed (no SA) - that is expected offline, but proves
    # validation passed and the tool proceeded to the bridge.
    check("create frame: reached bridge -> SA not running error",
          "not running" in (r.get("error") or ""), str(r))

    r = server.sa_create_frame(frame_name="СК2",
                               origin_point="A::гр::1",
                               point_on_x_axis="A::гр::2",
                               group="гр")
    check("create frame: origin_x_axis picked + full name",
          r["method"] == "origin_x_axis" and
          r["step"].startswith("Construct Frame, Pick origin"),
          str(r))


# --- sa_create_frame against a recording fake bridge ----------------------
# Pins three things that were wrong until they were checked live:
#   1. the result-name arg is a Collection Object Name in 'Construct Frame On
#      Object' but a plain Frame Name in the two-point step - the wrong setter
#      is accepted by the SDK and then silently ignored by SA;
#   2. the frame lands in the ACTIVE collection (no collection arg), so the
#      requested one must be activated first and restored afterwards;
#   3. NOTHING may run between the Set*Arg calls and ExecuteStep - any other
#      step re-arms the engine and ExecuteStep then runs THAT step ("success",
#      nothing created).
class _FakeBridge:
    def __init__(self, active="A", frames=()):
        self.trace = []
        self.active = active
        self.frames = list(frames)
        self.step = None

    # -- recording ---------------------------------------------------------
    def _rec(self, name, *args):
        self.trace.append((name, args))
        return True

    def set_step(self, step):
        self.trace.append(("set_step", (step,)))
        self.step = step

    def execute_step(self):
        self.trace.append(("execute_step", (self.step,)))
        if self.step == "Set (or construct) default collection":
            self.active = self._pending_collection

    def get_step_result(self):
        return 2

    def set_collection_name_arg(self, name, value):
        self._pending_collection = value
        return self._rec("set_collection_name_arg", name, value)

    def get_collection_name_arg(self, name):
        self.trace.append(("get_collection_name_arg", (name,)))
        return self.active

    def set_collection_object_name_ref_list_arg(self, name, value):
        return self._rec("set_collection_object_name_ref_list_arg", name, value)

    def set_collection_object_name_arg(self, name, *rest):
        return self._rec("set_collection_object_name_arg", name, *rest)

    def set_frame_name_arg(self, name, value):
        return self._rec("set_frame_name_arg", name, value)

    def set_point_name_arg(self, *args):
        return self._rec("set_point_name_arg", *args)

    def set_string_arg(self, name, value):
        return self._rec("set_string_arg", name, value)

    # -- helpers for the assertions ---------------------------------------
    def idx(self, name, pred=lambda a: True):
        return [i for i, (n, a) in enumerate(self.trace)
                if n == name and pred(a)]

    def steps_between(self, i0, i1):
        return [a[0] for n, a in self.trace[i0:i1] if n == "set_step"]


def _run_create(active="A", frames_before=(), frames_after=(), **kwargs):
    fake = _FakeBridge(active=active)
    seen = {"n": 0}
    orig_sa, orig_ensure = server.sa, server._ensure_sa
    orig_names, orig_msgs = server._frame_full_names, server._safe_messages

    def fake_names(collection):
        seen["n"] += 1
        fake.trace.append((f"frame_names#{seen['n']}", ()))
        return list(frames_after if seen["n"] > 1 else frames_before)

    server.sa = fake
    server._ensure_sa = lambda *a, **k: None
    server._frame_full_names = fake_names
    server._safe_messages = lambda: []
    try:
        return server.sa_create_frame(**kwargs), fake
    finally:
        server.sa, server._ensure_sa = orig_sa, orig_ensure
        server._frame_full_names, server._safe_messages = orig_names, orig_msgs


# method='on_object' -> Collection Object Name setter, Frame-Name setter unused
res, fake = _run_create(frame_name="СК1", reference_object="A::Пл",
                        collection="A")
check("frame fake: on_object uses the collection-object name setter",
      fake.idx("set_collection_object_name_arg",
               lambda a: a[0] == "Frame Name (Optional)") and
      not fake.idx("set_frame_name_arg"), str(fake.trace))

# method='origin_x_axis' -> plain Frame Name setter, collection-object one
# must NOT be used for the name
res, fake = _run_create(frame_name="СК2", origin_point="A::гр::1",
                        point_on_x_axis="A::гр::2", group="гр", collection="A")
check("frame fake: origin_x_axis uses the frame-name setter",
      fake.idx("set_frame_name_arg",
               lambda a: a[0] == "Frame Name (Optional)" and a[1] == "СК2"),
      str(fake.trace))
check("frame fake: origin_x_axis never sets the name as an object name",
      not fake.idx("set_collection_object_name_arg",
                   lambda a: str(a[0]).startswith("Frame Name")),
      str(fake.trace))

# ordering: no other step between the last Set*Arg and ExecuteStep
last_set = max(fake.idx("set_frame_name_arg") + fake.idx("set_point_name_arg"))
exec_i = fake.idx("execute_step")[-1]
check("frame fake: nothing runs between the arg setters and ExecuteStep",
      not fake.steps_between(last_set, exec_i),
      f"steps={fake.steps_between(last_set, exec_i)}")
# and the frame enumeration happens BEFORE the step is armed
check("frame fake: frame list enumerated before the step is armed",
      fake.idx("frame_names#1")[0] < fake.idx("set_step")[-1],
      str(fake.idx("frame_names#1")) + str(fake.idx("set_step")))

# active-collection handling: requested A while B is active -> switch + restore
res, fake = _run_create(active="B", frame_name="СК3", origin_point="A::гр::1",
                        point_on_x_axis="A::гр::2", group="гр", collection="A")
check("frame fake: switches the active collection to the requested one",
      fake.idx("set_collection_name_arg",
               lambda a: a[0] == "Collection Name" and a[1] == "A") and
      res.get("active_collection_before") == "B" and
      res.get("collection_activated") is True and
      res.get("active_collection_after") == "B", str(res))
switch_i = fake.idx("set_step", lambda a: a[0] == "Set (or construct) default "
                                        "collection")[0]
frame_step_i = fake.idx("set_step", lambda a: a[0].startswith("Construct Frame"))[0]
check("frame fake: activation precedes the frame step", switch_i < frame_step_i)

# the created frame is verified by enumeration (name really applied)
res, fake = _run_create(active="A", frames_before=["A::WORLD"],
                        frames_after=["A::WORLD", "A::СК4"], frame_name="СК4",
                        origin_point="A::гр::1", point_on_x_axis="A::гр::2",
                        group="гр", collection="A")
check("frame fake: name verified -> created_objects + name_applied",
      res["created"] and res["name_applied"] is True and
      res["created_objects"] == ["A::СК4"] and not res.get("warning"), str(res))

# ... and an auto-named (unnamed) frame is reported, not silently accepted
res, fake = _run_create(active="A", frames_before=["A::WORLD"],
                        frames_after=["A::WORLD", "A::Система координат"],
                        frame_name="СК5", origin_point="A::гр::1",
                        point_on_x_axis="A::гр::2", group="гр", collection="A")
check("frame fake: auto-named frame -> warning, not a silent pass",
      res["created"] and res["name_applied"] is False and
      "NOT under the requested name" in res.get("warning", ""), str(res))

# --- sa_set_working_frame / sa_current_working_frame validation -----------
with no_sa():
    r = server.sa_set_working_frame(frame_name="   ")
    check("set working frame: empty name -> error", bool(r.get("error")), str(r))

    r = server.sa_set_working_frame(frame_name="WORLD")
    check("set working frame: accepted offline -> SA not running error",
          "not running" in (r.get("error") or ""), str(r))

    r = server.sa_current_working_frame()
    check("current working frame offline -> SA not running error",
          "not running" in (r.get("error") or ""), str(r))

print()
if fails:
    print(f"{len(fails)} FAILURES:")
    for f in fails:
        print(" -", f)
    raise SystemExit(1)
print("ALL OK")
