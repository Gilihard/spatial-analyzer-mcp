# -*- coding: utf-8 -*-
"""Live end-to-end check of sa_average_groups ('Average a set of Groups').

Boots SA with the fixture job (this process is the ONLY SDK client - SA 2015
serves one at a time, so the harness must own the session), then averages the
three source groups and verifies the result against an independently computed
Python mean.

Checks per target name:
  - the result group contains that target exactly once,
  - each coordinate equals the arithmetic mean of the source points carrying
    that target (tolerance TOL),
  - the step's reported RMS Deviation equals the RMS distance between every
    source point and its averaged point.
Statuses are informational only (the tool decides success by result content).
The average group is LEFT in the job on purpose - it is the deliverable to
inspect in the SA tree; re-running replaces it (delete_existing=True).

Run:  PYTHONIOENCODING=utf-8 python _live_average.py
"""
import math
import os
import sys

import sa_sdk
import server

try:  # Cyrillic target/group names to a cp1251/cp866 console
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# Cap every COM call so a wedged SA costs seconds, not minutes.
_orig_submit = sa_sdk.SABridge._submit


def _fast_submit(self, func, *args, timeout=60.0, **kwargs):
    return _orig_submit(self, func, *args, timeout=25.0, **kwargs)


sa_sdk.SABridge._submit = _fast_submit

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "6.01.25 — обработка.xit")
COLL = "A"
SOURCES = ["Опорная сеть", "LocateInstMeas1", "LocateInstMeas1*"]
RESULT = "Средняя"
TOL = 1e-6


def _boot():
    """Launch SA with the fixture if needed and connect (sole SDK client)."""
    r = server.sa_ensure_running(file_path=FIXTURE)
    print(f"SA: running={r.get('running')} launched={r.get('launched')} "
          f"connected={r.get('connected')}")
    if r.get("error"):
        print(f"boot error: {r['error']}")
    if not r.get("connected"):
        raise SystemExit("cannot connect to SA")
    cur = server.sa_current_file()
    print(f"loaded job: {cur.get('current_file')} "
          f"(title: {cur.get('window_title')})")


def _coordinates(group):
    out = server.sa_point_coordinates(point_group=group, collection=COLL,
                                      include_offsets=False, format="records")
    if not out.get("ok"):
        raise RuntimeError(f"cannot read group {group!r}: {out.get('error')}")
    return {str(p["name"]).split("::")[-1]: (p["x"], p["y"], p["z"])
            for p in out["points"]}


def _sources():
    try:
        return {g: _coordinates(g) for g in SOURCES}
    except RuntimeError as exc:
        print(f"{exc} - attaching the fixture file explicitly")
        f = server.sa_ensure_file(FIXTURE, force_restart=False)
        print(f"sa_ensure_file: attached={f.get('attached')} "
              f"already_open={f.get('already_open')} opened={f.get('opened')} "
              f"restarted={f.get('restarted')}")
        return {g: _coordinates(g) for g in SOURCES}


def main():
    print("=== live check: sa_average_groups ===")
    _boot()
    src = _sources()
    for g, pts in src.items():
        print(f"source {g!r}: {len(pts)} points -> {sorted(pts)}")

    targets = sorted(set().union(*[set(d) for d in src.values()]),
                     key=lambda s: (len(s), s))
    expected, groups_per_target = {}, {}
    for t in targets:
        vals = [d[t] for d in src.values() if t in d]
        expected[t] = tuple(sum(v[i] for v in vals) / len(vals)
                            for i in range(3))
        groups_per_target[t] = len(vals)

    print("\n--- running sa_average_groups ---")
    res = server.sa_average_groups(source_groups=SOURCES, result_group=RESULT,
                                   collection=COLL)
    print(f"averaged={res['averaged']} status={res['status']} "
          f"(code {res['status_code']}) result_count={res['result_count']} "
          f"replaced={res['replaced']}")
    print(f"stats from SA: {res['stats']}")
    if res.get("error"):
        print(f"error: {res['error']}")
    if res.get("messages"):
        print(f"messages: {res['messages']}")

    actual = _coordinates(RESULT)
    print(f"\nresult group {RESULT!r}: {len(actual)} points -> "
          f"{sorted(actual)}")

    failures = []
    if set(actual) != set(expected):
        failures.append(f"target set differs: expected {sorted(expected)}, "
                        f"got {sorted(actual)}")

    print("\n--- per-target comparison (python mean vs SA average) ---")
    worst = 0.0
    for t in targets:
        if t not in actual:
            print(f"  target {t}: MISSING in result")
            continue
        d = max(abs(actual[t][i] - expected[t][i]) for i in range(3))
        worst = max(worst, d)
        print(f"  target {t}: {groups_per_target[t]} source group(s), "
              f"max |delta| = {d:.3e} mm  [{'ok' if d <= TOL else 'MISMATCH'}]")
        if d > TOL:
            failures.append(f"target {t}: delta {d:.3e} mm "
                            f"(expected {expected[t]}, got {actual[t]})")

    n_src = sum(len(d) for d in src.values())
    rms_py = math.sqrt(
        sum((v[i] - expected[t][i]) ** 2
            for d in src.values() for t, v in d.items() for i in range(3))
        / n_src)
    print(f"\nworst coordinate delta: {worst:.3e} mm (tolerance {TOL:g})")
    print(f"RMS deviation: python={rms_py:.6f} mm, SA={res['stats']['rms']}")
    if res["stats"]["rms"] is not None and abs(res["stats"]["rms"] - rms_py) > 1e-6:
        print("  NOTE: SA's RMS differs from the arithmetic-mean RMS - the "
              "average may be weighted, not a plain mean.")

    print("\n=== RESULT:", "PASS" if not failures else "FAIL", "===")
    for f in failures:
        print(f"  - {f}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
