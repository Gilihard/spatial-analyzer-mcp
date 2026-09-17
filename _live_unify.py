# -*- coding: utf-8 -*-
"""Live harness for sa_unify_groups (both methods) on a copy of the real job.

Run: PYTHONIOENCODING=utf-8 python _live_unify.py
"""
import json
import os
import shutil
import sys
import time

import sa_sdk
import server

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

_orig_submit = sa_sdk.SABridge._submit
sa_sdk.SABridge._submit = (
    lambda self, func, *a, timeout=60.0, **k:
    _orig_submit(self, func, *a, timeout=180.0, **k)
)

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "6.01.25 — обработка.xit")
COLL = "A"
GROUPS = ["Опорная сеть", "LocateInstMeas1", "LocateInstMeas1*"]


def fresh(tag):
    path = os.path.join(HERE, f"_unify_work_{tag}.xit")
    shutil.copyfile(FIXTURE, path)
    r = server.sa_open_file(path, import_mode=False)
    return r.get("opened"), r.get("status")


def coords(full):
    r = server.sa_point_coordinates(full, include_offsets=False)
    return {p["name"].rsplit("::", 1)[-1]: (p["x"], p["y"], p["z"])
            for p in r.get("points", [])}


def main():
    server.sa_ensure_running()
    src = [f"{COLL}::{g}" for g in GROUPS]

    opened, st = fresh("usmn")
    print(f"--- method=usmn (fresh job: {opened}/{st}) ---")
    t = time.time()
    r = server.sa_unify_groups(source_groups=src,
                               result_group=f"{COLL}::Средняя МНК",
                               method="usmn", auto_reject=True)
    print(json.dumps({k: v for k, v in r.items() if k != "alignment"},
                     ensure_ascii=False, indent=1)[:1800])
    print(f"({time.time() - t:.0f}s)")
    usmn_pts = coords(f"{COLL}::Средняя МНК") if r.get("unified") else {}
    for name in sorted(usmn_pts, key=_key):
        print(f"  {name:>3s} {_f(usmn_pts[name])}")

    opened, st = fresh("fit")
    print(f"\n--- method=fit (fresh job: {opened}/{st}) ---")
    t = time.time()
    r2 = server.sa_unify_groups(source_groups=src,
                                result_group=f"{COLL}::Средняя МНК",
                                method="fit")
    print(json.dumps({k: v for k, v in r2.items() if k != "alignment"},
                     ensure_ascii=False, indent=1)[:1800])
    print(json.dumps(r2.get("alignment"), ensure_ascii=False, indent=1)[:900])
    print(f"({time.time() - t:.0f}s)")
    fit_pts = coords(f"{COLL}::Средняя МНК") if r2.get("unified") else {}
    for name in sorted(fit_pts, key=_key):
        print(f"  {name:>3s} {_f(fit_pts[name])}")

    leftovers = [n for n in server.sa_inspect_project(
        collection=COLL).get("types", {}).get("Point Group", [])
        if n.split("::", 1)[-1].startswith("MCP_UNIFY")]
    print("\nleftover temp groups:", leftovers)

    if usmn_pts and fit_pts:
        worst = max((max(abs(usmn_pts[k][j] - fit_pts[k][j])
                         for j in range(3))
                     for k in set(usmn_pts) & set(fit_pts)), default=None)
        print(f"max |usmn - fit| = {worst}")
    return 0


def _key(n):
    try:
        return (0, int(n))
    except ValueError:
        return (1, n)


def _f(p):
    return " ".join(f"{v:13.6f}" for v in p)


if __name__ == "__main__":
    raise SystemExit(main())
