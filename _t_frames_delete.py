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

# --- sa_set_working_frame / sa_current_working_frame validation -----------
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
