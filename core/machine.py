"""CM110 machine geometry in the firmware's mat frame (mm), from COORDINATE_MAPPING.md.

The document's mm coordinates are the firmware's mat frame. ops_to_steps sends head = -X and
mat = +Y as absolute moves. So this module only describes where everything is located in that frame:

- the head's X travel (firmware clamp FUN_88db8f78) and the canvas built from it,
- the mat's printed 12x12 grid, as measured from a scan of a Standard 12x12 mat, and where the
  physical mat sits (core.mat builds any mat profile's layout from these),
- and the scanner footprint and the fixed scan->cut transform FUN_88d26e40 builds from the
  scan->cut adjustment values in EEPROM 0x110-0x11c (RAM 0x88012fc8).
"""
from __future__ import annotations
import math
import struct
from dataclasses import dataclass, asdict

FACTOR_X, FACTOR_Y = 56506, 56844        # FUN_88dbc770 factors (0.01 mm -> pre-x4 steps)
SCAN_K = 127899 / 127000                 # 6 Y-steps per 300 dpi line / (1/300 in)
SCAN_FULL_W_300 = 3504                   # corrected CIS width at 300 dpi (296.67 mm)
SCAN_FULL_H_300 = 3529                   # lines of a 12x12 scan (request [0x17])
SCAN_CAL_ADDR = 0x88012fc8               # 4 big-endian doubles: angle, x, y, xscale


def _fw_mm(v_001mm, factor):
    """Round-trip a firmware 0.01 mm constant through its step quantization."""
    return round(v_001mm * 10000 / factor) * factor / 10000 / 100.0


# Head travel in mat X (head coordinate = -X): FUN_88db8f78 max +1500, min -31000.
HEAD_X_MIN = -_fw_mm(1500, FACTOR_X)     # -14.97
HEAD_X_MAX = -_fw_mm(-31000, FACTOR_X)   # 309.99
# Mat feed limits in Y (lcs_MoveTarget clamp, mat = +Y).
MAT_Y_MIN = _fw_mm(-7000, FACTOR_Y)      # -70.00
MAT_Y_MAX = _fw_mm(65000, FACTOR_Y)      # 650.00


@dataclass
class ScanCal:
    """Scan->cut adjustment: skew angle (rad), offsets (mm), CIS X scale. Defaults = this unit's EEPROM."""
    angle: float = -3.1367861083708704e-4
    x_mm: float = 0.9313334226608276
    y_mm: float = 1.235703468322754
    xscale: float = 1.0003103017807007

    def validated(self):
        """Apply the firmware's own load checks (FUN_88d2756c)."""
        return ScanCal(angle=self.angle if abs(self.angle) <= 0.35 else 0.0,
                       x_mm=self.x_mm if abs(self.x_mm) <= 3.0 else 0.0,
                       y_mm=self.y_mm if abs(self.y_mm) <= 3.0 else 0.0,
                       xscale=self.xscale if abs(self.xscale - 1.0) <= 0.0025 else 1.0)

    @staticmethod
    def from_ram(raw32):
        """From the 32 bytes at SCAN_CAL_ADDR (read with the 0xC0 RAM dump)."""
        return ScanCal(*struct.unpack(">4d", raw32[:32])).validated()

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return ScanCal(**d).validated() if d else ScanCal()

    def matrix(self):
        """Firmware matrix (FUN_88d26e40) in mm: X = a*u + b*v + tx, Y = c*u + d*v + ty,
        where u, v are image coordinates in mm at the nominal pixel pitch."""
        a = math.cos(self.angle) * self.xscale
        c = -math.tan(self.angle) * SCAN_K
        return a, 0.0, c, SCAN_K, self.x_mm * self.xscale, self.y_mm


def wide_scan_width(dpi):
    """Width of a wide (full sensor) scan: 3638 CIS pixels at 300 dpi, scaled, rounded down to a
    multiple of 16 (core.scan.wide_geometry): 3632 / 2416 / 1808 / 1200."""
    return int(3638 * dpi / 300.0) // 16 * 16


def image_dpi(w_px, h_px=None, info_dpi=None):
    """Resolution of a scan image. Full-width scans identify themselves by width
    (3504 px = 300 dpi, scaled for 600/200/150/100). The scan engine's JPEG widths are
    rounded down to a multiple of 16 (150 dpi -> 1744, not 1752), with X offset 0, so a
    width up to 16 px short still means that dpi. Wide scans have their own widths
    (wide_scan_width). Otherwise use the file's DPI, else 300."""
    for dpi in (300, 600, 200, 150, 100):
        if -16 < w_px - SCAN_FULL_W_300 * dpi / 300.0 <= 1:
            return dpi
    for dpi in (300, 200, 150, 100):
        if w_px == wide_scan_width(dpi):
            return dpi
    if info_dpi:
        return float(info_dpi)
    return 300


def scan_pixel_transform(cal, dpi):
    """(m11, m12, m21, m22, dx, dy) mapping image pixel-corner coordinates (col, row) to mat mm,
    in QTransform argument order. Assumes the image starts at the scan window's origin, which
    holds for the firmware's full-width scans. The firmware's half-pixel term
    (FUN_88d27050) converts pixel centres, so it drops out for pixel corners."""
    a, b, c, d, tx, ty = cal.matrix()
    p = 25.4 / dpi
    return a * p, c * p, b * p, d * p, tx, ty


def map_pixel(cal, dpi, col, row):
    m11, m12, m21, m22, dx, dy = scan_pixel_transform(cal, dpi)
    return m11 * col + m21 * row + dx, m12 * col + m22 * row + dy


def scan_footprint(cal, w_px=SCAN_FULL_W_300, h_px=SCAN_FULL_H_300, dpi=300):
    """Corners (tl, tr, br, bl) in mat mm of a scan image of the given size."""
    return [map_pixel(cal, dpi, c, r) for c, r in ((0, 0), (w_px, 0), (w_px, h_px), (0, h_px))]


@dataclass
class MatGrid:
    """A mat's printed grid in the cut frame. The defaults are the Standard 12x12 mat: a
    least-squares fit of the 11 inner 1-inch lines in testscan_12x12.jpg mapped through ScanCal().
    Residuals are 0.03 mm in X and up to 0.19 mm in Y. The scan's first rows feed slightly long.
    The Y pitch is 0.6% larger than X because of the firmware's fixed SCAN_K, so this is where
    the machine will actually cut. Other grids (core.mat) reuse that scale.
    cols/rows may be fractional (the last cell is partial), and every major-th line is heavier."""
    x0: float = -3.064
    y0: float = -1.533
    pitch_x: float = 25.3857
    pitch_y: float = 25.5427
    cols: float = 12
    rows: float = 12
    major: int = 6

    def rect(self):
        return (self.x0, self.y0, self.x0 + self.cols * self.pitch_x, self.y0 + self.rows * self.pitch_y)

    @staticmethod
    def _inner(n):
        return [k for k in range(1, int(math.ceil(n))) if k < n - 1e-6]

    def lines(self):
        """Inner grid lines: ([(x, is_major)], [(y, is_major)])."""
        maj = lambda k: self.major > 0 and k % self.major == 0
        return ([(self.x0 + k * self.pitch_x, maj(k)) for k in self._inner(self.cols)],
                [(self.y0 + k * self.pitch_y, maj(k)) for k in self._inner(self.rows)])


# Scale from printed-mat mm to cut-frame mm, from the measured grid pitch (Y carries SCAN_K).
MAT_SCALE_X = MatGrid.pitch_x / 25.4
MAT_SCALE_Y = MatGrid.pitch_y / 25.4
# Ruler measurement of a Standard mat: printed grid -> physical mat edge.
STD_MARGIN_X = 20.0                      # left and right
STD_MARGIN_Y = 60.0                      # top and bottom
# The machine registers the mat by its physical edges (side guides, mat edge sensor), so the
# physical top-left corner is fixed in the cut frame and the grid moves with a profile's margins.
MAT_X0 = MatGrid.x0 - STD_MARGIN_X * MAT_SCALE_X
MAT_Y0 = MatGrid.y0 - STD_MARGIN_Y * MAT_SCALE_Y
