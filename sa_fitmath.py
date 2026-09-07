"""Constrained / offset-aware least-squares geometry fitting (pure Python).

SpatialAnalyzer 2015 has no MP step for a best fit with a FIXED parameter
(radius / diameter / cone included angle) and no way to choose the direction
of the reflector-offset compensation - "Fit Geometry to Point Group" only
takes a geometry type, a group, a profile and a tolerance. This module fits
the primitives ourselves so the server can offer those options:

  * cylinder / sphere / circle  - fixed radius R0 (or free), points measured
    with a reflector whose centre sits a probe radius r away from the true
    surface. The radial distance of a measured centre from the axis/centre is
    rho ~ R0 + s*r where s = +1 when the reflector was on the OUTSIDE of the
    feature (shaft / outer surface) and s = -1 when it was on the INSIDE
    (bore / inner surface). Fitting with the effective target (R0 + s*r_i)
    per point is exactly the constrained fit of the true surface.
  * cone - fixed included angle ("угол раствора", full apex angle 2*alpha).
    Distance of a point to the alpha-cone measured along the surface normal
    is g = rho*cos(alpha) - t*sin(alpha) (t axial from the apex); for a
    reflector centre offset along the normal by r it equals s*r on the true
    surface, so the fit minimises (g_i - s*r_i) after eliminating the apex
    axial position (g_i - mean(g)).
  * plane - free fit; side is above/below: the offset is applied along the
    plane normal. With the plane offset eliminated the normal is found from
    (n.p_i - s*r_i) - mean(n.p_j - s*r_j).

All fits use a Levenberg-Marquardt solver over raw coordinates with a
normalised direction vector; every parameter is a plain float and the
Jacobian is numeric (central differences). Nothing here touches COM - the
module is pure math, so it can be unit-tested without SA.

Coordinate/unit convention matches the rest of the repo: points are the raw
measured reflector-centre coordinates in the active (working) frame, all
lengths in the job distance unit (mm in the test file).
"""

import math


# --------------------------------------------------------------------------
# vector helpers (local copies so this module stays dependency-free)
# --------------------------------------------------------------------------

def _sub(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _norm(a):
    return math.sqrt(_dot(a, a))


def _unit(a):
    n = _norm(a)
    if n == 0:
        return [0.0, 0.0, 0.0]
    return [a[0] / n, a[1] / n, a[2] / n]


def _mean(rows):
    n = len(rows)
    if not n:
        return [0.0, 0.0, 0.0]
    return [sum(r[i] for r in rows) / n for i in range(3)]


def _eig_sym3(m):
    """Eigenvalues/eigenvectors of a symmetric 3x3 (Jacobi rotations).

    Returns a list of (value, unit_vector) sorted ascending by value.
    Only needed for fit initial guesses, so precision ~1e-10 suffices.
    """
    a = [[float(m[i][j]) for j in range(3)] for i in range(3)]
    v = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
    for _ in range(200):
        p, q = 0, 1
        best = abs(a[0][1])
        for i in range(3):
            for j in range(i + 1, 3):
                if abs(a[i][j]) > best:
                    best, p, q = abs(a[i][j]), i, j
        if best < 1e-15:
            break
        if a[p][p] == a[q][q]:
            theta = math.pi / 4.0
        else:
            theta = 0.5 * math.atan2(2.0 * a[p][q], a[q][q] - a[p][p])
        c, s = math.cos(theta), math.sin(theta)
        for k in range(3):
            akp, akq = a[k][p], a[k][q]
            a[k][p] = c * akp - s * akq
            a[k][q] = s * akp + c * akq
        for k in range(3):
            apk, aqk = a[p][k], a[q][k]
            a[p][k] = c * apk - s * aqk
            a[q][k] = s * apk + c * aqk
        for k in range(3):
            vkp, vkq = v[k][p], v[k][q]
            v[k][p] = c * vkp - s * vkq
            v[k][q] = s * vkp + c * vkq
    out = []
    for i in range(3):
        col = [v[k][i] for k in range(3)]
        n = _norm(col)
        out.append((a[i][i], [col[0] / n, col[1] / n, col[2] / n] if n
                    else [0.0, 0.0, 1.0]))
    out.sort(key=lambda t: t[0])
    return out


# --------------------------------------------------------------------------
# Levenberg-Marquardt over raw float parameters, numeric Jacobian
# --------------------------------------------------------------------------

def _lm(fun, p0, max_iter=150, ftol=1e-13, xtol=1e-11):
    """Minimise sum(fun(p)^2). fun returns a list of residuals.

    Returns (p, sse, iterations). Robust for the small (<=6) parameter
    counts and moderate point counts this module fits.
    """
    p = [float(x) for x in p0]
    n = len(p)
    r = fun(p)
    m = len(r)
    sse = sum(v * v for v in r)
    lam = 1e-6
    for _ in range(max_iter):
        # numeric Jacobian columns (central differences)
        cols = []
        for k in range(n):
            h = 1e-7 * max(1.0, abs(p[k]))
            pp = list(p)
            pm = list(p)
            pp[k] += h
            pm[k] -= h
            rp = fun(pp)
            rm = fun(pm)
            col = [(rp[i] - rm[i]) / (2.0 * h) for i in range(m)]
            cols.append(col)
        jtj = [[0.0] * n for _ in range(n)]
        jtr = [0.0] * n
        for k in range(n):
            ck = cols[k]
            for i in range(m):
                jtr[k] += ck[i] * r[i]
            for l in range(n):
                cl = cols[l]
                s = 0.0
                for i in range(m):
                    s += ck[i] * cl[i]
                jtj[k][l] = s
        # damped normal equations (JtJ + lam*diag) d = -Jt r
        accepted = False
        for _ in range(24):
            a = [[jtj[i][j] for j in range(n)] for i in range(n)]
            b = [-jtr[i] for i in range(n)]
            for k in range(n):
                a[k][k] += lam * (jtj[k][k] if jtj[k][k] > 0 else 1.0)
            d = _solve_linear(a, b)
            if d is None:
                lam *= 10.0
                if lam > 1e14:
                    return p, sse, _
                continue
            trial = [p[k] + d[k] for k in range(n)]
            rt = fun(trial)
            sse_t = sum(v * v for v in rt)
            if sse_t <= sse:
                p = trial
                r = rt
                step = max(abs(d[k]) / max(1.0, abs(p[k]))
                           for k in range(n))
                improved = sse - sse_t
                sse = sse_t
                lam = max(lam * 0.3, 1e-14)
                accepted = True
                if improved <= ftol * (sse + improved) or step <= xtol:
                    return p, sse, _
                break
            lam *= 10.0
            if lam > 1e14:
                break
        if not accepted:
            break
    return p, sse, max_iter


def _solve_linear(a, b):
    """Solve a*x = b by Gaussian elimination with partial pivoting (n<=6)."""
    n = len(b)
    a = [row[:] for row in a]
    b = b[:]
    for col in range(n):
        piv = col
        best = abs(a[col][col])
        for i in range(col + 1, n):
            if abs(a[i][col]) > best:
                best, piv = abs(a[i][col]), i
        if best < 1e-300:
            return None
        if piv != col:
            a[col], a[piv] = a[piv], a[col]
            b[col], b[piv] = b[piv], b[col]
        d = a[col][col]
        for j in range(col, n):
            a[col][j] /= d
        b[col] /= d
        for i in range(n):
            if i == col:
                continue
            f = a[i][col]
            if f == 0.0:
                continue
            for j in range(col, n):
                a[i][j] -= f * a[col][j]
            b[i] -= f * b[col]
    return b


# --------------------------------------------------------------------------
# shared geometry of residuals
# --------------------------------------------------------------------------

def _radial_dist(pt, base, u):
    """Perpendicular distance of pt from the axis line (base, unit u)."""
    w = _sub(pt, base)
    s = _dot(w, u)
    return _norm([w[0] - s * u[0], w[1] - s * u[1], w[2] - s * u[2]])


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------

def fit_geometry(geometry_type, pts, radius=None, apex_angle_deg=None,
                 side=0, offsets=None):
    """Fit a primitive to reflector-centre points, optionally constrained.

    Args:
        geometry_type: plane|circle|sphere|cylinder|cone.
        pts: list of (x, y, z) measured reflector-centre coordinates.
        radius: fixed nominal radius (mm) for circle/sphere/cylinder, or None
                for a free radius fit.
        apex_angle_deg: full cone apex angle ("угол раствора", degrees),
                        required for cone.
        side: +1 = reflector on the outside (shaft / above), -1 = inside
              (bore / below), 0 = no compensation.
        offsets: list of per-point reflector offset magnitudes (same length
                 as pts) applied with the sign of `side`; None = all zero.

    Returns:
        {ok, params, residuals, error?} where params carries the same keys as
        the server's "Get <Type> Properties" reading (cylinder begin/end/
        axis/radius/length, sphere center/radius, circle center/normal/radius,
        cone apex/axis/length/included_angle, plane normal/point) and
        residuals are the per-point compensated surface deviations.
    """
    gtype = geometry_type.lower()
    if not pts:
        return {"ok": False, "error": "no points to fit"}
    n = len(pts)
    if offsets is None:
        off = [0.0] * n
    else:
        off = [float(x) if x else 0.0 for x in offsets]
        if len(off) != n:
            off = ([float(offsets[0])] * n if offsets
                   else [0.0] * n)
    side = int(side)
    try:
        if gtype == "cylinder":
            return _fit_cylinder(pts, radius, side, off)
        if gtype == "sphere":
            return _fit_sphere(pts, radius, side, off)
        if gtype == "circle":
            return _fit_circle(pts, radius, side, off)
        if gtype == "cone":
            if apex_angle_deg is None:
                return _fit_cone_free(pts)
            if float(apex_angle_deg) <= 0 or float(apex_angle_deg) >= 180:
                return {"ok": False,
                        "error": "cone requires 0 < apex_angle_deg < 180"}
            return _fit_cone(pts, float(apex_angle_deg), side, off)
        if gtype == "line":
            return _fit_line(pts)
        if gtype == "plane":
            return _fit_plane(pts, side, off)
        return {"ok": False,
                "error": f"unknown geometry_type '{geometry_type}'"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _effective_target(radii, radius, side, off, i):
    """Target radius for radial shapes: fixed R0 or free (mean-centred)."""
    if radius is not None:
        return float(radius) + side * off[i]
    return None


# --------------------------------------------------------------------------
# cylinder
# --------------------------------------------------------------------------

def _fit_cylinder(pts, radius, side, off):
    cent = _mean(pts)
    cov = [[0.0] * 3 for _ in range(3)]
    for pt in pts:
        w = _sub(pt, cent)
        for i in range(3):
            for j in range(3):
                cov[i][j] += w[i] * w[j]
    evs = _eig_sym3(cov)

    def make_fun():
        return None  # placeholder (closures below need params)

    best = None
    for (_val, u0) in evs:
        res = _cylinder_residuals(pts, cent, u0, radius, side, off)
        sse = sum(v * v for v in res)
        if best is None or sse < best[0]:
            best = (sse, u0)
    u0 = best[1]
    p0 = cent + u0
    resfun = _cylinder_fun(pts, radius, side, off)
    p, sse, it = _lm(resfun, p0)
    e = resfun(p)
    u = _unit(p[3:])
    a = p[:3]
    radii = [_radial_dist(pt, a, u) for pt in pts]
    if radius is None:
        free = sum(radii[i] - side * off[i] for i in range(len(radii))) \
            / len(radii)
    else:
        free = float(radius)
    ts = [_dot(_sub(pt, a), u) for pt in pts]
    t0, t1 = min(ts), max(ts)
    begin = [a[i] + t0 * u[i] for i in range(3)]
    end = [a[i] + t1 * u[i] for i in range(3)]
    params = {"begin": begin, "end": end, "axis": u,
              "radius": free, "length": t1 - t0}
    return {"ok": True, "params": params, "residuals": e,
            "raw_residuals": [radii[i] - free for i in range(len(radii))],
            "iterations": it, "sse": sse}


def _cylinder_residuals(pts, base, uvec, radius, side, off):
    u = _unit(uvec)
    out = []
    mean_term = None
    if radius is None:
        acc = 0.0
        rho = [_radial_dist(pt, base, u) for pt in pts]
        for i in range(len(pts)):
            acc += rho[i] - side * off[i]
        mean_term = acc / len(pts)
    for i, pt in enumerate(pts):
        rho = _radial_dist(pt, base, u)
        if radius is not None:
            out.append(rho - (float(radius) + side * off[i]))
        else:
            out.append((rho - side * off[i]) - mean_term)
    return out


def _cylinder_fun(pts, radius, side, off):
    def fun(p):
        return _cylinder_residuals(pts, p[:3], p[3:], radius, side, off)
    return fun


# --------------------------------------------------------------------------
# sphere
# --------------------------------------------------------------------------

def _fit_sphere(pts, radius, side, off):
    c0 = _mean(pts)
    p, sse, it = _lm(_sphere_fun(pts, radius, side, off), c0)
    e = _sphere_fun(pts, radius, side, off)(p)
    dists = [_norm(_sub(pt, p)) for pt in pts]
    if radius is None:
        free = sum(dists[i] - side * off[i] for i in range(len(dists))) \
            / len(dists)
    else:
        free = float(radius)
    params = {"center": p, "radius": free}
    return {"ok": True, "params": params, "residuals": e,
            "raw_residuals": [dists[i] - free for i in range(len(dists))],
            "iterations": it, "sse": sse}


def _sphere_fun(pts, radius, side, off):
    def fun(c):
        out = []
        acc = 0.0
        dists = [_norm(_sub(pt, c)) for pt in pts]
        if radius is None:
            acc = sum(dists[i] - side * off[i] for i in range(len(pts))) \
                / len(pts)
        for i, pt in enumerate(pts):
            d = dists[i]
            if radius is not None:
                out.append(d - (float(radius) + side * off[i]))
            else:
                out.append((d - side * off[i]) - acc)
        return out
    return fun


# --------------------------------------------------------------------------
# circle (plane + in-plane centre, radius fixed or free)
# --------------------------------------------------------------------------

def _fit_circle(pts, radius, side, off):
    cent = _mean(pts)
    cov = [[0.0] * 3 for _ in range(3)]
    for pt in pts:
        w = _sub(pt, cent)
        for i in range(3):
            for j in range(3):
                cov[i][j] += w[i] * w[j]
    evs = _eig_sym3(cov)
    n0 = evs[0][1]  # smallest spread -> plane normal guess
    p0 = cent + n0
    fun = _circle_fun(pts, cent, radius, side, off)
    p, sse, it = _lm(fun, p0)
    e = fun(p)
    nvec = _unit(p[3:])
    # centre: drop the component along the normal (position along the axis
    # of the circle does not affect in-plane radii) and place it in the
    # plane that passes through the cloud mean.
    c = p[:3]
    mw = _sub(cent, c)
    c_on_plane = [c[i] + _dot(mw, nvec) * nvec[i] for i in range(3)]
    dists = []
    for pt in pts:
        w = _sub(pt, c_on_plane)
        s = _dot(w, nvec)
        dists.append(_norm([w[0] - s * nvec[0],
                            w[1] - s * nvec[1],
                            w[2] - s * nvec[2]]))
    if radius is None:
        free = sum(dists[i] - side * off[i] for i in range(len(dists))) \
            / len(dists)
    else:
        free = float(radius)
    params = {"center": c_on_plane, "normal": nvec, "radius": free}
    return {"ok": True, "params": params, "residuals": e,
            "raw_residuals": [dists[i] - free for i in range(len(dists))],
            "iterations": it, "sse": sse}


def _circle_fun(pts, cent, radius, side, off):
    def fun(p):
        nvec = _unit(p[3:])
        c = p[:3]
        mw = _sub(cent, c)
        c_on = [c[i] + _dot(mw, nvec) * nvec[i] for i in range(3)]
        out = []
        acc = 0.0
        dists = []
        for pt in pts:
            w = _sub(pt, c_on)
            s = _dot(w, nvec)
            dists.append(_norm([w[0] - s * nvec[0],
                                w[1] - s * nvec[1],
                                w[2] - s * nvec[2]]))
        if radius is None:
            acc = sum(dists[i] - side * off[i]
                      for i in range(len(pts))) / len(pts)
        for i, pt in enumerate(pts):
            d = dists[i]
            if radius is not None:
                out.append(d - (float(radius) + side * off[i]))
            else:
                out.append((d - side * off[i]) - acc)
        return out
    return fun


# --------------------------------------------------------------------------
# cone (fixed included angle)
# --------------------------------------------------------------------------

def _fit_cone(pts, included_deg, side, off):
    alpha = math.radians(included_deg) / 2.0
    sin_a, cos_a = math.sin(alpha), math.cos(alpha)
    if sin_a < 1e-9:
        return {"ok": False, "error": "cone angle too close to 0/180 deg"}
    # init axis: free cylinder fit on the same cloud (cone and cylinder of a
    # point set share the axis line)
    cyl = _fit_cylinder(pts, None, 0, [0.0] * len(pts))
    if not cyl.get("ok"):
        return cyl
    u0 = cyl["params"]["axis"]
    a0 = cyl["params"]["begin"]
    # residual is symmetric in the axis direction up to a shift of the apex,
    # but the sheet assignment is not - keep the orientation whose residuals
    # are smaller (points then lie on the + sheet used by g = rho cos - t sin)
    resfun = _cone_fun(pts, a0, alpha, side, off)
    e_plus = resfun(a0 + u0)
    sse_plus = sum(v * v for v in e_plus)
    e_minus = resfun(a0 + [-x for x in u0])
    sse_minus = sum(v * v for v in e_minus)
    u0 = u0 if sse_plus <= sse_minus else [-x for x in u0]
    base = a0
    p, sse, it = _lm(resfun, base + u0)
    e = resfun(p)
    u = _unit(p[3:])
    a = p[:3]
    rho = [_radial_dist(pt, a, u) for pt in pts]
    ss = [_dot(_sub(pt, a), u) for pt in pts]
    mean_r = sum(rho[i] for i in range(len(rho))) / len(rho)
    mean_s = sum(ss) / len(ss)
    mean_o = sum(side * off[i] for i in range(len(off))) / len(off)
    tau = mean_s - (cos_a * mean_r - mean_o) / sin_a  # apex axial from base
    apex = [a[i] + tau * u[i] for i in range(3)]
    ax_from_apex = [ss[i] - tau for i in range(len(ss))]
    length = max(ax_from_apex)
    length = max(length + 1e-6, 1e-6)
    params = {"apex": apex, "axis": u, "length": length,
              "included_angle": included_deg}
    return {"ok": True, "params": params, "residuals": e,
            "raw_residuals": [0.0] * len(pts),
            "iterations": it, "sse": sse}


def _cone_fun(pts, base0, alpha, side, off):
    sin_a, cos_a = math.sin(alpha), math.cos(alpha)

    def fun(p):
        u = _unit(p[3:])
        a = p[:3]
        n = len(pts)
        rho = [_radial_dist(pt, a, u) for pt in pts]
        ss = [_dot(_sub(pt, a), u) for pt in pts]
        g = [cos_a * rho[i] - sin_a * ss[i] - side * off[i]
             for i in range(n)]
        mean_g = sum(g) / n
        return [g[i] - mean_g for i in range(n)]
    return fun


# --------------------------------------------------------------------------
# plane (free; offset applied along the normal)
# --------------------------------------------------------------------------

def _fit_plane(pts, side, off):
    cent = _mean(pts)
    cov = [[0.0] * 3 for _ in range(3)]
    for pt in pts:
        w = _sub(pt, cent)
        for i in range(3):
            for j in range(3):
                cov[i][j] += w[i] * w[j]
    evs = _eig_sym3(cov)
    n0 = evs[0][1]
    if _dot(n0, [0.0, 0.0, 1.0]) < 0:  # deterministic +Z hemisphere
        n0 = [-x for x in n0]
    if abs(_dot(n0, [0.0, 0.0, 1.0])) < 0.1 and _dot(n0, [1.0, 0.0, 0.0]) < 0:
        n0 = [-x for x in n0]  # near-vertical plane: orient on +X
    p, sse, it = _lm(_plane_fun(pts, side, off), cent + n0)
    nvec = _unit(p[3:])
    dhat = 0.0
    for i, pt in enumerate(pts):
        dhat += _dot(pt, nvec) - side * off[i]
    dhat /= len(pts)
    point = [cent[i] + (dhat - _dot(cent, nvec)) * nvec[i] for i in range(3)]
    params = {"normal": nvec, "point": point}
    e = _plane_fun(pts, side, off)(p)
    raw = [_dot(_sub(pt, point), nvec) for pt in pts]
    return {"ok": True, "params": params, "residuals": e,
            "raw_residuals": raw,
            "iterations": it, "sse": sse}


def _plane_fun(pts, side, off):
    def fun(p):
        nvec = _unit(p[3:])
        acc = 0.0
        vals = []
        n = len(pts)
        for i, pt in enumerate(pts):
            v = _dot(pt, nvec) - side * off[i]
            vals.append(v)
            acc += v
        mean_v = acc / n
        return [vals[i] - mean_v for i in range(n)]
    return fun


# --------------------------------------------------------------------------
# line (free; total least squares along the principal axis)
# --------------------------------------------------------------------------

def _fit_line(pts):
    """Fit the axis line through the cloud (analytic, no LM).

    The best-fit line of a point set is the principal axis (largest
    eigenvector of the covariance). Residuals are the unsigned perpendicular
    distances to the axis - a line has no meaningful sign.
    """
    cent = _mean(pts)
    cov = [[0.0] * 3 for _ in range(3)]
    for pt in pts:
        w = _sub(pt, cent)
        for i in range(3):
            for j in range(3):
                cov[i][j] += w[i] * w[j]
    evs = _eig_sym3(cov)
    u = evs[2][1]  # largest spread -> axis direction
    if _norm(u) == 0:
        return {"ok": False, "error": "degenerate point set"}
    ts = [_dot(_sub(pt, cent), u) for pt in pts]
    t0, t1 = min(ts), max(ts)
    begin = [cent[i] + t0 * u[i] for i in range(3)]
    end = [cent[i] + t1 * u[i] for i in range(3)]
    residuals = []
    for pt in pts:
        w = _sub(pt, cent)
        s = _dot(w, u)
        residuals.append(_norm([w[0] - s * u[0],
                                w[1] - s * u[1],
                                w[2] - s * u[2]]))
    params = {"begin": begin, "end": end, "axis": u, "length": t1 - t0}
    return {"ok": True, "params": params, "residuals": residuals,
            "raw_residuals": list(residuals), "iterations": 0,
            "sse": sum(v * v for v in residuals)}


# --------------------------------------------------------------------------
# cone with a FREE included angle ("угол раствора" fitted, not fixed)
# --------------------------------------------------------------------------

def _cone_free_fun(pts):
    """Residuals of a cone whose half-angle alpha is a fitted parameter.

    Same geometry as _cone_fun (g = cos(a)*rho - sin(a)*s along the surface
    normal), mean-centred to eliminate the apex axial position; alpha lives in
    p[6] and must stay inside (0, pi) for sin(a) > 0.
    """

    def fun(p):
        alpha = p[6]
        if not (1e-4 < alpha < math.pi - 1e-4):
            return [1e9] * len(pts)
        u = _unit(p[3:6])
        a = p[:3]
        n = len(pts)
        cos_a, sin_a = math.cos(alpha), math.sin(alpha)
        g = []
        for pt in pts:
            w = _sub(pt, a)
            s = _dot(w, u)
            rho = _norm([w[0] - s * u[0], w[1] - s * u[1], w[2] - s * u[2]])
            g.append(cos_a * rho - sin_a * s)
        mean_g = sum(g) / n
        return [v - mean_g for v in g]
    return fun


def _fit_cone_free(pts):
    """Fit a cone with a free included angle (half-angle alpha = p[6]).

    Init from a free cylinder fit on the same cloud: cylinder axis == cone
    axis, and the half-angle is the slope of the rho-vs-axial-coordinate
    regression. Returns the same param keys as the fixed-angle _fit_cone
    (apex / axis / length / included_angle).
    """
    n = len(pts)
    cyl = _fit_cylinder(pts, None, 0, [0.0] * n)
    if not cyl.get("ok"):
        return cyl
    u0 = cyl["params"]["axis"]
    a0 = cyl["params"]["begin"]
    ts = [_dot(_sub(pt, a0), u0) for pt in pts]
    rho = [_radial_dist(pt, a0, u0) for pt in pts]
    mean_t = sum(ts) / n
    mean_r = sum(rho) / n
    var_t = sum((t - mean_t) ** 2 for t in ts)
    spread = max(_norm(_sub(pt, a0)) for pt in pts)
    if var_t <= 0.0 or math.sqrt(var_t) < 1e-9 * max(spread, 1e-9):
        return {"ok": False,
                "error": "cone needs axial extent (points collapse to a ring)"}
    slope = sum((ts[i] - mean_t) * (rho[i] - mean_r)
                for i in range(n)) / var_t
    if slope < 0.0:  # points open towards -u: flip the axis, slope -> +|slope|
        u0 = [-x for x in u0]
        ts = [_dot(_sub(pt, a0), u0) for pt in pts]
        slope = abs(slope)
    alpha0 = min(max(math.atan(slope), math.radians(0.5)),
                 math.radians(89.0))
    p, sse, it = _lm(_cone_free_fun(pts), a0 + u0 + [alpha0])
    alpha = p[6]
    if not (1e-4 < alpha < math.pi - 1e-4):
        return {"ok": False, "error": "cone fit diverged (bad angle)"}
    a = p[:3]
    u = _unit(p[3:6])
    cos_a, sin_a = math.cos(alpha), math.sin(alpha)
    rho = [_radial_dist(pt, a, u) for pt in pts]
    ss = [_dot(_sub(pt, a), u) for pt in pts]
    # apex axial offset from the base point a (mean over points of where the
    # cone surface g = cos*rho - sin*s crosses zero)
    tau = sum(ss[i] - cos_a * rho[i] / sin_a for i in range(n)) / n
    apex = [a[i] + tau * u[i] for i in range(3)]
    ax_from_apex = [ss[i] - tau for i in range(n)]
    length = max(ax_from_apex)
    length = max(length + 1e-6, 1e-6)
    e = _cone_free_fun(pts)(p)
    params = {"apex": apex, "axis": u, "length": length,
              "included_angle": math.degrees(2.0 * alpha)}
    return {"ok": True, "params": params, "residuals": e,
            "raw_residuals": [0.0] * n,
            "iterations": it, "sse": sse}


# --------------------------------------------------------------------------
# cloud shape descriptor (for the geometry-type classifier)
# --------------------------------------------------------------------------

def cloud_stats(pts):
    """Eigen-decomposition of the cloud's covariance, sorted ascending.

    The eigenvalue ratios separate the gross shape families cheaply, before
    any fit: a plane-like / ring-like cloud has the smallest eigenvalue ~ 0
    (points confined near a plane), a line has two small eigenvalues, a
    sphere/cylinder/cone spread in all three directions. Values are in the
    job length unit.
    """
    n = len(pts)
    if n == 0:
        return {"ok": False, "error": "no points"}
    cent = _mean(pts)
    cov = [[0.0] * 3 for _ in range(3)]
    for pt in pts:
        w = _sub(pt, cent)
        for i in range(3):
            for j in range(3):
                cov[i][j] += w[i] * w[j]
    evs = [v for v, _vec in _eig_sym3(cov)]  # ascending
    out = {"ok": True, "point_count": n, "centroid": cent,
           "eigenvalues": evs,
           "sizes": [math.sqrt(max(v, 0.0)) for v in evs]}
    lo, mid, hi = (math.sqrt(max(v, 0.0)) for v in evs)
    scale = max(hi, 1e-300)
    out["linear"] = mid < 0.02 * scale
    # "flat" = points hug a plane: thickness (smallest std) below ~5% of the
    # in-plane width. A thin ring (axial length << radius, e.g. L < ~0.12 R)
    # is flat; a real bore with length comparable to its radius is not.
    out["flat"] = lo < 0.05 * mid if mid > 0.0 else lo < 0.05 * scale
    return out
