"""Drawing tools for the canvas (mixin): line, freehand, curve (pen), polygon, rect, ellipse.

Interaction (all in scene mm, see core.shapes for the geometry helpers):
- line:     click = add vertex (polyline). Press-drag-release on the FIRST click = one single line.
            Enter / double-click / right-click finishes, clicking the first vertex (within CLOSE_PX
            screen px, 3+ vertices) closes, Shift = 15 deg steps, Backspace/Delete drops the last
            vertex and Esc cancels.
- freehand: press-drag-release. The stroke is simplified (RDP) with the Smoothing option (mm) and
            closes when it ends within CLOSE_PX of its start.
- curve:    click = corner anchor, press-drag = smooth anchor (hout = drag point, hin mirrored).
            Finish / close / cancel like line. Shift = 15 deg steps for the anchor position.
- polygon:  drag from the centre, with the Sides option. Shift snaps the rotation to 15 deg.
- rect:     drag a box corner to corner. Shift = square, Ctrl = from the centre.
            polygon and rect share the Corner radius option (mm, 0 = sharp corners): stored on
            each corner point (ControlPoint.radius), the corners stay single points.
- ellipse:  drag a bounding box. Shift = circle, Ctrl = from the centre.

Every new shape gets the Color option and the Thickness option (each point's line thickness, mm).

Session: shapes go into one "Drawing N" object (_draw_svg_id) until a non-drawing tool is
selected or that object disappears. Finished shapes become the selection.

The in-progress shape is kept in self._draw_state. _rebuild redraws it through
_draw_preview_items() (items of kind "preview_draw", ignored by _pick), and mouse moves only touch
those preview items, never the whole scene.
"""
import math

from PySide6 import QtGui, QtCore
from core import shapes
from core.doc import DEFAULT_THICKNESS, ControlPoint, VectorPath

DRAW_TOOLS = ("line", "freehand", "curve", "polygon", "rect", "ellipse")
DRAW_HINTS = {
    "line": "Click to add points, or drag for a single line. Double-click or Enter to finish, "
            "Esc to cancel. Shift snaps to 15°.",
    "freehand": "Drag to draw. End near the starting point to close the shape.",
    "curve": "Click for a corner, drag for a smooth point. Double-click or Enter to finish, "
             "Esc to cancel. Shift snaps to 15°.",
    "polygon": "Drag from the center. Shift snaps the rotation to 15°.",
    "rect": "Drag to draw. Hold Shift for a square, Ctrl to draw from the center.",
    "ellipse": "Drag to draw. Hold Shift for a circle, Ctrl to draw from the center.",
}
CLOSE_PX = 8.0      # clicking/ending within this many screen px of the first vertex closes
DRAG_PX = 3.0       # a press moving less than this is a click
DUP_PX = 1.0        # vertices closer than this to the previous one are ignored
SNAP_DEG = 15.0
PREVIEW_Z = 15
MIN_SIDES, MAX_SIDES = 3, 32
MAX_THICKNESS_MM = 10.0
MAX_RADIUS_MM = 500.0
BOX_TOOLS = ("rect", "ellipse")    # drag a bounding box
POLY_TOOLS = ("line", "curve")


class DrawMixin:
    """State + behaviour of the drawing tools. Canvas wires the dispatch tables and key/menu hooks."""

    def _init_draw(self):
        self.draw_color = "#000000"   # upper-case #RRGGBB
        self.draw_smooth_mm = 0.3
        self.draw_sides = 6
        self.draw_thickness_mm = DEFAULT_THICKNESS
        self.draw_radius_mm = 0.0    # corner radius of polygons and rectangles
        self._draw_svg_id = None     # object the next shapes go into (the draw session)
        self._draw_state = None      # shape in progress (dict) or None
        self._preview_items = []

    # -- options (panels drive these) --
    def set_draw_color(self, color):
        c = QtGui.QColor(color)
        if c.isValid() and c.name().upper() != self.draw_color:
            self.draw_color = c.name().upper()   # same form as doc.normalize_color
            self.options_changed.emit()
            self._update_draw_preview()

    def set_draw_smooth(self, mm):
        mm = max(0.0, min(10.0, float(mm)))
        if abs(mm - self.draw_smooth_mm) > 1e-9:
            self.draw_smooth_mm = mm
            self.options_changed.emit()

    def set_draw_thickness(self, mm):
        mm = max(0.0, min(MAX_THICKNESS_MM, float(mm)))
        if abs(mm - self.draw_thickness_mm) > 1e-9:
            self.draw_thickness_mm = mm
            self.options_changed.emit()

    def set_draw_sides(self, n):
        n = max(MIN_SIDES, min(MAX_SIDES, int(n)))
        if n != self.draw_sides:
            self.draw_sides = n
            self.options_changed.emit()
            self._update_draw_preview()

    def set_draw_radius(self, mm):
        mm = max(0.0, min(MAX_RADIUS_MM, float(mm)))
        if abs(mm - self.draw_radius_mm) > 1e-9:
            self.draw_radius_mm = mm
            self.options_changed.emit()
            self._update_draw_preview()

    # -- small geometry helpers --
    def _px_dist(self, a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1]) * self._px_per_mm()

    def _poly_point(self, pts, p, mods):
        """Where a new vertex goes: the cursor, or 15 deg steps from the last vertex with Shift."""
        if pts and mods & QtCore.Qt.ShiftModifier:
            return shapes.constrain_angle(pts[-1][0], pts[-1][1], p[0], p[1], SNAP_DEG)
        return p

    # -- session / lifecycle --
    def _draw_on_tool_change(self, new):
        """Called by set_tool before self.tool changes."""
        if self._draw_state is not None:
            self._draw_finish()   # a line/pen shape is kept, a half-done drag is dropped
        if new not in DRAW_TOOLS:
            self._draw_svg_id = None   # leaving the drawing tools ends the session

    def _draw_cancel(self):
        self._draw_state = None
        self._drag_kind = None if self._drag_kind == "draw" else self._drag_kind
        self._update_draw_preview()

    def _draw_finish(self, closed=False):
        """Finish the in-progress line/pen shape (right-click / Enter / double-click). Anything
        else in progress (a drag) is discarded."""
        st = self._draw_state
        if st is None:
            return
        if st["tool"] in POLY_TOOLS:
            self._finish_poly(closed)
        else:
            self._draw_cancel()

    def _commit_shape(self, anchors, closed, what, radius=0.0):
        d = self.doc
        if self._draw_svg_id not in d.svgs:
            self._draw_svg_id = d.add_drawing()
        pid = d.add_drawn_path(self._draw_svg_id, anchors, closed, self.draw_color,
                               self.draw_thickness_mm, radius)
        d.selection.clear()
        d.selection.path_ids = {pid}
        self._draw_state = None
        self._drag_kind = None if self._drag_kind == "draw" else self._drag_kind
        self._clear_draw_preview()
        n = len(anchors)
        self._after_edit("Added %s (%d point%s%s)" % (what.lower(), n, "" if n == 1 else "s",
                                                      ", closed" if closed else ""))
        return pid

    # -- press / drag / release (registered in Canvas._init_dispatch) --
    def _press_draw(self, ev, sp):
        mods = ev.modifiers()
        tool = self.tool
        p = (sp.x(), sp.y())
        st = self._draw_state
        if st is not None and st["tool"] != tool:
            self._draw_cancel()
            st = None
        self._drag_kind = "draw"
        if tool in POLY_TOOLS:
            self._press_poly(p, mods, st)
        elif tool == "freehand":
            self._draw_state = {"tool": tool, "raw": [p]}
        elif tool == "polygon":
            self._draw_state = {"tool": tool, "c": p, "cur": p, "mods": mods}
        else:
            self._draw_state = {"tool": tool, "p0": p, "cur": p, "mods": mods}
        self._update_draw_preview()

    def _press_poly(self, p, mods, st):
        if st is None:
            self._draw_state = {"tool": self.tool, "pts": [[p[0], p[1], None, None]], "cur": None,
                                "press": p, "first": True, "drag_idx": 0, "dragged": False}
            return
        pts = st["pts"]
        if len(pts) >= 3 and self._px_dist(p, pts[0]) <= CLOSE_PX:
            self._drag_kind = None
            self._finish_poly(closed=True)
            return
        q = self._poly_point(pts, p, mods)
        st.update(first=False, press=p, dragged=False, cur=None)
        if self._px_dist(q, pts[-1]) >= DUP_PX:
            pts.append([q[0], q[1], None, None])
            st["drag_idx"] = len(pts) - 1
        else:
            st["drag_idx"] = None   # a repeated click on the same spot adds nothing

    def _draw_drag_tick(self, ev, sp):
        st = self._draw_state
        if st is None:
            return
        p = (sp.x(), sp.y())
        mods = ev.modifiers()
        t = st["tool"]
        if t in POLY_TOOLS:
            if self._px_dist(p, st["press"]) >= DRAG_PX:
                st["dragged"] = True
            if t == "line":
                st["cur"] = self._poly_point(st["pts"], p, mods) if st["first"] and st["dragged"] else None
            elif st["dragged"] and st["drag_idx"] is not None:
                a = st["pts"][st["drag_idx"]]
                h = shapes.constrain_angle(a[0], a[1], p[0], p[1], SNAP_DEG) \
                    if mods & QtCore.Qt.ShiftModifier else p
                a[3] = h
                a[2] = (2 * a[0] - h[0], 2 * a[1] - h[1])    # smooth anchor: hin mirrors hout
                st["cur"] = None
        elif t == "freehand":
            if self._px_dist(p, st["raw"][-1]) >= 1.0:
                st["raw"].append(p)
        else:
            st["cur"], st["mods"] = p, mods
        self._update_draw_preview()

    def _draw_drag_end(self, ev, sp):
        self._drag_kind = None
        self._press_scene = None
        st = self._draw_state
        if st is None:
            return
        p = (sp.x(), sp.y())
        mods = ev.modifiers()
        t = st["tool"]
        if t == "line":
            first, st["first"] = st["first"], False
            if first and st["dragged"] and len(st["pts"]) == 1:   # press-drag-release = one line
                q = self._poly_point(st["pts"], p, mods)
                if self._px_dist(q, st["pts"][0]) >= DUP_PX:
                    st["pts"].append([q[0], q[1], None, None])
                    self._finish_poly(False)
                    return
        elif t == "curve":
            st["first"] = False
            st["drag_idx"] = None
        elif t == "freehand":
            if self._px_dist(p, st["raw"][-1]) >= 1.0:
                st["raw"].append(p)
            self._finish_freehand()
            return
        elif t == "polygon":
            st["cur"], st["mods"] = p, mods
            self._finish_polygon()
            return
        elif t in BOX_TOOLS:
            st["cur"], st["mods"] = p, mods
            self._finish_rect() if t == "rect" else self._finish_ellipse()
            return
        self._update_draw_preview()

    def _draw_hover(self, ev, sp):
        """Mouse move without a button: the rubber band of a line/pen follows the cursor."""
        st = self._draw_state
        if st is None or st["tool"] not in POLY_TOOLS:
            return
        st["cur"] = self._poly_point(st["pts"], (sp.x(), sp.y()), ev.modifiers())
        self._update_draw_preview()

    def _draw_double_click(self, ev):
        """Qt delivers press, release, doubleClick, release: the press already placed the last
        vertex, so a double-click only finishes (no duplicate vertex)."""
        self._draw_finish()

    def _draw_key(self, ev):
        """Key handling while a drawing tool is active. True = consumed."""
        key = ev.key()
        st = self._draw_state
        if key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            self._draw_finish()
            return True
        if key == QtCore.Qt.Key_Escape:
            if st is not None:
                self._draw_cancel()
                self.status.emit("Cancelled")
            return True
        if key in (QtCore.Qt.Key_Backspace, QtCore.Qt.Key_Delete):
            self._draw_pop_vertex()
            return True   # drawing tools never delete objects
        return False

    def _draw_pop_vertex(self):
        """Drop the last vertex of the line / pen shape in progress (the shape is cancelled when
        none is left).  Returns False when there is no such shape."""
        st = self._draw_state
        if st is None or st["tool"] not in POLY_TOOLS:
            return False
        st["pts"].pop()
        st["cur"] = None
        if not st["pts"]:
            self._draw_state = None
        self._update_draw_preview()
        return True

    def draw_undo_step(self):
        """Ctrl+Z while a shape is being drawn undoes inside the shape, not the document: a
        line / pen shape loses its last vertex, any other half-done shape is cancelled.
        True = consumed."""
        if self._draw_state is None:
            return False
        if not self._draw_pop_vertex():
            self._draw_cancel()
        return True

    # -- finishing --
    def _finish_poly(self, closed=False):
        st = self._draw_state
        tol = 0.5 / max(1e-6, self._px_per_mm())
        pts = []
        for a in st["pts"]:   # drop stray duplicate vertices
            if not pts or math.hypot(a[0] - pts[-1][0], a[1] - pts[-1][1]) > tol:
                pts.append(a)
        if len(pts) < 2:
            self._draw_cancel()
            return
        closed = bool(closed) and len(pts) >= 3
        if st["tool"] == "line":
            anchors = [(a[0], a[1], None, None) for a in pts]
        else:
            anchors = [(a[0], a[1], a[2], a[3]) for a in pts]
            if not closed:   # the end handles of an open path do nothing
                anchors[0] = (anchors[0][0], anchors[0][1], None, anchors[0][3])
                anchors[-1] = (anchors[-1][0], anchors[-1][1], anchors[-1][2], None)
        self._commit_shape(anchors, closed, "Line" if st["tool"] == "line" else "Curve")

    def _finish_freehand(self):
        raw = list(self._draw_state["raw"])
        ppm = self._px_per_mm()
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(raw, raw[1:]))
        if len(raw) < 2 or length * ppm < 2 * DRAG_PX:
            self._draw_cancel()
            return
        closed = len(raw) >= 4 and length * ppm > 3 * CLOSE_PX and self._px_dist(raw[0], raw[-1]) <= CLOSE_PX
        if closed:
            raw[-1] = raw[0]   # snap the end onto the start so the loop simplifies cleanly
        pts = shapes.rdp(raw, max(0.0, self.draw_smooth_mm))
        if closed:
            pts = pts[:-1]     # the closing segment is implicit
            closed = len(pts) >= 3
        if len(pts) < 2:
            self._draw_cancel()
            return
        self._commit_shape([(x, y, None, None) for x, y in pts], closed, "Freehand")

    def _polygon_geom(self, st):
        (cx, cy), (x, y) = st["c"], st["cur"]
        r = math.hypot(x - cx, y - cy)
        ang = math.atan2(y - cy, x - cx)
        if st["mods"] & QtCore.Qt.ShiftModifier:
            step = math.radians(SNAP_DEG)
            ang = round(ang / step) * step
        return cx, cy, r, ang

    def _finish_polygon(self):
        cx, cy, r, ang = self._polygon_geom(self._draw_state)
        if r * self._px_per_mm() < DRAG_PX:   # a click, not a drag
            self._draw_cancel()
            return
        pts = shapes.regular_polygon(cx, cy, r, self.draw_sides, ang)
        self._commit_shape([(x, y, None, None) for x, y in pts], True, "Polygon", self.draw_radius_mm)

    @staticmethod
    def _box_geom(st):
        """(cx, cy, rx, ry) of the box dragged from p0 to cur. Shift = equal sides, Ctrl = from
        the centre."""
        (x0, y0), (x, y) = st["p0"], st["cur"]
        dx, dy = x - x0, y - y0
        mods = st["mods"]
        if mods & QtCore.Qt.ShiftModifier:
            m = max(abs(dx), abs(dy))
            dx = m if dx >= 0 else -m
            dy = m if dy >= 0 else -m
        if mods & QtCore.Qt.ControlModifier:
            return x0, y0, abs(dx), abs(dy)
        return x0 + dx / 2, y0 + dy / 2, abs(dx) / 2, abs(dy) / 2

    def _finish_ellipse(self):
        cx, cy, rx, ry = self._box_geom(self._draw_state)
        ppm = self._px_per_mm()
        if rx * ppm < 1.0 or ry * ppm < 1.0:
            self._draw_cancel()
            return
        self._commit_shape(shapes.ellipse_anchors(cx, cy, rx, ry), True, "Ellipse")

    @staticmethod
    def _rect_points(cx, cy, rx, ry):
        """Corners clockwise from the top-left."""
        return [(cx - rx, cy - ry), (cx + rx, cy - ry), (cx + rx, cy + ry), (cx - rx, cy + ry)]

    def _finish_rect(self):
        cx, cy, rx, ry = self._box_geom(self._draw_state)
        ppm = self._px_per_mm()
        if rx * ppm < 1.0 or ry * ppm < 1.0:
            self._draw_cancel()
            return
        self._commit_shape([(x, y, None, None) for x, y in self._rect_points(cx, cy, rx, ry)], True,
                           "Rectangle", self.draw_radius_mm)

    # -- live preview --
    def _clear_draw_preview(self):
        for it in self._preview_items:
            try:
                self.scene.removeItem(it)
            except RuntimeError:
                pass
        self._preview_items = []

    def _update_draw_preview(self):
        self._clear_draw_preview()
        self._draw_preview_items()
        self.viewport().update()

    def _pv(self, item, z=PREVIEW_Z):
        item.setZValue(z)
        item.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        item.setData(2, "preview_draw")
        self._preview_items.append(item)
        return item

    def _draw_preview_items(self):
        """(Re)create the preview items of the in-progress shape. Also called by _rebuild."""
        st = self._draw_state
        if st is None:
            return
        solid = self._cosmetic_pen(self.draw_color, 1.5)
        rubber = self._cosmetic_pen(self.draw_color, 1.0, QtCore.Qt.DashLine)
        t = st["tool"]
        if t in POLY_TOOLS:
            self._preview_poly(st, solid, rubber)
        elif t == "freehand":
            raw = st["raw"]
            if len(raw) >= 2:
                qp = QtGui.QPainterPath(QtCore.QPointF(*raw[0]))
                for x, y in raw[1:]:
                    qp.lineTo(x, y)
                self._pv(self.scene.addPath(qp, solid))
        elif t == "polygon":
            cx, cy, r, ang = self._polygon_geom(st)
            if r > 1e-9:
                pts = shapes.regular_polygon(cx, cy, r, self.draw_sides, ang)
                self._pv(self.scene.addPath(self._corners_path(pts), solid))
                self._pv(self.scene.addLine(cx, cy, pts[0][0], pts[0][1], rubber))
        elif t == "rect":
            cx, cy, rx, ry = self._box_geom(st)
            if rx > 1e-9 or ry > 1e-9:
                self._pv(self.scene.addPath(self._corners_path(self._rect_points(cx, cy, rx, ry)), solid))
        elif t == "ellipse":
            cx, cy, rx, ry = self._box_geom(st)
            if rx > 1e-9 and ry > 1e-9:
                self._pv(self.scene.addPath(self._anchors_path(shapes.ellipse_anchors(cx, cy, rx, ry), True),
                                            solid))

    def _corners_path(self, pts):
        """QPainterPath of the closed polygon through pts (abs mm) with the Corner radius option,
        flattened exactly as the committed shape will be."""
        tmp = VectorPath(id="", svg_id="", color=self.draw_color, closed=True,
                         points=[ControlPoint(x, y, radius=self.draw_radius_mm) for x, y in pts])
        flat = self.doc.path_abs_flat(tmp)
        qp = QtGui.QPainterPath(QtCore.QPointF(flat[0][0], flat[0][1]))
        for x, y, _t in flat[1:]:
            qp.lineTo(x, y)
        qp.closeSubpath()
        return qp

    @staticmethod
    def _anchors_path(anchors, closed):
        """QPainterPath through (x, y, hin, hout) anchors (cubic where a handle exists)."""
        qp = QtGui.QPainterPath(QtCore.QPointF(anchors[0][0], anchors[0][1]))
        n = len(anchors)
        for i in range(n if closed else n - 1):
            a, b = anchors[i], anchors[(i + 1) % n]
            if a[3] is not None or b[2] is not None:
                c1 = a[3] if a[3] is not None else (a[0], a[1])
                c2 = b[2] if b[2] is not None else (b[0], b[1])
                qp.cubicTo(c1[0], c1[1], c2[0], c2[1], b[0], b[1])
            else:
                qp.lineTo(b[0], b[1])
        if closed:
            qp.closeSubpath()
        return qp

    def _preview_poly(self, st, solid, rubber):
        pts, cur = st["pts"], st["cur"]
        ppm = self._px_per_mm()
        if len(pts) >= 2:
            self._pv(self.scene.addPath(self._anchors_path([tuple(a) for a in pts], False), solid))
        last = pts[-1]
        if cur is not None:
            qp = QtGui.QPainterPath(QtCore.QPointF(last[0], last[1]))
            if st["tool"] == "curve" and last[3] is not None:
                qp.cubicTo(last[3][0], last[3][1], cur[0], cur[1], cur[0], cur[1])
            else:
                qp.lineTo(cur[0], cur[1])
            self._pv(self.scene.addPath(qp, rubber))
        r = 3.0 / ppm
        for i, a in enumerate(pts):
            closing = (i == 0 and len(pts) >= 3 and cur is not None
                       and self._px_dist(cur, a) <= CLOSE_PX)
            rr = r * (1.8 if closing else 1.0)
            pen = QtGui.QPen(QtGui.QColor("#ff5722"), 0)
            pen.setCosmetic(True)
            fill = QtGui.QColor("#ff5722") if closing or i == 0 else QtGui.QColor("#ffffff")
            self._pv(self.scene.addRect(a[0] - rr, a[1] - rr, 2 * rr, 2 * rr, pen, QtGui.QBrush(fill)),
                     PREVIEW_Z + 1)
        if st["tool"] == "curve":   # handles of the newest anchor
            hp = self._cosmetic_pen("#1e88e5", 1.0)
            for h in (last[2], last[3]):
                if h is not None:
                    self._pv(self.scene.addLine(last[0], last[1], h[0], h[1], hp))
                    self._pv(self.scene.addEllipse(h[0] - r * 0.8, h[1] - r * 0.8, r * 1.6, r * 1.6, hp,
                                                   QtGui.QBrush(QtGui.QColor("#ffffff"))), PREVIEW_Z + 1)
