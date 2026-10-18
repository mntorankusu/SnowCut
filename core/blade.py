"""Blade (swivel) compensation for cut jobs + toolpath preview data.

Sits between gcode.plan_ops_flat() and ops_to_gcode()/ops_to_steps():
    ops = plan_ops_flat(doc)
    ops, preview = compensate_ops(ops, doc)

Strokes of colors marked as blade tools (ColorParams.blade) are compensated with core.swivel
according to doc.blade (BladeSettings). Pen colors pass through untouched. Consecutive blade
strokes form one run sharing the blade heading, like the firmware's cut job. A run starts with a
lead-in and ends at a tool-change pause or a non-blade stroke.

The preview holds the holder-axis path (what the machine physically moves), travel moves, and the
simulated blade tip (what actually gets cut), colored by distance from the drawn path.
All preview coordinates are in mm, in the document/canvas frame.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field, asdict

from . import swivel
from .swivel import Pt, Move, SwivelConfig

MODES = ("off", "firmware", "custom")
SIM_STEP = 2.0          # tip simulation step, 0.01 mm units (0.02 mm)
KEEP_MM = 0.05          # preview decimation


@dataclass
class BladeSettings:
    mode: str = "off"                   # off | firmware | custom (off = output unchanged)
    offset_mm: float = 0.50             # custom mode compensation offset
    corner_tolerance_mm: float = 0.10   # custom mode
    arc_tolerance_mm: float = 0.005     # custom mode
    leadin: bool = True
    leadin_x_mm: float = 1.00           # lead-in start X (firmware slot 0 = 1.00)
    leadin_end_y_mm: float = -10.47     # lead-in end Y (firmware = -10.47, in the top margin)
    leadin_pressure: object = None      # None = same as the first cut
    sim_offset_mm: float = 0.50         # physical blade offset used by the preview simulation
    show_axis: bool = True
    show_tip: bool = True
    show_travel: bool = True

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        b = BladeSettings()
        for k, v in (d or {}).items():
            if hasattr(b, k):
                setattr(b, k, v)
        if b.mode not in MODES:
            b.mode = "off"
        return b

    def swivel_config(self) -> SwivelConfig | None:
        if self.mode == "firmware":
            cfg = SwivelConfig.firmware()
        elif self.mode == "custom":
            cfg = SwivelConfig.custom(self.offset_mm, corner_tolerance_mm=self.corner_tolerance_mm,
                                      arc_tolerance_mm=self.arc_tolerance_mm)
        else:
            return None
        cfg.leadin = bool(self.leadin)
        cfg.leadin_x_mm = self.leadin_x_mm
        cfg.leadin_end_y_mm = self.leadin_end_y_mm
        return cfg

    def comp_offset_mm(self):
        return {"off": 0.0, "firmware": 0.50, "custom": self.offset_mm}[self.mode]

    def display_tolerance_mm(self):
        return self.corner_tolerance_mm if self.mode == "custom" else 0.10


@dataclass
class Stroke:
    color: str
    blade: bool
    pts: list                    # [(x_mm, y_mm)], pts[0] = travel target
    attrs: list                  # per cut point: (feed, pressure)
    ops: list = field(default_factory=list)   # the original ops of this stroke (pass-through)


@dataclass
class Preview:
    travel: list = field(default_factory=list)   # [((x0, y0), (x1, y1))]
    axis: list = field(default_factory=list)     # [(lead, [(x, y), ...])] tool-down holder path
    tip: list = field(default_factory=list)      # [(bin, [(x, y), ...])], bin: ok|warn|bad|lead
    drawn: list = field(default_factory=list)    # [[(x, y), ...]] intended strokes (reference)
    tol_mm: float = 0.10
    stats: dict = field(default_factory=dict)

    def bbox(self):
        xs, ys = [], []
        for (a, b) in self.travel:
            xs += [a[0], b[0]]; ys += [a[1], b[1]]
        for _, pl in self.axis + self.tip:
            for x, y in pl:
                xs.append(x); ys.append(y)
        return (min(xs), min(ys), max(xs), max(ys)) if xs else None


# --- ops <-> strokes ---------------------------------------------------------------------------

def _split(ops, doc):
    """ops -> list of ('op', op) | ('stroke', Stroke). Color markers are dropped (re-emitted)."""
    out, cur, pos, color = [], None, (0.0, 0.0), None

    def flush():
        nonlocal cur
        if cur is not None:
            if len(cur.pts) > 1:
                out.append(('stroke', cur))
            else:                                   # no cut: keep its ops verbatim
                out.extend(('op', o) for o in cur.ops)
        cur = None

    def start(at):
        cp = doc.color_params.get(color)
        return Stroke(color=color, blade=bool(getattr(cp, "blade", True)) if cp else True,
                      pts=[at], attrs=[])

    for op in ops:
        k = op[0]
        if k == "color":
            flush(); color = op[1]
        elif k == "rapid":
            flush(); pos = (op[1], op[2]); cur = start(pos); cur.ops.append(op)
        elif k == "down":
            if cur is None:
                cur = start(pos)
            cur.ops.append(op)
        elif k == "cut":
            if cur is None:
                cur = start(pos)
            cur.pts.append((op[1], op[2]))
            cur.attrs.append((op[3], op[4]))
            cur.ops.append(op)
            pos = (op[1], op[2])
        elif k == "up":
            if cur is not None:
                cur.ops.append(op)
                flush()
            else:
                out.append(('op', op))
        else:
            flush(); out.append(('op', op))
    flush()
    return out


def _to_points(strokes):
    """Strokes -> swivel point list + per-point (stroke index, attrs)."""
    pts, meta = [], []
    for si, s in enumerate(strokes):
        pts.append(Pt(round(s.pts[0][0] * 100), round(s.pts[0][1] * 100), True))
        meta.append((si, s.attrs[0]))
        for (x, y), a in zip(s.pts[1:], s.attrs):
            pts.append(Pt(round(x * 100), round(y * 100), False))
            meta.append((si, a))
    return pts, meta


def _raw_moves(pts):
    return [Move('travel' if p.travel else 'cut', p.x, p.y, False, i) for i, p in enumerate(pts)]


def _emit(moves, strokes, meta, leadin_attrs, out, state):
    """Compensated moves -> ops (rapid/down/cut/up + color markers)."""
    down, cur_pres, last = False, object(), None
    for m in moves:
        si, attrs = meta[m.src] if m.src >= 0 else (0, leadin_attrs)
        color = strokes[si].color
        x, y = m.x / 100.0, m.y / 100.0
        if m.kind == 'travel':
            if down:
                out.append(("up",)); down = False
            if color != state.get("color"):
                out.append(("color", color)); state["color"] = color
            out.append(("rapid", x, y))
            last = (x, y)
            continue
        feed, pres = attrs
        if down and (x, y) == last and pres == cur_pres:
            continue                                 # rounds to no motion (0.01 mm grid)
        last = (x, y)
        if not down:
            out.append(("down", pres)); down, cur_pres = True, pres
        elif pres != cur_pres:
            out.append(("down", pres)); cur_pres = pres
        out.append(("cut", x, y, feed, pres))
    if down:
        out.append(("up",))


# --- preview -------------------------------------------------------------------------------------

def _segd(px, py, a, b):
    (x1, y1), (x2, y2) = a, b
    vx, vy = x2 - x1, y2 - y1
    L2 = vx * vx + vy * vy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / L2))
    return math.hypot(px - x1 - t * vx, py - y1 - t * vy)


class _Nearest:
    """Exact distance from a point to a stroke polyline, via a uniform grid of segments."""
    CELL = 100.0    # 1 mm in 0.01 mm units

    def __init__(self, poly):
        self.segs = list(zip(poly, poly[1:])) or [(poly[0], poly[0])]
        self.grid = {}
        c = self.CELL
        for j, ((x1, y1), (x2, y2)) in enumerate(self.segs):
            for gx in range(int(math.floor(min(x1, x2) / c)), int(math.floor(max(x1, x2) / c)) + 1):
                for gy in range(int(math.floor(min(y1, y2) / c)), int(math.floor(max(y1, y2) / c)) + 1):
                    self.grid.setdefault((gx, gy), []).append(j)

    def dist(self, x, y):
        c = self.CELL
        gx, gy = int(math.floor(x / c)), int(math.floor(y / c))
        for ring in range(0, 4):
            cand = set()
            for ix in range(gx - ring, gx + ring + 1):
                for iy in range(gy - ring, gy + ring + 1):
                    cand.update(self.grid.get((ix, iy), ()))
            if cand:
                best = min(_segd(x, y, *self.segs[j]) for j in cand)
                if best <= ring * c:          # nothing outside the searched rings can be closer
                    return best
        return min(_segd(x, y, *sg) for sg in self.segs)


def _tip_preview(moves, strokes, pts, meta, sim_r, tol, prev, worst):
    """Simulate the blade tip, append colored runs to prev.tip and return (worst, sum, n) in 0.01 mm."""
    total, count = 0.0, 0
    polys, near = {}, {}
    for i, p in enumerate(pts):
        polys.setdefault(meta[i][0], []).append((p.x, p.y))
    for seq in swivel.simulate_tip(moves, sim_r, 90.0, SIM_STEP):
        runs, last_bin, last_xy = [], None, None
        for x, y, lead, src in seq:
            if lead or src < 0:
                b, d = "lead", 0.0
            else:
                si = meta[src][0]
                if si not in near:
                    near[si] = _Nearest(polys[si])
                d = near[si].dist(x, y)
                worst = max(worst, d); total += d; count += 1
                b = "ok" if d <= tol * 50 else ("warn" if d <= tol * 100 else "bad")
            xy = (x / 100.0, y / 100.0)
            if b != last_bin:
                pl = [last_xy] if last_xy is not None else []
                runs.append((b, pl)); last_bin = b
            pl = runs[-1][1]
            if not pl or math.hypot(xy[0] - pl[-1][0], xy[1] - pl[-1][1]) >= KEEP_MM:
                pl.append(xy)
            last_xy = xy
        if runs and last_xy is not None and runs[-1][1][-1] != last_xy:
            runs[-1][1].append(last_xy)
        prev.tip += [r for r in runs if len(r[1]) > 1]
    return worst, total, count


def _axis_preview(moves, prev, state):
    seq = None
    for m in moves:
        xy = (m.x / 100.0, m.y / 100.0)
        if m.kind == 'travel':
            prev.travel.append((state["pos"], xy))
            seq = None
        else:
            if seq is None:
                seq = (bool(m.lead), [state["pos"]])
                prev.axis.append(seq)
            seq[1].append(xy)
        state["pos"] = xy


# --- main entry ----------------------------------------------------------------------------------

def compensate_ops(ops, doc, settings: BladeSettings | None = None, simulate=True):
    """Return (new_ops, Preview). new_ops keeps the plan_ops_flat op vocabulary."""
    bs = settings or getattr(doc, "blade", None) or BladeSettings()
    cfg = bs.swivel_config()
    tol = bs.display_tolerance_mm()
    sim_r = bs.sim_offset_mm * 100.0
    prev = Preview(tol_mm=tol)
    out, emit_state, axis_state = [], {}, {"pos": (0.0, 0.0)}
    st = dict(runs=0, leadins=0, arcs=0, moves=0, worst=0.0, mean=0.0, worst_raw=0.0)
    acc = [0.0, 0.0, 0]          # worst, sum, n (0.01 mm), compensated/as-sent
    acc_raw = [0.0]

    blocks = _split(ops, doc)
    i = 0
    while i < len(blocks):
        kind, item = blocks[i]
        if kind == 'op':
            out.append(item)
            i += 1
            continue
        run = [item]
        i += 1
        if item.blade:                                   # extend run over consecutive blade strokes
            while i < len(blocks) and blocks[i][0] == 'stroke' and blocks[i][1].blade:
                run.append(blocks[i][1]); i += 1
        pts, meta = _to_points(run)
        if item.blade and cfg is not None:
            moves = swivel.compensate(pts, cfg=cfg).moves
            st["runs"] += 1
            st["leadins"] += 1 if (cfg.leadin and cfg.r) else 0
        else:
            moves = _raw_moves(pts)
        if item.blade and cfg is not None:
            first = run[0].attrs[0]
            leadin_attrs = (first[0], first[1] if bs.leadin_pressure is None else bs.leadin_pressure)
            _emit(moves, run, meta, leadin_attrs, out, emit_state)
        else:                                            # uncompensated: original ops verbatim
            for s in run:
                if s.color != emit_state.get("color"):
                    out.append(("color", s.color)); emit_state["color"] = s.color
                out.extend(s.ops)
        st["arcs"] += sum(1 for m in moves if m.kind == 'arc')
        st["moves"] += len(moves)
        n_axis = len(prev.axis)
        _axis_preview(moves, prev, axis_state)
        prev.drawn += [list(s.pts) for s in run]
        if simulate:
            if item.blade and sim_r > 0:
                acc[0], s, n = _tip_preview(moves, run, pts, meta, sim_r, tol, prev, acc[0])
                acc[1] += s; acc[2] += n
                if cfg is not None:                      # baseline: same strokes, no compensation
                    scratch = Preview()
                    acc_raw[0], _, _ = _tip_preview(_raw_moves(pts), run, pts, meta, sim_r, tol,
                                                    scratch, acc_raw[0])
                else:
                    acc_raw[0] = acc[0]
            else:                                        # pen: tip == axis
                for lead, pl in prev.axis[n_axis:]:
                    prev.tip.append(("lead" if lead else "ok", list(pl)))
    st["worst"] = acc[0] / 100.0
    st["mean"] = (acc[1] / acc[2] / 100.0) if acc[2] else 0.0
    st["worst_raw"] = acc_raw[0] / 100.0
    prev.stats = st
    return out, prev
