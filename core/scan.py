"""Scan settings and window geometry (no USB).

A scan is the firmware's ScanToUSB request (mode 0, builder FUN_88dc5964) with a custom area
(area 7) at one of the ScanToUSB resolutions. Windows here are rectangles (x0, x1, y0, y1) in
0.01 mm of the stock full-mat scan frame (image mm: column 0 / row 0 = the first pixel of the
machine's own 12x12 scan). The machine's scan->cut transform places that frame on the canvas
(core.machine.scan_pixel_transform).

What the scanner can reach (scan_limits). Every edge has a physical reason:
- X: the firmware's shading-calibrated window, 0..296.67 mm. With the full-sensor option (vendor
  firmware 0xC3/0x55 bit1) the whole CIS instead: 3638 px at 300 dpi = -5.76..302.26 mm.
- Y top: a window may start above row 0: the mat is then started that much less fed in
  (memdump.scan_start_y_shift). It can back off only until its leading edge is EDGE_MARGIN past
  the point where the load found it at the mat sensor (Y counter c44), the furthest back the
  load itself takes the mat. Behind that, the edge would leave the feed rollers. c44 follows
  the load's feed depth (matedge_units).
- Y bottom: the mat's trailing edge (nothing to scan past it). The rollers sit by the mat
  sensor, ~26 mm deeper in than the scan line, so the mat is still held there.
- Size: the firmware allocates width * 3 * (lines + 1000) bytes in heap 3, so large windows
  need a lower dpi (max_dpi).

scan_request() turns a window into the firmware request: the builder's rectangle (its arithmetic
in window_pixels), whether the full sensor is needed, how far the mat starts backed off, and
where the returned pixels sit in the frame (col0, row0, where negative = left of / above it).

The user edits a box in the mat's grid units, counted from the grid's top-left corner. It is
kept in printed mm from that corner (ScanSettings.box), so it survives a units change.
box_to_window() maps it canvas -> machine (undo the mat skew) -> image mm (inverse ScanCal)
and takes the bounding rectangle. scannable_box() is the inverse view of the limits.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, asdict

from core import machine
from core import mat as matlib

DPIS = (300, 200, 150, 100)                       # ScanToUSB resolution index 0..3
STOCK_SKIP = {300: 35, 200: 23, 150: 17, 100: 11}  # lines the scan skips above row 0
FULL_WIDTH = {300: 3504, 200: 2336, 150: 1744, 100: 1168}  # widest valid window (px)
HEAP_BYTES = 70 * 1024 * 1024                     # usable part of heap 3 (74.4 MB)
FORMATS = ("jpeg", "png")
DESTS = ("canvas", "file", "both")
STOCK_QUALITY = 95                                # the machine's own JPEG tables
SAMPLING_444 = 3      # encoder sampling mode: 3 = colour, 8x8 MCUs (format reg 0x80), 1 = grey

# Sensor (300-dpi hardware pixels). The stock window reads 66..3569 (exec_correct_valid_pixel
# 3504 from the halved CIS start term). The image runs opposite to the pixel index, so frame
# column c is hardware pixel STOCK_HW_END - 1 - c. The full sensor is 0..3637.
STOCK_VALID_PX, STOCK_HW_START, STOCK_HW_END = 3504, 66, 3570
WIDE_SENSOR_PX = 3638
STOCK_X_LEN = matlib.MAT_X_LEN                    # 29667: 3504 px
WIDE_X = (-575, 30226)        # 0.01 mm: frame columns -68..3569 (hardware 3637..0), inward
WIDE_SHIFT = 576              # builder X 0 on the full sensor = frame X -5.76 mm (68 px)

# Mat Y (Y-counter units: 4 per motor step, 0.01421 mm, and a scan feeds 1800/dpi per line).
STOCK_SCAN_START_Y = -212     # c48: scans start and the mat parks here (-3.00 mm)
STOCK_MATEDGE_Y = -2464       # c44 after the machine's own Load: edge at the sensor (-35.00 mm)
EDGE_MARGIN_UNITS = 72        # ~1 mm: keep the leading edge past where the load found it


def px_per_mm(dpi):
    return dpi / 25.4


def window_pixels(rect, dpi):
    """(width, lines, offset, row0) the mode-0 builder makes of its rectangle rect = (x0, x1,
    y0, y1) in 0.01 mm: width rounded to 16 px, X offset centred on the rounding (never
    negative), skip lines = (y0 + 3 mm) * dpi but at least the stock count, and row0 = extra skipped
    lines. Where the columns land in the frame: scan_request."""
    x0, x1, y0, y1 = rect
    k = px_per_mm(dpi)
    dx = (x1 - x0) * 0.01 * k
    width = int(dx / 16 + 0.5) << 4
    off = (int(dx + 0.5) - width) / 2.0 + x0 * 0.01 * k + 0.5
    col0 = int(off) if off > 0 else 0
    lines = int((y1 - y0) * 0.01 * k + 0.5)
    d = (y0 + 300) * 0.01 * k + 0.5
    stock = STOCK_SKIP[dpi]
    skip = stock if d <= stock else int(d)
    return width, lines, col0, skip - stock


def _frame_col0(off, width, dpi, wide):
    """Frame column of the image's first pixel for a builder X offset / width: the driver
    clamps offset + width to the valid pixels, sets the hardware start to (term + valid - width -
    offset) rounded down to even, and the image runs opposite to the hardware pixels."""
    s = 300.0 / dpi
    valid, term = (WIDE_SENSOR_PX, 0) if wide else (STOCK_VALID_PX, STOCK_HW_START)
    out_in = width * s
    off_in = max(0.0, min(off * s, valid - out_in))
    offm = int(valid - out_in - off_in) // 2 * 2
    return int(round((STOCK_HW_END - term - offm - out_in) / s))


def _lines_rect_y1(y0, lines, dpi):
    """Smallest builder y1 for which a rectangle from y0 gives `lines` lines."""
    k = px_per_mm(dpi)
    y1 = y0 + int(round(lines / k * 100))
    while int((y1 - y0) * 0.01 * k + 0.5) > lines:
        y1 -= 1
    while int((y1 - y0) * 0.01 * k + 0.5) < lines:
        y1 += 1
    return y1


def scan_request(window, dpi, back_max=None):
    """How the firmware returns the frame window (x0, x1, y0, y1) at `dpi`, as a dict:
    rect   the builder's area-7 rectangle (0.01 mm)
    wide   True when the window needs the full sensor (it reaches outside 0..296.67 mm)
    back   Y units the mat starts less fed in (0 unless the window starts above row 0): whole
           lines, a whole number of motor steps, at most back_max (the top row then drops)
    width, lines, col0, row0   the image and its first pixel in the frame."""
    x0, x1, y0, y1 = window
    wide = x0 < 0 or x1 > STOCK_X_LEN
    shift = WIDE_SHIFT if wide else 0
    rx0, rx1 = x0 + shift, x1 + shift
    n = 0
    per = 1800 // dpi
    if y0 < 0:
        step = 4 // math.gcd(per, 4)                 # lines per whole motor step
        n = int(math.ceil(-y0 * 0.01 * px_per_mm(dpi) - 1e-9))
        n = -(-n // step) * step
        if back_max is not None:
            n = max(0, min(n, int(back_max) // per // step * step))
    ry0 = max(0, y0)
    width, lines, off, r0 = window_pixels((rx0, rx1, ry0, y1), dpi)
    ry1 = y1
    if n:
        lines += n
        ry1 = _lines_rect_y1(ry0, lines, dpi)
    return {"rect": (rx0, rx1, ry0, ry1), "wide": wide, "back": n * per, "width": width,
            "lines": lines, "col0": _frame_col0(off, width, dpi, wide), "row0": r0 - n}


def buffer_bytes(width, lines):
    """What the firmware allocates for the scan (JPEG or raw): width * 3 * (lines + 1000)."""
    return width * 3 * (lines + 1000)


def max_dpi(window, back_max=None):
    """Highest dpi whose buffer fits the firmware heap for this window."""
    for dpi in DPIS:
        r = scan_request(window, dpi, back_max)
        if buffer_bytes(r["width"], r["lines"]) <= HEAP_BYTES:
            return dpi
    return None


def kind_window(kind):
    """The firmware's own full window for a mat kind (x0, x1, y0, y1) in 0.01 mm."""
    return (0, matlib.MAT_X_LEN, 0, matlib.MAT_Y_LEN.get(kind, 29880))


def matedge_units(feed_depth_mm, scale_y=None):
    """Y counter where a load left the mat's leading edge at the sensor (c44): the stock
    -35.00 mm moved by (60 mm - feed depth), exactly as memdump.mat_edge_delta + 0xC3/0x54.
    The machine's own Load is a 60 mm feed depth."""
    sy = machine.MAT_SCALE_Y if scale_y is None else scale_y
    mm = (machine.STD_MARGIN_Y - feed_depth_mm) * sy
    return STOCK_MATEDGE_Y + 4 * int(round(mm * 100 * 10000 / machine.FACTOR_Y))


def back_max_units(matedge_y):
    """How far (Y units) a scan may start behind the stock start for a mat edge at matedge_y."""
    return max(0, STOCK_SCAN_START_Y - matedge_y - EDGE_MARGIN_UNITS)


def _inverse(cal):
    a, b, c, d, tx, ty = cal.matrix()
    det = a * d - b * c

    def inv(x, y):
        x, y = x - tx, y - ty
        return (d * x - b * y) / det, (-c * x + a * y) / det
    return inv


def image_to_canvas(cal, skew, u, v):
    """Image mm (scanner frame) -> canvas mm."""
    a, b, c, d, tx, ty = cal.matrix()
    return skew.to_canvas(a * u + b * v + tx, c * u + d * v + ty)


def canvas_to_image(cal, skew, x, y):
    return _inverse(cal)(*skew.to_machine(x, y))


def scan_limits(geometry, cal, skew, wide, matedge_y):
    """The frame rectangle (x0, x1, y0, y1) in 0.01 mm the scanner can reach (module doc):
    geometry = the mat profile's MatGeometry (its trailing edge), wide = full sensor allowed,
    matedge_y = Y counter of the mat edge at the sensor (matedge_units)."""
    x0, x1 = WIDE_X if wide else (0, STOCK_X_LEN)
    y0 = -int(back_max_units(matedge_y) * 2540 / 1800)
    mx0, _my0, mx1, my1 = geometry.mat
    y1 = int(max(canvas_to_image(cal, skew, x, my1)[1] for x in (mx0, mx1)) * 100)
    return (x0, x1, y0, max(y0 + 100, y1))


def box_to_window(box_canvas, cal, skew, limits):
    """Canvas-mm box (x0, y0, x1, y1) -> frame window (x0, x1, y0, y1) in 0.01 mm clamped to
    `limits` (scan_limits), or None if nothing of it is scannable."""
    bx0, by0, bx1, by1 = box_canvas
    pts = [canvas_to_image(cal, skew, x, y) for x, y in ((bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1))]
    lo_x, hi_x, lo_y, hi_y = limits
    x0 = max(lo_x, int(round(min(p[0] for p in pts) * 100)))
    x1 = min(hi_x, int(round(max(p[0] for p in pts) * 100)))
    y0 = max(lo_y, int(round(min(p[1] for p in pts) * 100)))
    y1 = min(hi_y, int(round(max(p[1] for p in pts) * 100)))
    if x1 - x0 < 100 or y1 - y0 < 100:          # under 1 mm: nothing to scan
        return None
    return (x0, x1, y0, y1)


def window_outline(window, dpi, cal, skew, back_max=None):
    """Canvas corners (tl, tr, br, bl) of the pixels a window actually returns."""
    r = scan_request(window, dpi, back_max)
    return pixels_outline(r["width"], r["lines"], r["col0"], r["row0"], dpi, cal, skew)


def pixels_outline(w, h, c0, r0, dpi, cal, skew):
    """Canvas corners (tl, tr, br, bl) of a w x h image whose first pixel is (c0, r0) of the
    stock full-mat frame."""
    m11, m12, m21, m22, dx, dy = machine.scan_pixel_transform(cal, dpi)
    out = []
    for c, r in ((c0, r0), (c0 + w, r0), (c0 + w, r0 + h), (c0, r0 + h)):
        out.append(skew.to_canvas(m11 * c + m21 * r + dx, m12 * c + m22 * r + dy))
    return out


def scannable_box(cal, skew, limits):
    """Largest axis-aligned canvas box inside the limits (scan_limits)."""
    x0, x1, y0, y1 = limits
    c = [image_to_canvas(cal, skew, u / 100.0, v / 100.0)
         for u, v in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    return (max(c[0][0], c[3][0]), max(c[0][1], c[1][1]),
            min(c[1][0], c[2][0]), min(c[2][1], c[3][1]))


# The CLI's full-mat wide scan (memdump.wide_window, which this mirrors): the whole sensor,
# centred, and WIDE_MARGIN_300 extra lines above and below the kind's window. Kept for files
# saved from it (wide_origin) and as a cross-check of the two implementations.
WIDE_MARGIN_300 = 64


def wide_margin_lines(dpi):
    """(lines, Y units) above and below a CLI wide scan: about the X margin, rounded so the mat
    move (1800/dpi units per line) is whole motor steps (a multiple of 4 units)."""
    per = 1800 // dpi
    want = WIDE_MARGIN_300 * dpi / 300.0
    n = min((m for m in range(max(1, int(want) - 4), int(want) + 6) if m * per % 4 == 0),
            key=lambda m: (abs(m - want), m))
    return n, n * per


def wide_geometry(kind, dpi):
    """The CLI wide scan of the mat kind's window at `dpi`: dict rect (area-7 rectangle,
    0.01 mm), width, lines, col0, row0 (first pixel in the frame, negative) and back."""
    y0, y1 = 0, kind_window(kind)[3]
    k = px_per_mm(dpi)
    width = int(WIDE_SENSOR_PX * dpi / 300.0) // 16 * 16
    in_w = width * 300.0 / dpi
    hw_start = int(WIDE_SENSOR_PX - 2 - in_w) // 2
    off_in = WIDE_SENSOR_PX - in_w - hw_start
    x0 = int(round(off_in * 2540.0 / 300))
    x1 = x0 + int(round(width * 2540.0 / dpi))
    n, back = wide_margin_lines(dpi)
    lines = int((y1 - y0) * 0.01 * k + 0.5) + 2 * n
    ny1 = _lines_rect_y1(y0, lines, dpi)
    _w, _h, _c, r0 = window_pixels((x0, x1, y0, ny1), dpi)
    return {"rect": (x0, x1, y0, ny1), "width": width, "lines": lines,
            "col0": int(round((hw_start - STOCK_HW_START) * dpi / 300.0)), "row0": r0 - n,
            "back": back}


def wide_origin(width, dpi):
    """(col0, row0) of an image file that is a CLI wide scan (by its width), else (0, 0). The
    margins do not depend on the mat kind."""
    if width == machine.wide_scan_width(dpi):
        g = wide_geometry(2, dpi)
        return g["col0"], g["row0"]
    return 0, 0


@dataclass
class ScanSettings:
    box: tuple = None           # (x, y, w, h) printed mm from the grid's top-left, None = all
    dpi: int = 300
    fmt: str = "jpeg"           # "jpeg" | "png" (raw scan, saved losslessly)
    quality: int = STOCK_QUALITY
    subsampling: bool = True    # JPEG 4:2:0 (the machine's own), off = 4:4:4
    dest: str = "canvas"        # "canvas" | "file" | "both"
    path: str = ""              # file to save to ("" = timestamped name in Documents/SnowCut/Scans)
    wide: bool = False          # allow the sensor's full width (vendor firmware 0x55 bit1)

    def to_dict(self):
        d = asdict(self)
        d["box"] = list(self.box) if self.box else None
        return d

    @staticmethod
    def from_dict(d):
        s = ScanSettings()
        d = d or {}
        try:
            b = d.get("box")
            if b and len(b) == 4 and float(b[2]) > 0 and float(b[3]) > 0:
                s.box = tuple(float(v) for v in b)
        except (TypeError, ValueError):
            pass
        if d.get("dpi") in DPIS:
            s.dpi = d["dpi"]
        if d.get("fmt") in FORMATS:
            s.fmt = d["fmt"]
        try:
            s.quality = max(1, min(100, int(d.get("quality", s.quality))))
        except (TypeError, ValueError):
            pass
        s.subsampling = bool(d.get("subsampling", True))
        if d.get("dest") in DESTS:
            s.dest = d["dest"]
        s.path = str(d.get("path") or "")
        s.wide = bool(d.get("wide", False))
        return s

    def sampling_mode(self):
        """Firmware JPEG sampling mode for 0xC3/0x55: 0 = stock (mode 4, 4:2:0), 3 = 4:4:4.
        (The encoder's modes: 1 greyscale, 2/4/5 subsampled colour, 3 colour with 8x8 MCUs.)"""
        return 0 if self.subsampling else SAMPLING_444


def box_canvas(settings_box, geometry):
    """Printed-mm box from the grid corner -> canvas mm (x0, y0, x1, y1)."""
    g = geometry.grid
    sx, sy = machine.MAT_SCALE_X, machine.MAT_SCALE_Y
    x, y, w, h = settings_box
    return (g.x0 + x * sx, g.y0 + y * sy, g.x0 + (x + w) * sx, g.y0 + (y + h) * sy)


def box_from_canvas(rect_canvas, geometry):
    """Canvas mm (x0, y0, x1, y1) -> printed-mm box (x, y, w, h) from the grid corner."""
    g = geometry.grid
    sx, sy = machine.MAT_SCALE_X, machine.MAT_SCALE_Y
    x0, y0, x1, y1 = rect_canvas
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    return ((x0 - g.x0) / sx, (y0 - g.y0) / sy, (x1 - x0) / sx, (y1 - y0) / sy)


def clamp_box(settings_box, geometry, cal, skew, kind):
    """The box clipped to the scannable area (printed mm from the grid corner)."""
    sx0, sy0, sx1, sy1 = scannable_box(cal, skew, kind)
    if settings_box is None:
        return box_from_canvas((sx0, sy0, sx1, sy1), geometry)
    x0, y0, x1, y1 = box_canvas(settings_box, geometry)
    x0, x1 = max(x0, sx0), min(x1, sx1)
    y0, y1 = max(y0, sy0), min(y1, sy1)
    if x1 - x0 < 1 or y1 - y0 < 1:
        return box_from_canvas((sx0, sy0, sx1, sy1), geometry)
    return box_from_canvas((x0, y0, x1, y1), geometry)


# --- output formats ---
def raw_to_rgb(raw, width, lines, offsets):
    """The scan engine's raw output (8-bit R, G, B) -> (lines, width, 3) uint8 array.
    offsets = byte offsets of R, G, B from the scan-end message. Offsets 0/1/2 apart mean
    interleaved RGB. Otherwise, each channel is a plane (row stride = plane spacing / lines,
    normally the width)."""
    import numpy as np
    buf = np.frombuffer(raw, dtype=np.uint8)
    lo = min(offsets)
    rel = [o - lo for o in offsets]
    if max(rel) <= 2:                                    # interleaved
        n = width * lines * 3
        px = buf[lo:lo + n]
        lines = min(lines, len(px) // (width * 3))
        px = px[:width * lines * 3].reshape(lines, width, 3)
        return np.ascontiguousarray(px[:, :, rel])
    spacing = sorted(rel)[1] - sorted(rel)[0]
    stride = max(width, spacing // max(1, lines))
    planes = []
    for o in offsets:
        p = buf[o:o + stride * lines]
        h = min(lines, len(p) // stride)
        planes.append(p[:stride * h].reshape(h, stride)[:, :width])
    h = min(p.shape[0] for p in planes)
    return np.ascontiguousarray(np.stack([p[:h] for p in planes], axis=2))


def rgb_to_png(rgb, dpi):
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(out, "PNG", dpi=(dpi, dpi), compress_level=6)
    return out.getvalue()


def jpeg_info(data):
    """(width, height, (H, V) sampling of the first component) from a JPEG's SOF, or None."""
    import struct
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        m = data[i + 1]
        n = struct.unpack(">H", data[i + 2:i + 4])[0]
        if m in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            s = data[i + 11]
            return w, h, (s >> 4, s & 15)
        if m == 0xDA:
            break
        i += 2 + n
    return None
