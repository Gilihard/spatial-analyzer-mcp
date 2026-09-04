import math
import random
import sys
import sa_fitmath as fm


def unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def add(a, b):
    return [a[i] + b[i] for i in range(3)]


def scale(a, s):
    return [x * s for x in a]


def cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


RNG = random.Random(7)
PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name} {extra}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def rms(vals):
    return math.sqrt(sum(v * v for v in vals) / len(vals)) if vals else 0.0


def orthonormal(n0):
    n0 = unit(n0)
    ref = [0.0, 0.0, 1.0] if abs(n0[2]) < 0.9 else [1.0, 0.0, 0.0]
    e1 = unit(cross(n0, ref))
    e2 = cross(n0, e1)
    return n0, e1, e2


def radial_dir(axis, th):
    """Unit radial vector at angle th, perpendicular to the given axis."""
    e1, e2 = orthonormal(axis)[1:]
    return [e1[i] * math.cos(th) + e2[i] * math.sin(th) for i in range(3)]


R = 19.05  # reflector radius used to build the synthetic measurements


# ---------------- cylinder ----------------
print("Cylinder")
axis_p = [1000.0, -500.0, 300.0]
axis_u = unit([0.2, -0.1, 0.97])
R0 = 1000.0
for noise, tag in ((0.0, "clean"), (0.05, "noisy")):
    for side, name in ((1, "outside"), (-1, "inside"), (0, "none")):
        pts, offs = [], []
        for it in range(6):
            t = 100.0 * it - 250.0
            for k in range(12):
                th = 2 * math.pi * k / 12
                rho_hat = radial_dir(axis_u, th)
                surf = add(axis_p, add(scale(axis_u, t), scale(rho_hat, R0)))
                c = add(surf, scale(rho_hat, side * R))
                c = [c[i] + RNG.gauss(0, noise) for i in range(3)]
                pts.append(c)
                offs.append(R if side != 0 else 0.0)
        res = fm.fit_geometry("cylinder", pts, radius=R0, side=side,
                              offsets=offs)
        ok = res.get("ok")
        p = res.get("params", {})
        err_ang = math.degrees(math.acos(max(-1, min(1, abs(dot(p.get("axis", []), axis_u))))))
        rad_err = abs(p.get("radius", -1) - R0)
        tol = 1e-6 if noise == 0 else noise
        check(f"cyl {name} {tag}", ok and err_ang < 0.01 and rad_err < 1e-3,
              f"ang_err={err_ang:.5f} deg rad={p.get('radius')} rms={rms(res.get('residuals', [])):.5f} raw_rms={rms(res.get('raw_residuals', [])):.4f}")

# ---------------- sphere ----------------
print("Sphere")
C0 = [200.0, 3000.0, -400.0]
R0 = 800.0
for noise, tag in ((0.0, "clean"), (0.05, "noisy")):
    for side, name in ((1, "outside"), (-1, "inside")):
        pts, offs = [], []
        for it in range(8):
            th = RNG.uniform(0, 2 * math.pi)
            ph = math.acos(RNG.uniform(-0.85, 0.85))
            d = [math.sin(ph) * math.cos(th), math.sin(ph) * math.sin(th),
                 math.cos(ph)]
            surf = add(C0, scale(d, R0))
            c = add(surf, scale(d, side * R))
            c = [c[i] + RNG.gauss(0, noise) for i in range(3)]
            pts.append(c)
            offs.append(R if side != 0 else 0.0)
        res = fm.fit_geometry("sphere", pts, radius=R0, side=side, offsets=offs)
        p = res.get("params", {})
        cerr = math.sqrt(sum((p.get("center", [0, 0, 0])[i] - C0[i]) ** 2 for i in range(3)))
        check(f"sph {name} {tag}", res.get("ok") and cerr < 1e-3 + 3 * noise * math.sqrt(3),
              f"cerr={cerr:.4f} r={p.get('radius')} rms={rms(res.get('residuals', [])):.5f}")

# ---------------- circle ----------------
print("Circle")
C0 = [500.0, 120.0, 800.0]
n0 = unit([0.3, -0.2, 0.93])
R0 = 400.0
for noise, tag in ((0.0, "clean"), (0.05, "noisy")):
    for side, name in ((1, "outside"), (-1, "inside")):
        nrm, e1, e2 = orthonormal(n0)
        pts, offs = [], []
        for k in range(16):
            th = 2 * math.pi * k / 16
            rho = radial_dir(n0, th)
            surf = add(C0, scale(rho, R0))
            c = add(surf, scale(rho, side * R))
            c = [c[i] + RNG.gauss(0, noise) for i in range(3)]
            pts.append(c)
            offs.append(R if side != 0 else 0.0)
        res = fm.fit_geometry("circle", pts, radius=R0, side=side, offsets=offs)
        p = res.get("params", {})
        cerr = math.sqrt(sum((p.get("center", [0, 0, 0])[i] - C0[i]) ** 2 for i in range(3)))
        nerr = math.degrees(math.acos(max(-1, min(1, abs(dot(p.get("normal", []), n0))))))
        # Normal of a full-360 deg circle is only weakly constrained by small
        # out-of-plane noise (radial residuals feel a plane tilt at 2nd order),
        # so allow ~1 deg of normal wobble on noisy data; clean is exact.
        nerr_tol = 1.0 if noise else 0.05
        check(f"cir {name} {tag}", res.get("ok") and cerr < 1e-2 + 5 * noise
              and nerr < nerr_tol,
              f"cerr={cerr:.4f} nerr={nerr:.4f} r={p.get('radius')} rms={rms(res.get('residuals', [])):.5f}")

# ---------------- cone ----------------
print("Cone")
alpha = math.radians(25.0)
incl = 50.0
apex0 = [0.0, 0.0, 0.0]
u0 = [0.0, 0.0, 1.0]
for noise, tag in ((0.0, "clean"), (0.05, "noisy")):
    for side, name in ((1, "outside"), (-1, "inside")):
        pts, offs = [], []
        for it in range(5):
            t = 150.0 + 100.0 * it
            rr = t * math.tan(alpha)
            for k in range(10):
                th = 2 * math.pi * k / 10
                rho_hat = [math.cos(th), math.sin(th), 0.0]
                surf = [rr * rho_hat[0], rr * rho_hat[1], t]
                nhat = [math.cos(alpha) * rho_hat[i] - math.sin(alpha) * u0[i]
                        for i in range(3)]
                c = [surf[i] + side * R * nhat[i] for i in range(3)]
                c = [c[i] + RNG.gauss(0, noise) for i in range(3)]
                pts.append(c)
                offs.append(R if side != 0 else 0.0)
        res = fm.fit_geometry("cone", pts, apex_angle_deg=incl, side=side,
                              offsets=offs)
        p = res.get("params", {})
        ax = p.get("axis", [])
        derr = math.degrees(math.acos(max(-1, min(1, dot(ax, u0)))))
        apex = p.get("apex", [0, 0, 0])
        apex_err = math.sqrt(apex[0] ** 2 + apex[1] ** 2 + (apex[2] - 0) ** 2)
        check(f"cone {name} {tag}", res.get("ok") and derr < 0.01,
              f"axerr={derr:.5f} deg apex_err={apex_err:.3f} len={p.get('length'):.2f} incl={p.get('included_angle')} rms={rms(res.get('residuals', [])):.5f}")

# ---------------- plane ----------------
print("Plane")
h = 120.0
for noise, tag in ((0.0, "clean"), (0.05, "noisy")):
    for side, name in ((1, "above"), (-1, "below")):
        pts, offs = [], []
        for i in range(-2, 3):
            for j in range(-2, 3):
                c = [50.0 * i, 50.0 * j, h + side * R + RNG.gauss(0, noise)]
                pts.append(c)
                offs.append(R)
        res = fm.fit_geometry("plane", pts, side=side, offsets=offs)
        p = res.get("params", {})
        z = p.get("point", [0, 0, 0])[2]
        nz = p.get("normal", [])[2]
        check(f"pln {name} {tag}", res.get("ok") and abs(z - h) < 1e-3 + 3 * noise and nz > 0.99,
              f"z={z:.4f} (want {h}) nz={nz:.6f} rms={rms(res.get('residuals', [])):.5f} raw={rms(res.get('raw_residuals', [])):.4f}")

# ---------------- free radius path ----------------
print("Free radius (surface radius must come out as R0)")
for gtype, R0 in (("cylinder", 1000.0), ("sphere", 800.0), ("circle", 400.0)):
    for noise, tag in ((0.0, "clean"),):
        if gtype == "cylinder":
            pts, offs = [], []
            for it in range(6):
                t = 100.0 * it - 250.0
                for k in range(12):
                    th = 2 * math.pi * k / 12
                    rho_hat = radial_dir(axis_u, th)
                    surf = add(axis_p, add(scale(axis_u, t), scale(rho_hat, R0)))
                    c = add(surf, scale(rho_hat, R))
                    pts.append(c)
                    offs.append(R)
            res = fm.fit_geometry("cylinder", pts, radius=None, side=1, offsets=offs)
        elif gtype == "sphere":
            pts, offs = [], []
            for it in range(10):
                th = RNG.uniform(0, 2 * math.pi)
                ph = math.acos(RNG.uniform(-0.85, 0.85))
                d = [math.sin(ph) * math.cos(th), math.sin(ph) * math.sin(th), math.cos(ph)]
                c = add(add(C0, scale(d, R0)), scale(d, R))
                pts.append(c)
                offs.append(R)
            res = fm.fit_geometry("sphere", pts, radius=None, side=1, offsets=offs)
        else:
            pts, offs = [], []
            for k in range(16):
                th = 2 * math.pi * k / 16
                rho = radial_dir(n0, th)
                c = add(add(C0, scale(rho, R0)), scale(rho, R))
                pts.append(c)
                offs.append(R)
            res = fm.fit_geometry("circle", pts, radius=None, side=1, offsets=offs)
        r = res.get("params", {}).get("radius", -1)
        check(f"free {gtype}", res.get("ok") and abs(r - R0) < 1e-3,
              f"r={r} rms={rms(res.get('residuals', [])):.6f}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
