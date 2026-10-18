"""Bezier/arc flattening to polylines. Tolerance in mm (default 0.1)."""
from __future__ import annotations
import math


def _dist_to_line(px, py, x0, y0, x1, y1):
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy)
    if L == 0:
        return math.hypot(px - x0, py - y0)
    return abs((px - x0) * dy - (py - y0) * dx) / L


def flatten_cubic(p0, p1, p2, p3, tol=0.1):
    """Returns list of points (excluding p0, including p3)."""
    # recursive subdivision
    m01 = ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)
    m12 = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
    m23 = ((p2[0] + p3[0]) / 2, (p2[1] + p3[1]) / 2)
    m012 = ((m01[0] + m12[0]) / 2, (m01[1] + m12[1]) / 2)
    m123 = ((m12[0] + m23[0]) / 2, (m12[1] + m23[1]) / 2)
    m = ((m012[0] + m123[0]) / 2, (m012[1] + m123[1]) / 2)
    d1 = _dist_to_line(p1[0], p1[1], p0[0], p0[1], p3[0], p3[1])
    d2 = _dist_to_line(p2[0], p2[1], p0[0], p0[1], p3[0], p3[1])
    if max(d1, d2) <= tol:
        return [p3]
    left = flatten_cubic(p0, m01, m012, m, tol)
    right = flatten_cubic(m, m123, m23, p3, tol)
    return _flat_combine(left, right, m)


def _flat_combine(left_pts, right_pts, mid):
    # left_pts ends at mid, right_pts ends at p3, so the duplicate mid is skipped
    if left_pts and abs(left_pts[-1][0] - mid[0]) < 1e-9 and abs(left_pts[-1][1] - mid[1]) < 1e-9:
        return left_pts + right_pts
    return left_pts + [mid] + right_pts


def flatten_quad(p0, p1, p2, tol=0.1):
    # elevate to cubic then flatten
    c1 = (p0[0] + 2 / 3 * (p1[0] - p0[0]), p0[1] + 2 / 3 * (p1[1] - p0[1]))
    c2 = (p2[0] + 2 / 3 * (p1[0] - p2[0]), p2[1] + 2 / 3 * (p1[1] - p2[1]))
    return flatten_cubic(p0, c1, c2, p2, tol)


def flatten_arc(x0, y0, rx, ry, phi_deg, large, sweep, x1, y1, tol=0.1):
    """Endpoint -> center arc approx, then angle-step polyline. Returns pts excl start."""
    if rx == 0 or ry == 0:
        return [(x1, y1)]
    phi = math.radians(phi_deg)
    # F.6.5 endpoint->center
    dx = (x0 - x1) / 2
    dy = (y0 - y1) / 2
    x1p = math.cos(phi) * dx + math.sin(phi) * dy
    y1p = -math.sin(phi) * dx + math.cos(phi) * dy
    lam = (x1p ** 2) / (rx ** 2) + (y1p ** 2) / (ry ** 2)
    if lam > 1:
        s = math.sqrt(lam)
        rx *= s
        ry *= s
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    f = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        f = -f
    cxp = f * rx * y1p / ry
    cyp = -f * ry * x1p / rx
    cx = math.cos(phi) * cxp - math.sin(phi) * cyp + (x0 + x1) / 2
    cy = math.sin(phi) * cxp + math.cos(phi) * cyp + (y0 + y1) / 2

    def ang(ux, uy, vx, vy):
        d = math.hypot(ux, uy) * math.hypot(vx, vy)
        c = max(-1.0, min(1.0, (ux * vx + uy * vy) / d)) if d else 1.0
        a = math.acos(c)
        if ux * vy - uy * vx < 0:
            a = -a
        return a

    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    if sweep and dt < 0:
        dt += 2 * math.pi
    # steps by tolerance: chord err ~ r*dA^2/8
    r = max(rx, ry)
    dA = math.sqrt(8 * tol / r) if r > 0 else abs(dt)
    dA = max(0.02, min(abs(dt), dA))
    n = max(2, int(math.ceil(abs(dt) / dA)))
    pts = []
    for i in range(1, n + 1):
        a = t1 + dt * i / n
        px = cx + rx * math.cos(phi) * math.cos(a) - ry * math.sin(phi) * math.sin(a)
        py = cy + rx * math.sin(phi) * math.cos(a) + ry * math.cos(phi) * math.sin(a)
        pts.append((px, py))
    pts[-1] = (x1, y1)
    return pts
