"""SVG paths -> ordered cut ops + G-code text + MOTION steps.

Ops: ("rapid", x_mm, y_mm) | ("cut", x_mm, y_mm, feed|None, pressure|None)
     | ("down", pressure|None) | ("up",) | ("pause", color) | ("msg", str)
     | ("color", hex)   marker: following strokes belong to this color key
Pressure resolution per sub-segment:
  - if color.use_thickness_map: quantized thickness->pressure int
  - elif color.pressure is not None: that int
  - else None (machine setting: M3 sent without value / default down)
Speed: color.speed None -> omit F, else 0..9 feed.
Dash: dash_enabled splits each edge into on/off runs in mm (perforation).
Tabs: every tab of a path leaves its gap uncut (the tool is lifted over it).
Color groups whose tool is "skip" produce no ops at all (no color marker, strokes or pause).
"""
from __future__ import annotations
import math
from .pressure import quantized_runs, thickness_to_pressure
from .doc import SKIP_TOOL, subtract_spans


def _pressure_for(cp, thickness):
    if cp.use_thickness_map:
        return thickness_to_pressure(thickness, cp.curve)
    return cp.pressure


def _dash_segment_ops(ops, poly, p, cp, cur_pres, tool_down):
    """Dash one original anchor segment (flattened polyline poly = [(x, y, thickness), ...]).

    The on/off pattern is laid out by cumulative arc length along the polyline and restarts at
    the segment start (identical to the canvas _draw_dash_gaps: on [k*period, k*period+on]).
    A dash follows the polyline vertices it spans. Returns (cur_pres, tool_down)."""
    # arc-length table over non-degenerate edges: (cum_start, length, x0, y0, ux, uy, t0, t1)
    edges = []
    L = 0.0
    for a, b in zip(poly, poly[1:]):
        el = math.hypot(b[0] - a[0], b[1] - a[1])
        if el <= 1e-12:
            continue
        edges.append((L, el, a[0], a[1], (b[0] - a[0]) / el, (b[1] - a[1]) / el, a[2], b[2]))
        L += el
    if L <= 1e-9:
        return cur_pres, tool_down
    period = p.dash_on_mm + p.dash_off_mm

    def pos(ei, arc):
        c0, _el, x0, y0, ux, uy, _t0, _t1 = edges[ei]
        return x0 + ux * (arc - c0), y0 + uy * (arc - c0)

    def thick(ei, arc):
        c0, el, _x0, _y0, _ux, _uy, t0, t1 = edges[ei]
        return t0 + (t1 - t0) * ((arc - c0) / el)

    def edge_of(arc):
        for ei in range(len(edges) - 1):
            if arc < edges[ei + 1][0]:
                return ei
        return len(edges) - 1

    s = 0.0
    while s < L - 1e-9:
        e = min(s + p.dash_on_mm, L)
        ei0 = edge_of(s)
        t0r = thick(ei0, s)
        # rapid to run start if gapped
        if s > 0 or not tool_down:
            rsx, rsy = pos(ei0, s)
            ops.append(("rapid", rsx, rsy))
            ops.append(("down", _pressure_for(cp, t0r)))
            cur_pres = _pressure_for(cp, t0r)
            tool_down = True
        # pieces of the run, each inside one polyline edge: (edge, arc_a, arc_b)
        pieces = []
        sa, ei = s, ei0
        while True:
            c_end = edges[ei][0] + edges[ei][1]
            if e <= c_end + 1e-9 or ei == len(edges) - 1:
                pieces.append((ei, sa, e))
                break
            pieces.append((ei, sa, c_end))
            sa, ei = c_end, ei + 1
        for ei, sa, sb in pieces:
            if cp.use_thickness_map:
                ta, tb = thick(ei, sa), thick(ei, sb)
                for q0, q1, pq in quantized_runs(ta, tb, cp.curve):
                    ex, ey = pos(ei, sa + (sb - sa) * q1)
                    if pq != cur_pres:
                        ops.append(("down", pq))
                        cur_pres = pq
                    ops.append(("cut", ex, ey, cp.speed, pq))
            else:
                ex, ey = pos(ei, sb)
                ops.append(("cut", ex, ey, cp.speed, cp.pressure))
        ops.append(("up",))
        tool_down = False
        s += period
    return cur_pres, tool_down


def _tabbed_path_ops(ops, doc, p, cp):
    """Ops for a path with tabs: every segment is cut except the tab gaps (and the dash gaps,
    when the path is dashed). Without dashes, cut pieces that meet at an anchor are one stroke,
    and a closed path starts after a gap, so that the line is cut in as few strokes as possible."""
    gaps = doc.tab_gaps(p)
    dashed = p.dash_enabled and p.dash_on_mm + p.dash_off_mm > 1e-9
    m = p.segment_count()
    tables = []
    pieces = []  # (segment, arc_a, arc_b, reaches_segment_end)
    for si in range(m):
        poly = doc.segment_abs_poly(p, si)
        cum = doc._arc_table(poly)
        L = cum[-1]
        tables.append((poly, cum))
        if L <= 1e-9:
            continue
        if dashed:
            period = p.dash_on_mm + p.dash_off_mm
            spans, s = [], 0.0
            while s < L - 1e-9:
                spans.append((s, min(s + p.dash_on_mm, L)))
                s += period
        else:
            spans = [(0.0, L)]
        for a, b in subtract_spans(spans, gaps.get(si, [])):
            pieces.append((si, a, b, b >= L - 1e-9))
    if not pieces:
        return
    # strokes: runs of pieces that continue across an anchor (never for dashes, which lift
    # after every dash like the untabbed dash cut)
    strokes = []
    for pc in pieces:
        prev = strokes[-1][-1] if strokes else None
        if (not dashed and prev is not None and prev[3] and pc[1] <= 1e-9
                and pc[0] == prev[0] + 1):
            strokes[-1].append(pc)
        else:
            strokes.append([pc])
    if (p.closed and not dashed and len(strokes) > 1 and strokes[-1][-1][3]
            and strokes[-1][-1][0] == m - 1 and strokes[0][0][0] == 0 and strokes[0][0][1] <= 1e-9):
        strokes[0] = strokes.pop() + strokes[0]  # the cut runs on through the start anchor

    def at(si, s):
        """(x, y, thickness) at arc s of segment si."""
        poly, cum = tables[si]
        for k in range(len(poly) - 1):
            if s <= cum[k + 1] or k == len(poly) - 2:
                el = cum[k + 1] - cum[k]
                f = 0.0 if el <= 1e-12 else max(0.0, min(1.0, (s - cum[k]) / el))
                a, b = poly[k], poly[k + 1]
                return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)
        return poly[0]

    for stroke in strokes:
        si0, s0 = stroke[0][0], stroke[0][1]
        x, y, th = at(si0, s0)
        ops.append(("rapid", x, y))
        cur_pres = _pressure_for(cp, th)
        ops.append(("down", cur_pres))
        for si, sa, sb, _end in stroke:
            poly, cum = tables[si]
            # the piece's own vertices: its ends plus every polyline vertex strictly inside
            stops = [sa] + [c for c in cum[1:-1] if sa + 1e-9 < c < sb - 1e-9] + [sb]
            prev = at(si, sa)
            for s in stops[1:]:
                nxt = at(si, s)
                if cp.use_thickness_map:
                    for _q0, q1, pq in quantized_runs(prev[2], nxt[2], cp.curve):
                        ex = prev[0] + (nxt[0] - prev[0]) * q1
                        ey = prev[1] + (nxt[1] - prev[1]) * q1
                        if pq != cur_pres:
                            ops.append(("down", pq))
                            cur_pres = pq
                        ops.append(("cut", ex, ey, cp.speed, pq))
                else:
                    ops.append(("cut", nxt[0], nxt[1], cp.speed, cp.pressure))
                prev = nxt
        ops.append(("up",))


def plan_ops_flat(doc):
    """Materialized op list with dash-gap up/down + quantized pressure splits."""
    ops = []
    for color in doc.ordered_colors():
        cp = doc.color_params[color]
        if cp.tool == SKIP_TOOL:
            continue  # the user left this group out of the job
        ops.append(("color", color))
        for p in [q for q in doc.paths.values() if q.color == color]:
            svg = doc.svgs.get(p.svg_id)
            if svg is not None and not svg.visible:
                continue  # hidden SVGs are not cut
            pts = doc.path_abs_flat(p)  # curves flattened, and closed paths omit the repeated start
            if len(pts) < 2:
                continue
            if p.tabs:
                _tabbed_path_ops(ops, doc, p, cp)
                continue
            ops.append(("rapid", pts[0][0], pts[0][1]))
            first_pres = _pressure_for(cp, pts[0][2])
            ops.append(("down", first_pres))
            cur_pres = first_pres
            tool_down = True
            if p.dash_enabled and p.dash_on_mm + p.dash_off_mm > 1e-9:
                # dash pattern restarts at every original anchor segment (matches the canvas)
                for si in range(p.segment_count()):
                    cur_pres, tool_down = _dash_segment_ops(
                        ops, doc.segment_abs_poly(p, si), p, cp, cur_pres, tool_down)
            else:
                n = len(pts) if p.closed else len(pts) - 1
                for si in range(n):
                    a = pts[si % len(pts)]
                    b = pts[(si + 1) % len(pts)]
                    if cp.use_thickness_map:
                        for q0, q1, pq in quantized_runs(a[2], b[2], cp.curve):
                            ex = a[0] + (b[0] - a[0]) * q1
                            ey = a[1] + (b[1] - a[1]) * q1
                            if pq != cur_pres:
                                ops.append(("down", pq))
                                cur_pres = pq
                            ops.append(("cut", ex, ey, cp.speed, pq))
                    else:
                        ops.append(("cut", b[0], b[1], cp.speed, cp.pressure))
            # remove trailing up dup
            if not ops or ops[-1][0] != "up":
                ops.append(("up",))
        if doc.pause_after.get(color):
            ops.append(("pause", color))
    return ops


def _to_machine(doc):
    """Canvas mm -> machine mm: undoes the mat skew correction (core.mat.MatSkew)."""
    skew = getattr(doc, "mat_skew", None)
    if skew is None or skew.is_identity():
        return lambda x, y: (x, y)
    return skew.to_machine


def ops_to_gcode(ops, doc):
    lines = ["G21 ; mm", "G90 ; abs"]
    tm = _to_machine(doc)
    skew = getattr(doc, "mat_skew", None)
    if skew is not None and not skew.is_identity():
        lines.append("; mat skew: %.3f deg, offset %.2f, %.2f mm (moves below are in machine mm)"
                     % (skew.angle_deg, skew.dx, skew.dy))
    for op in ops:
        if op[0] == "rapid":
            x, y = tm(op[1], op[2])
            lines.append("G0 X%.3f Y%.3f" % (-x, y))
        elif op[0] == "cut":
            _, x, y, feed, pres = op
            x, y = tm(x, y)
            s = "G1 X%.3f Y%.3f" % (-x, y)
            if feed is not None:
                s += " F%d" % int(feed)
            if pres is not None:
                s += " ; P%d" % int(pres)
            lines.append(s)
        elif op[0] == "down":
            pres = op[1]
            lines.append("M3" if pres is None else "M3 P%d" % int(pres))
        elif op[0] == "up":
            lines.append("M5")
        elif op[0] == "pause":
            lines.append("; PAUSE after %s (tool change, manual resume)" % op[1])
        elif op[0] == "color":
            lines.append("; color %s" % op[1])
    lines.append("G0 X0.000 Y0.000 ; return home")
    return "\n".join(lines) + "\n"


def ops_to_steps(ops, doc, dry_run=False):
    """Convert mm ops to MOTION steps + M3 pressure values. A ("TOOL", tool) step precedes
    each color group whose tool differs from the previous group's."""
    out = []
    tm = _to_machine(doc)
    tool = None
    for op in ops:
        if op[0] == "color":
            # tells the sender which machine setting (cut/draw/emboss) an unset speed or
            # pressure stands for
            cp = doc.color_params.get(op[1])
            if cp is not None and cp.tool != tool:
                tool = cp.tool
                out.append(("TOOL", tool))
        elif op[0] == "rapid":
            x, y = tm(op[1], op[2])
            out.append(("G0", int(round(-x * doc.steps_x)), int(round(y * doc.steps_y))))
        elif op[0] == "cut":
            _, x, y, feed, pres = op
            x, y = tm(x, y)
            out.append(("G1", int(round(-x * doc.steps_x)), int(round(y * doc.steps_y)),
                        feed, pres))
        elif op[0] == "down":
            if dry_run:
                continue
            out.append(("M3", op[1]))
        elif op[0] == "up":
            if dry_run:
                continue
            out.append(("M5",))
        elif op[0] == "pause":
            out.append(("PAUSE", op[1]))
    out.append(("G0", 0, 0))
    return out


def stats(ops):
    import math
    dist = 0.0
    cuts = downs = pauses = 0
    prev = None
    for op in ops:
        if op[0] in ("rapid", "cut"):
            x, y = op[1], op[2]
            if prev is not None:
                dist += math.hypot(x - prev[0], y - prev[1])
            prev = (x, y)
            if op[0] == "cut":
                cuts += 1
        elif op[0] == "down":
            downs += 1
            prev = None
        elif op[0] == "pause":
            pauses += 1
            prev = None
        elif op[0] == "up":
            prev = None
    return {"cut_mm": dist, "cuts": cuts, "downs": downs, "pauses": pauses}
