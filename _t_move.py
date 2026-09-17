# -*- coding: utf-8 -*-
"""Offline regression for the move/transform tools' pure logic.

No SA, no COM: exercises the transform-matrix helpers (_matrix_to_nested /
_variant_to_matrix / _matrix_variant) and the input validation of
sa_move_objects / sa_object_transform (the COM half is covered by
_live_transform.py).
Run: python _t_move.py
"""

import pythoncom

import sa_sdk
import server

fails = []


def check(desc, cond, extra=""):
    tag = "PASS" if cond else "FAIL"
    print(f"{tag} {desc} {extra}")
    if not cond:
        fails.append(desc)


# --- _matrix_to_nested: 4x4 normalization + shape guard -------------------
ident = [[1, 0, 0, 5], [0, 1, 0, 6], [0, 0, 1, 7], [0, 0, 0, 1]]
nested = sa_sdk._matrix_to_nested(ident)
check("matrix_to_nested: floats", nested == [
    [1.0, 0.0, 0.0, 5.0], [0.0, 1.0, 0.0, 6.0],
    [0.0, 0.0, 1.0, 7.0], [0.0, 0.0, 0.0, 1.0]], str(nested))
check("matrix_to_nested: returns plain list-of-lists",
      isinstance(nested, list) and isinstance(nested[0], list))

for bad, label in (([1, 2, 3], "3 rows"),
                   ([[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]], "3 cols"),
                   ([[1, 2, 3, 4]] * 5, "5 rows")):
    try:
        sa_sdk._matrix_to_nested(bad)
        check(f"matrix_to_nested: rejects {label}", False, "no raise")
    except sa_sdk.SAError:
        check(f"matrix_to_nested: rejects {label}", True)

# --- _matrix_variant: the transport contract (SA Transform args) ----------
# SA re-reads the transform SAFEARRAY's raw bytes as doubles, so the VARIANT
# must declare VT_ARRAY|VT_R8; a VT_ARRAY|VT_VARIANT (what a bare Python list
# produces) is read back as garbage. Pin the declaration.
mv = sa_sdk._matrix_variant(ident)
check("matrix_variant: declared VT_ARRAY|VT_R8",
      mv.varianttype == (pythoncom.VT_ARRAY | pythoncom.VT_R8),
      f"0x{mv.varianttype:04x}")
check("matrix_variant: value is the nested 4x4",
      [list(r) for r in mv.value] ==
      [[1.0, 0.0, 0.0, 5.0], [0.0, 1.0, 0.0, 6.0],
       [0.0, 0.0, 1.0, 7.0], [0.0, 0.0, 0.0, 1.0]], str(mv.value))

# --- _variant_to_matrix: pywin32 nested tuple / flat / empty --------------
check("variant_to_matrix: nested tuples",
      sa_sdk._variant_to_matrix(((1, 0, 0, 5), (0, 1, 0, 6),
                                 (0, 0, 1, 7), (0, 0, 0, 1))) ==
      [[1.0, 0.0, 0.0, 5.0], [0.0, 1.0, 0.0, 6.0],
       [0.0, 0.0, 1.0, 7.0], [0.0, 0.0, 0.0, 1.0]])
flat = [float(i) for i in range(16)]
check("variant_to_matrix: flat 16 reshaped row-major",
      sa_sdk._variant_to_matrix(flat) ==
      [flat[0:4], flat[4:8], flat[8:12], flat[12:16]])
check("variant_to_matrix: None -> []", sa_sdk._variant_to_matrix(None) == [])
check("variant_to_matrix: non-numeric -> []",
      sa_sdk._variant_to_matrix(["a", "b"]) == [])
check("variant_to_matrix: nested 6-double vector kept",
      sa_sdk._variant_to_matrix([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]) ==
      [[1.0, 2.0, 3.0, 4.0, 5.0, 6.0]])

# --- mode table -----------------------------------------------------------
check("modes: all four defined",
      set(server._MOVE_MODES) ==
      {"about_working_frame", "world", "translate", "frame_to_frame"})

# --- sa_move_objects validation (returns before any bridge/COM call) ------
called = []
_orig_ensure = server._ensure_sa


def _fake_ensure(*a, **k):
    called.append(1)
    raise server.SAError("bridge-not-expected-offline")


server._ensure_sa = _fake_ensure
try:
    r = server.sa_move_objects(objects=[])
    check("move: no objects -> error before bridge",
          bool(r.get("error")) and r["moved"] is False and not called, str(r))

    r = server.sa_move_objects(objects=["A::X"], mode="bogus")
    check("move: unknown mode -> error before bridge",
          "unknown mode" in r["error"] and not called, str(r))

    r = server.sa_move_objects(objects=["A::X"], mode="translate", rx=10.0)
    check("move: translate + rotation -> error before bridge",
          "translation only" in r["error"] and not called, str(r))

    r = server.sa_move_objects(objects=["A::X"], mode="frame_to_frame",
                               source_frame="С1")
    check("move: frame_to_frame missing destination -> error before bridge",
          "destination_frame" in r["error"] and not called, str(r))

    # validation passes -> names resolved, then the fake bridge error lands.
    r = server.sa_move_objects(objects=["X", "A::Y"], collection="A", dx=1.0)
    check("move: full names resolved + default mode",
          r["objects"] == ["A::X", "A::Y"] and
          r["mode"] == "about_working_frame" and called, str(r))
    check("move: reached bridge -> bridge error surfaced",
          "bridge-not-expected-offline" in (r.get("error") or ""), str(r))

    called[:] = []
    r = server.sa_move_objects(objects=["X"], collection="A",
                               mode=" Translate ")
    check("move: mode is trimmed/lowercased",
          r["mode"] == "translate" and called, str(r))

    called[:] = []
    r = server.sa_object_transform(object="X", collection="A")
    check("object_transform: full name resolved + bridge reached",
          r["object"] == "A::X" and
          "bridge-not-expected-offline" in (r.get("error") or "") and called,
          str(r))
finally:
    server._ensure_sa = _orig_ensure

print()
if fails:
    print(f"{len(fails)} FAILURE(S): {fails}")
    raise SystemExit(1)
print("ALL OK")
