"""Core document model: cut area, color-keyed params, vector paths, selection."""
from __future__ import annotations
import hashlib
import json
import math
import os
import uuid
import zipfile
from dataclasses import dataclass, field, asdict, replace
from .blade import BladeSettings
from .machine import ScanCal
from .mat import MatSkew
from .shapes import flatten_cubic_t, corner_fillet, split_cubic, seg_point

# Project file (.sncproj): a zip holding the project JSON (Document.to_dict, unchanged) plus the
# binary data the project embeds (scan images) under blobs/. A plain JSON file (the old .json
# projects) loads as if it were the zip's project.json.
PROJECT_EXT = ".sncproj"
PROJECT_JSON = "project.json"
BLOB_DIR = "blobs/"


def read_project(path):
    """(project dict, {blob name: bytes}) from a .sncproj zip or a plain JSON project file."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            d = json.loads(z.read(PROJECT_JSON).decode("utf-8"))
            blobs = {n[len(BLOB_DIR):]: z.read(n) for n in z.namelist()
                     if n.startswith(BLOB_DIR) and len(n) > len(BLOB_DIR)}
        return d, blobs
    with open(path, encoding="utf-8") as f:
        return json.load(f), {}


AREA_DEFAULT_MM = 304.8  # legacy project field (the canvas now uses core.machine geometry)
STEPS_X_DEFAULT = 70.8
STEPS_Y_DEFAULT = 70.4

DEFAULT_CURVE = [(0.0, -9), (2.0, 9)]  # thickness_mm -> pressure -9..9
DEFAULT_DASH_ON_MM = 1.0
DEFAULT_DASH_OFF_MM = 1.0
DEFAULT_TAB_GAP_MM = 1.0
TAB_GAP_MIN_MM, TAB_GAP_MAX_MM = 0.1, 100.0
DEFAULT_THICKNESS = 1.0
# Bend focus (Document.bend_weights): pressing beyond these fractions of a segment moves only
# the nearer handle.
BEND_FOCUS_LO, BEND_FOCUS_HI = 0.25, 0.75


def new_id(prefix="id"):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@dataclass
class ControlPoint:
    x: float  # mm, svg-local coords
    y: float
    thickness: float = DEFAULT_THICKNESS
    # bezier handles, svg-local coords (None = no handle)
    hin: object = None   # control point of the segment ENDING at this anchor
    hout: object = None  # control point of the segment STARTING at this anchor
    # corner radius, mm on the mat (like thickness, not scaled with the object): the corner is
    # drawn and cut as an arc (Document._seg_poly_u), and the anchor itself stays where it is
    radius: float = 0.0


@dataclass
class Tab:
    """A gap in a line where the cut is interrupted, so the cut part stays attached to the
    material. The gap is centered on the tab and measured along the line, in mm on the mat (like
    dash lengths, not scaled with the object). It can reach into neighbouring segments."""
    id: str
    seg: int        # anchor segment of the path that the tab is attached to
    pos: float      # place on that segment as a fraction of its length (0..1)
    gap_mm: float = DEFAULT_TAB_GAP_MM


@dataclass
class VectorPath:
    id: str
    svg_id: str
    color: str  # exact hex "#RRGGBB"
    points: list = field(default_factory=list)  # list[ControlPoint]
    closed: bool = False
    # per-path overrides (dash/thickness live on geometry, not color key)
    dash_enabled: bool = False
    dash_on_mm: float = DEFAULT_DASH_ON_MM
    dash_off_mm: float = DEFAULT_DASH_OFF_MM
    generated: bool = False  # produced by an image outline: regenerated, not hand-edited
    tabs: list = field(default_factory=list)  # list[Tab]

    def __post_init__(self):
        self.color = normalize_color(self.color)

    def tab(self, tab_id):
        return next((t for t in self.tabs if t.id == tab_id), None)

    def segment_count(self):
        n = len(self.points)
        if n < 2:
            return 0
        return n if self.closed else n - 1

    def segment_points(self, idx):
        n = len(self.points)
        a = self.points[idx % n]
        b = self.points[(idx + 1) % n]
        return a, b


@dataclass
class ColorParams:
    color: str
    speed: object = None       # None = unset -> machine setting, else 0..9 feed
    pressure: object = None    # None = unset, else -9..9 int
    use_thickness_map: bool = False  # toggled OFF by default
    curve: list = field(default_factory=lambda: [list(p) for p in DEFAULT_CURVE])
    blade: bool = True         # swivel-blade tool: gets blade compensation (False = pen etc.)
    tool: str = "cut"          # GROUP_TOOLS: what the group is done with ("skip" = left out of the job)
    keep: bool = False         # created by the user: listed in the cut order even while empty
    # dash defaults per color are informational only. The actual dash is stored on paths.
    # They are kept so that a new path of this color could inherit them (v1: paths default to solid).

    def __post_init__(self):
        self.color = normalize_color(self.color)


GROUP_TOOLS = ("cut", "draw", "emboss", "skip")
SKIP_TOOL = "skip"         # a group with this tool is not cut, drawn or embossed

# Colors handed out for new (empty) color groups, in order. The first one not in use wins.
NEW_GROUP_COLORS = ("#E53935", "#1E88E5", "#43A047", "#FB8C00", "#8E24AA", "#00ACC1",
                    "#FDD835", "#6D4C41", "#D81B60", "#3949AB", "#7CB342", "#546E7A")


def normalize_color(c):
    c = (c or "#000000").strip().upper()
    if len(c) == 4 and c.startswith("#"):  # #RGB -> #RRGGBB
        c = "#" + "".join(ch * 2 for ch in c[1:])
    if not (len(c) == 7 and c.startswith("#")):
        return "#000000"
    try:
        int(c[1:], 16)
    except ValueError:
        return "#000000"
    return c


SELECT_MODES = ("new", "add", "subtract", "intersect")


def combine_selection(snap, picked, mode):
    """Combine box-picked sets with a pre-drag snapshot.

    snap/picked: 4-tuples (paths, segments, points, svgs) of sets.
    new: replace, add: union, subtract: remove picked, intersect: keep overlap
    (per category, so boxing points never wipes path selections).
    """
    if mode == "add":
        return tuple(set(s) | set(p) for s, p in zip(snap, picked))
    if mode == "subtract":
        return tuple(set(s) - set(p) for s, p in zip(snap, picked))
    if mode == "intersect":
        return tuple(set(s) & set(p) for s, p in zip(snap, picked))
    return tuple(set(p) for p in picked)  # new


TEXT_ALIGNS = ("left", "center", "right")


@dataclass
class TextStyle:
    """How a text object is set. Sizes are mm, and faux values are fractions of the font size:
    faux_bold grows the outline outward by that much, faux_italic shifts the top of the em
    sideways by that much relative to the baseline. The faux values are kept while their
    toggles are off. font_style is an upright style of the family ("Regular", "Bold", ...),
    and font_italic picks its real italic counterpart (ui.text_ops resolves both)."""
    font_family: str = ""
    font_style: str = "Regular"
    font_italic: bool = False
    size_mm: float = 10.0
    align: str = "left"
    faux_bold_on: bool = False
    faux_bold: float = 0.03
    faux_italic_on: bool = False
    faux_italic: float = 0.2
    thickness_mm: float = DEFAULT_THICKNESS   # line thickness of every point of the letters

    def to_dict(self):
        return {"family": self.font_family, "style": self.font_style, "italic": self.font_italic,
                "size": self.size_mm, "align": self.align, "fb_on": self.faux_bold_on,
                "fb": self.faux_bold, "fi_on": self.faux_italic_on, "fi": self.faux_italic,
                "th": self.thickness_mm}

    @staticmethod
    def from_dict(d):
        d = d or {}
        return TextStyle(font_family=str(d.get("family", "")), font_style=str(d.get("style", "Regular")),
                         font_italic=bool(d.get("italic", False)),
                         size_mm=float(d.get("size", 10.0)),
                         align=d.get("align") if d.get("align") in TEXT_ALIGNS else "left",
                         faux_bold_on=bool(d.get("fb_on", False)), faux_bold=float(d.get("fb", 0.03)),
                         faux_italic_on=bool(d.get("fi_on", False)),
                         faux_italic=float(d.get("fi", 0.2)),
                         thickness_mm=float(d.get("th", DEFAULT_THICKNESS)))


@dataclass
class SvgInstance:
    id: str
    label: str  # file name
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    path_ids: list = field(default_factory=list)
    scale: float = 1.0       # uniform scale about svg-local origin
    rotation: float = 0.0    # degrees clockwise on the mat, about the svg-local origin
    flip: bool = False       # mirrored left-right in the local frame (before the rotation)
    visible: bool = True     # Hide toggle
    ghost: bool = False      # Ghost toggle: 1px dotted regardless of thickness
    kind: str = "svg"        # "svg" | "drawing" | "image" | "pdf" | "text"
    source_path: str = ""    # absolute path for image/pdf
    dpi: float = 300.0       # image: physical size, pdf: render resolution
    px_w: int = 0            # raster size at `dpi` (pdf: rendered size)
    px_h: int = 0
    pdf_page: int = 0        # 0-based
    outline_enabled: bool = False
    outline_margin_mm: float = 0.0   # + grows outward, - shrinks
    color_key_mode: str = "none"     # "none" | "auto" (corner pixel) | "custom"
    color_key: str = "#FFFFFF"
    color_key_tol: int = 32          # 0..255, max abs channel difference
    outline_holes: bool = False
    outline_color: str = "#000000"   # color of generated paths (image outlines, text)
    text: str = ""                   # kind "text": the text, lines split by "\n"
    text_style: object = None        # kind "text": TextStyle


class Xf:
    """An object's local -> mat (abs mm) mapping: abs = (a*x + c*y + ox, b*x + d*y + oy).
    Its linear part is scale * rotation * flip, so it never shears."""
    __slots__ = ("a", "b", "c", "d", "ox", "oy")

    def __init__(self, a=1.0, b=0.0, c=0.0, d=1.0, ox=0.0, oy=0.0):
        self.a, self.b, self.c, self.d, self.ox, self.oy = a, b, c, d, ox, oy

    @staticmethod
    def of(svg):
        """The mapping of an SvgInstance (identity for None)."""
        if svg is None:
            return Xf()
        co, si = _cos_sin(svg.rotation)
        sc, fx = svg.scale, (-1.0 if svg.flip else 1.0)
        return Xf(sc * co * fx, sc * si * fx, -sc * si, sc * co, svg.offset_x_mm, svg.offset_y_mm)

    def to_abs(self, x, y):
        return (self.a * x + self.c * y + self.ox, self.b * x + self.d * y + self.oy)

    def vec_local(self, dx, dy):
        """An abs-mm displacement in local units."""
        det = self.a * self.d - self.b * self.c
        return ((self.d * dx - self.c * dy) / det, (self.a * dy - self.b * dx) / det)

    def to_local(self, x, y):
        return self.vec_local(x - self.ox, y - self.oy)


def _cos_sin(deg):
    """cos/sin of an angle in degrees, exact at quarter turns (90° steps stay free of float noise)."""
    q = deg / 90.0
    if abs(q - round(q)) < 1e-12:
        return ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[int(round(q)) % 4]
    r = math.radians(deg)
    return math.cos(r), math.sin(r)


def norm_angle(deg):
    """An angle in degrees as 0 <= a < 360 (tiny float noise snapped away)."""
    a = round(float(deg), 9) % 360.0
    return 0.0 if abs(a) < 1e-9 or abs(a - 360.0) < 1e-9 else a


def image_size_mm(svg):
    """(w_mm, h_mm) of an image/pdf object's raster in its local frame (before scale)."""
    dpi = svg.dpi if svg.dpi > 0 else 300.0
    return (svg.px_w / dpi * 25.4, svg.px_h / dpi * 25.4)


def cubic_point(p0, c1, c2, p3, t):
    """Point on a cubic bezier at parameter t."""
    u = 1.0 - t
    w0, w1, w2, w3 = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (w0 * p0[0] + w1 * c1[0] + w2 * c2[0] + w3 * p3[0],
            w0 * p0[1] + w1 * c1[1] + w2 * c2[1] + w3 * p3[1])


# -- path-splitting helpers (shared by break_apart / delete_selection) --
def _copy_pts(p, idxs, closed):
    """Copies of p.points[idxs]. An open piece drops the dangling hin/hout at its ends."""
    pts = [replace(p.points[i % len(p.points)]) for i in idxs]
    if not closed and pts:
        pts[0].hin = None
        pts[-1].hout = None
    return pts


def _split_runs(p, sel):
    """Group a path's segments by membership in `sel` -> [(selected, closed, anchor_idxs)].

    A uniform path (all or none selected) is one entry covering every anchor with p.closed.
    Otherwise runs are maximal groups of consecutive segments (open pieces), and a closed path
    is rotated to start on a run boundary so no run wraps."""
    m, n = p.segment_count(), len(p.points)
    flags = [j in sel for j in range(m)]
    if not flags or all(f == flags[0] for f in flags):
        return [(bool(flags and flags[0]), p.closed, list(range(n)))]
    start = 0
    if p.closed:
        start = next(j for j in range(m) if flags[j] != flags[j - 1])
    groups = []
    for k in range(m):
        j = (start + k) % m
        if groups and groups[-1][0] == flags[j]:
            groups[-1][1].append(j)
        else:
            groups.append((flags[j], [j]))
    return [(f, False, [segs[0]] + [(j + 1) % n for j in segs]) for f, segs in groups]


def _piece_tabs(p, idxs, closed, rm=()):
    """Copies of p's tabs for a piece made of the anchors p.points[idxs] (anchors in rm are
    removed). A tab is kept when its segment survives unchanged, and it is moved to that
    segment's index in the piece. Tabs on joined or removed segments are dropped."""
    if not p.tabs:
        return []
    n = len(p.points)
    kept = [i for i in idxs if i not in rm]
    m = len(kept) if closed else len(kept) - 1
    where = {}
    for k in range(max(m, 0)):
        a, b = kept[k], kept[(k + 1) % len(kept)]
        if b == (a + 1) % n:
            where[a] = k
    return [replace(t, seg=where[t.seg]) for t in p.tabs if t.seg in where]


def subtract_spans(spans, gaps):
    """Parts of the sorted intervals `spans` outside the sorted, merged intervals `gaps`."""
    out = []
    for a, b in spans:
        for g0, g1 in gaps:
            if g1 <= a or g0 >= b:
                continue
            if g0 - a > 1e-9:
                out.append((a, g0))
            a = max(a, g1)
            if a >= b:
                break
        if b - a > 1e-9:
            out.append((a, b))
    return out


def _remove_anchors(idxs, pts, closed, rm):
    """Drop anchors whose original index is in rm. Neighbours get a straight joining segment."""
    k, out = len(idxs), []
    for pos, (i, cp) in enumerate(zip(idxs, pts)):
        if i in rm:
            continue
        if (closed or pos > 0) and idxs[(pos - 1) % k] in rm:
            cp.hin = None
        if (closed or pos < k - 1) and idxs[(pos + 1) % k] in rm:
            cp.hout = None
        out.append(cp)
    return out


@dataclass
class Selection:
    path_ids: set = field(default_factory=set)
    segments: set = field(default_factory=set)  # (path_id, seg_idx)
    points: set = field(default_factory=set)    # (path_id, pt_idx)
    svg_ids: set = field(default_factory=set)   # entire-SVG selection
    tabs: set = field(default_factory=set)      # (path_id, tab_id)

    def clear(self):
        self.path_ids.clear()
        self.segments.clear()
        self.points.clear()
        self.svg_ids.clear()
        self.tabs.clear()

    def is_empty(self):
        return not (self.path_ids or self.segments or self.points or self.svg_ids or self.tabs)

    def has_geometry(self):
        """Any line/path-level content (paths, segments, or svg selections)."""
        if self.path_ids or self.segments or self.svg_ids:
            return True
        return False

    def single_path_id(self):
        if (len(self.path_ids) == 1 and not self.segments and not self.points and not self.svg_ids
                and not self.tabs):
            return next(iter(self.path_ids))
        return None


class Document:
    """Owns all editable state + project (de)serialization."""

    def __init__(self):
        self.area_w_mm = AREA_DEFAULT_MM
        self.area_h_mm = AREA_DEFAULT_MM
        self.steps_x = STEPS_X_DEFAULT
        self.steps_y = STEPS_Y_DEFAULT
        self.paths: dict = {}          # path_id -> VectorPath
        self.svgs: dict = {}           # svg_id -> SvgInstance
        self.color_params: dict = {}   # hex -> ColorParams
        self.color_order: list = []    # top-first execution order
        self.pause_after: dict = {}    # hex -> bool (manual resume for tool change)
        self.selection = Selection()
        self.bg_jpeg_path = ""         # display background (scan image, placed via scan_cal)
        self.file_path = ""            # project .json this doc was saved to / loaded from (not serialized)
        self.scan_cal = ScanCal()      # machine scan->cut adjustment used to place the scan on the mat
        self.blade = BladeSettings()   # job-level swivel compensation settings
        self.mat_skew = MatSkew()      # rotation/shift of the mat in the machine (core.mat)
        # Scan shown on the canvas, embedded in the project: {"blob": name, "dpi", "col0",
        # "row0"} (col0/row0 = where the image starts in the full-mat scan frame, pixels).
        # Takes precedence over bg_jpeg_path (an external file, older projects).
        self.scan_image = None
        # name -> bytes, content-addressed. NOT in to_dict (undo snapshots stay small). Blobs are
        # never dropped during a session, so undo can bring back any scan, and save() writes only
        # the ones the document references.
        self.blobs = {}
        self._last_single_path = None  # for "click again -> segment" rule

    # -- colors --
    def ensure_color(self, color):
        color = normalize_color(color)
        if color not in self.color_params:
            self.color_params[color] = ColorParams(color=color)
            if color not in self.color_order:
                self.color_order.append(color)
            self.pause_after.setdefault(color, False)
        return self.color_params[color]

    def set_path_color(self, path_id, new_color):
        new_color = normalize_color(new_color)
        p = self.paths[path_id]
        p.color = new_color
        self.ensure_color(new_color)  # inherit existing or defaults
        self._follow_generated_color(p)
        self._prune_colors()

    def _follow_generated_color(self, p):
        """A recolored generated path (image outline, text) sets its object's outline_color, so
        regenerating keeps the new color."""
        s = self.svgs.get(p.svg_id)
        if p.generated and s is not None:
            s.outline_color = p.color

    def _prune_colors(self):
        used = {p.color for p in self.paths.values()}
        for c in list(self.color_params):
            if c not in used and c in self.color_order:
                pass  # keep: user may reorder/keep params for later reuse (v1: keep)

    def ordered_colors(self):
        """Colors that have paths, in cut order (what a job cuts)."""
        used = {p.color for p in self.paths.values()}
        ordered = [c for c in self.color_order if c in used]
        for c in sorted(used):
            if c not in ordered:
                ordered.append(c)
        return ordered

    def group_colors(self):
        """The color groups the UI lists: ordered_colors() plus groups the user created that are
        still empty, each at its place in color_order."""
        used = {p.color for p in self.paths.values()}
        listed = [c for c in self.color_order if c in used or
                  (c in self.color_params and self.color_params[c].keep)]
        return listed + [c for c in self.ordered_colors() if c not in listed]

    def group_path_ids(self, color):
        color = normalize_color(color)
        return [pid for pid, p in self.paths.items() if p.color == color]

    def add_group(self, color=None):
        """A new, empty color group (kept in the list until removed). Without a color, the first
        of NEW_GROUP_COLORS not already a group (grey shades after that). Returns its color."""
        groups = set(self.group_colors())
        if color is None:
            spare = [c for c in NEW_GROUP_COLORS if c not in groups]
            for k in range(1, 256):
                if spare:
                    break
                g = "#%02X%02X%02X" % (((37 * k) % 256,) * 3)
                spare = [g] if g not in groups else []
            color = spare[0] if spare else "#808080"
        color = normalize_color(color)
        self.ensure_color(color).keep = True
        if color in self.color_order:      # an old unused entry: list it last, like a new one
            self.color_order.remove(color)
        self.color_order.append(color)
        return color

    def remove_group(self, color):
        """Drop an empty color group. Groups that still have paths stay. Returns whether it was removed."""
        color = normalize_color(color)
        if self.group_path_ids(color):
            return False
        self.color_params.pop(color, None)
        self.pause_after.pop(color, None)
        if color in self.color_order:
            self.color_order.remove(color)
        return True

    def recolor_group(self, old, new):
        """Give every path of group `old` the color `new`. When `new` is not a group yet, the group
        keeps its settings and place in the cut order under the new color. When it is, the two
        groups merge and `new` keeps its own settings. Returns True when groups merged."""
        old, new = normalize_color(old), normalize_color(new)
        if old == new:
            return False
        merge = new in self.group_colors()
        for pid in self.group_path_ids(old):
            self.paths[pid].color = new
            self._follow_generated_color(self.paths[pid])
        cp = self.color_params.pop(old, None)
        pause = self.pause_after.pop(old, False)
        if not merge:
            cp = cp or ColorParams(color=new)
            cp.color = new
            self.color_params[new] = cp
            self.pause_after[new] = pause
            if new in self.color_order:
                self.color_order.remove(new)
            if old in self.color_order:
                self.color_order[self.color_order.index(old)] = new
            else:
                self.color_order.append(new)
        if old in self.color_order:
            self.color_order.remove(old)
        return merge

    def move_color(self, color, new_index):
        order = self.ordered_colors()
        if color in order:
            order.remove(color)
        new_index = max(0, min(new_index, len(order)))
        order.insert(new_index, color)
        # keep unused tail
        tail = [c for c in self.color_order if c not in order]
        self.color_order = order + tail

    # -- svg/path management --
    def add_svg(self, label, paths, offset=(0.0, 0.0)):
        svg_id = new_id("svg")
        self.svgs[svg_id] = SvgInstance(id=svg_id, label=label,
                                        offset_x_mm=offset[0], offset_y_mm=offset[1],
                                        path_ids=[])
        for p in paths:
            p.svg_id = svg_id
            self.paths[p.id] = p
            self.svgs[svg_id].path_ids.append(p.id)
            self.ensure_color(p.color)
        return svg_id

    def move_svg(self, svg_id, dx_mm, dy_mm):
        s = self.svgs[svg_id]
        s.offset_x_mm += dx_mm
        s.offset_y_mm += dy_mm

    def set_svg_pos(self, svg_id, x_mm, y_mm):
        s = self.svgs[svg_id]
        s.offset_x_mm = float(x_mm)
        s.offset_y_mm = float(y_mm)

    def set_svg_scale(self, svg_id, scale):
        s = self.svgs[svg_id]
        s.scale = max(0.01, min(100.0, float(scale)))

    def delete_svg(self, svg_id):
        s = self.svgs.pop(svg_id, None)
        if s is None:
            return
        for pid in s.path_ids:
            self.paths.pop(pid, None)
        self.selection.svg_ids.discard(svg_id)
        self.selection.path_ids = {p for p in self.selection.path_ids if p in self.paths}
        self.selection.segments = {(p, i) for (p, i) in self.selection.segments if p in self.paths}
        self.selection.points = {(p, i) for (p, i) in self.selection.points if p in self.paths}
        self.selection.tabs = {(p, t) for (p, t) in self.selection.tabs if p in self.paths}

    def _xf(self, path):
        """Xf of the path's svg (identity if orphaned)."""
        return Xf.of(self.svgs.get(path.svg_id))

    def path_abs_points(self, path):
        """Anchors only, abs mm, (x, y, thickness)."""
        f = self._xf(path).to_abs
        return [f(pt.x, pt.y) + (pt.thickness,) for pt in path.points]

    # -- bezier geometry --
    def segment_is_curve(self, path, i):
        n = len(path.points)
        if n < 2:
            return False
        return path.points[i % n].hout is not None or path.points[(i + 1) % n].hin is not None

    def _seg_abs(self, path, i):
        """Segment i in abs mm as (p0, c1, c2, p3), with c1 = c2 = None for a straight segment."""
        n = len(path.points)
        a, b = path.points[i % n], path.points[(i + 1) % n]
        f = self._xf(path).to_abs
        pa, pb = f(a.x, a.y), f(b.x, b.y)
        if a.hout is None and b.hin is None:
            return (pa, None, None, pb)
        return (pa, f(*a.hout) if a.hout is not None else pa, f(*b.hin) if b.hin is not None else pb, pb)

    @staticmethod
    def _rounds(path, k):
        """Anchor k has a corner radius and a segment on both sides (not an open path's end)."""
        n = len(path.points)
        return path.points[k % n].radius > 0 and n >= 2 and (path.closed or 0 < k % n < n - 1)

    def _fillet(self, path, k, tol):
        """corner_fillet of anchor k: (u on the incoming segment, u on the outgoing one, arc) or
        None. Each side may use the whole segment, or half when its other end rounds too."""
        if not self._rounds(path, k):
            return None
        n = len(path.points)
        k %= n
        s_in, s_out = self._seg_abs(path, k - 1), self._seg_abs(path, k)

        def room(seg, other):
            L = math.hypot(seg[3][0] - seg[0][0], seg[3][1] - seg[0][1])
            return L / 2 if self._rounds(path, other) else L
        return corner_fillet(s_in, s_out, path.points[k].radius, room(s_in, k - 1), room(s_out, k + 1), tol)

    def _seg_poly_u(self, path, i, tol):
        """Flattened segment in abs mm as (x, y, thickness, u), u = curve parameter 0..1.

        A rounded corner (ControlPoint.radius) is split between its two segments: segment i
        starts with the second half of the arc at its start anchor (u = 0), then the part of the
        segment between the arcs, then the first half of the arc at its end anchor (u = 1)."""
        n = len(path.points)
        a, b = path.points[i % n], path.points[(i + 1) % n]
        seg = self._seg_abs(path, i)
        pa, c1, c2, pb = seg
        dt = b.thickness - a.thickness
        fa = self._fillet(path, i, tol) if a.radius > 0 else None
        fb = self._fillet(path, i + 1, tol) if b.radius > 0 else None
        if fa is None and fb is None:
            if c1 is None:
                return [(pa[0], pa[1], a.thickness, 0.0), (pb[0], pb[1], b.thickness, 1.0)]
            return [(x, y, a.thickness + dt * u, u) for (x, y, u) in flatten_cubic_t(pa, c1, c2, pb, tol)]
        out = []

        def add(x, y, t, u):
            if not out or abs(x - out[-1][0]) > 1e-9 or abs(y - out[-1][1]) > 1e-9:
                out.append((x, y, t, u))
        if fa is not None:
            for x, y, _v in flatten_cubic_t(*split_cubic(*fa[2], 0.5)[1], tol):
                add(x, y, a.thickness, 0.0)
        u0 = fa[1] if fa is not None else 0.0
        u1 = fb[0] if fb is not None else 1.0
        if u1 - u0 > 1e-9:
            if c1 is None:
                for u in (u0, u1):
                    x, y = seg_point(seg, u)
                    add(x, y, a.thickness + dt * u, u)
            else:
                part = split_cubic(*seg, u1)[0] if u1 < 1.0 else seg
                if u0 > 0.0:
                    part = split_cubic(*part, u0 / u1)[1]
                for x, y, v in flatten_cubic_t(*part, tol):
                    u = u0 + (u1 - u0) * v
                    add(x, y, a.thickness + dt * u, u)
        if fb is not None:
            for x, y, _v in flatten_cubic_t(*split_cubic(*fb[2], 0.5)[0], tol):
                add(x, y, b.thickness, 1.0)
        return out

    def segment_abs_poly(self, path, i, tol=0.05):
        """Flattened segment incl. both endpoints, abs mm, (x, y, thickness lerped by curve param)."""
        return [(x, y, t) for (x, y, t, _u) in self._seg_poly_u(path, i, tol)]

    def path_abs_flat(self, path, tol=0.05):
        """Whole path flattened, abs mm. Closed paths do not repeat the start point (consumers
        close back to it) but include the interior points of the closing segment."""
        out = []
        for i in range(path.segment_count()):
            out.extend(self.segment_abs_poly(path, i, tol)[:-1])
        if not path.closed or not out:
            out.extend(self.path_abs_points(path)[-1:])  # open: end anchor, <2 points: the anchor
        return out

    def nearest_t_on_segment(self, path, i, x, y):
        """(t, dist_mm): curve parameter of the point on segment i nearest abs (x, y)."""
        poly = self._seg_poly_u(path, i, 0.02)
        best = (0.0, float("inf"))
        for (x0, y0, _t0, u0), (x1, y1, _t1, u1) in zip(poly, poly[1:]):
            dx, dy = x1 - x0, y1 - y0
            L2 = dx * dx + dy * dy
            s = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / L2))
            d = math.hypot(x - (x0 + dx * s), y - (y0 + dy * s))
            if d < best[1]:
                best = (u0 + (u1 - u0) * s, d)
        return best

    # -- tabs --
    @staticmethod
    def _arc_table(poly):
        """Cumulative arc length (mm) at each vertex of a flattened polyline."""
        cum = [0.0]
        for a, b in zip(poly, poly[1:]):
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        return cum

    def nearest_pos_on_segment(self, path, i, x, y):
        """(pos, dist_mm): the point of segment i nearest abs (x, y) as a fraction of the
        segment's length (Tab.pos), and its distance."""
        poly = self.segment_abs_poly(path, i)
        cum = self._arc_table(poly)
        best = (0.0, float("inf"))
        for k, (a, b) in enumerate(zip(poly, poly[1:])):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L2 = dx * dx + dy * dy
            s = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / L2))
            d = math.hypot(x - (a[0] + dx * s), y - (a[1] + dy * s))
            if d < best[1]:
                best = (cum[k] + (cum[k + 1] - cum[k]) * s, d)
        total = cum[-1]
        return (best[0] / total if total > 1e-12 else 0.0), best[1]

    def tab_point(self, path, tab):
        """(x, y, ux, uy): abs mm position of the tab and the unit direction of the line there,
        or None when the tab is not on a segment of the path."""
        if not 0 <= tab.seg < path.segment_count():
            return None
        poly = self.segment_abs_poly(path, tab.seg)
        cum = self._arc_table(poly)
        L = cum[-1]

        def at(s):
            for k in range(len(poly) - 1):
                if s <= cum[k + 1] or k == len(poly) - 2:
                    el = cum[k + 1] - cum[k]
                    f = 0.0 if el <= 1e-12 else max(0.0, min(1.0, (s - cum[k]) / el))
                    a, b = poly[k], poly[k + 1]
                    return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
            return poly[0][0], poly[0][1]
        s = max(0.0, min(1.0, tab.pos)) * L
        x, y = at(s)
        # the direction over a short stretch around the tab, so that a tab on a vertex of the
        # flattened curve follows the curve and not one of the two chords
        h = min(0.05, L / 2)
        (x0, y0), (x1, y1) = at(max(0.0, s - h)), at(min(L, s + h))
        d = math.hypot(x1 - x0, y1 - y0)
        if d <= 1e-12:
            return (x, y, 1.0, 0.0)  # a segment of zero length
        return (x, y, (x1 - x0) / d, (y1 - y0) / d)

    def tab_gaps(self, path):
        """{segment index: [(s0, s1), ...]}: the parts of each segment that tabs leave uncut, as
        sorted, merged arc-length intervals in mm from the segment start (along
        segment_abs_poly). A gap is centered on its tab. It runs on into the neighbouring
        segments, wraps around a closed path and ends at an open path's ends."""
        m = path.segment_count()
        if not path.tabs or m == 0:
            return {}
        lens = [self._arc_table(self.segment_abs_poly(path, i))[-1] for i in range(m)]
        starts = [0.0]
        for L in lens:
            starts.append(starts[-1] + L)
        total = starts[-1]
        if total <= 1e-9:
            return {}
        spans = []  # arc-length intervals on the whole path
        for t in path.tabs:
            if not 0 <= t.seg < m:
                continue
            c = starts[t.seg] + max(0.0, min(1.0, t.pos)) * lens[t.seg]
            h = max(0.0, t.gap_mm) / 2
            if h <= 0:
                continue
            a, b = c - h, c + h
            if not path.closed:
                spans.append((max(a, 0.0), min(b, total)))
            elif b - a >= total:
                spans.append((0.0, total))
            else:
                if a < 0:
                    spans.append((a + total, total))
                    a = 0.0
                if b > total:
                    spans.append((0.0, b - total))
                    b = total
                spans.append((a, b))
        out = {}
        for i in range(m):
            s0, s1 = starts[i], starts[i + 1]
            parts = sorted((max(a, s0) - s0, min(b, s1) - s0) for a, b in spans
                           if min(b, s1) - max(a, s0) > 1e-9)
            merged = []
            for a, b in parts:
                if merged and a <= merged[-1][1] + 1e-9:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], b))
                else:
                    merged.append((a, b))
            if merged:
                out[i] = merged
        return out

    def add_tab(self, pid, seg, pos, gap_mm=DEFAULT_TAB_GAP_MM):
        """Attach a new tab to segment seg of path pid at pos (fraction of the segment length).
        Returns its id, or None when the path or segment does not exist."""
        p = self.paths.get(pid)
        if p is None or not 0 <= seg < p.segment_count():
            return None
        t = Tab(id=new_id("tab"), seg=int(seg), pos=max(0.0, min(1.0, float(pos))),
                gap_mm=max(TAB_GAP_MIN_MM, min(TAB_GAP_MAX_MM, float(gap_mm))))
        p.tabs.append(t)
        return t.id

    def selected_tabs(self):
        """The selected Tab objects, in a stable order."""
        out = []
        for pid, tid in sorted(self.selection.tabs):
            p = self.paths.get(pid)
            t = p.tab(tid) if p else None
            if t is not None:
                out.append(t)
        return out

    def set_tab_gap(self, refs, gap_mm):
        """Set the gap length (mm) of the tabs refs [(path_id, tab_id)]. Returns how many changed."""
        g = max(TAB_GAP_MIN_MM, min(TAB_GAP_MAX_MM, float(gap_mm)))
        n = 0
        for pid, tid in refs:
            p = self.paths.get(pid)
            t = p.tab(tid) if p else None
            if t is not None and t.gap_mm != g:
                t.gap_mm = g
                n += 1
        return n

    def svg_bbox(self, svg_id):
        """Abs-mm bbox (xmin, ymin, xmax, ymax) of flattened geometry (plus the raster rect for
        image/pdf objects), or None if empty."""
        s = self.svgs.get(svg_id)
        if s is None:
            return None
        xs, ys = [], []
        for pid in s.path_ids:
            p = self.paths.get(pid)
            if p is None:
                continue
            for (x, y, _t) in self.path_abs_flat(p):
                xs.append(x)
                ys.append(y)
        if s.kind in ("image", "pdf") and s.px_w > 0 and s.px_h > 0:
            w, h = image_size_mm(s)
            xf = Xf.of(s)
            for x, y in (xf.to_abs(0.0, 0.0), xf.to_abs(w, 0.0), xf.to_abs(w, h), xf.to_abs(0.0, h)):
                xs.append(x)
                ys.append(y)
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def selection_bbox(self):
        """Abs-mm bbox of current selection (paths+segments+points+svgs) or None."""
        xs, ys = [], []
        sel = self.selection
        for svg_id in sel.svg_ids:
            b = self.svg_bbox(svg_id)
            if b:
                xs += [b[0], b[2]]
                ys += [b[1], b[3]]
        for pid in sel.path_ids:
            p = self.paths.get(pid)
            if p:
                for (x, y, _t) in self.path_abs_flat(p):
                    xs.append(x)
                    ys.append(y)
        for (pid, si) in sel.segments:
            p = self.paths.get(pid)
            if p and len(p.points) >= 2:
                for (x, y, _t) in self.segment_abs_poly(p, si):
                    xs.append(x)
                    ys.append(y)
        for (pid, pi) in sel.points:
            p = self.paths.get(pid)
            if p and 0 <= pi < len(p.points):
                x, y, _t = self.path_abs_points(p)[pi]
                xs.append(x)
                ys.append(y)
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def svg_ids_for_selection(self):
        """All svg ids touched by the current selection."""
        out = set(self.selection.svg_ids)
        for pid in self.selection.path_ids:
            p = self.paths.get(pid)
            if p:
                out.add(p.svg_id)
        for (pid, _i) in list(self.selection.segments) + list(self.selection.points):
            p = self.paths.get(pid)
            if p:
                out.add(p.svg_id)
        return {s for s in out if s in self.svgs}

    def is_entire_svg_selected(self, svg_id):
        """True when the selection covers the whole SVG (explicit or all-paths)."""
        if svg_id in self.selection.svg_ids:
            return True
        s = self.svgs.get(svg_id)
        if s is None or not s.path_ids:
            return False
        if self.selection.segments or self.selection.points:
            return False
        return set(s.path_ids) == set(self.selection.path_ids) and len(self.selection.path_ids) > 0

    def selected_point_refs(self):
        """All control points implied by the selection.

        Explicit points + segment endpoints + all points of selected paths +
        all points of selected SVGs. Returns list of (path_id, pt_idx).
        """
        out = set()
        for key in self.selection.points:
            out.add(key)
        for (pid, si) in self.selection.segments:
            p = self.paths.get(pid)
            if p and p.points:
                n = len(p.points)
                out.add((pid, si % n))
                out.add((pid, (si + 1) % n))
        for pid in self.selection.path_ids:
            p = self.paths.get(pid)
            if p:
                for i in range(len(p.points)):
                    out.add((pid, i))
        for svg_id in self.selection.svg_ids:
            s = self.svgs.get(svg_id)
            if s:
                for pid in s.path_ids:
                    p = self.paths.get(pid)
                    if p:
                        for i in range(len(p.points)):
                            out.add((pid, i))
        return sorted(out)

    # -- editing operations --
    def _next_label(self, prefix):
        """'<prefix> N' with N one past the highest existing '<prefix> N' label."""
        top = 0
        for s in self.svgs.values():
            head, _, tail = s.label.rpartition(" ")
            if head == prefix and tail.isdigit():
                top = max(top, int(tail))
        return f"{prefix} {top + 1}"

    def _drop_empty(self, svg_ids, keep=None):
        """Delete touched svg/drawing objects left without paths (rasters are never deleted)."""
        for sid in svg_ids:
            s = self.svgs.get(sid)
            if s and sid != keep and not s.path_ids and s.kind in ("svg", "drawing"):
                self.delete_svg(sid)

    def _replace_path(self, p, pieces):
        """Swap path p for open/closed pieces [(closed, points, tabs)] at the same z-position
        (the first keeps p's id, and no pieces removes it)."""
        s = self.svgs.get(p.svg_id)
        ids = s.path_ids if s else None
        pos = ids.index(p.id) if ids is not None and p.id in ids else None
        self.paths.pop(p.id, None)
        new_ids = []
        for k, (closed, pts, tabs) in enumerate(pieces):
            if k == 0:  # keep the object identity of the original
                p.points, p.closed, p.tabs, q = pts, closed, tabs, p
            else:
                q = replace(p, id=new_id("path"), points=pts, closed=closed, tabs=tabs)
            self.paths[q.id] = q
            new_ids.append(q.id)
        if ids is not None:
            if pos is None:
                ids.extend(new_ids)
            else:
                ids[pos:pos + 1] = new_ids

    def selection_anchor_refs(self):
        """Anchors implied by points, selected segments (both ends) and selected paths.
        Paths of wholly selected svgs are excluded (those move via the svg offset)."""
        sel = self.selection
        out = set()

        def ok(pid):
            p = self.paths.get(pid)
            return p if p and p.svg_id not in sel.svg_ids else None
        for (pid, i) in sel.points:
            p = ok(pid)
            if p and 0 <= i < len(p.points):
                out.add((pid, i))
        for (pid, i) in sel.segments:
            p = ok(pid)
            if p and p.segment_count() > 0:
                n = len(p.points)
                out.add((pid, i % n))
                out.add((pid, (i + 1) % n))
        for pid in sel.path_ids:
            p = ok(pid)
            if p:
                out.update((pid, i) for i in range(len(p.points)))
        return out

    def move_anchors(self, refs, dx_mm, dy_mm):
        """Move anchors (and their handles) by an abs-mm delta."""
        for (pid, i) in set(refs):
            p = self.paths.get(pid)
            if p is None or not (0 <= i < len(p.points)):
                continue
            dx, dy = self._xf(p).vec_local(dx_mm, dy_mm)
            pt = p.points[i]
            pt.x += dx
            pt.y += dy
            if pt.hin is not None:
                pt.hin = (pt.hin[0] + dx, pt.hin[1] + dy)
            if pt.hout is not None:
                pt.hout = (pt.hout[0] + dx, pt.hout[1] + dy)

    def move_selection(self, dx_mm, dy_mm):
        for sid in self.selection.svg_ids:
            if sid in self.svgs:
                self.move_svg(sid, dx_mm, dy_mm)
        self.move_anchors(self.selection_anchor_refs(), dx_mm, dy_mm)

    # -- rotate / flip --
    def _content_center_local(self, s):
        """Centre of an object's own (unrotated) content box, in its local frame: paths plus
        the raster rect of image/pdf objects. The origin for an empty object."""
        xf = Xf.of(s)
        xs, ys = [], []
        for pid in s.path_ids:
            p = self.paths.get(pid)
            if p is not None:
                for (x, y, _t) in self.path_abs_flat(p):
                    lx, ly = xf.to_local(x, y)
                    xs.append(lx)
                    ys.append(ly)
        if s.kind in ("image", "pdf") and s.px_w > 0 and s.px_h > 0:
            w, h = image_size_mm(s)
            xs += [0.0, w]
            ys += [0.0, h]
        if not xs:
            return (0.0, 0.0)
        return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)

    def set_svg_rotation(self, svg_id, deg):
        """Turn an object to an absolute angle (degrees clockwise) about the centre of its
        content, which stays where it is on the mat."""
        s = self.svgs[svg_id]
        c = self._content_center_local(s)
        before = Xf.of(s).to_abs(*c)
        s.rotation = norm_angle(deg)
        after = Xf.of(s).to_abs(*c)
        s.offset_x_mm += before[0] - after[0]
        s.offset_y_mm += before[1] - after[1]

    def transform_selection(self, op, deg=0.0):
        """Rotate (op "rotate", by `deg` degrees clockwise), or flip ("flip_h" mirrors left-right,
        "flip_v" top-bottom) the selection about the centre of its bounding box. Whole objects
        keep their geometry and change their rotation / flip, while selected paths, segments and
        points of other objects have their points moved. Returns True if anything changed."""
        bbox = self.selection_bbox()
        if bbox is None:
            return False
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        if op == "rotate":
            co, si = _cos_sin(deg)
            m = (co, si, -si, co)
        elif op == "flip_h":
            m = (-1.0, 0.0, 0.0, 1.0)
        elif op == "flip_v":
            m = (1.0, 0.0, 0.0, -1.0)
        else:
            raise ValueError(op)

        def f(x, y):   # the abs-mm mapping about the centre
            dx, dy = x - cx, y - cy
            return (cx + m[0] * dx + m[2] * dy, cy + m[1] * dx + m[3] * dy)
        changed = False
        for sid in self.selection.svg_ids:
            s = self.svgs.get(sid)
            if s is None:
                continue
            s.offset_x_mm, s.offset_y_mm = f(s.offset_x_mm, s.offset_y_mm)
            # mirror * R(a) = R(-a) * mirror: a flip negates the angle and toggles the local flip
            if op == "rotate":
                s.rotation = norm_angle(s.rotation + deg)
            else:
                s.rotation = norm_angle((180.0 if op == "flip_v" else 0.0) - s.rotation)
                s.flip = not s.flip
            changed = True
        for (pid, i) in self.selection_anchor_refs():
            p = self.paths.get(pid)
            if p is None or not (0 <= i < len(p.points)):
                continue
            xf = self._xf(p)

            def g(h):
                return xf.to_local(*f(*xf.to_abs(*h)))
            pt = p.points[i]
            pt.x, pt.y = g((pt.x, pt.y))
            if pt.hin is not None:
                pt.hin = g(pt.hin)
            if pt.hout is not None:
                pt.hout = g(pt.hout)
            changed = True
        return changed

    def set_handle(self, pid, idx, which, abs_x, abs_y, mirror=True):
        """Set an anchor's "in"/"out" handle to an abs-mm position. With mirror, the opposite
        handle (if any) is rotated to stay collinear, keeping its length."""
        p = self.paths.get(pid)
        if p is None or not (0 <= idx < len(p.points)):
            return
        pt = p.points[idx]
        h = self._xf(p).to_local(abs_x, abs_y)
        other = pt.hout if which == "in" else pt.hin
        if which == "in":
            pt.hin = h
        else:
            pt.hout = h
        if mirror and other is not None:
            vx, vy = h[0] - pt.x, h[1] - pt.y
            L = math.hypot(vx, vy)
            if L > 1e-12:
                r = math.hypot(other[0] - pt.x, other[1] - pt.y) / L
                m = (pt.x - vx * r, pt.y - vy * r)
                if which == "in":
                    pt.hout = m
                else:
                    pt.hin = m

    def bend_segment(self, pid, i, t, dx_mm, dy_mm):
        """Make segment i cubic so its point at parameter t moves by exactly (dx, dy) mm."""
        p = self.paths.get(pid)
        if p is None or p.segment_count() == 0:
            return
        n = len(p.points)
        a, b = p.points[i % n], p.points[(i + 1) % n]
        t = max(0.05, min(0.95, t))
        dx, dy = self._xf(p).vec_local(dx_mm, dy_mm)
        k = 1.0 / (3 * t * (1 - t) * ((1 - t) ** 2 + t ** 2))
        c1 = a.hout if a.hout is not None else (a.x, a.y)
        c2 = b.hin if b.hin is not None else (b.x, b.y)
        a.hout = (c1[0] + dx * k * (1 - t), c1[1] + dy * k * (1 - t))
        b.hin = (c2[0] + dx * k * t, c2[1] + dy * k * t)

    @staticmethod
    def bend_weights(t, focus):
        """How much a bend pressed at parameter t moves (start handle, end handle), 0..1.
        Full focus: both move fully at t = 0.5, and the far handle fades out linearly and stops
        moving at t >= 0.75 (start handle) or t <= 0.25 (end handle). focus 0: always (1, 1)."""
        f = max(0.0, min(1.0, float(focus)))
        a1 = max(0.0, min(1.0, (BEND_FOCUS_HI - t) / (BEND_FOCUS_HI - 0.5)))
        a2 = max(0.0, min(1.0, (t - BEND_FOCUS_LO) / (0.5 - BEND_FOCUS_LO)))
        return 1.0 - f * (1.0 - a1), 1.0 - f * (1.0 - a2)

    def bend_segment_to(self, pid, i, t, base, dx_mm, dy_mm, target, strength, focus=0.0):
        """One bend drag, relative to the drag's start: base = (start anchor's hout, end anchor's
        hin) when the drag began (None = no handle), (dx, dy) mm = pointer movement since the
        press, target = pointer now (abs mm).

        Each handle moves by its bend_weights share. With strength 0 the point pressed at t
        follows the pointer exactly (the handle moves are scaled so the weighted pair still
        carries it), and strength pulls the handles toward the pointer instead, 1 = onto it. The pull
        grows with the drag (a handle reaches the pointer once the drag is as long as the
        handle's distance from the press point), so starting a drag never jumps the curve, and a
        handle with weight 0 never moves."""
        p = self.paths.get(pid)
        if p is None or p.segment_count() == 0:
            return
        n = len(p.points)
        a, b = p.points[i % n], p.points[(i + 1) % n]
        w1, w2 = self.bend_weights(t, focus)           # by position along the segment
        if base[0] is None and base[1] is None:
            # straight: t is the fraction along the line (nearest_t_on_segment), but the cubic
            # with both handles on its anchors puts that point at u where 3u^2 - 2u^3 = t
            lo, hi = 0.0, 1.0
            for _ in range(40):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if 3 * mid * mid - 2 * mid ** 3 < t else (lo, mid)
            t = (lo + hi) / 2
        t = max(0.05, min(0.95, t))
        xf = self._xf(p)
        dx, dy = xf.vec_local(dx_mm, dy_mm)
        tx, ty = xf.to_local(*target)
        px, py = tx - dx, ty - dy                      # the press point
        k = 1.0 / (3 * t * (1 - t) * ((1 - t) ** 2 * w1 + t ** 2 * w2))
        s = max(0.0, min(1.0, float(strength)))
        drag = math.hypot(dx, dy)
        out = []
        for c, w, share, anchor in ((base[0], w1, 1 - t, (a.x, a.y)), (base[1], w2, t, (b.x, b.y))):
            if w <= 0:
                out.append(c)                          # untouched, None stays None
                continue
            c = c if c is not None else anchor
            ex, ey = dx * k * w * share, dy * k * w * share
            reach = min(1.0, drag / max(1e-9, math.hypot(px - c[0], py - c[1])))
            pull_x, pull_y = (tx - c[0]) * w * reach, (ty - c[1]) * w * reach
            out.append((c[0] + (1 - s) * ex + s * pull_x, c[1] + (1 - s) * ey + s * pull_y))
        a.hout, b.hin = out

    def straighten_segments(self, seg_refs):
        for (pid, i) in seg_refs:
            p = self.paths.get(pid)
            if p is None or p.segment_count() == 0:
                continue
            n = len(p.points)
            p.points[i % n].hout = None
            p.points[(i + 1) % n].hin = None

    def break_apart(self):
        """Move selected paths / selected segments into a new drawing object. Partially
        selected paths are split: selected runs go to the new object, the rest stays as open
        paths. Returns the new svg id or None."""
        sel = self.selection
        full = {pid for pid in sel.path_ids if pid in self.paths}
        segs = {}
        for (pid, i) in sel.segments:
            p = self.paths.get(pid)
            if p and pid not in full and 0 <= i < p.segment_count():
                segs.setdefault(pid, set()).add(i)
        plans = []  # (path, split) split=None: move whole, else (runs, selected_runs)
        for pid, p in self.paths.items():  # doc order
            if pid in full:
                plans.append((p, None))
            elif pid in segs:
                runs = _split_runs(p, segs[pid])
                if len(runs) == 1:
                    if runs[0][0]:
                        plans.append((p, None))
                else:
                    plans.append((p, runs))
        if not plans:
            return None
        new_svg = self.add_svg(self._next_label("Part"), [])
        self.svgs[new_svg].kind = "drawing"
        touched = set()
        for p, runs in plans:
            old = self.svgs.get(p.svg_id)
            touched.add(p.svg_id)
            xf = self._xf(p)
            if runs is None:  # whole path moves, keeping its id
                if old and p.id in old.path_ids:
                    old.path_ids.remove(p.id)
                p.points = self._reframe(p.points, xf, Xf())
                p.svg_id, p.generated = new_svg, False
                self.svgs[new_svg].path_ids.append(p.id)
                continue
            keep = []
            for sel_run, closed, idxs in runs:
                pts = _copy_pts(p, idxs, closed)
                tabs = _piece_tabs(p, idxs, closed)
                if not sel_run:
                    keep.append((closed, pts, tabs))
                    continue
                q = replace(p, id=new_id("path"), svg_id=new_svg, closed=closed, generated=False,
                            points=self._reframe(pts, xf, Xf()), tabs=tabs)
                self.paths[q.id] = q
                self.svgs[new_svg].path_ids.append(q.id)
            self._replace_path(p, keep)
        self._drop_empty(touched)
        sel.clear()
        sel.svg_ids.add(new_svg)
        return new_svg

    @staticmethod
    def _reframe(points, src, dst):
        """Copies of control points (and handles) moved from frame src into frame dst (Xf)."""
        def f(x, y):
            return dst.to_local(*src.to_abs(x, y))
        out = []
        for pt in points:
            x, y = f(pt.x, pt.y)
            out.append(replace(pt, x=x, y=y, hin=f(*pt.hin) if pt.hin is not None else None,
                               hout=f(*pt.hout) if pt.hout is not None else None))
        return out

    def combine(self):
        """Merge selected paths (and all paths of selected svgs) into one object: the first
        selected svg/drawing, else a new drawing. Returns the target id or None."""
        sel = self.selection
        want = set(sel.path_ids)
        for sid in sel.svg_ids:
            if sid in self.svgs:
                want.update(self.svgs[sid].path_ids)
        order = [pid for s in self.svgs.values() for pid in s.path_ids if pid in want and pid in self.paths]
        if not order:
            return None
        target = next((sid for sid, s in self.svgs.items()
                       if sid in sel.svg_ids and s.kind in ("svg", "drawing")), None)
        if target is not None and all(self.paths[pid].svg_id == target for pid in order):
            return None
        if target is None:
            target = self.add_svg(self._next_label("Drawing"), [])
            self.svgs[target].kind = "drawing"
        ts = self.svgs[target]
        touched = set()
        for pid in order:
            p = self.paths[pid]
            if p.svg_id == target:
                continue
            old = self.svgs.get(p.svg_id)
            xf = self._xf(p)
            if old and pid in old.path_ids:
                old.path_ids.remove(pid)
            touched.add(p.svg_id)
            p.points = self._reframe(p.points, xf, Xf.of(ts))
            p.svg_id, p.generated = target, False
            ts.path_ids.append(pid)
        self._drop_empty(touched, keep=target)
        sel.clear()
        sel.svg_ids.add(target)
        return target

    def delete_selection(self):
        """Delete selected svgs, paths, segments (splitting paths), points (joining
        neighbours) and tabs. Returns True if anything was deleted."""
        sel = self.selection
        if sel.is_empty():
            return False
        for pid, tid in sel.tabs:
            p = self.paths.get(pid)
            if p is not None:
                p.tabs = [t for t in p.tabs if t.id != tid]
        for sid in list(sel.svg_ids):
            self.delete_svg(sid)
        segs, pts = {}, {}
        for (pid, i) in sel.segments:
            segs.setdefault(pid, set()).add(i)
        for (pid, i) in sel.points:
            pts.setdefault(pid, set()).add(i)
        touched = set()
        for pid in set(sel.path_ids) | set(segs) | set(pts):
            p = self.paths.get(pid)
            if p is None:
                continue
            touched.add(p.svg_id)
            if pid in sel.path_ids:
                self._replace_path(p, [])
                continue
            S = {i for i in segs.get(pid, ()) if 0 <= i < p.segment_count()}
            P = {i for i in pts.get(pid, ()) if 0 <= i < len(p.points)}
            if not (S or P):
                continue
            pieces = []
            for sel_run, closed, idxs in _split_runs(p, S):
                if sel_run:
                    continue
                cps = _remove_anchors(idxs, _copy_pts(p, idxs, closed), closed, P)
                if len(cps) >= 2:
                    pieces.append((closed, cps, _piece_tabs(p, idxs, closed, P)))
            self._replace_path(p, pieces)
        self._drop_empty(touched)
        sel.clear()
        return True

    # -- clipboard (whole objects) --
    def copy_objects(self, svg_ids):
        """A self-contained snapshot of whole objects (their paths and color group settings),
        in drawing order, for paste_objects. None when nothing is copied."""
        ids = [sid for sid in self.svgs if sid in svg_ids]
        if not ids:
            return None
        full = self.to_dict()
        pids = {pid for sid in ids for pid in self.svgs[sid].path_ids}
        paths = [p for p in full["paths"] if p["id"] in pids]
        colors = {p["color"] for p in paths}
        return {"svgs": [d for d in full["svgs"] if d["id"] in ids],
                "paths": paths,
                "color_params": {c: v for c, v in full["color_params"].items() if c in colors},
                "pause_after": {c: v for c, v in full["pause_after"].items() if c in colors}}

    def paste_objects(self, data, dx=0.0, dy=0.0):
        """Add copies of the objects in `data` (from copy_objects) moved by (dx, dy) mm, with
        new ids, and select them. Colors new to this document bring their group settings.
        Returns the new object ids."""
        if not data:
            return []
        src = Document.from_dict({"svgs": data["svgs"], "paths": data["paths"],
                                  "color_params": data.get("color_params", {})})
        new_ids = []
        for s in src.svgs.values():
            s.id = new_id("svg")
            s.offset_x_mm += dx
            s.offset_y_mm += dy
            old, s.path_ids = s.path_ids, []
            for pid in old:
                p = src.paths.get(pid)
                if p is None:
                    continue
                p.id, p.svg_id = new_id("path"), s.id
                self.paths[p.id] = p
                s.path_ids.append(p.id)
                if p.color not in self.color_params and p.color in src.color_params:
                    self.color_params[p.color] = src.color_params[p.color]
                    if p.color not in self.color_order:
                        self.color_order.append(p.color)
                    self.pause_after.setdefault(p.color,
                                                bool(data.get("pause_after", {}).get(p.color)))
                self.ensure_color(p.color)
            self.svgs[s.id] = s
            new_ids.append(s.id)
        self.selection.clear()
        self.selection.svg_ids.update(new_ids)
        return new_ids

    def select_all(self):
        """Select every visible object. Returns True if anything is selected."""
        self.selection.clear()
        self.selection.svg_ids.update(sid for sid, s in self.svgs.items() if s.visible)
        return bool(self.selection.svg_ids)

    # -- drawing / raster objects --
    def add_drawing(self, label=None):
        svg_id = self.add_svg(label or self._next_label("Drawing"), [])
        self.svgs[svg_id].kind = "drawing"
        return svg_id

    def add_drawn_path(self, svg_id, abs_anchors, closed, color, thickness=DEFAULT_THICKNESS, radius=0.0):
        """abs_anchors: [(x, y, hin|None, hout|None)] in abs mm -> new path in the svg's frame,
        every point with the given line thickness and corner radius (mm)."""
        xf = Xf.of(self.svgs[svg_id])
        s = self.svgs[svg_id]

        def loc(h):
            return None if h is None else xf.to_local(h[0], h[1])
        pts = []
        for a in abs_anchors:
            hin = a[2] if len(a) > 2 else None
            hout = a[3] if len(a) > 3 else None
            x, y = loc(a)
            pts.append(ControlPoint(x, y, float(thickness), loc(hin), loc(hout), float(radius)))
        p = VectorPath(id=new_id("path"), svg_id=svg_id, color=normalize_color(color), points=pts,
                       closed=closed)   # same key as color_params / color_order
        self.paths[p.id] = p
        s.path_ids.append(p.id)
        self.ensure_color(p.color)
        return p.id

    def add_image(self, kind, source_path, px_w, px_h, dpi, page=0, offset=(10.0, 10.0)):
        label = os.path.basename(source_path) or kind
        if kind == "pdf":
            label += f" p{page + 1}"
        svg_id = self.add_svg(label, [], offset)
        s = self.svgs[svg_id]
        s.kind, s.source_path, s.px_w, s.px_h, s.dpi, s.pdf_page = kind, source_path, int(px_w), int(px_h), float(dpi), int(page)
        return svg_id

    def add_text(self, x_mm, y_mm, style, color):
        """A new, empty text object anchored at (x, y) abs mm (the first line's baseline, at its
        left / center / right end by the alignment). Returns its id."""
        svg_id = self.add_svg("Text", [], (x_mm, y_mm))
        s = self.svgs[svg_id]
        s.kind, s.text, s.text_style = "text", "", replace(style)
        s.outline_color = normalize_color(color)
        return svg_id

    def text_svg_ids(self, svg_ids):
        return {sid for sid in svg_ids if sid in self.svgs and self.svgs[sid].kind == "text"}

    def promote_text_selection(self):
        """Text objects are selected whole: parts of their paths in the selection become the
        object (their paths are rebuilt from the text, so points and segments are not edited)."""
        sel = self.selection
        changed = False
        for attr, key in (("path_ids", lambda r: r), ("segments", lambda r: r[0]),
                          ("points", lambda r: r[0])):
            refs = getattr(sel, attr)
            for r in list(refs):
                p = self.paths.get(key(r))
                s = self.svgs.get(p.svg_id) if p else None
                if s is not None and s.kind == "text":
                    refs.discard(r)
                    sel.svg_ids.add(s.id)
                    changed = True
        return changed

    def convert_text_to_paths(self, svg_ids):
        """Text objects become plain drawings: their paths stay, now editable, and the text is
        gone. Returns how many were converted."""
        n = 0
        for sid in self.text_svg_ids(svg_ids):
            s = self.svgs[sid]
            for pid in s.path_ids:
                if pid in self.paths:
                    self.paths[pid].generated = False
            s.kind, s.text, s.text_style = "drawing", "", None
            n += 1
        return n

    def set_generated_paths(self, svg_id, shapes_local, thickness=DEFAULT_THICKNESS):
        """Replace the object's generated paths with [(anchors, closed)], anchors
        [(x, y, hin|None, hout|None)] in local mm, every point with the given line thickness."""
        s = self.svgs.get(svg_id)
        if s is None:
            return
        self.set_outline_paths(svg_id, [])
        for anchors, closed in shapes_local:
            if len(anchors) < 2:
                continue
            pts = [ControlPoint(float(a[0]), float(a[1]), float(thickness),
                                tuple(a[2]) if a[2] is not None else None,
                                tuple(a[3]) if a[3] is not None else None) for a in anchors]
            p = VectorPath(id=new_id("path"), svg_id=svg_id, color=s.outline_color, closed=closed,
                           points=pts, generated=True)
            self.paths[p.id] = p
            s.path_ids.append(p.id)
            self.ensure_color(p.color)

    def set_outline_paths(self, svg_id, loops_local_mm):
        """Replace the object's generated paths with closed loops [[(x, y)...]] (local mm)."""
        s = self.svgs.get(svg_id)
        if s is None:
            return
        for pid in [q for q in s.path_ids if q in self.paths and self.paths[q].generated]:
            self._replace_path(self.paths[pid], [])
        sel = self.selection
        sel.path_ids = {q for q in sel.path_ids if q in self.paths}
        sel.segments = {(q, i) for (q, i) in sel.segments if q in self.paths}
        sel.points = {(q, i) for (q, i) in sel.points if q in self.paths}
        sel.tabs = {(q, t) for (q, t) in sel.tabs if q in self.paths}
        for loop in loops_local_mm:
            if len(loop) < 3:
                continue
            p = VectorPath(id=new_id("path"), svg_id=svg_id, color=s.outline_color, closed=True,
                           points=[ControlPoint(float(x), float(y)) for (x, y) in loop], generated=True)
            self.paths[p.id] = p
            s.path_ids.append(p.id)
            self.ensure_color(p.color)

    # -- (de)serialization --
    def to_dict(self):
        return {
            "area_w_mm": self.area_w_mm, "area_h_mm": self.area_h_mm,
            "steps_x": self.steps_x, "steps_y": self.steps_y,
            "bg_jpeg_path": self.bg_jpeg_path,
            "scan_cal": self.scan_cal.to_dict(),
            "color_order": self.color_order, "pause_after": self.pause_after,
            "color_params": {c: {"speed": cp.speed, "pressure": cp.pressure,
                                 "use_thickness_map": cp.use_thickness_map,
                                 "curve": cp.curve, "blade": cp.blade,
                                 "tool": cp.tool, "keep": cp.keep}
                             for c, cp in self.color_params.items()},
            "blade": self.blade.to_dict(),
            "mat_skew": self.mat_skew.to_dict(),
            "scan_image": dict(self.scan_image) if self.scan_image else None,
            "svgs": [self._svg_to_dict(s) for s in self.svgs.values()],
            "paths": [self._path_to_dict(p) for p in self.paths.values()],
        }

    @staticmethod
    def _svg_to_dict(s):
        d = {"id": s.id, "label": s.label, "ox": s.offset_x_mm,
             "oy": s.offset_y_mm, "paths": s.path_ids, "scale": s.scale,
             "visible": s.visible, "ghost": s.ghost}
        if s.rotation:
            d["rot"] = s.rotation
        if s.flip:
            d["flip"] = True
        if s.kind != "svg":  # plain svgs keep the legacy layout
            d.update({"kind": s.kind, "src": s.source_path, "dpi": s.dpi, "pxw": s.px_w,
                      "pxh": s.px_h, "page": s.pdf_page, "ol": s.outline_enabled,
                      "olm": s.outline_margin_mm, "ckm": s.color_key_mode, "ck": s.color_key,
                      "ckt": s.color_key_tol, "olh": s.outline_holes, "olc": s.outline_color})
        if s.kind == "text":
            d.update({"text": s.text, "ts": (s.text_style or TextStyle()).to_dict()})
        return d

    @staticmethod
    def _path_to_dict(p):
        def pt_json(pt):
            row = [pt.x, pt.y, pt.thickness]
            if pt.hin is not None or pt.hout is not None or pt.radius:
                row += [list(pt.hin) if pt.hin is not None else None,
                        list(pt.hout) if pt.hout is not None else None]
            if pt.radius:
                row.append(pt.radius)
            return row
        d = {"id": p.id, "svg_id": p.svg_id, "color": p.color,
             "closed": p.closed, "dash_enabled": p.dash_enabled,
             "dash_on": p.dash_on_mm, "dash_off": p.dash_off_mm,
             "pts": [pt_json(pt) for pt in p.points]}
        if p.generated:
            d["gen"] = True
        if p.tabs:
            d["tabs"] = [{"id": t.id, "seg": t.seg, "pos": t.pos, "gap": t.gap_mm} for t in p.tabs]
        return d

    @staticmethod
    def from_dict(d):
        doc = Document()
        doc.area_w_mm = d.get("area_w_mm", AREA_DEFAULT_MM)
        doc.area_h_mm = d.get("area_h_mm", AREA_DEFAULT_MM)
        doc.steps_x = d.get("steps_x", STEPS_X_DEFAULT)
        doc.steps_y = d.get("steps_y", STEPS_Y_DEFAULT)
        doc.bg_jpeg_path = d.get("bg_jpeg_path", "")
        doc.scan_cal = ScanCal.from_dict(d.get("scan_cal"))
        doc.mat_skew = MatSkew.from_dict(d.get("mat_skew"))
        si = d.get("scan_image")
        doc.scan_image = dict(si) if isinstance(si, dict) and si.get("blob") else None
        # copies, never the caller's lists/dicts: a Document built from a dict (undo restores)
        # must not share mutable state with it
        doc.color_order = list(d.get("color_order", []))
        doc.pause_after = dict(d.get("pause_after", {}))
        for c, cp in d.get("color_params", {}).items():
            doc.color_params[c] = ColorParams(color=c, speed=cp.get("speed"),
                                             pressure=cp.get("pressure"),
                                             use_thickness_map=cp.get("use_thickness_map", False),
                                             curve=[list(p) for p in (DEFAULT_CURVE if cp.get("curve") is None else cp["curve"])],
                                             blade=cp.get("blade", True),
                                             tool=cp.get("tool", "cut") if cp.get("tool") in GROUP_TOOLS
                                             else "cut",
                                             keep=bool(cp.get("keep", False)))
        doc.blade = BladeSettings.from_dict(d.get("blade"))
        for s in d.get("svgs", []):
            doc.svgs[s["id"]] = SvgInstance(id=s["id"], label=s.get("label", "?"),
                                            offset_x_mm=s.get("ox", 0), offset_y_mm=s.get("oy", 0),
                                            path_ids=list(s.get("paths", [])), scale=s.get("scale", 1.0),
                                            rotation=norm_angle(s.get("rot", 0.0)),
                                            flip=bool(s.get("flip", False)),
                                            visible=s.get("visible", True), ghost=s.get("ghost", False),
                                            kind=s.get("kind", "svg"), source_path=s.get("src", ""),
                                            dpi=s.get("dpi", 300.0), px_w=s.get("pxw", 0),
                                            px_h=s.get("pxh", 0), pdf_page=s.get("page", 0),
                                            outline_enabled=s.get("ol", False),
                                            outline_margin_mm=s.get("olm", 0.0),
                                            color_key_mode=s.get("ckm", "none"),
                                            color_key=s.get("ck", "#FFFFFF"),
                                            color_key_tol=s.get("ckt", 32),
                                            outline_holes=s.get("olh", False),
                                            outline_color=s.get("olc", "#000000"),
                                            text=s.get("text", ""),
                                            text_style=TextStyle.from_dict(s["ts"]) if "ts" in s else None)

        def handle(h):
            return (float(h[0]), float(h[1])) if h else None
        for p in d.get("paths", []):
            pts = [ControlPoint(x=pt[0], y=pt[1], thickness=pt[2] if len(pt) > 2 else DEFAULT_THICKNESS,
                                hin=handle(pt[3]) if len(pt) > 3 else None,
                                hout=handle(pt[4]) if len(pt) > 4 else None,
                                radius=float(pt[5]) if len(pt) > 5 and pt[5] else 0.0)
                   for pt in p.get("pts", [])]
            doc.paths[p["id"]] = VectorPath(id=p["id"], svg_id=p.get("svg_id", ""),
                                            color=p.get("color", "#000000"), points=pts,
                                            closed=p.get("closed", False),
                                            dash_enabled=p.get("dash_enabled", False),
                                            dash_on_mm=p.get("dash_on", DEFAULT_DASH_ON_MM),
                                            dash_off_mm=p.get("dash_off", DEFAULT_DASH_OFF_MM),
                                            generated=p.get("gen", False),
                                            tabs=[Tab(id=str(t.get("id") or new_id("tab")),
                                                      seg=int(t.get("seg", 0)),
                                                      pos=float(t.get("pos", 0.5)),
                                                      gap_mm=float(t.get("gap", DEFAULT_TAB_GAP_MM)))
                                                  for t in p.get("tabs", [])])
        return doc

    def restore_dict(self, d):
        """Replace this document's contents with the project dict `d`, IN PLACE: every holder
        of this Document (canvas, panels, cut job) stays valid.  The Selection object keeps its
        identity (its sets are mutated) and is pruned to ids that still exist. Callers that know
        the intended selection set it afterwards (set_selection_state)."""
        new = Document.from_dict(d)    # from_dict copies every list/dict it takes from `d`
        for k, v in new.__dict__.items():
            if k not in ("selection", "file_path", "blobs"):   # undo does not change the file / blobs
                setattr(self, k, v)
        self.set_selection_state(self.selection_state())

    # -- selection snapshots (undo/redo) --
    def selection_state(self):
        """Immutable copy of the selection: (path_ids, segments, points, svg_ids, tabs) frozensets."""
        s = self.selection
        return (frozenset(s.path_ids), frozenset(s.segments), frozenset(s.points),
                frozenset(s.svg_ids), frozenset(s.tabs))

    def set_selection_state(self, state):
        """Select `state` (as returned by selection_state), dropping ids/indices that do not exist.
        A state without the tabs entry (the older 4-tuple) selects no tabs."""
        paths, segs, pts, svgs = state[:4]
        tabs = state[4] if len(state) > 4 else ()
        s = self.selection

        def idx_ok(key):
            p = self.paths.get(key[0])
            return p is not None and 0 <= key[1] < len(p.points)
        s.path_ids.clear()
        s.path_ids.update(p for p in paths if p in self.paths)
        s.segments.clear()
        s.segments.update(k for k in segs if idx_ok(k))
        s.points.clear()
        s.points.update(k for k in pts if idx_ok(k))
        s.svg_ids.clear()
        s.svg_ids.update(v for v in svgs if v in self.svgs)
        s.tabs.clear()
        s.tabs.update(k for k in tabs if k[0] in self.paths and self.paths[k[0]].tab(k[1]) is not None)

    # -- embedded data --
    def add_blob(self, data, ext):
        """Store bytes in the project and return the blob name (content hash + ext)."""
        name = hashlib.sha1(data).hexdigest()[:20] + ext
        self.blobs[name] = bytes(data)
        return name

    def referenced_blobs(self):
        return {self.scan_image["blob"]} if self.scan_image else set()

    def scan_image_bytes(self):
        if not self.scan_image:
            return None
        return self.blobs.get(self.scan_image["blob"])

    def set_scan_image(self, data, ext, dpi, col0=0, row0=0):
        """Embed a scan image and show it (replaces any external bg_jpeg_path)."""
        self.scan_image = {"blob": self.add_blob(data, ext), "dpi": int(dpi),
                           "col0": int(col0), "row0": int(row0)}
        self.bg_jpeg_path = ""

    def save(self, path):
        """Write a .sncproj: project.json (to_dict) + the referenced blobs, atomically."""
        data = json.dumps(self.to_dict(), indent=2).encode("utf-8")
        tmp = path + ".tmp"
        with zipfile.ZipFile(tmp, "w") as z:
            z.writestr(PROJECT_JSON, data, compress_type=zipfile.ZIP_DEFLATED)
            for name in sorted(self.referenced_blobs()):
                if name in self.blobs:     # images are already compressed
                    z.writestr(BLOB_DIR + name, self.blobs[name], compress_type=zipfile.ZIP_STORED)
        os.replace(tmp, path)
        self.file_path = os.path.abspath(path)

    @staticmethod
    def load(path):
        """A .sncproj zip, or a plain JSON project (read as the zip's project.json)."""
        d, blobs = read_project(path)
        doc = Document.from_dict(d)
        doc.blobs = blobs
        doc.file_path = os.path.abspath(path)
        return doc
