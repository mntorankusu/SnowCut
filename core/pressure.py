"""Thickness -> pressure mapping: multi-point linear, clamp, quantize -9..9."""
from __future__ import annotations

import math
from functools import lru_cache


def sort_curve(curve):
    pts = sorted(((float(t), float(p)) for t, p in curve), key=lambda q: q[0])
    # merge duplicate thickness: keep last
    out = []
    for t, p in pts:
        if out and out[-1][0] == t:
            out[-1] = (t, p)
        else:
            out.append((t, p))
    return out


def _interp_sorted(t, pts):
    """interp_raw for an already sorted_curve()'d point list."""
    if not pts:
        return 0.0
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            if t1 == t0:
                return p1
            f = (t - t0) / (t1 - t0)
            return p0 + f * (p1 - p0)
    return pts[-1][1]


def _quantize(v):
    q = int(v + (0.5 if v >= 0 else -0.5))
    return max(-9, min(9, q))


def interp_raw(t, curve):
    """Linear interp, clamped outside endpoints. Returns float pressure."""
    return _interp_sorted(t, sort_curve(curve))


def thickness_to_pressure(t, curve):
    """Quantized int pressure -9..9 (round-half-away, then clamp)."""
    return _quantize(interp_raw(t, curve))


@lru_cache(maxsize=16384)
def _quantized_runs_cached(t0, t1, ckey, samples):
    if samples < 2:
        samples = 2
    pts = sort_curve(ckey)
    if t0 == t1 and isinstance(t0, (int, float)) and math.isfinite(t0):
        # t0 + 0.0 * f == t0 for every sample: one run, one evaluation
        return ((0.0, 1.0, _quantize(_interp_sorted(t0, pts))),)
    vals = []
    for i in range(samples + 1):
        f = i / samples
        t = t0 + (t1 - t0) * f
        vals.append(_quantize(_interp_sorted(t, pts)))
    runs = []
    start = 0.0
    cur = vals[0]
    for i in range(1, len(vals)):
        if vals[i] != cur:
            runs.append((start, i / samples, cur))
            start = i / samples
            cur = vals[i]
    runs.append((start, 1.0, cur))
    # merge (paranoia)
    merged = [runs[0]]
    for f0, f1, p in runs[1:]:
        if p == merged[-1][2]:
            merged[-1] = (merged[-1][0], f1, p)
        else:
            merged.append((f0, f1, p))
    return tuple(merged)


def quantized_runs(t0, t1, curve, samples=64):
    """Split a thickness-varying edge into hard quantized runs.

    Returns [(f0, f1, pressure_int)] with f in [0,1] along the edge.
    Finds where the rounded pressure changes by dense sampling (cheap,
    exact enough for display + G-code splitting).  Memoized on
    (t0, t1, curve contents, samples). The returned list is a fresh copy.
    """
    try:
        ckey = tuple(tuple(q) for q in curve)
        return list(_quantized_runs_cached(t0, t1, ckey, samples))
    except TypeError:
        # unhashable args: compute uncached (re-raises genuine errors)
        return list(_quantized_runs_cached.__wrapped__(t0, t1, tuple(tuple(q) for q in curve), samples))
