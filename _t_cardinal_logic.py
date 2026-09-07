# -*- coding: utf-8 -*-
"""Offline logic test of server._purge_auto_cardinal_groups (no SA, no COM).

Mocks the two COM-backed helpers so only the name-filtering + delete-selection
logic is exercised, against the exact naming SA 2015 produced live
("<geometry name>Кардинальные точки").
"""
import server

CALLS = []
server._delete_geometry_objects = lambda coll, names: CALLS.append((coll, list(names))) or True
PG_NAMES = []


def _fake_enum(coll, t):
    return list(PG_NAMES)


server._objects_in_collection_by_type = _fake_enum

FAILS = []


def check(label, cond, extra=""):
    print(f"  CHECK {label}: {'PASS' if cond else 'FAIL'} {extra}", flush=True)
    if not cond:
        FAILS.append(label)


# 1) plane fit named MCP_PROJ_PLN -> only its cardinal group is deleted
PG_NAMES = ["A::Опорная сеть", "A::т контур", "A::БО",
            "A::MCP_PROJ_PLNКардинальные точки",
            "A::MCP_PROJ_SRC_PLN",       # plain source group, same prefix
            "A::MCP_PROJ_PLN_proj",      # projection result, same prefix
            "A::MCP_PROJ_PLNКардинальные точки1"]  # SA suffixed-duplicate variant
CALLS.clear()
got = server._purge_auto_cardinal_groups("A", "MCP_PROJ_PLN")
check("T1 deletes both cardinal groups (plain + suffixed dup), nothing else",
      sorted(got) == ["A::MCP_PROJ_PLNКардинальные точки",
                      "A::MCP_PROJ_PLNКардинальные точки1"]
      and CALLS == [("A", ["MCP_PROJ_PLNКардинальные точки",
                           "MCP_PROJ_PLNКардинальные точки1"])],
      f"got={got} calls={CALLS}")

# 2) geometry named MCP_PROJ_SRC_PLN (a source group) -> nothing deleted
CALLS.clear()
got = server._purge_auto_cardinal_groups("A", "MCP_PROJ_SRC_PLN")
check("T2 source-group name deletes nothing", got == [] and CALLS == [],
      f"got={got} calls={CALLS}")

# 3) cylinder fit -> its own cardinal group only
PG_NAMES = ["A::MCP_PROJ_CYLКардинальные точки", "A::MCP_PROJ_CYL",
            "A::MCP_PROJ_CYL_R"]
CALLS.clear()
got = server._purge_auto_cardinal_groups("A", "MCP_PROJ_CYL")
check("T3 cylinder cardinal group only",
      got == ["A::MCP_PROJ_CYLКардинальные точки"]
      and CALLS == [("A", ["MCP_PROJ_CYLКардинальные точки"])],
      f"got={got} calls={CALLS}")

# 4) real group 'т контур' fit -> its cardinal group caught (not 'т контур1')
PG_NAMES = ["A::т контурКардинальные точки", "A::т контур", "A::т контур1"]
CALLS.clear()
got = server._purge_auto_cardinal_groups("A", "т контур")
check("T4 real-group fit deletes only its cardinal group",
      got == ["A::т контурКардинальные точки"]
      and CALLS == [("A", ["т контурКардинальные точки"])],
      f"got={got} calls={CALLS}")

# 5) English SA naming variants are matched too
PG_NAMES = ["A::Fit1Cardinal Points", "A::Fit1", "A::Fit1Cardinal Pts",
            "A::Anything", "A::Fit1_proj"]
CALLS.clear()
got = server._purge_auto_cardinal_groups("A", "Fit1")
check("T5 English suffix variants caught",
      sorted(got) == ["A::Fit1Cardinal Points", "A::Fit1Cardinal Pts"]
      and CALLS == [("A", ["Fit1Cardinal Points", "Fit1Cardinal Pts"])],
      f"got={got} calls={CALLS}")

# 6) object name that itself contains 'cardinal' still matches only the suffix
PG_NAMES = ["A::CardinalXКардинальные точки", "A::CardinalX"]
CALLS.clear()
got = server._purge_auto_cardinal_groups("A", "CardinalX")
check("T6 keyword in base name still matches suffix only",
      got == ["A::CardinalXКардинальные точки"], f"got={got}")

# 7) collection-empty (bare names) path
PG_NAMES = ["G1Cardinal Points", "G1"]
CALLS.clear()
got = server._purge_auto_cardinal_groups("", "G1")
check("T7 bare-name (no collection) path",
      got == ["G1Cardinal Points"] and CALLS == [("", ["G1Cardinal Points"])],
      f"got={got} calls={CALLS}")

# 8) enumeration failure -> [] without raising
server._objects_in_collection_by_type = lambda c, t: (_ for _ in ()).throw(RuntimeError("boom"))
got = server._purge_auto_cardinal_groups("A", "X")
check("T8 enumeration error swallowed", got == [], f"got={got}")

print("FAILED: " + "; ".join(FAILS) if FAILS else "ALL OK", flush=True)
raise SystemExit(1 if FAILS else 0)
