"""Full-fidelity SVG import -> VectorPath list (real beziers, svg-local mm space).

Curves are NOT flattened here: path C/S/Q/T/A commands, circles, ellipses and rounded
rects become anchors with cubic handles (ControlPoint.hin/hout). Everything downstream
(canvas, gcode, bbox) flattens at use time with its own tolerance.

Judgment calls (flagged for review):
- 1 SVG user-unit/px is treated as 1 mm (NOT 96dpi px->mm). Keeps art at
  sane cutter sizes. viewBox+width/height still rescale properly.
- fill is ignored for toolpaths (stroke-only cuts). Fill shapes with no
  stroke get a default black stroke so they still cut their outline.
- data-thickness="w0 w1 ..." distributes across the anchors of that element
  (by index), and a single value = uniform.
- Not imported: <text>, <image>, <use> (and anything inside <defs>, <symbol>,
  <clipPath>, <mask>, <marker>, <pattern>).
"""
from __future__ import annotations
import math
import re
import xml.etree.ElementTree as ET
from .doc import VectorPath, ControlPoint, normalize_color, new_id, DEFAULT_THICKNESS
from .shapes import ellipse_anchors

NAMED = {
    "black": "#000000", "white": "#FFFFFF", "red": "#FF0000", "green": "#00FF00",
    "blue": "#0000FF", "yellow": "#FFFF00", "magenta": "#FF00FF", "cyan": "#00FFFF",
    "gray": "#808080", "grey": "#808080", "orange": "#FFA500", "purple": "#800080",
    "lime": "#00FF00", "maroon": "#800000", "navy": "#000080", "teal": "#008080",
}

# elements whose subtree is never rendered directly
NON_RENDERED = {"defs", "symbol", "clipPath", "mask", "marker", "pattern"}


def parse_color(s, default="#000000"):
    if s is None:
        return default
    s = s.strip().lower()
    if s in ("none", "transparent"):
        return None
    if s in NAMED:
        return NAMED[s]
    if s.startswith("#"):
        try:
            return normalize_color(s)
        except Exception:
            return default
    m = re.match(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", s)
    if m:
        r, g, b = (max(0, min(255, int(m.group(i)))) for i in (1, 2, 3))
        return "#%02X%02X%02X" % (r, g, b)
    return default


def parse_length(s, default=1.0):
    if s is None:
        return default
    s = str(s).strip().lower()
    m = re.match(r"^([+-]?[\d.]+(?:[eE][+-]?\d+)?)\s*(px|mm|cm|in|pt|pc|q)?$", s)
    if not m:
        try:
            return float(s)
        except ValueError:
            return default
    v, u = float(m.group(1)), (m.group(2) or "px")
    # 1 unit treated as 1mm (see module docstring), and absolute units convert to mm.
    if u == "mm" or u == "px" or u == "":
        return v
    if u == "cm":
        return v * 10.0
    if u == "in":
        return v * 25.4
    if u == "pt":
        return v * 25.4 / 72.0
    if u == "pc":
        return v * 25.4 / 6.0
    if u == "q":
        return v * 0.25
    return v


def mat_mul(m1, m2):
    """Compose affine (a,b,c,d,e,f) matrices: result applies m2 first, then m1."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def parse_transform(s):
    """Returns 3x3-ish (a,b,c,d,e,f) meaning x'=a*x+c*y+e, y'=b*x+d*y+f."""
    if not s:
        return (1, 0, 0, 1, 0, 0)
    M = (1, 0, 0, 1, 0, 0)
    mul = mat_mul

    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", s):
        nums = [float(x) for x in re.split(r"[,\s]+", args.strip()) if x != ""]
        if name == "translate":
            tx = nums[0] if len(nums) > 0 else 0
            ty = nums[1] if len(nums) > 1 else 0
            M = mul(M, (1, 0, 0, 1, tx, ty))
        elif name == "scale":
            sx = nums[0] if nums else 1
            sy = nums[1] if len(nums) > 1 else sx
            M = mul(M, (sx, 0, 0, sy, 0, 0))
        elif name == "rotate":
            a = math.radians(nums[0]) if nums else 0
            ca, sa = math.cos(a), math.sin(a)
            if len(nums) == 3:
                cx, cy = nums[1], nums[2]
                M = mul(M, (1, 0, 0, 1, cx, cy))
                M = mul(M, (ca, sa, -sa, ca, 0, 0))
                M = mul(M, (1, 0, 0, 1, -cx, -cy))
            else:
                M = mul(M, (ca, sa, -sa, ca, 0, 0))
        elif name == "matrix" and len(nums) == 6:
            M = mul(M, tuple(nums))
        elif name == "skewx" and nums:
            M = mul(M, (1, 0, math.tan(math.radians(nums[0])), 1, 0, 0))
        elif name == "skewy" and nums:
            M = mul(M, (1, math.tan(math.radians(nums[0])), 0, 1, 0, 0))
    return M


def xform_pt(M, x, y):
    a, b, c, d, e, f = M
    return (a * x + c * y + e, b * x + d * y + f)


def parse_style(style_str):
    out = {}
    if not style_str:
        return out
    for part in style_str.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------------------
# path data -> anchors with bezier handles
#
# An anchor is a list [x, y, hin, hout], where hin/hout are (x, y) tuples or None.
#   hout = first control point of the segment starting at this anchor
#   hin  = second control point of the segment ending at this anchor
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_SEP_RE = re.compile(r"[\s,]*")
_CMD_CHARS = "MmLlHhVvCcSsQqTtAaZz"
_ARGC = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}
_REL_EPS = 1e-9


def _coincide(x0, y0, x1, y1):
    """Same point within 1e-9 relative (to max(1, |coordinate|))."""
    return (abs(x0 - x1) <= _REL_EPS * max(1.0, abs(x0), abs(x1)) and
            abs(y0 - y1) <= _REL_EPS * max(1.0, abs(y0), abs(y1)))


def arc_to_cubics(x0, y0, rx, ry, phi_deg, large, sweep, x1, y1):
    """SVG endpoint arc -> list of cubic pieces [(c1, c2, p)] (each <= 90 deg).

    Returns [] for a zero-length arc (endpoints coincide: the arc is omitted) and None when a
    radius is zero (the arc degenerates to a straight line, and the caller draws a line).
    """
    if x0 == x1 and y0 == y1:
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        return None
    phi = math.radians(phi_deg)
    cp, sp = math.cos(phi), math.sin(phi)
    # F.6.5 endpoint -> center parameterisation
    dx, dy = (x0 - x1) / 2, (y0 - y1) / 2
    x1p = cp * dx + sp * dy
    y1p = -sp * dx + cp * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:  # radii too small: scale up uniformly
        s = math.sqrt(lam)
        rx *= s
        ry *= s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    f = math.sqrt(max(0.0, num / den)) if den else 0.0
    if bool(large) == bool(sweep):
        f = -f
    cxp = f * rx * y1p / ry
    cyp = -f * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x0 + x1) / 2
    cy = sp * cxp + cp * cyp + (y0 + y1) / 2
    t1 = math.atan2((y1p - cyp) / ry, (x1p - cxp) / rx)
    t2 = math.atan2((-y1p - cyp) / ry, (-x1p - cxp) / rx)
    dt = t2 - t1
    if sweep and dt < 0:
        dt += 2 * math.pi
    elif not sweep and dt > 0:
        dt -= 2 * math.pi
    n = max(1, int(math.ceil(abs(dt) / (math.pi / 2) - 1e-9)))
    d = dt / n
    k = 4.0 / 3.0 * math.tan(d / 4.0)

    def mp(u, v):
        return (cx + cp * rx * u - sp * ry * v, cy + sp * rx * u + cp * ry * v)

    out = []
    for i in range(n):
        a = t1 + i * d
        b = a + d
        ca, sa, cb, sb = math.cos(a), math.sin(a), math.cos(b), math.sin(b)
        c1 = mp(ca - k * sa, sa + k * ca)
        c2 = mp(cb + k * sb, sb - k * cb)
        p = (x1, y1) if i == n - 1 else mp(cb, sb)
        out.append((c1, c2, p))
    return out


class _Builder:
    """Accumulates subpaths of anchors. Handles dedup of zero-length lines and Z merging."""

    def __init__(self):
        self.subs = []     # [(anchors, closed)]
        self.cur = []      # anchors of the open subpath being built
        self.start = None  # start point of the current subpath (kept after Z)
        self.pos = (0.0, 0.0)

    # -- subpath management --
    def _flush_open(self):
        if len(self.cur) >= 2:
            self.subs.append((self.cur, False))
        self.cur = []

    def move_to(self, x, y):
        self._flush_open()
        self.pos = (x, y)
        self.start = (x, y)
        self.cur = [[x, y, None, None]]

    def _ensure(self):
        """Start a subpath at the previous start point after Z (SVG spec)."""
        if not self.cur and self.start is not None:
            self.cur = [[self.start[0], self.start[1], None, None]]
            self.pos = self.start
        return bool(self.cur)

    def close(self):
        cur = self.cur
        if len(cur) >= 2:
            first, last = cur[0], cur[-1]
            if len(cur) > 2 and _coincide(first[0], first[1], last[0], last[1]):
                first[2] = last[2]  # closing curve's second control moves to the first anchor
                cur.pop()
            self.subs.append((cur, True))
        self.cur = []
        if self.start is not None:
            self.pos = self.start

    def finish(self):
        self._flush_open()
        out = []
        for anchors, closed in self.subs:
            for a in anchors:  # a handle sitting on its anchor is no handle
                if a[2] is not None and _coincide(a[2][0], a[2][1], a[0], a[1]):
                    a[2] = None
                if a[3] is not None and _coincide(a[3][0], a[3][1], a[0], a[1]):
                    a[3] = None
            out.append((anchors, closed))
        return out

    # -- segments --
    def line_to(self, x, y):
        if not self._ensure():
            return
        if _coincide(self.pos[0], self.pos[1], x, y):
            return  # zero-length straight segment
        self.cur.append([x, y, None, None])
        self.pos = (x, y)

    def cubic_to(self, c1, c2, p):
        if not self._ensure():
            return
        if (_coincide(self.pos[0], self.pos[1], p[0], p[1]) and
                _coincide(self.pos[0], self.pos[1], c1[0], c1[1]) and
                _coincide(self.pos[0], self.pos[1], c2[0], c2[1])):
            return
        self.cur[-1][3] = (c1[0], c1[1])
        self.cur.append([p[0], p[1], (c2[0], c2[1]), None])
        self.pos = (p[0], p[1])

    def quad_to(self, q, p):
        x0, y0 = self.pos
        c1 = (x0 + 2.0 / 3.0 * (q[0] - x0), y0 + 2.0 / 3.0 * (q[1] - y0))
        c2 = (p[0] + 2.0 / 3.0 * (q[0] - p[0]), p[1] + 2.0 / 3.0 * (q[1] - p[1]))
        self.cubic_to(c1, c2, p)

    def arc_to(self, rx, ry, phi, large, sweep, x, y):
        if not self._ensure():
            return
        pieces = arc_to_cubics(self.pos[0], self.pos[1], rx, ry, phi, large, sweep, x, y)
        if pieces is None:
            self.line_to(x, y)
            return
        for c1, c2, p in pieces:
            self.cubic_to(c1, c2, p)


def _read_args(d, i, cmd):
    """Read the arguments of command `cmd` at d[i:]. Returns (values, new_i) or None."""
    n = _ARGC[cmd.upper()]
    vals = []
    for k in range(n):
        i = _SEP_RE.match(d, i).end()
        if cmd in "Aa" and k in (3, 4):  # arc flags: single char 0/1, may be unseparated
            if i < len(d) and d[i] in "01":
                vals.append(int(d[i]))
                i += 1
                continue
            return None
        m = _NUM_RE.match(d, i)
        if not m:
            return None
        vals.append(float(m.group(0)))
        i = m.end()
    return vals, i


def parse_path_d(d, tol=0.1):
    """Parse path data -> list of (anchors, closed), anchors = [[x, y, hin, hout], ...].

    Curves are kept as cubic handles (Q/T elevated exactly, arcs split into <= 90 deg cubics).
    `tol` is accepted for backward compatibility and ignored. Parsing stops at the first
    malformed token (SVG error handling), keeping what was built so far.
    """
    d = d or ""
    b = _Builder()
    i = 0
    cmd = None
    n = len(d)
    refl_c = None  # second control point of previous C/S segment (abs), else None
    refl_q = None  # control point of previous Q/T segment (abs), else None
    first = True
    while True:
        i = _SEP_RE.match(d, i).end()
        if i >= n:
            break
        if d[i] in _CMD_CHARS:
            cmd = d[i]
            i += 1
            if first and cmd not in "Mm":
                break
            first = False
            if cmd in "Zz":
                b.close()
                refl_c = refl_q = None
                continue
        elif cmd is None or cmd in "Zz":
            break  # numbers with no valid command to repeat
        r = _read_args(d, i, cmd)
        if r is None:
            break
        v, i = r
        rel = cmd.islower()
        c = cmd.upper()
        px, py = b.pos
        ox, oy = (px, py) if rel else (0.0, 0.0)
        new_c = new_q = None
        if c == "M":
            b.move_to(v[0] + ox, v[1] + oy)
            cmd = "l" if rel else "L"  # implicit lineto
        elif c == "L":
            b.line_to(v[0] + ox, v[1] + oy)
        elif c == "H":
            b.line_to(v[0] + ox, py)
        elif c == "V":
            b.line_to(px, v[0] + oy)
        elif c == "C":
            c1 = (v[0] + ox, v[1] + oy)
            c2 = (v[2] + ox, v[3] + oy)
            b.cubic_to(c1, c2, (v[4] + ox, v[5] + oy))
            new_c = c2
        elif c == "S":
            c1 = (2 * px - refl_c[0], 2 * py - refl_c[1]) if refl_c else (px, py)
            c2 = (v[0] + ox, v[1] + oy)
            b.cubic_to(c1, c2, (v[2] + ox, v[3] + oy))
            new_c = c2
        elif c == "Q":
            q = (v[0] + ox, v[1] + oy)
            b.quad_to(q, (v[2] + ox, v[3] + oy))
            new_q = q
        elif c == "T":
            q = (2 * px - refl_q[0], 2 * py - refl_q[1]) if refl_q else (px, py)
            b.quad_to(q, (v[0] + ox, v[1] + oy))
            new_q = q
        elif c == "A":
            b.arc_to(v[0], v[1], v[2], v[3], v[4], v[5] + ox, v[6] + oy)
        refl_c, refl_q = new_c, new_q
    return b.finish()


def _localname(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _rect_subpath(x, y, w, h, rx, ry):
    """Rect (optionally rounded) -> (anchors, closed). rx/ry already resolved/clamped."""
    if rx <= 0 or ry <= 0:
        return ([[x, y, None, None], [x + w, y, None, None],
                 [x + w, y + h, None, None], [x, y + h, None, None]], True)
    b = _Builder()
    b.move_to(x + rx, y)
    b.line_to(x + w - rx, y)
    b.arc_to(rx, ry, 0, 0, 1, x + w, y + ry)
    b.line_to(x + w, y + h - ry)
    b.arc_to(rx, ry, 0, 0, 1, x + w - rx, y + h)
    b.line_to(x + rx, y + h)
    b.arc_to(rx, ry, 0, 0, 1, x, y + h - ry)
    b.line_to(x, y + ry)
    b.arc_to(rx, ry, 0, 0, 1, x + rx, y)
    b.close()
    return b.finish()[0]


def import_svg(path_or_text, tol=0.1, from_file=True):
    """Parse SVG -> list[VectorPath] (bezier anchors + handles, svg-local mm). `tol` unused."""
    if from_file:
        with open(path_or_text, encoding="utf-8", errors="replace") as f:
            text = f.read()
    else:
        text = path_or_text
    root = ET.fromstring(text)
    # viewport scaling
    w = parse_length(root.get("width"), default=None) if root.get("width") else None
    h = parse_length(root.get("height"), default=None) if root.get("height") else None
    vb = None
    if root.get("viewBox"):
        try:
            vb = [float(x) for x in root.get("viewBox").replace(",", " ").split()]
        except ValueError:
            vb = None
    base_scale = (1.0, 1.0)
    base_off = (0.0, 0.0)
    if vb and len(vb) == 4 and w and h:
        sx = w / vb[2] if vb[2] else 1.0
        sy = h / vb[3] if vb[3] else 1.0
        base_scale = (sx, sy)
        base_off = (-vb[0] * sx, -vb[1] * sy)
    base = (base_scale[0], 0.0, 0.0, base_scale[1], base_off[0], base_off[1])

    out_paths = []

    def walk(el, style, M):
        name = _localname(el.tag)
        if name in NON_RENDERED:
            return
        # inherited style
        st = dict(style)
        for k in ("stroke", "stroke-width", "stroke-dasharray", "fill", "opacity"):
            if el.get(k) is not None:
                st[k] = el.get(k)
        st.update(parse_style(el.get("style", "")))
        M2 = M
        if el.get("transform"):
            M2 = mat_mul(M, parse_transform(el.get("transform")))
        if name == "g" or name == "svg":
            for ch in el:
                walk(ch, st, M2)
            return
        if name in ("line", "polyline", "polygon", "rect", "circle", "ellipse", "path"):
            emit_shape(el, name, st, M2)

    def emit_shape(el, name, st, M):
        stroke = parse_color(st.get("stroke"), default=None)
        fill = parse_color(st.get("fill"), default=None)
        if stroke is None:
            # outline of filled shape still cuts
            stroke = parse_color("black") if (fill is not None) else parse_color("black")
            if st.get("stroke", "").strip().lower() == "none" and fill is None:
                return  # truly invisible
        width = parse_length(st.get("stroke-width"), default=DEFAULT_THICKNESS) if st.get("stroke-width") else DEFAULT_THICKNESS
        dash_on, dash_off, dash_on_flag = parse_dash(st.get("stroke-dasharray"), width)
        thick_spec = el.get("data-thickness", "").strip()
        thick_vals = []
        if thick_spec:
            try:
                thick_vals = [float(x) for x in re.split(r"[,\s]+", thick_spec) if x != ""]
            except ValueError:
                thick_vals = []

        raw_subs = []  # (anchors [[x, y, hin, hout]], closed)
        if name == "path":
            raw_subs = parse_path_d(el.get("d", ""))
        elif name == "line":
            x1 = parse_length(el.get("x1", "0"), 0)
            y1 = parse_length(el.get("y1", "0"), 0)
            x2 = parse_length(el.get("x2", "0"), 0)
            y2 = parse_length(el.get("y2", "0"), 0)
            raw_subs = [([[x1, y1, None, None], [x2, y2, None, None]], False)]
        elif name in ("polyline", "polygon"):
            raw = (el.get("points") or "").replace(",", " ").split()
            nums = [float(x) for x in raw]
            pts = [[nums[k], nums[k + 1], None, None] for k in range(0, len(nums) - 1, 2)]
            if len(pts) >= 2:
                raw_subs = [(pts, name == "polygon")]
        elif name == "rect":
            x = parse_length(el.get("x", "0"), 0)
            y = parse_length(el.get("y", "0"), 0)
            w_ = parse_length(el.get("width", "0"), 0)
            h_ = parse_length(el.get("height", "0"), 0)
            if w_ > 0 and h_ > 0:
                rx = parse_length(el.get("rx"), None) if el.get("rx") else None
                ry = parse_length(el.get("ry"), None) if el.get("ry") else None
                if rx is not None and rx < 0:
                    rx = None
                if ry is not None and ry < 0:
                    ry = None
                if rx is None:
                    rx = ry if ry is not None else 0.0
                if ry is None:
                    ry = rx
                rx, ry = min(rx, w_ / 2.0), min(ry, h_ / 2.0)
                raw_subs = [_rect_subpath(x, y, w_, h_, rx, ry)]
        elif name in ("circle", "ellipse"):
            cx = parse_length(el.get("cx", "0"), 0)
            cy = parse_length(el.get("cy", "0"), 0)
            if name == "circle":
                rx = ry = parse_length(el.get("r", "0"), 0)
            else:
                rx = parse_length(el.get("rx", "0"), 0)
                ry = parse_length(el.get("ry", "0"), 0)
            if rx > 0 and ry > 0:
                raw_subs = [([[ax, ay, hi, ho] for (ax, ay, hi, ho) in ellipse_anchors(cx, cy, rx, ry)], True)]

        # viewBox base scale/offset composed with the element/group transform: one affine map,
        # exact for anchors and bezier handles alike
        T = mat_mul(M, base)

        def tp(p):
            return None if p is None else xform_pt(T, p[0], p[1])

        for anchors, closed in raw_subs:
            n = len(anchors)
            if n < 2:
                continue
            # thickness distribution (by anchor index)
            if len(thick_vals) >= 2:
                th = [thick_vals[min(len(thick_vals) - 1, int(i * len(thick_vals) / n))] for i in range(n)]
            elif len(thick_vals) == 1:
                th = [thick_vals[0]] * n
            else:
                th = [width] * n
            cpts = []
            for (ax, ay, hi, ho), t in zip(anchors, th):
                x, y = xform_pt(T, ax, ay)
                cpts.append(ControlPoint(x=x, y=y, thickness=t, hin=tp(hi), hout=tp(ho)))
            vp = VectorPath(id=new_id("path"), svg_id="", color=stroke, points=cpts,
                            closed=closed, dash_enabled=dash_on_flag,
                            dash_on_mm=dash_on, dash_off_mm=dash_off)
            out_paths.append(vp)

    walk(root, {}, (1, 0, 0, 1, 0, 0))
    return out_paths


def parse_dash(s, width):
    if not s or s.strip().lower() == "none":
        return 1.0, 1.0, False
    parts = [p for p in re.split(r"[,\s]+", s.strip()) if p != ""]
    try:
        vals = [parse_length(p, default=1.0) for p in parts]
    except ValueError:
        return 1.0, 1.0, False
    if not vals:
        return 1.0, 1.0, False
    if len(vals) % 2 == 1:
        vals = vals + vals
    return vals[0], vals[1], True
