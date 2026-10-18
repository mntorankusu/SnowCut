"""Drag-blade (swivel) compensation: an exact copy of the CM110 firmware's algorithm, plus a
generalized mode for any blade offset.

Mirrors Mechanism\\MovState\\movStCut.c in the main firmware (see SWIVEL_COMPENSATION.md):
  lead-in      FUN_88dc1870  three-point blade-orientation cut in the mat's top margin
  decide       FUN_88dbe604  per point: overshoot? swivel arc?
  cut point    FUN_88dbeda0  (+ FUN_88dbe1f8 overshoot, FUN_88dbdda0 arc)
  travel point FUN_88dbe95c
SwivelConfig.firmware() (the default) matches the firmware point-for-point (tools/test_swivel.py).
SwivelConfig.custom(offset_mm) generalizes the firmware's r=0.5 mm shortcuts to any offset:
  - corners: arc when the no-arc corner error r*g(turn) would exceed corner_tolerance_mm
    (the firmware's fixed 45 deg rule tolerates up to 0.6*r, i.e. 0.31 mm at r=0.5)
  - arcs: step size from arc_tolerance_mm (chord sagitta), and the last step lands exactly on the
    outgoing direction (firmware: 1 deg steps, ceil, can overrun by <1 deg)
  - heading kept as float (firmware rounds it to whole degrees: r*0.009 error)
  - lead-in legs scaled by r/0.5 mm, so the blade gets the same number of offsets of travel
    to align
simulate_tip() models a rigid-offset drag knife to measure the real cut error of a move list.

Units are firmware path units, 0.01 mm, in the firmware's path frame (the same frame the
cut job uses before the X axis is negated for the motor). Float32 rounding is reproduced
where the firmware stores float32. Use mm_* helpers for millimetres.

Not wired into gcode.py yet: call compensate() on a flat point list, then emit the moves.
"""
from __future__ import annotations
import math
import struct
from dataclasses import dataclass, field

BLADE_OFFSET = 50          # 0.50 mm, standard + deep blade (blade table word1), 0 for pens/emboss/foil
ARC_MIN_TURN_DEG = 45      # 180 - 135 (DAT_88014108): smaller turns only overshoot, no arc
LEADIN_SHAPE = ((True, 100, -1600), (False, 100, -1400), (False, 454, -1047))  # (travel?, x, y)
LEADIN_SLOTS_X = [i * 2540 + j * 500 for i in range(12) for j in range(4)]       # 48 slots (0x8903a8c0)
LEADIN_FIXED = (0x7301, 1)  # slot used after a test cut (FUN_88dc2060 else-branch): X+=29441, Y+=1

# Max tip deviation / r when a corner of the given turn (0,5,...,180 deg) is cut with the
# overshoot only (no pivot arc). Measured with simulate_tip() on long segments.
CORNER_ERR = [0.0, 0.0772, 0.1537, 0.229, 0.3025, 0.3736, 0.4417, 0.5063, 0.5669, 0.623, 0.6741,
              0.7198, 0.7598, 0.7938, 0.8215, 0.8427, 0.8572, 0.8649, 0.8658, 0.8598, 0.8472,
              0.8278, 0.8021, 0.7701, 0.7321, 0.6886, 0.6399, 0.5864, 0.5287, 0.4673, 0.4027,
              0.3357, 0.3571, 0.4515, 0.5707, 0.7295, 2.0]


@dataclass
class SwivelConfig:
    offset_mm: float = 0.50                 # blade tip offset from the holder axis (0 = no compensation)
    corner_tolerance_mm: float | None = None  # None: fixed min_turn_deg rule (firmware)
    min_turn_deg: float = ARC_MIN_TURN_DEG  # used when corner_tolerance_mm is None
    arc_step_deg: float = 1.0               # largest pivot step
    arc_tolerance_mm: float | None = None   # if set, step also limited so chord sagitta <= this
    exact_arc_end: bool = False             # True: steps evenly divide the turn, last lands on target
    integer_heading: bool = True            # firmware remembers the heading in whole degrees
    leadin: bool = True
    leadin_slot: int = 0                    # firmware picks rand() % 48
    leadin_scale: float | None = 1.0        # None: max(1, offset_mm / 0.5), end point stays at y=-10.47 mm
    leadin_x_mm: float | None = None        # lead-in start X (None: slot position, as in the firmware)
    leadin_end_y_mm: float | None = None    # lead-in end Y (None: -10.47 mm, as in the firmware)

    @classmethod
    def firmware(cls) -> "SwivelConfig":
        """Exactly what the CM110 does for its own 0.50 mm blades."""
        return cls()

    @classmethod
    def custom(cls, offset_mm: float, corner_tolerance_mm: float = 0.10,
               arc_tolerance_mm: float = 0.005, **kw) -> "SwivelConfig":
        """Any offset: tolerance-driven corners and arcs, float heading, scaled lead-in."""
        return cls(offset_mm=offset_mm, corner_tolerance_mm=corner_tolerance_mm,
                   arc_step_deg=kw.pop('arc_step_deg', 15.0), arc_tolerance_mm=arc_tolerance_mm,
                   exact_arc_end=True, integer_heading=False, leadin_scale=None, **kw)

    @property
    def r(self) -> float:
        """Offset in path units (0.01 mm)."""
        return self.offset_mm * 100.0

    def turn_threshold_deg(self) -> float:
        """Smallest |turn| that gets a pivot arc."""
        if self.corner_tolerance_mm is None:
            return self.min_turn_deg
        if self.offset_mm <= 0:
            return 360.0
        lim = self.corner_tolerance_mm / self.offset_mm
        for i in range(1, len(CORNER_ERR)):          # first crossing, linear interpolation
            if CORNER_ERR[i] > lim:
                g0, g1 = CORNER_ERR[i - 1], CORNER_ERR[i]
                return 5.0 * (i - 1) + 5.0 * (lim - g0) / (g1 - g0) if g1 > g0 else 5.0 * i
        return 180.0

    def step_deg(self) -> float:
        step = self.arc_step_deg
        if self.arc_tolerance_mm is not None and self.offset_mm > 0:
            c = 1.0 - self.arc_tolerance_mm / self.offset_mm
            step = min(step, 180.0 if c <= -1 else 2.0 * math.degrees(math.acos(c)))
        return max(step, 1e-3)

    def leadin_factor(self) -> float:
        return max(1.0, self.offset_mm / 0.5) if self.leadin_scale is None else self.leadin_scale


def f32(x: float) -> float:
    return struct.unpack('>f', struct.pack('>f', x))[0]


def fround(x: float) -> int:
    """FUN_88c98d00: round half away from zero."""
    return -math.floor(-x + 0.5) if x <= 0 else math.floor(x + 0.5)


def _angle(ax, ay, bx, by) -> float:
    """FUN_88c90154: CCW angle from vector a to vector b, radians in [0, 2pi)."""
    # "+ 0.0" folds -0.0 to +0.0: the firmware's atan2 gives 0 for a zero vector (a
    # duplicate next point), where Python's atan2(0.0, -0.0) would give pi.
    t = math.atan2(ax * by - ay * bx + 0.0, ax * bx + ay * by + 0.0)
    return t + 2 * math.pi if t < 0 else t


@dataclass
class Pt:
    x: int
    y: int
    travel: bool            # True = move here tool-up, False = cut to here
    lead: bool = False      # part of the lead-in (lighter depth, never compensated)


@dataclass
class Move:
    kind: str               # 'travel' | 'cut' | 'arc'
    x: int
    y: int
    lead: bool = False
    src: int = -1           # index of the input point that produced it (-1 = lead-in)


@dataclass
class State:
    heading: float | None = None  # DAT_880140f4, degrees (None = -1, unknown)
    moves: list = field(default_factory=list)


def leadin_points(slot: int | None = 0, fixed: bool = False, scale: float = 1.0,
                  origin: tuple[int, int] | None = None) -> list[Pt]:
    """slot: index into the 48 slots (firmware uses rand() % N, N=48 by default).
    scale > 1 lengthens both legs, keeping the end point (y = -10.47 mm) and start X.
    origin: (start x, end y) in 0.01 mm, overriding the slot position."""
    if origin is not None:
        bx, by = origin[0] - LEADIN_SHAPE[0][1], origin[1] - LEADIN_SHAPE[2][2]
    elif fixed:
        bx, by = LEADIN_FIXED
    else:
        bx, by = LEADIN_SLOTS_X[slot % len(LEADIN_SLOTS_X)], 0
    if scale == 1.0:
        return [Pt(x + bx, y + by, trav, lead=True) for trav, x, y in LEADIN_SHAPE]
    (_, x0, _), (_, _, y1), (_, x2, y2) = LEADIN_SHAPE   # legs: (0,+200) then (+354,+353)
    leg1, leg2 = (0, y1 - LEADIN_SHAPE[0][2]), (x2 - x0, y2 - y1)
    end = (x0 + round(leg2[0] * scale) + bx, y2 + by)
    mid = (end[0] - round(leg2[0] * scale), end[1] - round(leg2[1] * scale))
    start = (mid[0], mid[1] - round(leg1[1] * scale))
    return [Pt(*start, True, lead=True), Pt(*mid, False, lead=True), Pt(*end, False, lead=True)]


def _decide(p: Pt, prev: Pt | None, nxt: Pt | None, heading, r, min_turn) -> tuple[bool, bool]:
    """FUN_88dbe604 -> (overshoot, arc)."""
    if r == 0 or p.lead or heading is None or prev is None:
        return False, False
    if nxt is None or nxt.travel:                       # last cut of a stroke
        return True, False
    if p.travel:                                        # stroke start: always swivel
        return True, True
    ix, iy = f32(p.x - prev.x), f32(p.y - prev.y)
    ox, oy = f32(nxt.x - p.x), f32(nxt.y - p.y)
    turn = _angle(ix, iy, ox, oy) * 180.0 / math.pi
    if turn > 180.0:
        turn -= 360.0
    return True, abs(turn) >= min_turn


def _arc(ax, ay, bx, by, cx, cy, r, step=1.0, exact_end=False) -> list[tuple[int, int]]:
    """FUN_88dbdda0: pivot around b (radius r) from dir(a->b) to dir(b->c).
    Firmware: step=1, exact_end=False (n = ceil(turn) 1-degree steps)."""
    v1x, v1y = f32(bx - ax), f32(by - ay)
    v2x, v2y = f32(cx - bx), f32(cy - by)
    turn = _angle(v1x, v1y, v2x, v2y) * 180.0 / math.pi
    cw = turn > 180.0
    span = abs(turn - 360.0) if cw else turn
    if exact_end:
        n = math.ceil(span / step - 1e-9)
        step = span / n if n else step
    else:
        n = math.ceil(span) if step == 1.0 else math.ceil(span / step)
    if n < 1:
        return []
    h0 = _angle(1.0, 0.0, v1x, v1y) * 180.0 / math.pi
    out = []
    for k in range(1, n + 1):
        a = h0 - k * step if cw else h0 + k * step
        if a >= 360.0:
            a -= 360.0
        if a < 0.0:
            a += 360.0
        rad = a * math.pi / 180.0
        out.append((fround(f32(r * math.cos(rad) + f32(bx))), fround(f32(r * math.sin(rad) + f32(by)))))
    return out


def compensate(points: list[Pt], r: float | None = None, leadin: bool | None = None,
               slot: int | None = None, st: State | None = None,
               cfg: SwivelConfig | None = None) -> State:
    """Compensate a flat point list. points[0] must be a travel point.
    cfg defaults to SwivelConfig.firmware(), and r/leadin/slot override cfg fields (r in 0.01 mm).
    Returns State with .moves (absolute targets):
      travel -> tool up, move to (x,y)   [firmware: per-axis moves]
      arc    -> tool down (if up), cut to (x,y)   [swivel pivot]
      cut    -> tool down (if up), cut to (x,y)
    """
    cfg = cfg or SwivelConfig.firmware()
    if r is not None:
        cfg = SwivelConfig(**{**cfg.__dict__, 'offset_mm': r / 100.0})
    r = cfg.r
    if r == int(r):
        r = int(r)                                      # firmware integer offset
    leadin = cfg.leadin if leadin is None else leadin
    slot = cfg.leadin_slot if slot is None else slot
    min_turn, step = cfg.turn_threshold_deg(), cfg.step_deg()
    st = st or State()
    pts = list(points)
    nlead = 0
    if leadin and r != 0 and pts:
        origin = None
        if cfg.leadin_x_mm is not None or cfg.leadin_end_y_mm is not None:
            origin = (round((cfg.leadin_x_mm if cfg.leadin_x_mm is not None else 1.0) * 100),
                      round((cfg.leadin_end_y_mm if cfg.leadin_end_y_mm is not None else -10.47) * 100))
        lead = leadin_points(slot, scale=cfg.leadin_factor(), origin=origin)
        pts, nlead = lead + pts, len(lead)
    for i, p in enumerate(pts):
        src = i - nlead
        prev = pts[i - 1] if i > 0 else None
        nxt = pts[i + 1] if i + 1 < len(pts) else None
        if prev is not None and prev.x == p.x and prev.y == p.y:
            continue                                    # zero-length: firmware skips entirely
        over, arc = _decide(p, prev, nxt, st.heading, r, min_turn)
        if p.travel:                                    # FUN_88dbe95c
            if over:
                h = st.heading * math.pi / 180.0
                tx = fround(f32(r * math.cos(h) + p.x))
                ty = fround(f32(r * math.sin(h) + p.y))
            else:
                tx, ty = p.x, p.y
            st.moves.append(Move('travel', tx, ty, p.lead, src))
            if arc:
                h = st.heading * math.pi / 180.0
                ax, ay = f32(p.x - f32(math.cos(h))), f32(p.y - f32(math.sin(h)))
                st.moves += [Move('arc', x, y, False, src + 1) for x, y in
                             _arc(ax, ay, p.x, p.y, nxt.x, nxt.y, r, step, cfg.exact_arc_end)]
        else:                                           # FUN_88dbeda0
            if over:                                    # FUN_88dbe1f8: extend by r along prev->p
                L = math.hypot(f32(prev.x - p.x), f32(prev.y - p.y)) + r
                tx = fround(f32((-(r * prev.x) + L * p.x) / (L - r)))
                ty = fround(f32((-(r * prev.y) + L * p.y) / (L - r)))
            else:
                tx, ty = p.x, p.y
            st.moves.append(Move('cut', tx, ty, p.lead, src))
            vx = f32(p.x - (prev.x if prev else 0))
            vy = f32(p.y - (prev.y if prev else 0))
            h = _angle(1.0, 0.0, vx, vy) * 180.0 / math.pi
            st.heading = fround(h) if cfg.integer_heading else h
            if arc:
                st.moves += [Move('arc', x, y, False, src) for x, y in
                             _arc(prev.x, prev.y, p.x, p.y, nxt.x, nxt.y, r, step, cfg.exact_arc_end)]
    return st


def simulate_tip(moves: list[Move], r: float, heading_deg: float = 90.0, step: float = 0.5):
    """Rigid-offset drag knife: the tip stays r behind the holder axis and is dragged along the
    blade line while the tool is down. Travel moves carry the blade orientation unchanged.
    Returns a list of strokes, each [(x, y, lead, src), ...] of tip positions while cutting (0.01 mm).
    heading_deg: blade direction before the first move (tip = axis - r*dir)."""
    if not moves:
        return []
    ux, uy = math.cos(math.radians(heading_deg)), math.sin(math.radians(heading_deg))
    ax, ay = float(moves[0].x), float(moves[0].y)
    tx, ty = ax - r * ux, ay - r * uy
    strokes, cur = [], None
    for m in moves:
        bx, by = float(m.x), float(m.y)
        if m.kind == 'travel':
            dx, dy = tx - ax, ty - ay
            ax, ay, tx, ty = bx, by, bx + dx, by + dy
            cur = None
            continue
        if cur is None:
            cur = [(tx, ty, m.lead, m.src)]
            strokes.append(cur)
        L = math.hypot(bx - ax, by - ay)
        n = max(1, int(L / step))
        for k in range(1, n + 1):
            x, y = ax + (bx - ax) * k / n, ay + (by - ay) * k / n
            dx, dy = tx - x, ty - y
            d = math.hypot(dx, dy) or 1e-12
            tx, ty = x + dx / d * r, y + dy / d * r
            cur.append((tx, ty, m.lead, m.src))
        ax, ay = bx, by
    return strokes


def mm_strokes_to_points(strokes) -> list[Pt]:
    """strokes: iterable of [(x_mm, y_mm), ...] polylines -> flat firmware point list."""
    out = []
    for s in strokes:
        s = list(s)
        if len(s) < 2:
            continue
        out.append(Pt(round(s[0][0] * 100), round(s[0][1] * 100), True))
        out += [Pt(round(x * 100), round(y * 100), False) for x, y in s[1:]]
    return out
