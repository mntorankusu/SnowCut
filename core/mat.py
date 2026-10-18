"""Mat profiles: a mat's printed grid and its physical size, and where both land in the cut frame.

A profile is the grid size in its own units (inch or cm cells) plus the margins in mm from each
edge of the grid out to the physical edge of the mat. The built-in presets are the Standard
12x12 and 12x24 mats (ruler: 20 mm left/right, 60 mm top/bottom). Custom profiles are JSON files
in Documents/SnowCut/Mats (MatStore).

Layout (MatProfile.geometry): printed mm become cut-frame mm through the measured grid scale
(MAT_SCALE_X/Y), the same scale the firmware's scan->cut transform puts on the grid.
- X: the side guides register the mat's left edge (core.machine.MAT_X0), so the grid starts one
  left margin in from there.
- Y: the app loads mats with 0xC3/0x54 LOAD-CUSTOM, which moves the firmware's mat-edge reference
  by (60 mm - feed depth) (pc-driver memdump.mat_edge_delta). The point one feed depth behind the
  leading edge therefore lands where a Standard mat's grid top does (core.machine.MatGrid.y0):
  the feed line (initial feed position, the Y origin). (A mat loaded with the machine's own Load
  button gets the stock 60 mm.)
- Feed depth: the top margin, unless that is under MIN_FEED_DEPTH_MM (the mat must be at least
  that far into the machine, and the load also cannot back a mat out after finding its edge). Then
  the fewest whole grid units are added to reach it, so the feed line falls on a grid line and
  the first rows of the grid lie above it (negative Y).
- The head can reach the mat only from MIN_FEED_DEPTH_MM behind its leading edge (travel).

Each profile also names the mat kind the machine is told is loaded (spoof_kind, one of
SPOOF_KINDS). The kind sets the firmware's idea of the mat size, so it also sets the scan
length: a 24" kind scans the full 12x24 (at 200 dpi at most, see scan_size).

MatSkew is the per-document correction for a mat that went in slightly rotated / shifted.
"""
from __future__ import annotations
import json
import math
import os
import re
from dataclasses import dataclass, asdict, fields, replace

from core import machine, userdata

UNITS = {"in": 25.4, "cm": 10.0}
UNIT_LABELS = {"in": "inches", "cm": "centimeters"}
MAJOR_EVERY = {"in": 6, "cm": 5}     # heavier grid line every 6 in / 5 cm
MAX_GRID_MM = 2000.0
MAX_MARGIN_MM = 200.0

# Firmware mat kinds a profile can claim (the descriptor table at 0x88ffb3e8). 6 and 7 are
# service entries. MAT_Y_LEN = the descriptor's Y length in 0.01 mm (the firmware's mat area).
SPOOF_KINDS = {1: "Low Tack 12x12", 2: "Standard 12x12", 3: "Low Tack 12x24",
               4: "Standard 12x24", 5: "Scanning 12x12", 8: "Scanning 12x24"}
MAT_Y_LEN = {1: 29880, 2: 29880, 3: 60360, 4: 60360, 5: 29880, 8: 60360}
MAT_X_LEN = 29667
MIN_FEED_DEPTH_MM = 32.0  # printed mm from the leading edge the mat must be fed in, at least
LONG_SCAN_DPI = 200      # a 24" scan buffer only fits the firmware heap at <= 200 dpi


def default_kind(grid_h_mm):
    return 4 if grid_h_mm > 13 * 25.4 else 2


def scan_size(kind):
    """(width px, lines, dpi) of the full-mat JPEG scan the app requests for a mat kind:
    the stock 12x12 window, or for a 24" kind the area-7 window (0..296.67, 0..603.60 mm)."""
    ylen = MAT_Y_LEN.get(kind, 29880)
    if ylen <= 29880:
        return machine.SCAN_FULL_W_300, machine.SCAN_FULL_H_300, 300
    k = LONG_SCAN_DPI / 2540.0
    return int(MAT_X_LEN * k / 16 + 0.5) << 4, int(ylen * k + 0.5), LONG_SCAN_DPI


@dataclass
class MatProfile:
    name: str = "Standard 12x12"
    units: str = "in"            # grid cell = 1 unit
    grid_w: float = 12.0         # in units
    grid_h: float = 12.0
    margin_left: float = machine.STD_MARGIN_X     # mm, grid edge -> physical mat edge
    margin_right: float = machine.STD_MARGIN_X
    margin_top: float = machine.STD_MARGIN_Y
    margin_bottom: float = machine.STD_MARGIN_Y
    spoof_kind: int = 2          # mat kind the machine is told is loaded (SPOOF_KINDS)

    @property
    def unit_mm(self):
        return UNITS[self.units]

    def grid_mm(self):
        """Printed grid size (w, h) in mm."""
        return self.grid_w * self.unit_mm, self.grid_h * self.unit_mm

    def feed_extra_units(self):
        """Whole grid units added to the top margin to reach MIN_FEED_DEPTH_MM (0 if it already does)."""
        short = MIN_FEED_DEPTH_MM - self.margin_top
        return max(0, int(math.ceil(short / self.unit_mm - 1e-9))) if short > 1e-9 else 0

    def feed_depth_mm(self):
        """Printed mm from the leading edge to the feed line: what the custom load aligns."""
        return self.margin_top + self.feed_extra_units() * self.unit_mm

    def physical_mm(self):
        """Physical mat size (w, h) in mm."""
        gw, gh = self.grid_mm()
        return gw + self.margin_left + self.margin_right, gh + self.margin_top + self.margin_bottom

    def layout_key(self):
        """Everything but the name, for 'is this edited?' checks."""
        return tuple(round(getattr(self, f.name), 6) if f.name not in ("units", "spoof_kind")
                     else getattr(self, f.name) for f in fields(self) if f.name != "name")

    def with_units(self, units):
        """Same physical grid size expressed in other units (rounded to 0.01)."""
        if units == self.units:
            return replace(self)
        f = UNITS[self.units] / UNITS[units]
        return replace(self, units=units, grid_w=round(self.grid_w * f, 2), grid_h=round(self.grid_h * f, 2))

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        """Validated profile from (possibly hand-edited) JSON. Bad fields fall back to defaults."""
        p = MatProfile()
        d = d or {}
        name = str(d.get("name", "") or "").strip()
        p.name = name or "Custom mat"
        if d.get("units") in UNITS:
            p.units = d["units"]
        for key, lo, hi in (("grid_w", 1e-3, MAX_GRID_MM), ("grid_h", 1e-3, MAX_GRID_MM)):
            try:
                v = float(d[key])
                if lo <= v * UNITS[p.units] <= hi:
                    setattr(p, key, v)
            except (KeyError, TypeError, ValueError):
                pass
        for key in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
            try:
                v = float(d[key])
                if 0.0 <= v <= MAX_MARGIN_MM:
                    setattr(p, key, v)
            except (KeyError, TypeError, ValueError):
                pass
        try:
            k = int(d["spoof_kind"])
        except (KeyError, TypeError, ValueError):
            k = None
        p.spoof_kind = k if k in SPOOF_KINDS else default_kind(p.grid_mm()[1])
        return p

    def geometry(self):
        return MatGeometry.of(self)


@dataclass
class MatGeometry:
    """A profile laid out in the cut frame (mm). Rects are (x0, y0, x1, y1)."""
    mat: tuple       # physical mat edges
    grid: machine.MatGrid
    travel: tuple    # where the blade can reach: head X travel x the fed-in part of the mat
    scan: tuple      # (width px, lines, dpi) of the full-mat scan for the profile's mat kind
    feed_y: float = machine.MatGrid.y0   # the feed line (initial feed position, Y origin)

    @staticmethod
    def of(p):
        sx, sy = machine.MAT_SCALE_X, machine.MAT_SCALE_Y
        mx0 = machine.MAT_X0
        feed_y = machine.MatGrid.y0                        # the load puts the feed depth here
        my0 = feed_y - p.feed_depth_mm() * sy
        grid_y0 = feed_y - p.feed_extra_units() * p.unit_mm * sy   # feed line on a grid line
        grid = machine.MatGrid(x0=mx0 + p.margin_left * sx, y0=grid_y0,
                               pitch_x=p.unit_mm * sx, pitch_y=p.unit_mm * sy,
                               cols=p.grid_w, rows=p.grid_h, major=MAJOR_EVERY[p.units])
        gx1, gy1 = grid.rect()[2:]
        mat = (mx0, my0, gx1 + p.margin_right * sx, gy1 + p.margin_bottom * sy)
        ty0 = max(machine.MAT_Y_MIN, my0 + MIN_FEED_DEPTH_MM * sy)
        ty1 = max(ty0, min(machine.MAT_Y_MAX, mat[3]))
        travel = (machine.HEAD_X_MIN, ty0, machine.HEAD_X_MAX, ty1)
        return MatGeometry(mat, grid, travel, scan_size(p.spoof_kind), feed_y)

    def canvas_rect(self):
        """Everything the canvas shows: the physical mat and the head travel area."""
        m, t = self.mat, self.travel
        return (min(m[0], t[0]), min(m[1], t[1]), max(m[2], t[2]), max(m[3], t[3]))


PRESETS = (MatProfile("Standard 12x12", "in", 12.0, 12.0, spoof_kind=2),
           MatProfile("Standard 12x24", "in", 12.0, 24.0, spoof_kind=4))
DEFAULT_NAME = PRESETS[0].name


def preset(name):
    for p in PRESETS:
        if p.name == name:
            return replace(p)
    return None


def is_preset(name):
    return preset(name) is not None


def _slug(name):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return s[:80] or "mat"


class MatStore:
    """Built-in presets plus the custom profiles in a folder (default Documents/SnowCut/Mats),
    one JSON file each. The name inside the file is authoritative, and file names are only slugs."""

    def __init__(self, folder=None):
        self._folder = folder

    @property
    def folder(self):
        if self._folder is None:
            self._folder = userdata.user_dir("Mats")
        os.makedirs(self._folder, exist_ok=True)
        return self._folder

    def _load_files(self):
        """{name: (path, profile)} for the custom profiles on disk (presets' names are reserved)."""
        out = {}
        try:
            names = sorted(os.listdir(self.folder))
        except OSError:
            return out
        for fn in names:
            if not fn.lower().endswith(".json"):
                continue
            path = os.path.join(self.folder, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    p = MatProfile.from_dict(json.load(f))
            except (OSError, ValueError):
                continue
            if not is_preset(p.name) and p.name not in out:
                out[p.name] = (path, p)
        return out

    def custom(self):
        return [p for _path, p in sorted(self._load_files().values(), key=lambda t: t[1].name.lower())]

    def names(self):
        return [p.name for p in PRESETS] + [p.name for p in self.custom()]

    def get(self, name):
        p = preset(name)
        if p is not None:
            return p
        hit = self._load_files().get(name)
        return replace(hit[1]) if hit else None

    def save(self, profile):
        """Write a custom profile (overwrites the one with the same name). Returns its path."""
        name = profile.name.strip()
        if not name:
            raise ValueError("A mat profile needs a name.")
        if is_preset(name):
            raise ValueError('"%s" is a built-in preset. Save it under another name.' % name)
        hit = self._load_files().get(name)
        if hit:
            path = hit[0]
        else:
            base = os.path.join(self.folder, _slug(name))
            path, n = base + ".json", 2
            while os.path.exists(path):
                path, n = "%s_%d.json" % (base, n), n + 1
        data = replace(profile, name=name).to_dict()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return path

    def delete(self, name):
        hit = self._load_files().get(name)
        if hit:
            os.remove(hit[0])
            return True
        return False


MAX_SKEW_DEG = 2.0       # more than this and the mat feeds badly anyway
MAX_SKEW_MM = 20.0


@dataclass
class MatSkew:
    """Correction for a mat that sits slightly rotated / shifted in the machine.

    It is defined as what lines the SCAN up with the drawing: the scan image is shown rotated by
    angle_deg about the cut origin (0, 0) (positive = clockwise on screen, Y points down), then
    shifted by (dx, dy) mm. Cuts go the opposite way (to_machine), so a design drawn on the
    straightened scan lands on the physical mat where it was drawn."""
    angle_deg: float = 0.0
    dx: float = 0.0
    dy: float = 0.0

    def is_identity(self):
        return abs(self.angle_deg) < 1e-9 and abs(self.dx) < 1e-9 and abs(self.dy) < 1e-9

    def image_transform(self):
        """(m11, m12, m21, m22, tx, ty) in QTransform order: machine mm -> canvas mm."""
        a = math.radians(self.angle_deg)
        c, s = math.cos(a), math.sin(a)
        return c, s, -s, c, self.dx, self.dy

    def to_canvas(self, x, y):
        m11, m12, m21, m22, tx, ty = self.image_transform()
        return m11 * x + m21 * y + tx, m12 * x + m22 * y + ty

    def to_machine(self, x, y):
        """Canvas (design) mm -> machine mm: the inverse of image_transform."""
        if self.is_identity():
            return x, y
        a = math.radians(self.angle_deg)
        c, s = math.cos(a), math.sin(a)
        u, v = x - self.dx, y - self.dy
        return c * u + s * v, -s * u + c * v

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        k = MatSkew()
        for key, lim in (("angle_deg", MAX_SKEW_DEG), ("dx", MAX_SKEW_MM), ("dy", MAX_SKEW_MM)):
            try:
                setattr(k, key, max(-lim, min(lim, float((d or {})[key]))))
            except (KeyError, TypeError, ValueError):
                pass
        return k
