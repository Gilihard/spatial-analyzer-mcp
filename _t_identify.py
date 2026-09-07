# -*- coding: utf-8 -*-
"""Offline regression for the geometry-type classifier (_classify_cloud).

No SA / no COM: builds synthetic point clouds of known shape, runs the same
fitting + decision path the sa_identify_geometry tool uses, and prints the
pick, confidence and notes for each case. Expected picks:
  plane       -> plane
  sphere      -> sphere
  cylinder    -> cylinder      (long axial segment)
  ring        -> circle        (full ring in a plane)
  arc90       -> circle        (quarter arc in a plane)
  circle2d    -> circle        (points on a plane circle)
  cone40      -> cone          (40 deg included angle)
  line        -> line          (collinear)
  shortbore   -> cylinder      (r=50, L=4 -> still volumetric)
  box         -> recognized False / a "not a single primitive" note
Run:  python _t_identify.py
"""

import math
import random
import sys

import server
from server import _classify_cloud

SEED = 12345


def gen(fn, n):
    rnd = random.Random(SEED)
    pts = []
    while len(pts) < n:
        p = fn(rnd)
        if all(math.isfinite(v) for v in p):
            pts.append(p)
    return pts


def build_cases():
    """Return {case_name: [[x, y, z], ...]} for every synthetic shape."""
    rnd = random.Random(SEED)
    cases = {}

    # plane patch z=5 across a 20x20 square (grid, not just corners)
    cases["plane"] = [[x, y, 5.0] for x in range(-10, 11, 2)
                      for y in range(-10, 11, 2)]

    # sphere r=100
    cases["sphere"] = gen(lambda r: _on_sphere(r, 100.0), 64)

    # cylinder r=50, axis z, length 200
    cases["cylinder"] = []
    for i in range(48):
        a = rnd.uniform(0.0, 2 * math.pi)
        z = rnd.uniform(-100.0, 100.0)
        cases["cylinder"].append([50.0 * math.cos(a), 50.0 * math.sin(a), z])

    # full ring in the z=0 plane, r=50 (+ tiny noise)
    cases["ring"] = [[50.0 * math.cos(2 * math.pi * i / 40)
                      + rnd.gauss(0, 1e-3),
                      50.0 * math.sin(2 * math.pi * i / 40)
                      + rnd.gauss(0, 1e-3),
                      rnd.gauss(0, 1e-3)] for i in range(40)]

    # quarter arc in the z=0 plane, r=50
    cases["arc90"] = [[50.0 * math.cos(math.pi / 2 * i / 20)
                       + rnd.gauss(0, 1e-3),
                       50.0 * math.sin(math.pi / 2 * i / 20)
                       + rnd.gauss(0, 1e-3),
                       rnd.gauss(0, 1e-3)] for i in range(21)]

    # points on a plane circle r=50 (a.k.a. circle2d)
    cases["circle2d"] = gen(lambda r: _on_circle(r, 50.0, 0.0), 48)

    # cone: apex (0,0,0), axis z, included angle 40 deg (half 20), length 100
    cases["cone40"] = gen(lambda r: _on_cone(r, 40.0, 100.0), 64)

    # collinear segment
    base = (1.0, 2.0, 3.0)
    cases["line"] = [[base[0] + 100.0 * t + rnd.gauss(0, 1e-3),
                      base[1] + 20.0 * t + rnd.gauss(0, 1e-3),
                      base[2] + 10.0 * t + rnd.gauss(0, 1e-3)]
                     for t in (rnd.random() for _ in range(40))]

    # short axial bore: r=50, L=4 (two rings at z=+-2)
    cases["shortbore"] = []
    for i in range(12):
        a = 2 * math.pi * i / 12
        for z in (-2.0, 2.0):
            cases["shortbore"].append(
                [50.0 * math.cos(a), 50.0 * math.sin(a), z])

    # box corner: 60 pts on three faces of a 100 cube
    cases["box"] = []
    for i in range(60):
        kind = i % 3
        if kind == 0:
            x, y, z = rnd.uniform(0, 100), rnd.uniform(0, 100), 0.0
        elif kind == 1:
            x, y, z = rnd.uniform(0, 100), 0.0, rnd.uniform(0, 100)
        else:
            x, y, z = 0.0, rnd.uniform(0, 100), rnd.uniform(0, 100)
        cases["box"].append([x, y, z])
    return cases


def main():
    cases = build_cases()

    width = max(len(k) for k in cases)
    print(f"{'case':<12}{'expected':<11}{'picked':<11}{'conf':<8}{'rms':<12}"
          f"{'sigma_hat':<11} note")
    print("-" * 100)
    bad = []
    for name, want in (("plane", "plane"), ("sphere", "sphere"),
                       ("cylinder", "cylinder"), ("ring", "circle"),
                       ("arc90", "circle"), ("circle2d", "circle"),
                       ("cone40", "cone"), ("line", "line"),
                       ("shortbore", "cylinder"), ("box", None)):
        pts = cases[name]
        res = _classify_cloud(pts)
        if not res.get("ok"):
            print(f"{name:<12}{want:<11}{'FAILED':<11} {res['error']}")
            bad.append(name)
            continue
        best = res["best"]
        got = best["geometry_type"]
        rms = best["rms"]
        sh = best["sigma_hat"]
        note = "; ".join(res["notes"]) if res["notes"] else ""
        if note:
            note = note[:72]
        if want is None:
            ok = not res.get("recognized", True)
            exp = "~none (recognized False)"
        else:
            ok = got == want
            exp = want
        flag = "ok" if ok else "!!"
        print(f"{name:<12}{exp:<23}{got:<11}{res['confidence']:<8}"
              f"{rms:<12.6g}{sh:<11.5g}{flag} {note}")
        if not ok:
            bad.append(name)
        if want is not None and got == want:
            p = best["parameters"]
            extra = []
            for k in ("radius", "diameter", "included_angle", "length"):
                if k in p:
                    extra.append(f"{k}={p[k]:.3f}")
            print(f"{'':<23}params: {', '.join(extra)}")
    print("-" * 100)
    if bad:
        print("NOT all expected picks:", bad)
        sys.exit(1)
    print("all picks as expected")


def _on_sphere(rnd, radius):
    z = rnd.uniform(-1.0, 1.0)
    a = rnd.uniform(0.0, 2 * math.pi)
    s = math.sqrt(max(0.0, 1.0 - z * z))
    return [radius * s * math.cos(a), radius * s * math.sin(a), radius * z]


def _on_circle(rnd, radius, z):
    a = rnd.uniform(0.0, 2 * math.pi)
    return [radius * math.cos(a), radius * math.sin(a), z]


def _on_cone(rnd, apex_deg, length):
    half = math.radians(apex_deg / 2.0)
    s = rnd.uniform(0.05, 1.0)
    z = s * length
    r = z * math.tan(half)
    a = rnd.uniform(0.0, 2 * math.pi)
    return [r * math.cos(a), r * math.sin(a), z]


if __name__ == "__main__":
    main()
