"""QGraphicsView canvas: JPEG bg + vector overlay, tools, marquee selection.

Tools: mouse | box | zoom | hand | transform | bend | line | freehand | curve | polygon | rect | ellipse
(space = temporary hand pan). The five drawing tools live in ui/draw_tools.py (DrawMixin).
- Mouse: click selects (Alt+click = whole SVG). Drag inside bbox moves the
  SVG only when a single entire SVG is selected. Otherwise a drag does nothing,
  unless the box multi-tool toggle is on (drag elsewhere = rubber band).
  Dragging a control point (or a selected line) moves the selected anchors and
  their bezier handles, and dragging a bezier handle reshapes the curve (Alt = no
  mirroring of the opposite handle). Arrow keys nudge the selection by
  nudge_mm (Shift = x10), Delete/Backspace delete it, and right-click opens the
  break apart / combine / straighten / delete menu (mouse, box, transform, bend).
- Bend: press on any line and drag. The pressed point follows the cursor
  (straight lines become curves) and the segment's handles are pulled toward the
  cursor by bend_strength (0 = an even bend, 1 = handles on the cursor). With bend_focus a
  press near one end moves mostly that end's handle (only it beyond 75% along the segment).
- Box: drag selects intersected points/segments/paths per box_mode, combined
  per select_mode (new/add/subtract/intersect).
- Zoom: slider + drag-box to zoom (leaves margin so panning has room). zoom_to_mouse (global,
  Zoom tool options) makes the wheel and click-zoom keep the point under the pointer in place.
  When it is off, they zoom about the view centre.
- Hand: manual scrollbar panning (deterministic, and middle-drag and Space work
  in every tool). Wheel always zooms. Panning works at any zoom and may push
  the mat partly off screen, but the scroll bounds (_update_pan_bounds) stop it
  while PAN_KEEP_PX of the mat is still visible. Scrollbars are hidden.
- Transform: drag inside bbox moves every touched SVG, and handles resize the
  single entire-SVG selection about the cycled anchor (opposite/center/topleft).

Machine geometry (core.machine, core.mat): the canvas shows the active mat profile laid out in
the cut frame: the physical mat, the head travel / cut area, the mat's printed grid and the
feed line. A scan image is placed by the firmware's scan->cut transform
(doc.scan_cal), so it covers exactly the area the machine scanned, and then by the mat skew
correction (doc.mat_skew), which straightens a mat that went in rotated / shifted. The cut
output applies the inverse (core.gcode), so drawings line up with the physical mat. The scan is
the image embedded in the project (doc.scan_image: blob, dpi and its pixel offset in the
full-mat scan frame), else the external file of older projects (doc.bg_jpeg_path).

Scan box (set_scan_overlay / set_scan_box_mode): while the Scan panel is open the canvas shows
the scannable area, the requested box and the pixels that box really returns. Drawing a new box
is a tool of its own (SCANBOX_TOOL): it takes over from the current tool, a left drag emits
scan_box_drawn (canvas mm), and then (or on Esc) the previous tool comes back. Choosing any other
tool cancels it.

Toolpath preview (set_toolpath_preview): non-interactive overlay of a generated cut job:
travel moves, the holder-axis path (incl. swivel overshoots/pivots/lead-in) and the simulated
blade tip colored by its distance from the drawing (core.blade.Preview).

Selection display: 1 px half-transparent marching ants on selected runs (the lines stay visible
underneath), control points only for the selected part (path=all pts, segment=endpoints only,
point=that point, svg-level selection shows no points), a dashed bbox drawn BBOX_PAD_PX outside
the artwork, and resize handles when a single entire SVG is selected and resizing is enabled.
Points and handles are a constant size on screen, and the scene is rebuilt once zooming settles.
"""
import bisect
import math
import os
from collections import OrderedDict

from PySide6 import QtWidgets, QtGui, QtCore
from core.doc import combine_selection
from core.pressure import quantized_runs
from core import machine
from core import mat as matlib
from core.doc import Xf, image_size_mm
from . import image_ops
from .draw_tools import DrawMixin, DRAW_TOOLS, DRAW_HINTS
from .text_tool import TextMixin, TYPE_TOOL, TYPE_HINT

MIN_PX_WIDTH = 1.0
PAN_KEEP_PX = 48   # at the furthest pan, this much of the mat stays on screen
POINT_PX = 7.0     # control points (screen px, any zoom)
HANDLE_PX = 8.0    # resize handles
BHANDLE_PX = 6.0   # bezier handle dots
BBOX_PAD_PX = 5.0  # the selection box sits this far outside the artwork so it never hides a line
MARQUEE_OPACITY = 0.55   # selection outline / box: the selected lines stay visible underneath
ZOOM_SETTLE_MS = 80      # re-render (screen-size handles, min line width) once zooming pauses
DASH_GAP_OPACITY = 0.3   # translucent full line under the dash pieces = the gaps
ANT_DASH = 4             # marching-ants dash / gap length (px)
SELECT_COLOR = "#00acc1"
DRAG_PX = 3.0      # geometry/handle/bend drags start after this much movement
DEFAULT_BEND_STRENGTH = 0.5
DEFAULT_BEND_FOCUS = 1.0
PICK_PX = 3.0      # hit tolerance when nothing is exactly under the cursor

TOOLS = ("mouse", "box", "zoom", "hand", "transform", "bend") + DRAW_TOOLS + (TYPE_TOOL,)
SCANBOX_TOOL = "scanbox"   # temporary tool of the Scan panel's Draw box (not in the toolbar)
# DRAW_TOOLS (draw_tools.py) own Delete/Backspace/Enter/Esc/right-click (drawing sessions)
CONTEXT_TOOLS = ("mouse", "box", "transform", "bend", TYPE_TOOL)
PICK_RANK = {"handle": 0, "bhandle": 1, "point": 2, "seg": 3, "image": 4}   # lower wins
TOOL_HINTS = {
    "mouse": "Click to select, drag to move. Alt+click selects the whole object.",
    "box": "Drag a box to select.",
    "zoom": "Drag a box to zoom in.",
    "hand": "Drag to pan.",
    "transform": "Drag to move the selection, or drag a handle to resize it.",
    "bend": "Drag a line to bend it, or drag a handle to reshape. Hold Alt to move one handle only.",
    SCANBOX_TOOL: "Drag a box on the mat to set the scan area. Esc cancels.",
    TYPE_TOOL: TYPE_HINT,
}
TOOL_HINTS.update(DRAW_HINTS)
BOX_MODES = ("points", "segments", "paths")
SELECT_MODES = ("new", "add", "subtract", "intersect")
ANCHORS = ("opposite", "center", "topleft")

PREVIEW_COLORS = {"drawn": "#212121", "travel": "#9e9e9e", "axis": "#1e88e5", "lead": "#d500f9",
                  "ok": "#2e7d32", "warn": "#f9a825", "bad": "#e53935"}
GEOM_COLORS = {"outside": "#d6d6d6", "mat": "#cfdae5", "mat_edge": "#6f7f8f",
               "travel": "#fbfbfb", "travel_edge": "#666666",
               "grid": "#a7b4c0", "grid_main": "#5b6b7a", "scan": "#1e88e5", "scanbox": "#ef6c00",
               "feed": "#c62828"}
BG_PX_PER_MM = 4.0   # scan images are downsampled to this for display
IMG_PX_PER_MM = 8.0  # image / pdf objects are downsampled to at most this for display
IMG_CACHE_MAX = 6
GHOST_IMAGE_OPACITY = 0.3


def _qrect(r):
    x0, y0, x1, y1 = r
    return QtCore.QRectF(x0, y0, x1 - x0, y1 - y0)


def _poly_path(pts):
    qp = QtGui.QPainterPath()
    qp.moveTo(pts[0][0], pts[0][1])
    for x, y in pts[1:]:
        qp.lineTo(x, y)
    return qp


def _cumlen(pts):
    """Cumulative arc length at each polyline vertex (cum[0] = 0)."""
    cum = [0.0]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
    return cum


def _poly_at(pts, cum, s):
    i = min(max(bisect.bisect_right(cum, s) - 1, 0), len(pts) - 2)
    seg = cum[i + 1] - cum[i]
    f = 0.0 if seg <= 1e-12 else max(0.0, min(1.0, (s - cum[i]) / seg))
    (x0, y0), (x1, y1) = pts[i], pts[i + 1]
    return (x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)


def _poly_slice(pts, cum, s0, s1):
    """Sub-polyline between arc lengths s0 and s1."""
    out = [_poly_at(pts, cum, s0)]
    i = bisect.bisect_right(cum, s0)
    while i < len(pts) and cum[i] < s1:
        out.append(pts[i])
        i += 1
    out.append(_poly_at(pts, cum, s1))
    return out


class Canvas(DrawMixin, TextMixin, QtWidgets.QGraphicsView):
    selection_changed = QtCore.Signal()
    status = QtCore.Signal(str)
    svg_moved = QtCore.Signal()
    svg_dragging = QtCore.Signal()  # during a move/resize drag (svg_moved once on release)
    tool_changed = QtCore.Signal(str)
    zoom_changed = QtCore.Signal(float)  # relative zoom factor
    options_changed = QtCore.Signal()  # modes/toggles/anchor changed
    scan_box_drawn = QtCore.Signal(object)   # (x0, y0, x1, y1) canvas mm
    scan_box_mode_changed = QtCore.Signal(bool)
    cursor_moved = QtCore.Signal(object)     # (x, y) canvas mm under the pointer, None = left

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.mat_profile = matlib.preset(matlib.DEFAULT_NAME)
        self.scan_overlay = None       # {"area", "box": canvas rects, "actual": polygon}
        self._tool_before_scanbox = "mouse"
        self._mat_geom = self.mat_profile.geometry()
        self.scene = QtWidgets.QGraphicsScene(self)
        self.setScene(self.scene)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._content = self._canvas_qrect()
        self.setBackgroundBrush(QtGui.QColor(GEOM_COLORS["outside"]))
        self.setRenderHint(QtGui.QPainter.Antialiasing)
        self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        # files dropped on the canvas go to the main window, which imports them
        self.setAcceptDrops(False)
        self.viewport().setAcceptDrops(False)
        self.bg_item = None
        self.tool = "mouse"
        self.box_mode = "paths"
        self.select_mode = "new"
        self.mouse_box_enabled = False
        self.mouse_transform_enabled = False
        self.resize_anchor = "opposite"
        self._base_zoom = 1.0
        self._zoom_rel = 1.0
        self.zoom_to_mouse = False   # wheel / click-zoom focus: pointer (True) or view centre
        self._auto_fit = True        # keep the mat fitted to the window until the user zooms / pans
        self._press_scene = None
        self._pending_click = None
        self._drag_kind = None  # None | pan | move_svg | resize | box | zoombox | move_geom | bhandle | bend
        self.nudge_mm = 1.0
        self.bend_strength = DEFAULT_BEND_STRENGTH   # 0..1, see Doc.bend_segment_to
        self.bend_focus = DEFAULT_BEND_FOCUS         # 0..1, see Doc.bend_weights
        self._geom = None        # state of a move_geom / bhandle / bend drag (_drag_state)
        self._move_svgs = []
        self._resize_svg = None
        self._resize_anchor_pt = None
        self._resize_local_anchor = None
        self._resize_init_scale = 1.0
        self._resize_init_dist = 1.0
        self._box_origin = None
        self._box_item = None
        self._sel_snapshot = None
        self._pan_state = None  # (x, y, hbar, vbar)
        self._space_hand = False
        self._last_path_click = None
        self.history_coalesce_key = None   # set just before an edit signal. The main window's
                                           # undo history consumes it (repeated nudges = one step)
        self._ants = []
        self._ant_offset = 0
        self._bg_cache = None    # (key, scaled QPixmap)
        self._img_cache = OrderedDict()   # image/pdf display pixmaps: key -> (QPixmap, px w, px h) | None
        self._ppm = None         # px/mm cached for the duration of a refresh()
        self._refresh_pending = False
        self._drag_moved = False
        self._ant_timer = QtCore.QTimer(self)
        self._ant_timer.timeout.connect(self._tick_ants)
        self._ant_timer.start(120)
        self._built_ppm = None   # px/mm the scene was last built at
        self._zoom_timer = QtCore.QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.timeout.connect(self._zoom_settled)
        self._tp = None          # core.blade.Preview shown as overlay, or None
        self._tp_flags = {}
        self.setMinimumSize(480, 360)
        self._init_draw()
        self._init_text()
        self._init_dispatch()
        self._update_cursor()

    # -- tool/option API (panels drive these, and the canvas emits options_changed) --
    def set_tool(self, name):
        if name not in TOOLS and name != SCANBOX_TOOL:
            return
        if name != self.tool:
            was = self.tool
            if name == SCANBOX_TOOL:
                self._tool_before_scanbox = was
            if was == SCANBOX_TOOL and self._drag_kind == "scanbox":
                self._drag_kind = None
                self._clear_box_item()
            self._draw_on_tool_change(name)
            self._text_on_tool_change(name)
            self.tool = name
            self._update_cursor()
            self.tool_changed.emit(name)
            self.refresh()
            if name in TOOL_HINTS:
                self.status.emit(TOOL_HINTS[name])
            if SCANBOX_TOOL in (name, was):
                self.scan_box_mode_changed.emit(name == SCANBOX_TOOL)

    def set_nudge_mm(self, v):
        self.nudge_mm = max(0.01, min(100.0, float(v)))

    def set_box_mode(self, mode):
        if mode in BOX_MODES and mode != self.box_mode:
            self.box_mode = mode
            self.options_changed.emit()

    def set_select_mode(self, mode):
        if mode in SELECT_MODES and mode != self.select_mode:
            self.select_mode = mode
            self.options_changed.emit()

    def set_mouse_box(self, on):
        if bool(on) != self.mouse_box_enabled:
            self.mouse_box_enabled = bool(on)
            self.options_changed.emit()

    def set_mouse_transform(self, on):
        if bool(on) != self.mouse_transform_enabled:
            self.mouse_transform_enabled = bool(on)
            self.options_changed.emit()
            self.refresh()

    def cycle_anchor(self):
        self.resize_anchor = ANCHORS[(ANCHORS.index(self.resize_anchor) + 1) % len(ANCHORS)]
        self.options_changed.emit()

    def anchor_label(self):
        return {"opposite": "Opposite corner", "center": "Center",
                "topleft": "Top-left"}[self.resize_anchor]

    def handles_enabled(self):
        return self.tool == "transform" or (self.tool == "mouse" and self.mouse_transform_enabled)

    def _update_cursor(self):
        if self.tool == SCANBOX_TOOL:
            self.viewport().setCursor(QtCore.Qt.CrossCursor)
        elif self.tool == "hand":
            self.viewport().setCursor(QtCore.Qt.OpenHandCursor)
        elif self.tool in ("box", "zoom", "bend") + DRAW_TOOLS:
            self.viewport().setCursor(QtCore.Qt.CrossCursor)
        elif self.tool == TYPE_TOOL:
            self.viewport().setCursor(QtCore.Qt.IBeamCursor)
        else:
            self.viewport().unsetCursor()

    # -- zoom --
    def zoom_relative(self):
        return self._zoom_rel

    def _sync_zoom(self):
        m = self.transform().m11() or self._base_zoom
        self._zoom_rel = max(0.1, min(20.0, m / self._base_zoom))
        self.zoom_changed.emit(self._zoom_rel)
        self._zoom_timer.start(ZOOM_SETTLE_MS)

    def _zoom_settled(self):
        if self._drag_kind in ("box", "zoombox", "scanbox"):
            self._zoom_timer.start(ZOOM_SETTLE_MS)   # the rubber band lives in the scene
            return
        if self._built_ppm is None or abs(self._px_per_mm() - self._built_ppm) > 1e-9:
            self.refresh()

    def _px(self, n):
        """n screen pixels in canvas mm at the current zoom."""
        return n / max(1e-6, self._ppm if self._ppm is not None else self._px_per_mm())

    def set_bend_strength(self, v):
        self.bend_strength = max(0.0, min(1.0, float(v)))

    def set_bend_focus(self, v):
        self.bend_focus = max(0.0, min(1.0, float(v)))

    def set_zoom_to_mouse(self, on):
        self.zoom_to_mouse = bool(on)

    def _zoom_at(self, f, sp):
        """Zoom by f keeping the scene point sp at the same place in the viewport (zoom_to_mouse)."""
        vp0 = self.mapFromScene(sp)
        c = self._view_center()
        self._zoom_by(f, center=QtCore.QPointF(sp.x() + (c.x() - sp.x()) / f,
                                               sp.y() + (c.y() - sp.y()) / f))
        d = self.mapFromScene(sp) - vp0          # centerOn rounds to whole scroll steps
        for bar, dv in ((self.horizontalScrollBar(), d.x()), (self.verticalScrollBar(), d.y())):
            if dv:
                bar.setValue(bar.value() + dv)

    def set_zoom_relative(self, f):
        f = max(0.1, min(20.0, float(f)))
        cur = (self.transform().m11() or self._base_zoom) / self._base_zoom
        self._zoom_by(f / cur)

    # -- mat profile --
    def _canvas_qrect(self):
        return _qrect(self._mat_geom.canvas_rect())

    def set_mat_profile(self, profile, fit=False):
        """Show another mat (core.mat.MatProfile). fit=True also fits the view to it."""
        self.mat_profile = profile
        self._mat_geom = profile.geometry()
        self.refresh()
        if fit or self._auto_fit:
            self.fit_view()

    # -- scan box --
    def set_scan_overlay(self, overlay):
        """overlay = {"area": (x0, y0, x1, y1), "box": (x0, y0, x1, y1), "actual": [4 points]}
        in canvas mm, or None to hide it."""
        self.scan_overlay = overlay
        self.refresh()

    def set_scan_box_mode(self, on):
        """Switch to the scan-box tool (on) or back to the tool it replaced (off)."""
        if on:
            self.set_tool(SCANBOX_TOOL)
        elif self.tool == SCANBOX_TOOL:
            self.set_tool(self._tool_before_scanbox)

    def scan_box_mode(self):
        return self.tool == SCANBOX_TOOL

    def _press_scanbox(self, ev, sp):
        self._drag_kind = "scanbox"
        self._box_origin = (sp.x(), sp.y())
        self._ensure_box_item(sp.x(), sp.y(), sp.x(), sp.y())

    def _scanbox_tick(self, ev, sp):
        self._ensure_box_item(self._box_origin[0], self._box_origin[1], sp.x(), sp.y())

    def _scanbox_end(self, ev, sp):
        x0, y0 = self._box_origin          # read before _clear_box_item resets it
        self._drag_kind = None
        self._clear_box_item()
        self._press_scene = None
        ppm = self._px_per_mm()
        if abs(sp.x() - x0) * ppm < DRAG_PX or abs(sp.y() - y0) * ppm < DRAG_PX:
            return                      # a click, not a box: keep the tool for another try
        self.set_scan_box_mode(False)   # back to the tool this one replaced
        self.scan_box_drawn.emit((min(x0, sp.x()), min(y0, sp.y()), max(x0, sp.x()), max(y0, sp.y())))

    def _draw_scan_overlay(self):
        ov = self.scan_overlay
        if not ov:
            return
        col = QtGui.QColor(GEOM_COLORS["scanbox"])
        if ov.get("area"):
            r = _qrect(ov["area"])
            self._geom_item(self.scene.addRect(r, self._cosmetic_pen(GEOM_COLORS["scan"], 1.0,
                                                                     QtCore.Qt.DashLine)), 30)
            self._add_label("Scannable area", r.right(), r.bottom(), GEOM_COLORS["scan"],
                            "below_right", z=30)
        if ov.get("box"):
            fill = QtGui.QColor(col)
            fill.setAlpha(28)
            self._geom_item(self.scene.addRect(_qrect(ov["box"]),
                                               self._cosmetic_pen(col, 1.5, QtCore.Qt.DashLine),
                                               QtGui.QBrush(fill)), 31)
        if ov.get("actual"):
            self._geom_item(self.scene.addPolygon(
                QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in ov["actual"]]),
                self._cosmetic_pen(col, 1.0)), 31)
            xs = [x for x, _y in ov["actual"]]
            ys = [y for _x, y in ov["actual"]]
            self._add_label("Scan", max(xs), min(ys), GEOM_COLORS["scanbox"], "inside_right", z=31)

    def fit_view(self):
        """Fit the mat to the window. The view then stays fitted through window resizes
        until the user zooms or pans."""
        r = self._canvas_qrect()
        self.fitInView(r, QtCore.Qt.KeepAspectRatio)
        self.scale(0.95, 0.95)  # small margin around the canvas
        self._update_pan_bounds(r.center())
        self._sync_zoom()
        self._auto_fit = True

    # -- pan bounds --
    def _view_center(self):
        return self.mapToScene(self.viewport().rect().center())

    def _zoom_by(self, f, center=None):
        """Scale about the view center (or `center`), keeping it in place."""
        if center is None:
            center = self._view_center()
        self._auto_fit = False
        self.scale(f, f)
        self._update_pan_bounds(center)
        self._sync_zoom()

    def _update_pan_bounds(self, center=None):
        """Scrollable area = content padded by one viewport minus PAN_KEEP_PX
        on every side: the mat can be panned anywhere, including partly off
        screen, but the scroll range ends while PAN_KEEP_PX of it is visible.
        Recomputed whenever the zoom, the viewport size or the content
        changes. The view keeps (or moves to) `center`."""
        s = abs(self.transform().m11()) or 1.0
        vp = self.viewport().size()
        keep = PAN_KEEP_PX / s
        mx = max(0.0, vp.width() / s - keep)
        my = max(0.0, vp.height() / s - keep)
        rect = self._content.adjusted(-mx, -my, mx, my)
        if center is None:
            if rect == self.sceneRect():
                return
            center = self._view_center()
        self.setSceneRect(rect)
        self.centerOn(center)

    def resizeEvent(self, ev):
        center = self._view_center() if ev.oldSize().isValid() else None
        super().resizeEvent(ev)
        if self._auto_fit and self.viewport().width() > 0 and self.viewport().height() > 0:
            self.fit_view()
            return
        self._update_pan_bounds(center)

    # -- geometry helpers --
    def _px_per_mm(self):
        # the scene is in mm, so the view transform already is device px per mm
        return abs(self.transform().m11()) or 3.0

    def _pen_width(self, thick_mm):
        min_mm = MIN_PX_WIDTH / max(1e-6, self._ppm if self._ppm is not None else self._px_per_mm())
        return max(thick_mm, min_mm)

    def _resize_target(self):
        d = self.doc
        if len(d.selection.svg_ids) == 1 and not (d.selection.path_ids or d.selection.segments or d.selection.points):
            return next(iter(d.selection.svg_ids))
        touched = d.svg_ids_for_selection()
        if len(touched) == 1:
            only = next(iter(touched))
            if d.is_entire_svg_selected(only):
                return only
        return None

    @staticmethod
    def _in_bbox(x, y, bbox, pad=0.0):
        return (bbox[0] - pad) <= x <= (bbox[2] + pad) and (bbox[1] - pad) <= y <= (bbox[3] + pad)

    def _anchor_point(self, bbox):
        x0, y0, x1, y1 = bbox
        if self.resize_anchor == "center":
            return ((x0 + x1) / 2, (y0 + y1) / 2)
        if self.resize_anchor == "topleft":
            return (x0, y0)
        return None  # opposite: computed per-handle below

    @staticmethod
    def _opposite_anchor(bbox, handle):
        x0, y0, x1, y1 = bbox
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        return {"nw": (x1, y1), "se": (x0, y0), "ne": (x0, y1), "sw": (x1, y0),
                "n": (mx, y1), "s": (mx, y0), "e": (x0, my), "w": (x1, my)}.get(handle, (x0, y0))

    # -- toolpath preview overlay --
    def set_toolpath_preview(self, preview, flags=None):
        """flags: travel/axis/tip/dim booleans (missing = True)."""
        self._tp = preview
        self._tp_flags = dict(flags or {})
        self.refresh()

    def clear_toolpath_preview(self):
        if self._tp is not None:
            self._tp = None
            self.refresh()

    def _draw_toolpath_preview(self):
        tp, f = self._tp, self._tp_flags

        def add(polys, color, width_px, z, style=None, opacity=1.0):
            qp = QtGui.QPainterPath()
            for pl in polys:
                if len(pl) < 2:
                    continue
                qp.moveTo(pl[0][0], pl[0][1])
                for x, y in pl[1:]:
                    qp.lineTo(x, y)
            if qp.isEmpty():
                return
            pen = QtGui.QPen(QtGui.QColor(color), width_px)
            pen.setCosmetic(True)               # constant on-screen width at any zoom
            pen.setCapStyle(QtCore.Qt.RoundCap)
            pen.setJoinStyle(QtCore.Qt.RoundJoin)
            if style is not None:
                pen.setStyle(style)
            it = self.scene.addPath(qp, pen)
            it.setZValue(z)
            it.setOpacity(opacity)
            it.setAcceptedMouseButtons(QtCore.Qt.NoButton)
            it.setData(2, "preview")

        if f.get("dim", True):
            add(tp.drawn, PREVIEW_COLORS["drawn"], 1.0, 3.05)
        if f.get("travel", True):
            add([[a, b] for a, b in tp.travel], PREVIEW_COLORS["travel"], 1.0, 3.0, QtCore.Qt.DashLine)
        if f.get("axis", True):
            add([pl for lead, pl in tp.axis if not lead], PREVIEW_COLORS["axis"], 1.0, 3.1)
            add([pl for lead, pl in tp.axis if lead], PREVIEW_COLORS["lead"], 1.0, 3.1)
        if f.get("tip", True):
            for b in ("lead", "ok", "warn", "bad"):
                add([pl for bb, pl in tp.tip if bb == b], PREVIEW_COLORS[b], 2.0, 3.2 + 0.01 * len(b),
                    opacity=0.9)

    # -- scene rebuild (drawing identical to previous version) --
    def _schedule_refresh(self):
        if not self._refresh_pending:
            self._refresh_pending = True
            QtCore.QTimer.singleShot(0, self._flush_refresh)

    def _flush_refresh(self):
        if self._refresh_pending:
            self.refresh()

    def _bg_pixmap(self, d):
        """(display pixmap, original width px, original height px, dpi) or None. The embedded
        scan (d.scan_image) carries its dpi, and an external file's dpi is worked out from it."""
        si = d.scan_image
        if si:
            key = ("blob", si["blob"])
        else:
            try:
                mt = os.path.getmtime(d.bg_jpeg_path)
            except OSError:
                mt = None
            key = (d.bg_jpeg_path, mt)
        if self._bg_cache is not None and self._bg_cache[0] == key:
            return self._bg_cache[1]
        if si:
            img = QtGui.QImage.fromData(d.scan_image_bytes() or b"")
        else:
            img = QtGui.QImage(d.bg_jpeg_path)
        out = None
        if not img.isNull():
            w, h = img.width(), img.height()
            if si:
                dpi = si["dpi"]
            else:
                info = img.dotsPerMeterX() * 0.0254 if img.dotsPerMeterX() > 0 else None
                dpi = machine.image_dpi(w, h, info)
            k = min(1.0, BG_PX_PER_MM * 25.4 / dpi)
            if k < 1.0:
                img = img.scaled(max(1, round(w * k)), max(1, round(h * k)),
                                 QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.SmoothTransformation)
            out = (QtGui.QPixmap.fromImage(img), w, h, dpi)
        self._bg_cache = (key, out)
        return out

    def _add_scan_image(self, d):
        """Place the scan by the firmware transform: pixel corners -> mat mm (doc.scan_cal)."""
        bg = self._bg_pixmap(d)
        if bg is None:
            return
        pm, w, h, dpi = bg
        m11, m12, m21, m22, dx, dy = machine.scan_pixel_transform(d.scan_cal, dpi)
        if d.scan_image:     # a window of the full-mat scan: its first pixel's place in it
            c0, r0 = d.scan_image.get("col0", 0), d.scan_image.get("row0", 0)
            dx, dy = dx + m11 * c0 + m21 * r0, dy + m12 * c0 + m22 * r0
        sx, sy = w / pm.width(), h / pm.height()   # display pixmap -> original pixels
        self.bg_item = self.scene.addPixmap(pm)
        self.bg_item.setTransformationMode(QtCore.Qt.SmoothTransformation)
        self.bg_item.setTransform(QtGui.QTransform(m11 * sx, m12 * sx, m21 * sy, m22 * sy, dx, dy)
                                  * self._skew_transform())
        self.bg_item.setZValue(-10)
        self.bg_item.setAcceptedMouseButtons(QtCore.Qt.NoButton)

    def _image_pixmap(self, svg):
        """(display QPixmap, source px w, source px h) of an image/pdf object, or None when the
        file is missing / unreadable.  Cached (by file, mtime, page, pdf dpi). The full-size numpy
        array lives in core.raster's cache and the display copy is downsampled to IMG_PX_PER_MM."""
        try:
            mt = os.path.getmtime(svg.source_path)
        except OSError:
            mt = None
        pdf = svg.kind == "pdf"
        key = (svg.kind, svg.source_path, mt, svg.pdf_page if pdf else 0, svg.dpi if pdf else 0.0,
               round(svg.dpi) if not pdf else 0)
        if key in self._img_cache:
            self._img_cache.move_to_end(key)
            return self._img_cache[key]
        out = None
        if mt is not None:
            try:
                rgba = image_ops.get_pixels(svg)
                h, w = rgba.shape[:2]
                img = QtGui.QImage(rgba.data, w, h, w * 4, QtGui.QImage.Format_RGBA8888)
                k = min(1.0, IMG_PX_PER_MM * 25.4 / max(1.0, svg.dpi))
                if k < 1.0:   # scaled() returns an independent copy of the pixels
                    img = img.scaled(max(1, round(w * k)), max(1, round(h * k)),
                                     QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.SmoothTransformation)
                else:
                    img = img.copy()   # never keep a QImage that aliases the shared numpy buffer
                out = (QtGui.QPixmap.fromImage(img), w, h)
            except Exception:
                out = None
        self._img_cache[key] = out
        while len(self._img_cache) > IMG_CACHE_MAX:
            self._img_cache.popitem(last=False)
        return out

    def _add_image_item(self, svg):
        """Draw an image/pdf object (Z -1: above the mat grid, below paths).  A missing or
        unreadable source gets a dashed placeholder rect with its label so it stays selectable."""
        w_mm, h_mm = image_size_mm(svg)       # local frame, before scale / rotation / flip
        xf = Xf.of(svg)
        px = self._image_pixmap(svg) if svg.px_w > 0 and svg.px_h > 0 else None
        if px is not None:
            pm = px[0]
            it = self.scene.addPixmap(pm)
            it.setTransformationMode(QtCore.Qt.SmoothTransformation)
            it.setShapeMode(QtWidgets.QGraphicsPixmapItem.BoundingRectShape)   # transparent areas hit too
            kx, ky = w_mm / pm.width(), h_mm / pm.height()
            it.setTransform(QtGui.QTransform(xf.a * kx, xf.b * kx, xf.c * ky, xf.d * ky, xf.ox, xf.oy))
        else:
            sc = svg.scale or 1.0
            w_mm, h_mm = max(w_mm, 1.0 / sc), max(h_mm, 1.0 / sc)
            pen = self._cosmetic_pen("#b71c1c", 1.0, QtCore.Qt.DashLine)
            it = self.scene.addPolygon(QtGui.QPolygonF([QtCore.QPointF(*xf.to_abs(x, y)) for x, y in (
                (0.0, 0.0), (w_mm, 0.0), (w_mm, h_mm), (0.0, h_mm))]), pen,
                QtGui.QBrush(QtGui.QColor(255, 255, 255, 90)))
            t = QtWidgets.QGraphicsSimpleTextItem("%s (file missing)" % svg.label, it)
            t.setBrush(QtGui.QColor("#b71c1c"))
            t.setFlag(QtWidgets.QGraphicsItem.ItemIgnoresTransformations, True)
            t.setPos(*xf.to_abs(1.0 / sc, 1.0 / sc))
            t.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        it.setData(0, svg.id)
        it.setData(2, "image")
        it.setZValue(-1)
        if svg.ghost:
            it.setOpacity(GHOST_IMAGE_OPACITY)
        return it

    def _cosmetic_pen(self, color, width_px=1.0, style=None):
        pen = QtGui.QPen(QtGui.QColor(color), width_px)
        pen.setCosmetic(True)
        if style is not None:
            pen.setStyle(style)
        return pen

    def _add_label(self, text, x, y, color, anchor="inside", z=-3):
        """Screen-size text anchored at canvas point (x, y). Offsets are in screen pixels so
        labels sit the same distance from their lines at every zoom. anchor: "inside" = just
        inside a top-left corner, "inside_right" = just inside a top-right corner, "below" /
        "below_right" = under a bottom-left / bottom-right corner, "right" = right of the
        point, vertically centred."""
        t = self.scene.addSimpleText(text)
        t.setBrush(QtGui.QColor(color))
        t.setFlag(QtWidgets.QGraphicsItem.ItemIgnoresTransformations, True)
        br = t.boundingRect()
        w, h = br.width(), br.height()
        dx, dy = {"inside": (4.0, 2.0), "inside_right": (-4.0 - w, 2.0), "below": (0.0, 2.0),
                  "below_right": (-w, 2.0), "right": (6.0, -h / 2.0)}[anchor]
        t.setPos(x, y)
        t.setTransform(QtGui.QTransform.fromTranslate(dx, dy))
        t.setZValue(z)
        t.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        return t

    def _skew_transform(self):
        """Machine mm -> canvas mm for things the machine saw (doc.mat_skew)."""
        return QtGui.QTransform(*self.doc.mat_skew.image_transform())

    def _geom_item(self, it, z):
        it.setZValue(z)
        it.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        return it

    def _draw_machine_geometry(self):
        """Physical mat, head travel / cut area, the mat's printed grid and the feed line. The scan
        area is drawn by the Scan panel's overlay (_draw_scan_overlay) while that panel is open."""
        geo, prof = self._mat_geom, self.mat_profile
        m = _qrect(geo.mat)
        self._geom_item(self.scene.addRect(m, self._cosmetic_pen(GEOM_COLORS["mat_edge"], 1.5),
                                           QtGui.QBrush(QtGui.QColor(GEOM_COLORS["mat"]))), -21)
        c = _qrect(geo.travel)
        self._geom_item(self.scene.addRect(c, self._cosmetic_pen(GEOM_COLORS["travel_edge"], 1.0,
                                                                 QtCore.Qt.DashLine),
                                           QtGui.QBrush(QtGui.QColor(GEOM_COLORS["travel"]))), -20)
        g = geo.grid
        gx0, gy0, gx1, gy1 = g.rect()
        thin = self._cosmetic_pen(GEOM_COLORS["grid"], 1.0, QtCore.Qt.DotLine)
        main = self._cosmetic_pen(GEOM_COLORS["grid_main"], 1.0, QtCore.Qt.DotLine)
        qp_thin, qp_main = QtGui.QPainterPath(), QtGui.QPainterPath()
        xs, ys = g.lines()
        for x, major in xs:
            qp = qp_main if major else qp_thin
            qp.moveTo(x, gy0)
            qp.lineTo(x, gy1)
        for y, major in ys:
            qp = qp_main if major else qp_thin
            qp.moveTo(gx0, y)
            qp.lineTo(gx1, y)
        for qp, pen in ((qp_thin, thin), (qp_main, main)):
            self._geom_item(self.scene.addPath(qp, pen), -5)
        self._geom_item(self.scene.addRect(_qrect(g.rect()),
                                           self._cosmetic_pen(GEOM_COLORS["grid_main"], 1.5)), -5)
        # feed line: where the load leaves the mat (Y origin), across the mat, labelled
        # outside the mat's right edge so it never sits on the artwork
        feed_pen = self._cosmetic_pen(GEOM_COLORS["feed"], 1.0, QtCore.Qt.DashDotLine)
        self._geom_item(self.scene.addLine(m.left(), geo.feed_y, m.right(), geo.feed_y, feed_pen), -3)
        extra = prof.feed_extra_units()
        self._add_label("Feed line", m.right(), geo.feed_y, GEOM_COLORS["feed"], "right").setToolTip(
            "Cut origin (Y = 0): the mat is fed in %.1f mm%s" % (
                prof.feed_depth_mm(), " (top margin + %d %s)" % (extra, prof.units) if extra else ""))
        # the scan area is shown (and labelled) by the Scan panel's overlay while it is open
        pw, ph = prof.physical_mm()
        self._add_label("%s  ·  %.0f × %.0f mm" % (prof.name, pw, ph),
                        m.left(), m.bottom(), GEOM_COLORS["mat_edge"], "below")
        self._add_label("Cut area", c.left(), c.top(), GEOM_COLORS["travel_edge"])

    def refresh(self):
        self._refresh_pending = False
        self._ppm = self._built_ppm = self._px_per_mm()
        try:
            self._rebuild()
        finally:
            self._ppm = None

    def _rebuild(self):
        self._ants = []
        self._preview_items = []   # the scene clear below deletes them
        self.scene.clear()
        self.bg_item = None
        d = self.doc
        rect = self._canvas_qrect()
        bb = self._tp.bbox() if self._tp is not None else None
        if bb:   # e.g. the lead-in sits in the margin above the cut area
            rect = rect.united(QtCore.QRectF(bb[0] - 5, bb[1] - 5, bb[2] - bb[0] + 10, bb[3] - bb[1] + 10))
        self.scene.setSceneRect(rect)
        self._content = rect
        self._update_pan_bounds()
        self._draw_machine_geometry()
        if d.scan_image or d.bg_jpeg_path:
            self._add_scan_image(d)
        for svg in d.svgs.values():
            if image_ops.is_raster(svg) and svg.visible:
                self._add_image_item(svg)
        for p in d.paths.values():
            svg = d.svgs.get(p.svg_id)
            if svg is not None and not svg.visible:
                continue
            self._draw_path(p)
        if self._tp is not None:
            if self._tp_flags.get("dim", True):
                for it in self.scene.items():
                    if it.data(2) == "seg":
                        it.setOpacity(it.opacity() * 0.15)
            self._draw_toolpath_preview()
        self._draw_selection_overlays()
        self._draw_preview_items()
        self._text_items()
        self._draw_scan_overlay()
        self.viewport().update()

    def _draw_path(self, p):
        d = self.doc
        svg = d.svgs.get(p.svg_id)
        ghost = bool(svg is not None and svg.ghost)
        if len(p.points) < 2:
            return
        cp = d.color_params.get(p.color)
        if cp is not None and not cp.use_thickness_map:
            cp = None   # only a thickness map splits a segment into pressure runs
        base = QtGui.QColor(p.color)
        for si in range(p.segment_count()):
            poly = d.segment_abs_poly(p, si)   # curves are flattened, straight = 2 points
            if ghost:
                pen = QtGui.QPen(QtGui.QColor("#999999"), 0)
                pen.setStyle(QtCore.Qt.DotLine)
                self._add_seg_item(_poly_path([(x, y) for x, y, _t in poly]), pen, p.id, si, 2)
                continue
            dashes = self._dash_spans(poly, p) if p.dash_enabled else None
            run_s = 0.0   # runs are contiguous along the segment: arc length where this one starts
            for pq, pts, tm in self._thickness_runs(poly, cp):
                col = QtGui.QColor(base)
                if pq is not None:
                    k = (pq + 9) / 18.0
                    col = col.lighter(int(60 + 140 * k)) if k > 0.5 else col.darker(int(200 - 140 * k))
                pen = QtGui.QPen(col, self._pen_width(tm))
                pen.setCapStyle(QtCore.Qt.RoundCap)
                pen.setJoinStyle(QtCore.Qt.RoundJoin)
                item = self._add_seg_item(_poly_path(pts), pen, p.id, si, 2)
                if dashes is None:
                    continue
                # dashed: the whole run translucent = the gaps, solid dash pieces on top
                item.setOpacity(DASH_GAP_OPACITY)
                cum = _cumlen(pts)
                for s0, s1 in dashes:
                    a, b = max(s0 - run_s, 0.0), min(s1 - run_s, cum[-1])
                    if b - a > 1e-9:
                        self._add_seg_item(_poly_path(_poly_slice(pts, cum, a, b)), pen, p.id, si, 2.1)
                run_s += cum[-1]

    def _add_seg_item(self, qp, pen, pid, si, z):
        item = self.scene.addPath(qp, pen)
        item.setData(0, pid)
        item.setData(1, si)
        item.setData(2, "seg")
        item.setZValue(z)
        return item

    @staticmethod
    def _thickness_runs(poly, cp):
        """[(pressure|None, [(x, y), ...], thickness_mm)] along one flattened segment.
        cp None = a single run. Adjacent pieces with the same quantized pressure are merged."""
        if cp is None:
            return [(None, [(x, y) for x, y, _t in poly], (poly[0][2] + poly[-1][2]) / 2)]
        out = []   # [pq, pts, thickness sum, count, ends at the piece end]
        for (xa, ya, ta), (xb, yb, tb) in zip(poly, poly[1:]):
            for f0, f1, pq in quantized_runs(ta, tb, cp.curve):
                p1 = (xa + (xb - xa) * f1, ya + (yb - ya) * f1)
                tm = ta + (tb - ta) * (f0 + f1) / 2
                last = out[-1] if out else None
                if last is not None and last[0] == pq and f0 <= 1e-9 and last[4]:
                    last[1].append(p1)
                    last[2] += tm
                    last[3] += 1
                    last[4] = f1 >= 1 - 1e-9
                else:
                    p0 = (xa + (xb - xa) * f0, ya + (yb - ya) * f0)
                    out.append([pq, [p0, p1], tm, 1, f1 >= 1 - 1e-9])
        return [(pq, pts, ts / n) for pq, pts, ts, n, _e in out]

    @staticmethod
    def _dash_spans(poly, path):
        """[(s0, s1)] arc-length spans of the dashes: `dash_on_mm` long every period, from the start."""
        period = path.dash_on_mm + path.dash_off_mm
        L = _cumlen([(x, y) for x, y, _t in poly])[-1]
        if L <= 1e-9 or period <= 1e-9:
            return [(0.0, L)]
        out, s = [], 0.0
        while s < L - 1e-9:
            out.append((s, min(s + path.dash_on_mm, L)))
            s += period
        return out

    def _selected_runs(self):
        """(path, seg_idx, [(x, y, t), ...]) flattened polyline of every selected segment."""
        d = self.doc
        sel = d.selection
        seen = set()

        def refs():
            for pid in sel.path_ids:
                p = d.paths.get(pid)
                if p is not None:
                    for si in range(p.segment_count()):
                        yield pid, si
            for ref in sel.segments:
                yield ref
            for svg_id in sel.svg_ids:
                s = d.svgs.get(svg_id)
                for pid in (s.path_ids if s is not None else ()):
                    p = d.paths.get(pid)
                    if p is not None:
                        for si in range(p.segment_count()):
                            yield pid, si
        for pid, si in refs():
            p = d.paths.get(pid)
            if (pid, si) in seen or p is None or not 0 <= si < p.segment_count():
                continue
            seen.add((pid, si))
            yield (p, si, d.segment_abs_poly(p, si))

    def _draw_selection_overlays(self):
        d = self.doc
        qp = QtGui.QPainterPath()
        for (_p, _si, poly) in self._selected_runs():
            qp.moveTo(poly[0][0], poly[0][1])
            for x, y, _t in poly[1:]:
                qp.lineTo(x, y)
        if not qp.isEmpty():
            # 1 px marching ants, half transparent: thin selected lines stay visible under them
            for color, offset in (("#000000", 0), ("#ffffff", ANT_DASH)):
                pen = self._cosmetic_pen(color, 1.0)
                pen.setDashPattern([ANT_DASH, ANT_DASH])
                pen.setDashOffset(self._ant_offset + offset)
                it = self.scene.addPath(qp, pen)
                it.setAcceptedMouseButtons(QtCore.Qt.NoButton)
                it.setData(2, "ant")
                it.setZValue(6)
                it.setOpacity(MARQUEE_OPACITY)
                self._ants.append(it)
        pts_to_draw = set()
        for pid in d.selection.path_ids:
            p = d.paths.get(pid)
            if p:
                for i in range(len(p.points)):
                    pts_to_draw.add((pid, i))
        for (pid, si) in d.selection.segments:
            p = d.paths.get(pid)
            if p and p.points:
                n = len(p.points)
                pts_to_draw.add((pid, si % n))
                pts_to_draw.add((pid, (si + 1) % n))
        for key in d.selection.points:
            pts_to_draw.add(key)
        for (pid, i) in sorted(pts_to_draw):
            p = d.paths.get(pid)
            if p is None or not (0 <= i < len(p.points)):
                continue
            svg = d.svgs.get(p.svg_id)
            if svg is not None and not svg.visible:
                continue
            f = Xf.of(d.svgs.get(p.svg_id)).to_abs
            x, y = f(p.points[i].x, p.points[i].y)
            sel = (pid, i) in d.selection.points
            r = self._px(POINT_PX)
            it = self.scene.addRect(x - r / 2, y - r / 2, r, r, self._cosmetic_pen("#ff5722", 1.0),
                                    QtGui.QBrush(QtGui.QColor("#ff5722") if sel else QtGui.QColor("#ffffff")))
            it.setData(0, pid)
            it.setData(1, i)
            it.setData(2, "point")
            it.setZValue(10)
            for which, hnd in (("in", p.points[i].hin), ("out", p.points[i].hout)):
                if hnd is not None:
                    self._draw_bhandle(pid, i, which, x, y, *f(*hnd))
        bbox = d.selection_bbox()
        if bbox is None:
            return
        # the box is drawn a few pixels outside the artwork so it never covers an edge line
        pad = self._px(BBOX_PAD_PX)
        bbox = (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad)
        box = self.scene.addRect(bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1],
                                 self._cosmetic_pen(SELECT_COLOR, 1.0, QtCore.Qt.DashLine))
        box.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        box.setData(2, "bbox")
        box.setZValue(8)
        box.setOpacity(MARQUEE_OPACITY)
        if self.handles_enabled() and self._resize_target() is not None:
            for hx, hy, name in self._handle_positions(bbox):
                r = self._px(HANDLE_PX)
                h = self.scene.addRect(hx - r / 2, hy - r / 2, r, r, self._cosmetic_pen(SELECT_COLOR, 1.0),
                                       QtGui.QBrush(QtGui.QColor("#ffffff")))
                h.setData(0, self._resize_target())
                h.setData(1, name)
                h.setData(2, "handle")
                h.setZValue(13)   # above the bezier handles: resize wins where they overlap

    def _draw_bhandle(self, pid, idx, which, ax, ay, hx, hy):
        """One bezier handle: thin line anchor -> handle plus a draggable dot (kind "bhandle")."""
        ln = self.scene.addLine(ax, ay, hx, hy, self._cosmetic_pen("#1e88e5", 1.0))
        ln.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        ln.setData(2, "bline")
        ln.setZValue(9)
        r = self._px(BHANDLE_PX)
        dot = self.scene.addEllipse(hx - r / 2, hy - r / 2, r, r, self._cosmetic_pen("#1e88e5", 1.0),
                                    QtGui.QBrush(QtGui.QColor("#ffffff")))
        dot.setData(0, pid)
        dot.setData(1, (idx, which))
        dot.setData(2, "bhandle")
        dot.setZValue(12)

    @staticmethod
    def _handle_positions(bbox):
        x0, y0, x1, y1 = bbox
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        return [(x0, y0, "nw"), (mx, y0, "n"), (x1, y0, "ne"),
                (x1, my, "e"), (x1, y1, "se"), (mx, y1, "s"),
                (x0, y1, "sw"), (x0, my, "w")]

    def _tick_ants(self):
        if not self._ants:
            return
        self._ant_offset = (self._ant_offset + 1) % (2 * ANT_DASH)
        for it in self._ants:
            try:
                pen = it.pen()
                pen.setDashOffset(self._ant_offset + (ANT_DASH if pen.color().name() == "#ffffff" else 0))
                it.setPen(pen)
            except RuntimeError:
                pass

    @staticmethod
    def _item_kind(it):
        try:
            return it.data(2)
        except RuntimeError:
            return None

    def _pick_dist(self, it, kind, sp):
        """Distance (mm) from scene point sp to a pickable item, to rank equal-kind candidates."""
        if kind == "seg":
            p = self.doc.paths.get(it.data(0))
            if p is not None and p.segment_count() > 0:
                return self.doc.nearest_t_on_segment(p, int(it.data(1)), sp.x(), sp.y())[1]
        c = it.sceneBoundingRect().center()
        return math.hypot(c.x() - sp.x(), c.y() - sp.y())

    def _pick(self, view_pos, kinds=None):
        """Best pickable item at view_pos: exactly under the cursor first, else within PICK_PX.
        Ranked handle > bhandle > point > seg (PICK_RANK), then nearest. kinds limits the kinds."""
        sp = self.mapToScene(view_pos)
        r = PICK_PX / max(1e-6, self._px_per_mm())
        under = self.scene.items(sp)
        for items in (under,
                      self.scene.items(QtCore.QRectF(sp.x() - r, sp.y() - r, 2 * r, 2 * r),
                                       QtCore.Qt.IntersectsItemShape)):
            best = None
            for it in items:
                kind = self._item_kind(it)
                if kind == "image" or kind not in PICK_RANK or (kinds is not None and kind not in kinds):
                    continue   # images are only a fallback: a nearby line / handle wins over them
                key = (PICK_RANK[kind], self._pick_dist(it, kind, sp))
                if best is None or key < best[0]:
                    best = (key, it)
            if best is not None:
                return best[1]
        if kinds is None or "image" in kinds:
            for it in under:   # topmost raster under the cursor (scene.items is front-to-back)
                if self._item_kind(it) == "image":
                    return it
        return None

    # -- manual panning (deterministic, in every tool via hand/middle/space) --
    def _pan_press(self, ev):
        self._auto_fit = False
        pos = ev.pos()
        self._pan_state = (pos.x(), pos.y(),
                           self.horizontalScrollBar().value(),
                           self.verticalScrollBar().value())
        self._drag_kind = "pan"
        self.viewport().setCursor(QtCore.Qt.ClosedHandCursor)
        ev.accept()

    def _pan_move(self, ev):
        if self._pan_state is None:
            return
        x0, y0, h0, v0 = self._pan_state
        pos = ev.pos()
        self.horizontalScrollBar().setValue(h0 - (pos.x() - x0))
        self.verticalScrollBar().setValue(v0 - (pos.y() - y0))
        ev.accept()

    def _pan_end(self, ev=None):
        self._pan_state = None
        self._drag_kind = None
        self._update_cursor()
        if ev is not None:
            ev.accept()

    # -- interaction --
    # Dispatch tables: a tool registers its press handler in _tool_press[tool], and a drag kind
    # registers its move/release handlers in _drag_moves/_drag_ends[kind]. Handlers get (ev, sp)
    # with sp the scene point in mm. New tools (drawing, ...) only add entries here.
    def _init_dispatch(self):
        self._tool_press = {"box": self._press_box, "zoom": self._press_zoom,
                            "mouse": self._press_select, "transform": self._press_select,
                            "bend": self._press_bend, SCANBOX_TOOL: self._press_scanbox}
        for t in DRAW_TOOLS:
            self._tool_press[t] = self._press_draw
        self._tool_press[TYPE_TOOL] = self._press_type
        self._drag_moves = {"move_svg": self._move_svg_tick, "resize": self._resize_tick,
                            "move_geom": self._move_geom_tick, "bhandle": self._bhandle_tick,
                            "bend": self._bend_tick, "draw": self._draw_drag_tick,
                            "scanbox": self._scanbox_tick, "text_select": self._text_drag_tick}
        self._drag_ends = {"move_svg": self._end_move_svg, "resize": self._end_resize,
                           "move_geom": self._end_geom, "bhandle": self._end_geom,
                           "bend": self._end_geom, "draw": self._draw_drag_end,
                           "scanbox": self._scanbox_end, "text_select": self._text_drag_end}

    def wheelEvent(self, ev):
        f = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
        target = max(0.1, min(20.0, self._zoom_rel * f))
        if self.zoom_to_mouse:
            self._zoom_at(target / self._zoom_rel, self.mapToScene(ev.position().toPoint()))
        else:
            self._zoom_by(target / self._zoom_rel)

    _ARROWS = {QtCore.Qt.Key_Left: (-1, 0), QtCore.Qt.Key_Right: (1, 0),
               QtCore.Qt.Key_Up: (0, -1), QtCore.Qt.Key_Down: (0, 1)}

    _TEXT_SHORTCUTS = (QtGui.QKeySequence.SelectAll, QtGui.QKeySequence.Copy,
                       QtGui.QKeySequence.Cut, QtGui.QKeySequence.Paste)

    def event(self, ev):
        # while text is being typed, Ctrl+A/C/X/V edit the text, not the window's Edit menu
        if (ev.type() == QtCore.QEvent.ShortcutOverride and self.editing_text_id() is not None
                and any(ev.matches(k) for k in self._TEXT_SHORTCUTS)):
            ev.accept()
            return True
        return super().event(ev)

    def keyPressEvent(self, ev):
        key, mods = ev.key(), ev.modifiers()
        if self._text_key(ev):      # typing owns every plain key, Space and Delete included
            ev.accept()
            return
        if key == QtCore.Qt.Key_Escape and self.tool == SCANBOX_TOOL:
            self.set_scan_box_mode(False)
            ev.accept()
            return
        if key == QtCore.Qt.Key_Space and not ev.isAutoRepeat() and not self._space_hand:
            self._space_hand = True
            self.viewport().setCursor(QtCore.Qt.OpenHandCursor)
        elif self.tool in DRAW_TOOLS and key != QtCore.Qt.Key_Space and self._draw_key(ev):
            ev.accept()
            return
        elif (self.tool == "mouse" and key in self._ARROWS
              and not mods & (QtCore.Qt.ControlModifier | QtCore.Qt.AltModifier)):
            self.nudge_selection(*self._ARROWS[key], big=bool(mods & QtCore.Qt.ShiftModifier))
            ev.accept()   # arrows never scroll the view in the mouse tool
            return
        elif (key in (QtCore.Qt.Key_Delete, QtCore.Qt.Key_Backspace) and self.tool not in DRAW_TOOLS
              and not mods & (QtCore.Qt.ControlModifier | QtCore.Qt.AltModifier)):
            self.delete_selected()
            ev.accept()
            return
        super().keyPressEvent(ev)

    def nudge_selection(self, ux, uy, big=False):
        """Move the selection by nudge_mm along the unit direction (ux, uy), ten times as far when big."""
        if self.doc.selection.is_empty():
            return
        step = self.nudge_mm * (10.0 if big else 1.0)
        self.doc.move_selection(ux * step, uy * step)
        self._drag_moved = True
        # consecutive nudges of the same selection merge into one undo step
        self.history_coalesce_key = ("nudge", self.doc.selection_state())
        try:
            self._end_svg_drag()   # svg_moved -> full refresh -> history commit
        finally:
            self.history_coalesce_key = None

    def cancel_transient(self):
        """Drop every in-progress interaction (drawing shape + previews, drag / gesture state,
        selection box) before the document is swapped under the canvas (undo / redo).  The draw
        session object (_draw_svg_id) is kept: _commit_shape recreates it if it vanished."""
        self._draw_state = None
        self._clear_draw_preview()
        self._text_stop()
        self._drag_kind = None
        self._geom = None
        self._move_svgs = []
        self._resize_svg = None
        self._resize_anchor_pt = None
        self._resize_local_anchor = None
        self._press_scene = None
        self._pending_click = None
        self._drag_moved = False
        self._sel_snapshot = None
        self._pan_state = None
        self._clear_box_item()
        if hasattr(self, "_last_drag"):
            delattr(self, "_last_drag")
        self._last_path_click = None

    def delete_selected(self):
        if self.doc.delete_selection():
            self._after_edit("Deleted")

    TRANSFORM_MESSAGES = {("rotate", 90.0): "Rotated right", ("rotate", -90.0): "Rotated left",
                          ("flip_h", 0.0): "Flipped horizontally", ("flip_v", 0.0): "Flipped vertically"}

    def transform_selected(self, op, deg=0.0):
        """Rotate / flip the selection (Document.transform_selection) as one edit. Text being
        typed is finished first, so it turns as a whole object. Returns True if anything changed."""
        if self._drag_kind is not None:
            return False
        self._text_finish()
        self.doc.promote_text_selection()
        if not self.doc.transform_selection(op, deg):
            self.status.emit("Nothing selected")
            return False
        self._after_edit(self.TRANSFORM_MESSAGES.get((op, float(deg)), "Rotated"))
        return True

    def rotate_selected(self, deg):
        return self.transform_selected("rotate", deg)

    def flip_selected(self, vertical=False):
        return self.transform_selected("flip_v" if vertical else "flip_h")

    def keyReleaseEvent(self, ev):
        if ev.key() == QtCore.Qt.Key_Space and not ev.isAutoRepeat() and self._space_hand:
            self._space_hand = False
            if self._drag_kind != "pan":
                self._update_cursor()
        super().keyReleaseEvent(ev)

    def _snap_selection(self):
        sel = self.doc.selection
        return (frozenset(sel.path_ids), frozenset(sel.segments),
                frozenset(sel.points), frozenset(sel.svg_ids))

    def mousePressEvent(self, ev):
        self.setFocus(QtCore.Qt.MouseFocusReason)   # arrow/Delete keys go to the canvas
        if ev.button() == QtCore.Qt.MiddleButton or self._space_hand or self.tool == "hand":
            if ev.button() == QtCore.Qt.MiddleButton or ev.button() == QtCore.Qt.LeftButton:
                self._pan_press(ev)
            return
        if ev.button() != QtCore.Qt.LeftButton:
            super().mousePressEvent(ev)
            return
        sp = self.mapToScene(ev.pos())
        self._press_scene = (sp.x(), sp.y())
        self._drag_moved = False
        press = self._tool_press.get(self.tool)
        if press is not None:
            press(ev, sp)

    def _press_box(self, ev, sp):
        self._drag_kind = "box"
        self._box_origin = (sp.x(), sp.y())
        self._sel_snapshot = self._snap_selection()
        self._ensure_box_item(sp.x(), sp.y(), sp.x(), sp.y())

    def _press_zoom(self, ev, sp):
        self._drag_kind = "zoombox"
        self._box_origin = (sp.x(), sp.y())
        self._ensure_box_item(sp.x(), sp.y(), sp.x(), sp.y())

    def _seg_selected(self, pid, si):
        sel = self.doc.selection
        return pid in sel.path_ids or (pid, si) in sel.segments

    def _press_select(self, ev, sp):
        """Mouse / transform tool. Priority: resize handle > bezier handle > control point >
        selected line > whole-SVG move > box / click select."""
        it = self._pick(ev.pos())
        kind = self._item_kind(it) if it is not None else None
        if kind == "handle" and self.handles_enabled():
            svg_id = it.data(0)
            bbox = self.doc.selection_bbox()
            if bbox and svg_id:
                fixed = self._anchor_point(bbox)
                if fixed is None:
                    fixed = self._opposite_anchor(bbox, it.data(1))
                self._resize_anchor_pt = fixed
                svg = self.doc.svgs[svg_id]
                s0 = svg.scale or 1.0
                self._resize_local_anchor = Xf.of(svg).to_local(*fixed)
                self._resize_init_dist = max(1e-6, math.hypot(sp.x() - fixed[0], sp.y() - fixed[1]))
                self._resize_svg = svg_id
                self._resize_init_scale = s0
                self._drag_kind = "resize"
                return
        if kind == "bhandle":
            self._begin_bhandle(it, sp)
            return
        if kind == "point":
            self._begin_geom("move_geom", sp, it, ev.modifiers(), point=(it.data(0), int(it.data(1))))
            return
        if kind == "seg" and self._seg_selected(it.data(0), int(it.data(1))):
            self._begin_geom("move_geom", sp, it, ev.modifiers())
            return
        rt = self._resize_target()
        bbox = self.doc.selection_bbox()
        in_box = bbox is not None and self._in_bbox(sp.x(), sp.y(), bbox, self._px(BBOX_PAD_PX))
        if self.tool == "transform":
            if bbox is not None and in_box and self.doc.svg_ids_for_selection():
                self._drag_kind = "move_svg"
                self._move_svgs = sorted(self.doc.svg_ids_for_selection())
                self._last_drag = (sp.x(), sp.y())
                return
            self._drag_kind = None
            self._pending_click = (it, ev.modifiers())
            return
        # mouse tool
        if rt is not None and bbox is not None and in_box:
            self._drag_kind = "move_svg"
            self._move_svgs = [rt]
            self._last_drag = (sp.x(), sp.y())
            return
        if self.mouse_box_enabled and not in_box:
            self._press_box(ev, sp)
            return
        self._drag_kind = None
        self._pending_click = (it, ev.modifiers())

    def _press_bend(self, ev, sp):
        it = self._pick(ev.pos(), kinds=("bhandle", "seg"))
        kind = self._item_kind(it) if it is not None else None
        if kind == "seg" and self._is_text_path(it.data(0)):
            kind = None     # text is rebuilt from its letters: convert it to paths to bend it
        if kind == "bhandle":
            self._begin_bhandle(it, sp)
            return
        if kind == "seg":
            pid, si = it.data(0), int(it.data(1))
            p = self.doc.paths.get(pid)
            if p is not None and p.segment_count() > 0:
                t, _dist = self.doc.nearest_t_on_segment(p, si, sp.x(), sp.y())
                self._begin_geom("bend", sp, it, ev.modifiers(), pid=pid, si=si, t=t)
                return
        self._drag_kind = None
        self._pending_click = (it, ev.modifiers())   # click on nothing / a point: plain select

    # -- geometry drags (move_geom / bhandle / bend share one dead-zone state in self._geom) --
    def _begin_geom(self, drag_kind, sp, it, mods, **extra):
        """Start a drag that only takes effect after DRAG_PX of movement. Releasing sooner is a
        plain click (_end_geom replays it through _click_select)."""
        self._drag_kind = drag_kind
        self._geom = dict(press=(sp.x(), sp.y()), last=(sp.x(), sp.y()), active=False, mods=mods, **extra)
        self._pending_click = (it, mods) if drag_kind != "bhandle" else None

    def _begin_bhandle(self, it, sp):
        idx, which = it.data(1)
        c = it.sceneBoundingRect().center()
        self._begin_geom("bhandle", sp, it, QtCore.Qt.NoModifier, pid=it.data(0), idx=int(idx), which=which,
                         off=(c.x() - sp.x(), c.y() - sp.y()))

    def _geom_delta(self, sp):
        """(first_tick, dx_mm, dy_mm) since the previous tick, or None inside the dead zone."""
        g = self._geom
        if g is None:
            return None
        first = not g["active"]
        if first:
            if math.hypot(sp.x() - g["press"][0], sp.y() - g["press"][1]) * self._px_per_mm() < DRAG_PX:
                return None
            g["active"] = True
        lx, ly = g["last"]
        g["last"] = (sp.x(), sp.y())
        return first, sp.x() - lx, sp.y() - ly

    def _geom_changed(self):
        self._drag_moved = True
        self._schedule_refresh()
        self.svg_dragging.emit()

    def _move_geom_tick(self, ev, sp):
        step = self._geom_delta(sp)
        if step is None:
            return
        first, dx, dy = step
        g = self._geom
        if first:   # selection is only touched once the press turned into a real drag
            sel = self.doc.selection
            key = g.get("point")
            if key is not None and key not in sel.points:
                if g["mods"] & (QtCore.Qt.ShiftModifier | QtCore.Qt.ControlModifier):
                    sel.points.add(key)
                else:
                    sel.clear()
                    sel.points.add(key)
                self._last_path_click = None
                self.selection_changed.emit()
            g["refs"] = self.doc.selection_anchor_refs()
        self.doc.move_anchors(g["refs"], dx, dy)
        self._geom_changed()

    def _bhandle_tick(self, ev, sp):
        if self._geom_delta(sp) is None:
            return
        g = self._geom
        mirror = not (ev.modifiers() & QtCore.Qt.AltModifier)
        self.doc.set_handle(g["pid"], g["idx"], g["which"],
                            sp.x() + g["off"][0], sp.y() + g["off"][1], mirror)
        self._geom_changed()

    def _bend_tick(self, ev, sp):
        step = self._geom_delta(sp)
        if step is None:
            return
        g = self._geom
        p = self.doc.paths.get(g["pid"])
        if p is None or p.segment_count() == 0:
            return
        if step[0]:   # the handles as they were when the drag began
            n = len(p.points)
            g["base"] = (p.points[g["si"] % n].hout, p.points[(g["si"] + 1) % n].hin)
        px, py = g["press"]
        self.doc.bend_segment_to(g["pid"], g["si"], g["t"], g["base"], sp.x() - px, sp.y() - py,
                                 (sp.x(), sp.y()), self.bend_strength, self.bend_focus)
        self._geom_changed()

    def _end_geom(self, ev, sp):
        g, self._geom = self._geom, None
        kind, self._drag_kind = self._drag_kind, None
        pending, self._pending_click = self._pending_click, None
        self._press_scene = None
        if g is not None and g["active"]:
            self._end_svg_drag()
            return
        self._drag_moved = False
        if pending is not None:   # no movement: behave like the click it was
            self._click_select(ev, pending[0], pending[1])

    def leaveEvent(self, ev):
        self.cursor_moved.emit(None)
        super().leaveEvent(ev)

    def mouseMoveEvent(self, ev):
        sp = self.mapToScene(ev.pos())
        self.cursor_moved.emit((sp.x(), sp.y()))
        if self._drag_kind == "pan":
            self._pan_move(ev)
            return
        tick = self._drag_moves.get(self._drag_kind)
        if tick is not None:
            tick(ev, sp)
            return
        if self._drag_kind is None and self.tool in DRAW_TOOLS:
            self._draw_hover(ev, sp)
        if self._drag_kind in ("box", "zoombox") and self._box_origin is not None:
            self._ensure_box_item(self._box_origin[0], self._box_origin[1], sp.x(), sp.y())
            return
        super().mouseMoveEvent(ev)

    def _move_svg_tick(self, ev, sp):
        if not self._move_svgs:
            return
        if not hasattr(self, "_last_drag"):
            self._last_drag = self._press_scene
        lx, ly = self._last_drag
        for svg_id in self._move_svgs:
            if svg_id in self.doc.svgs:
                self.doc.move_svg(svg_id, sp.x() - lx, sp.y() - ly)
        self._last_drag = (sp.x(), sp.y())
        self._drag_moved = True
        self._schedule_refresh()
        self.svg_dragging.emit()

    def _resize_tick(self, ev, sp):
        if self._resize_svg is None:
            return
        ax, ay = self._resize_anchor_pt
        dist = max(1e-6, math.hypot(sp.x() - ax, sp.y() - ay))
        new_scale = self._resize_init_scale * dist / self._resize_init_dist
        svg = self.doc.svgs.get(self._resize_svg)
        if svg is not None:
            svg.scale = max(0.01, min(100.0, new_scale))
            now = Xf.of(svg).to_abs(*self._resize_local_anchor)   # keep the anchor where it was
            svg.offset_x_mm += ax - now[0]
            svg.offset_y_mm += ay - now[1]
            self._drag_moved = True
            self._schedule_refresh()
            self.svg_dragging.emit()

    def mouseReleaseEvent(self, ev):
        if self._drag_kind == "pan":
            self._pan_end(ev)
            return
        if ev.button() != QtCore.Qt.LeftButton:
            super().mouseReleaseEvent(ev)
            return
        sp = self.mapToScene(ev.pos())
        end = self._drag_ends.get(self._drag_kind)
        if end is not None:
            end(ev, sp)
            return
        if self._drag_kind == "box":
            self._drag_kind = None
            if self._box_is_click(sp):
                pending_it = self._pick(ev.pos())
                self._clear_box_item()
                self._press_scene = None
                if self.tool == "mouse":
                    self._click_select(ev, pending_it, ev.modifiers())
                return
            self._box_select(sp)
            self._clear_box_item()
            self._press_scene = None
            return
        if self._drag_kind == "zoombox":
            self._drag_kind = None
            self._zoom_to_box(sp)
            self._clear_box_item()
            self._press_scene = None
            return
        pending = getattr(self, "_pending_click", None)
        self._pending_click = None
        self._press_scene = None
        if self.tool in ("mouse", "transform", "bend") and pending is not None:
            it, mods = pending
            self._click_select(ev, it, mods)

    def _end_move_svg(self, ev, sp):
        self._drag_kind = None
        self._move_svgs = []
        if hasattr(self, "_last_drag"):
            delattr(self, "_last_drag")
        self._press_scene = None
        self._end_svg_drag()

    def _end_resize(self, ev, sp):
        self._drag_kind = None
        self._resize_svg = None
        self._press_scene = None
        self._end_svg_drag()

    def _end_svg_drag(self):
        moved, self._drag_moved = self._drag_moved, False
        if moved:
            self._refresh_pending = False   # svg_moved -> refresh_all rebuilds the canvas
            self.svg_moved.emit()

    # -- context menu (right click) --
    def _is_item_selected(self, kind, pid, sub):
        sel = self.doc.selection
        p = self.doc.paths.get(pid)
        if p is None or p.svg_id in sel.svg_ids or pid in sel.path_ids:
            return True
        if kind == "seg":
            return (pid, int(sub)) in sel.segments
        return (pid, int(sub)) in sel.points

    def select_for_context(self, view_pos):
        """Right click: an unselected line / point under the cursor becomes the selection."""
        it = self._pick(view_pos, kinds=("point", "seg", "image"))
        if it is None:
            return
        kind, pid, sub = it.data(2), it.data(0), it.data(1)
        sel = self.doc.selection
        if kind == "image":
            if pid in sel.svg_ids or pid not in self.doc.svgs:
                return
            sel.clear()
            sel.svg_ids.add(pid)
            self._last_path_click = None
            self.selection_changed.emit()
            return
        if self._is_item_selected(kind, pid, sub):
            return
        sel.clear()
        if kind == "point":
            sel.points.add((pid, int(sub)))
        else:
            sel.path_ids.add(pid)
        self._last_path_click = None
        self.selection_changed.emit()

    def _selected_seg_refs(self):
        """Segments of the selected lines: explicit segments plus all segments of selected paths."""
        d = self.doc
        refs = set()
        for pid in d.selection.path_ids:
            p = d.paths.get(pid)
            if p is not None:
                refs.update((pid, i) for i in range(p.segment_count()))
        for pid, i in d.selection.segments:
            p = d.paths.get(pid)
            if p is not None and 0 <= i < p.segment_count():
                refs.add((pid, i))
        return refs

    def context_actions(self):
        """Which context-menu actions would do something for the current selection."""
        d = self.doc
        sel = d.selection
        paths = {pid for pid in sel.path_ids if pid in d.paths}
        movable = bool(paths) or any(pid in d.paths and 0 <= i < d.paths[pid].segment_count()
                                     for pid, i in sel.segments)
        want = set(paths)
        for sid in sel.svg_ids:
            if sid in d.svgs:
                want.update(q for q in d.svgs[sid].path_ids if q in d.paths)
        text = bool(d.text_svg_ids(d.svg_ids_for_selection()))
        bb = d.selection_bbox()     # rotate / flip: something wider than a single point
        return {"break_apart": (movable and not text)     # text: Convert to Paths first
                or bool(self._selected_pdf_ids()),
                "combine": len({d.paths[q].svg_id for q in want}) >= 2 and not text,
                "straighten": not text and any(d.segment_is_curve(d.paths[pid], i)
                                               for pid, i in self._selected_seg_refs()),
                "convert_text": text,
                "transform": bb is not None and (bb[2] - bb[0] > 1e-9 or bb[3] - bb[1] > 1e-9),
                "delete": not sel.is_empty()}

    def _is_text_path(self, pid):
        p = self.doc.paths.get(pid)
        s = self.doc.svgs.get(p.svg_id) if p is not None else None
        return s is not None and s.kind == "text"

    def ctx_convert_text(self):
        d = self.doc
        n = d.convert_text_to_paths(d.svg_ids_for_selection())
        if n:
            self._after_edit("Converted %d text object%s to paths" % (n, "" if n == 1 else "s"))

    def _after_edit(self, msg=None):
        self._last_path_click = None
        if msg:
            self.status.emit(msg)
        self.selection_changed.emit()

    def _selected_pdf_ids(self):
        d = self.doc
        return [sid for sid in d.svgs if sid in d.selection.svg_ids and d.svgs[sid].kind == "pdf"]

    def ctx_break_apart(self):
        pdfs = self._selected_pdf_ids()
        if pdfs:
            self._break_apart_pdfs(pdfs)
        elif self.doc.break_apart():
            self._after_edit("Broke apart into a new object")

    def _break_apart_pdfs(self, pdf_ids):
        """Each selected PDF object becomes its page's separate elements (image_ops.break_apart_pdf).
        The new objects end up selected."""
        d = self.doc
        made, skipped, errors = [], 0, []
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            for sid in pdf_ids:
                label = d.svgs[sid].label
                try:
                    ids, n = image_ops.break_apart_pdf(d, sid)
                except Exception as e:
                    errors.append("%s: %s" % (label, e))
                    continue
                if not ids:
                    errors.append("%s: nothing to break apart" % label)
                made += ids
                skipped += n
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if made:
            d.selection.clear()
            d.selection.svg_ids.update(made)
        msg = "Broke PDF apart into %d object%s" % (len(made), "" if len(made) == 1 else "s") if made else ""
        if skipped:
            msg += " (%d unsupported element%s left out)" % (skipped, "" if skipped == 1 else "s")
        if errors:
            msg = ". ".join(([msg] if msg else []) + ["Break Apart failed for " + ", ".join(errors)])
        self._after_edit(msg)

    def ctx_combine(self):
        if self.doc.combine():
            self._after_edit("Combined into one object")

    def ctx_straighten(self):
        refs = [r for r in self._selected_seg_refs() if self.doc.segment_is_curve(self.doc.paths[r[0]], r[1])]
        if refs:
            self.doc.straighten_segments(refs)
            self._after_edit("Straightened %d curve(s)" % len(refs))

    def build_context_menu(self):
        en = self.context_actions()
        menu = QtWidgets.QMenu(self)
        for key, label, fn in (("break_apart", "Break Apart", self.ctx_break_apart),
                               ("combine", "Combine", self.ctx_combine),
                               ("straighten", "Straighten Curves", self.ctx_straighten),
                               ("convert_text", "Convert to Paths", self.ctx_convert_text)):
            menu.addAction(label, fn).setEnabled(en[key])
        menu.addSeparator()
        for label, fn in (("Rotate Right", lambda: self.rotate_selected(90.0)),
                          ("Rotate Left", lambda: self.rotate_selected(-90.0)),
                          ("Flip Horizontal", lambda: self.flip_selected(False)),
                          ("Flip Vertical", lambda: self.flip_selected(True))):
            menu.addAction(label, fn).setEnabled(en["transform"])
        menu.addSeparator()
        menu.addAction("Delete", self.delete_selected).setEnabled(en["delete"])
        return menu

    def mouseDoubleClickEvent(self, ev):
        if (self.tool in ("mouse", "transform") and ev.button() == QtCore.Qt.LeftButton
                and not self._space_hand and self._drag_kind is None):
            sp = self.mapToScene(ev.pos())
            if self._text_at(sp.x(), sp.y()) is not None:   # double-click text: type into it
                self.set_tool(TYPE_TOOL)
                self.edit_text_at(sp.x(), sp.y())
                ev.accept()
                return
        if self.tool in DRAW_TOOLS and ev.button() == QtCore.Qt.LeftButton and not self._space_hand:
            self._draw_double_click(ev)   # press, release, doubleClick, release: see DrawMixin
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)

    def contextMenuEvent(self, ev):
        if self.tool in DRAW_TOOLS and not self._space_hand and self._drag_kind is None:
            self._draw_finish()   # right-click finishes the shape in progress instead of a menu
            ev.accept()
            return
        if self.tool not in CONTEXT_TOOLS or self._space_hand or self._drag_kind is not None:
            super().contextMenuEvent(ev)
            return
        self._text_finish()     # right-click while typing: finish, then the menu
        self.select_for_context(ev.pos())
        self.doc.promote_text_selection()
        menu = self.build_context_menu()
        menu.exec(ev.globalPos())
        menu.deleteLater()
        ev.accept()

    # -- rubber band --
    def _ensure_box_item(self, x0, y0, x1, y1):
        if self._box_item is not None:
            try:
                self.scene.removeItem(self._box_item)
            except Exception:
                pass
        pen = QtGui.QPen(QtGui.QColor("#00bcd4"), 0.4)
        pen.setStyle(QtCore.Qt.DashLine)
        self._box_item = self.scene.addRect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0), pen)
        self._box_item.setZValue(20)
        self._box_item.setAcceptedMouseButtons(QtCore.Qt.NoButton)

    def _clear_box_item(self):
        if self._box_item is not None:
            try:
                self.scene.removeItem(self._box_item)
            except Exception:
                pass
            self._box_item = None
        self._box_origin = None

    def _box_is_click(self, sp):
        if self._box_origin is None or self._press_scene is None:
            return True
        px = self._px_per_mm()
        return abs(sp.x() - self._press_scene[0]) + abs(sp.y() - self._press_scene[1]) < 4 / max(1e-6, px)

    def _box_select(self, sp):
        if self._box_origin is None:
            return
        x0, y0 = self._box_origin
        rect = QtCore.QRectF(min(x0, sp.x()), min(y0, sp.y()), abs(sp.x() - x0), abs(sp.y() - y0))
        d = self.doc
        paths, segs, pts, svgs = set(), set(), set(), set()
        if self.box_mode == "points":
            for p in d.paths.values():
                svg = d.svgs.get(p.svg_id)
                if svg is not None and not svg.visible:
                    continue
                for i, (x, y, _t) in enumerate(d.path_abs_points(p)):
                    if rect.contains(x, y):
                        pts.add((p.id, i))
        else:
            for item in self.scene.items(rect):
                try:
                    if item.data(2) == "seg":
                        pid, si = item.data(0), int(item.data(1))
                    else:
                        continue
                except RuntimeError:
                    continue
                if self.box_mode == "segments":
                    segs.add((pid, si))
                else:
                    paths.add(pid)
            if self.box_mode == "paths":
                # image / pdf objects join only when the box fully encloses their raster (a box
                # dragged across a picture to grab its outline lines must not grab the picture)
                for sid, svg in d.svgs.items():
                    if not (image_ops.is_raster(svg) and svg.visible):
                        continue
                    bb = d.svg_bbox(sid)
                    if bb and rect.contains(QtCore.QRectF(bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1])):
                        svgs.add(sid)
                        paths.difference_update(svg.path_ids)   # the object covers its outline paths
        picked = (paths, segs, pts, svgs)
        snap = self._sel_snapshot or (frozenset(), frozenset(), frozenset(), frozenset())
        c_paths, c_segs, c_pts, c_svgs = combine_selection(snap, picked, self.select_mode)
        sel = d.selection
        sel.path_ids = set(c_paths)
        sel.segments = set(c_segs)
        sel.points = set(c_pts)
        sel.svg_ids = set(c_svgs)
        self.selection_changed.emit()

    def _zoom_to_box(self, sp):
        x0, y0 = self._box_origin
        w, h = abs(sp.x() - x0), abs(sp.y() - y0)
        if w < 1e-3 or h < 1e-3:          # a click: zoom in one step
            if self.zoom_to_mouse:
                self._zoom_at(1.25, sp)
            else:
                self._zoom_by(1.25, center=sp)
            return
        box = QtCore.QRectF(min(x0, sp.x()), min(y0, sp.y()), w, h)
        self.fitInView(box, QtCore.Qt.KeepAspectRatio)
        self.scale(0.9, 0.9)  # small margin around the box
        self._update_pan_bounds(box.center())
        self._sync_zoom()

    # -- mouse/transform-tool selection --
    def _click_select(self, ev, it, mods):
        sel = self.doc.selection
        shift = bool(mods & QtCore.Qt.ShiftModifier)
        ctrl = bool(mods & QtCore.Qt.ControlModifier)
        alt = bool(mods & QtCore.Qt.AltModifier)
        sp = self.mapToScene(ev.pos())
        bbox = self.doc.selection_bbox()
        pad = PICK_PX / max(1e-6, self._px_per_mm())   # lines lie on the bbox edge: allow a few px
        inside = bbox is not None and self._in_bbox(sp.x(), sp.y(), bbox, pad)
        if it is None:
            if inside:
                return
            if not shift and not ctrl:
                sel.clear()
                self._last_path_click = None
                self.selection_changed.emit()
            return
        kind = it.data(2)
        pid = it.data(0)
        sub = it.data(1)
        if kind in ("handle", "bhandle"):
            return
        if kind == "image":   # a raster object: selects the whole object (move / resize work as for SVGs)
            if pid not in self.doc.svgs:
                return
            if shift or ctrl:
                if pid in sel.svg_ids:
                    sel.svg_ids.discard(pid)
                else:
                    sel.svg_ids.add(pid)
            else:
                sel.clear()
                sel.svg_ids.add(pid)
            self._last_path_click = None
            self.selection_changed.emit()
            return
        if alt and pid in self.doc.paths:
            if not shift:
                sel.clear()
            sel.svg_ids.add(self.doc.paths[pid].svg_id)
            self.selection_changed.emit()
            return
        if not inside and not sel.is_empty():
            sel.clear()
            self._last_path_click = None
        if kind == "point":
            key = (pid, int(sub))
            if shift or ctrl:
                if key in sel.points:
                    sel.points.discard(key)
                else:
                    sel.points.add(key)
            else:
                sel.clear()
                sel.points.add(key)
            self.selection_changed.emit()
            return
        seg_idx = int(sub)
        if ctrl:
            key = (pid, seg_idx)
            if shift:
                if key in sel.segments:
                    sel.segments.discard(key)
                else:
                    sel.segments.add(key)
            else:
                if key in sel.segments and len(sel.segments) == 1 and not sel.path_ids and not sel.svg_ids:
                    sel.segments.discard(key)
                else:
                    sel.clear()
                    sel.segments.add(key)
            self.selection_changed.emit()
            return
        if shift:
            sel.path_ids.add(pid)
            self.selection_changed.emit()
            return
        if sel.single_path_id() == pid and self._last_path_click == pid:
            mm_per_px = 1.0 / max(1e-6, self._px_per_mm())
            p = self.doc.paths[pid]
            pts = self.doc.path_abs_points(p)
            near_pt = any(abs(sp.x() - x) < 5 * mm_per_px and abs(sp.y() - y) < 5 * mm_per_px
                          for x, y, _t in pts)
            if not near_pt:
                sel.clear()
                sel.segments.add((pid, seg_idx))
                self._last_path_click = None
                self.selection_changed.emit()
                return
        sel.clear()
        sel.path_ids.add(pid)
        self._last_path_click = pid
        self.selection_changed.emit()
