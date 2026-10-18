"""Shape helpers: ellipse/polygon construction, RDP simplify, abs-space cubic flattening."""
from __future__ import annotations
import math
from .flatten import flatten_cubic, _dist_to_line

KAPPA = 0.5522847498  # cubic circle-arc constant


def ellipse_anchors(cx, cy, rx, ry):
    """4 cubic anchors (x, y, hin, hout), right/bottom/left/top (clockwise on a Y-down canvas)."""
    out = []
    for k in range(4):
        th = k * math.pi / 2
        c, s = math.cos(th), math.sin(th)
        x, y = cx + rx * c, cy + ry * s
        tx, ty = -rx * s * KAPPA, ry * c * KAPPA  # tangent (direction of increasing th)
        out.append((x, y, (x - tx, y - ty), (x + tx, y + ty)))
    return out


def regular_polygon(cx, cy, r, n, rot_rad=0.0):
    """n vertices on a circle of radius r, first vertex at angle rot_rad."""
    n = max(3, int(n))
    return [(cx + r * math.cos(rot_rad + 2 * math.pi * k / n),
             cy + r * math.sin(rot_rad + 2 * math.pi * k / n)) for k in range(n)]


def split_cubic(p0, c1, c2, p3, u):
    """de Casteljau: the cubic split at parameter u -> ((p0, a, b, m), (m, c, d, p3))."""
    def lerp(a, b):
        return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)
    m01, m12, m23 = lerp(p0, c1), lerp(c1, c2), lerp(c2, p3)
    m012, m123 = lerp(m01, m12), lerp(m12, m23)
    m = lerp(m012, m123)
    return (tuple(p0[:2]), m01, m012, m), (m, m123, m23, tuple(p3[:2]))


def _unit(dx, dy):
    L = math.hypot(dx, dy)
    return (dx / L, dy / L) if L > 1e-12 else None


def seg_point(seg, u):
    """Point at parameter u of a segment (p0, c1, c2, p3), where c1 None = straight (u linear)."""
    p0, c1, c2, p3 = seg
    if c1 is None:
        return (p0[0] + (p3[0] - p0[0]) * u, p0[1] + (p3[1] - p0[1]) * u)
    v = 1.0 - u
    w0, w1, w2, w3 = v * v * v, 3 * v * v * u, 3 * v * u * u, u * u * u
    return (w0 * p0[0] + w1 * c1[0] + w2 * c2[0] + w3 * p3[0],
            w0 * p0[1] + w1 * c1[1] + w2 * c2[1] + w3 * p3[1])


def seg_tangent(seg, u):
    """Unit direction of travel at parameter u (None for a zero-length segment)."""
    p0, c1, c2, p3 = seg
    if c1 is None:
        return _unit(p3[0] - p0[0], p3[1] - p0[1])
    v = 1.0 - u
    t = _unit(3 * v * v * (c1[0] - p0[0]) + 6 * v * u * (c2[0] - c1[0]) + 3 * u * u * (p3[0] - c2[0]),
              3 * v * v * (c1[1] - p0[1]) + 6 * v * u * (c2[1] - c1[1]) + 3 * u * u * (p3[1] - c2[1]))
    if t is None:   # an end whose handle sits on its anchor: the curve leaves toward the next one
        t = (_unit(c2[0] - p0[0], c2[1] - p0[1]) if u < 0.5 else _unit(p3[0] - c1[0], p3[1] - c1[1]))             or _unit(p3[0] - p0[0], p3[1] - p0[1])
    return t


def _param_at_dist(seg, dist, from_end, tol):
    """Parameter of the first point of seg that is `dist` (straight line) from its start, or from
    its end with from_end."""
    p0, c1, c2, p3 = seg
    if c1 is None:
        L = math.hypot(p3[0] - p0[0], p3[1] - p0[1])
        f = min(1.0, dist / L) if L > 1e-12 else 0.0
        return 1.0 - f if from_end else f
    poly = flatten_cubic_t(p0, c1, c2, p3, tol)
    if from_end:
        poly = poly[::-1]
    ox, oy = poly[0][0], poly[0][1]
    prev = (0.0, poly[0][2])
    for x, y, u in poly[1:]:
        d = math.hypot(x - ox, y - oy)
        if d >= dist:
            f = (dist - prev[0]) / (d - prev[0]) if d - prev[0] > 1e-12 else 1.0
            return prev[1] + (u - prev[1]) * f
        prev = (d, u)
    return poly[-1][2]


def corner_fillet(seg_in, seg_out, radius, max_in, max_out, tol=0.05):
    """Round the corner where seg_in ends and seg_out starts (segments (p0, c1, c2, p3), c1/c2
    None = straight) with a circular arc of `radius`. The arc's ends are at most max_in / max_out
    (straight distance) from the corner, and a radius that does not fit shrinks.

    Returns (u_in, u_out, arc), where seg_in is kept up to u_in, seg_out from u_out and arc = one cubic
    (s, c1, c2, e) between them, or None when the corner stays sharp (radius 0, the segments
    already meet smoothly, or a zero-length segment). Exact circle for straight segments. On a
    curve the arc's ends follow the curve's tangents there."""
    if radius <= 0:
        return None
    din, dout = seg_tangent(seg_in, 1.0), seg_tangent(seg_out, 0.0)
    if din is None or dout is None:
        return None
    theta = math.acos(max(-1.0, min(1.0, -(din[0] * dout[0] + din[1] * dout[1]))))   # corner angle
    if not 1e-4 < theta < math.pi - 1e-4:
        return None
    t = min(radius / math.tan(theta / 2), max_in, max_out)   # distance of the arc ends from the corner
    if t <= 1e-9:
        return None
    u_in = _param_at_dist(seg_in, t, True, tol)
    u_out = _param_at_dist(seg_out, t, False, tol)
    s, e = seg_point(seg_in, u_in), seg_point(seg_out, u_out)
    ts, te = seg_tangent(seg_in, u_in) or din, seg_tangent(seg_out, u_out) or dout
    phi = math.acos(max(-1.0, min(1.0, ts[0] * te[0] + ts[1] * te[1])))   # the arc's sweep
    chord = math.hypot(e[0] - s[0], e[1] - s[1])
    if phi < 1e-6 or chord < 1e-12:
        return u_in, u_out, (s, s, e, e)
    k = 4.0 / 3.0 * math.tan(phi / 4) * chord / (2 * math.sin(phi / 2))   # handle length
    return u_in, u_out, (s, (s[0] + ts[0] * k, s[1] + ts[1] * k), (e[0] - te[0] * k, e[1] - te[1] * k), e)


def rdp(points, tol):
    """Ramer-Douglas-Peucker on a polyline (iterative). Keeps endpoints and returns the same tuples."""
    n = len(points)
    if n < 3:
        return list(points)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i0, i1 = stack.pop()
        a, b = points[i0], points[i1]
        dmax, imax = -1.0, -1
        for i in range(i0 + 1, i1):
            d = _dist_to_line(points[i][0], points[i][1], a[0], a[1], b[0], b[1])
            if d > dmax:
                dmax, imax = d, i
        if dmax > tol:
            keep[imax] = True
            stack.append((i0, imax))
            stack.append((imax, i1))
    return [p for p, k in zip(points, keep) if k]


def flatten_cubic_abs(p0, c1, c2, p3, tol=0.05):
    """Flatten a cubic in abs mm: polyline INCLUDING both endpoints (thin wrapper over flatten_cubic)."""
    return [tuple(p0[:2])] + flatten_cubic(p0, c1, c2, p3, tol)


def flatten_cubic_t(p0, c1, c2, p3, tol=0.05, max_depth=18):
    """Like flatten_cubic_abs but returns (x, y, u) with u the curve parameter in [0, 1]."""
    out = [(p0[0], p0[1], 0.0)]

    def rec(a, b, c, d, u0, u1, depth):
        d1 = _dist_to_line(b[0], b[1], a[0], a[1], d[0], d[1])
        d2 = _dist_to_line(c[0], c[1], a[0], a[1], d[0], d[1])
        if max(d1, d2) <= tol or depth >= max_depth:
            out.append((d[0], d[1], u1))
            return
        m01 = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        m12 = ((b[0] + c[0]) / 2, (b[1] + c[1]) / 2)
        m23 = ((c[0] + d[0]) / 2, (c[1] + d[1]) / 2)
        m012 = ((m01[0] + m12[0]) / 2, (m01[1] + m12[1]) / 2)
        m123 = ((m12[0] + m23[0]) / 2, (m12[1] + m23[1]) / 2)
        m = ((m012[0] + m123[0]) / 2, (m012[1] + m123[1]) / 2)
        um = (u0 + u1) / 2
        rec(a, m01, m012, m, u0, um, depth + 1)
        rec(m, m123, m23, d, um, u1, depth + 1)

    rec(p0, c1, c2, p3, 0.0, 1.0, 0)
    return out


def constrain_angle(x0, y0, x, y, step_deg=15.0):
    """Snap the direction (x0,y0)->(x,y) to a multiple of step_deg, keeping the length."""
    dx, dy = x - x0, y - y0
    L = math.hypot(dx, dy)
    if L == 0:
        return (x, y)
    step = math.radians(step_deg)
    ang = round(math.atan2(dy, dx) / step) * step
    return (x0 + L * math.cos(ang), y0 + L * math.sin(ang))
