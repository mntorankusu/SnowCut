"""Authoritative icon-button drawer: one painter for every icon in the app.

IconButton (QAbstractButton) paints everything explicitly so buttons can
never collapse, overlap, or render blank regardless of style/palette:
- the icon is an IconPark SVG (icon_sets/), recoloured for the dark theme
  (#333 ink -> light ink, #FFF cut-outs -> dark) and centred in the button.
- `scale` sizes the icon relative to the button (toolbar/options read
  bigger than small panel icons).
- a subtle dark outline always frames the button so it reads as clickable.
- disabled buttons keep the same outline but paint the icon at 25% opacity
  with no hover highlight.
- an optional `checked_name` icon is drawn while a checkable button is
  checked (e.g. the filled ghost).
- hovering any IconButton reports its tooltip through set_hover_hook (the
  main window shows it in the status bar immediately).

ICONS maps app icon names to SVG basenames, looked up in icon_sets/iconpark1
(the hand-picked set) first, then icon_sets/iconpark_full.
"""
import os
import sys
from PySide6 import QtWidgets, QtGui, QtCore, QtSvg

BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ICON_DIRS = [os.path.join(BUNDLE_DIR, "icon_sets", sub) for sub in ("iconpark1", "iconpark_full")]

ICONS = {
    # tools
    "mouse": "move-one",                 # cursor arrow
    "box": "selected-focus",             # dotted selection box
    "zoom": "search",                    # magnifier
    "hand": "five-five",                 # open hand
    "transform": "moving-picture",       # move/resize the picture
    "bend": "curve-adjustment",          # drag a line into a curve
    "tab": "link-break",                 # tab tool: a gap left uncut in a line
    "line": "draw-line",                 # straight line between two nodes (custom, iconpark1)
    "freehand": "writing-fluently",      # pencil scribbling a squiggle
    "curve": "electronic-pen",           # pen tool (bezier anchors)
    "polygon": "pentagon-one",
    "rect": "rectangle-one",
    "ellipse": "oval-one",
    "type": "text",                      # T in a box
    # box target modes
    "points": "circle-five-line",        # nodes
    "segments": "bezier-curve",          # one curve segment with handles
    "paths": "connection-point-two",     # whole path, end to end
    # accumulation modes
    "new": "square",                     # plain (replace) selection
    "add": "union-selection",
    "subtract": "subtract-selection",
    "intersect": "intersect-selection",
    # chrome
    "float": "internal-expansion",       # pop out
    "dock": "internal-reduction",        # pop back in
    "close": "close-small",
    "chev_up": "up",
    "chev_down": "down",
    # misc
    "anchor": "anchor",
    "show": "preview-open",
    "hide": "preview-close-one",
    "ghost": "ghost_outline",
    "ghost_on": "ghost_fill",
    "delete": "delete",
    "apply": "check-small",
    "assign": "arrow-right",
    "plus": "plus",
    "minus": "minus",
    "zoomin": "zoom-in",
    "zoomout": "zoom-out",
    "fit": "full-screen",
    "zoom_mouse": "mouse",               # zoom toward the pointer (off: view centre)
    "cancel": "close-small",
    # device control
    "feed": "import-and-export",
    "cut": "cutting-one",
    "draw": "pencil",
    "emboss": "stamp",
    "skip": "forbid",                    # color group left out of the job
    "scan": "add-picture",
    "jog": "direction-adjustment",
    "load_scan": "picture-album",
    "mount_usb": "u-disk",
    "config": "setting-two",
    "mat": "grid-sixteen",
    "abort": "power",
    # cut panel
    "code_preview": "file-code",
    "generate": "magic",
    "send": "send",
    "resume": "play-one",
    "cancel_send": "close-one",
    # cut order
    "pause": "pause",
    # type tool options
    "align_left": "align-text-left",
    "align_center": "align-text-center",
    "align_right": "align-text-right",
    "bold": "text-bold",
    "italic": "text-italic",
    "faux_bold": "faux_bold",            # custom (iconpark1): B with a plus
    "faux_italic": "faux_italic",        # custom (iconpark1): slanted stroke in a sheared box
}

ACCENT = QtGui.QColor("#0a6cb6")
HOVER_BG = QtGui.QColor("#5a5a5a")
IDLE_BG = QtGui.QColor(255, 255, 255, 14)
DANGER_BG = QtGui.QColor("#8b1e1e")
DANGER_HOVER = QtGui.QColor("#aa2424")
OUTLINE = QtGui.QColor("#161616")
INK = "#f2f2f2"
HOLE = "#1e1e1e"   # cut-outs inside filled icons

_renderers = {}
_hover_hook = None


def set_hover_hook(fn):
    """fn(text) on hover and fn(None) on leave, one hook for the whole app."""
    global _hover_hook
    _hover_hook = fn


def icon_path(name):
    base = ICONS.get(name, name)
    for d in ICON_DIRS:
        p = os.path.join(d, base + ".svg")
        if os.path.exists(p):
            return p
    return None


def renderer(name):
    """Cached recoloured QSvgRenderer for an app icon name (None if missing)."""
    if name in _renderers:
        return _renderers[name]
    r = None
    p = icon_path(name)
    if p is not None:
        try:
            data = open(p, encoding="utf-8").read()
            data = data.replace("#333", INK).replace("#FFF", HOLE).replace("#fff", HOLE)
            r = QtSvg.QSvgRenderer(QtCore.QByteArray(data.encode("utf-8")))
            if not r.isValid():
                r = None
        except OSError:
            r = None
    _renderers[name] = r
    return r


class IconButton(QtWidgets.QAbstractButton):
    """Square icon-only button with fully explicit painting.

    sizeHint/minimumSize are exactly (px, px) so layouts can never collapse
    or overlap buttons. Words live in the tooltip (and the status bar on
    hover), never on the face.
    """

    def __init__(self, name, tooltip="", px=36, scale=0.55, parent=None,
                 checked_name=None, danger=False):
        super().__init__(parent)
        self._name = name
        self._checked_name = checked_name
        self._danger = bool(danger)
        self._px = max(16, int(px))
        self._scale = min(0.95, max(0.2, float(scale)))
        self.setToolTip(tooltip)
        self.setFixedSize(self._px, self._px)
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.toggled.connect(lambda *a: self.update())
        self.pressed.connect(self.update)
        self.released.connect(self.update)

    # -- API --
    def icon_name(self):
        return self._name

    def set_icon_name(self, name):
        if name != self._name:
            self._name = name
            self.update()

    def set_scale(self, scale):
        self._scale = min(0.95, max(0.2, float(scale)))
        self.update()

    def sizeHint(self):
        # Fixed policy alone does not fix sizeHint on all styles/backends,
        # so it is reported explicitly and flow/box layouts can never collapse buttons.
        return QtCore.QSize(self._px, self._px)

    def minimumSizeHint(self):
        return QtCore.QSize(self._px, self._px)

    def glyph_pixel_size(self):
        return max(8, int(round(self._px * self._scale)))

    def _current_icon(self):
        if self._checked_name and self.isCheckable() and self.isChecked():
            return self._checked_name
        return self._name

    # -- painting: the single authoritative drawer --
    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        enabled = self.isEnabled()
        hover = self.underMouse() and enabled
        active = self.isDown() or (self.isCheckable() and self.isChecked())
        if not enabled:
            bg = QtCore.Qt.transparent
        elif active:
            bg = ACCENT
        elif self._danger:
            bg = DANGER_HOVER if hover else DANGER_BG
        elif hover:
            bg = HOVER_BG
        else:
            bg = IDLE_BG
        p.setBrush(bg)
        p.setPen(QtCore.Qt.NoPen)
        p.drawRoundedRect(r, 5, 5)
        p.setBrush(QtCore.Qt.NoBrush)
        p.setPen(QtGui.QPen(OUTLINE, 1.5))
        p.drawRoundedRect(r, 5, 5)
        s = self.glyph_pixel_size()
        box = QtCore.QRectF((self.width() - s) / 2, (self.height() - s) / 2, s, s)
        if not enabled:
            p.setOpacity(0.25)
        rend = renderer(self._current_icon())
        if rend is not None:
            rend.render(p, box)
        else:
            p.setPen(QtGui.QColor(INK))
            p.drawText(box, QtCore.Qt.AlignCenter, "?")
        p.end()

    def enterEvent(self, ev):
        self.update()
        if _hover_hook is not None:
            tip = self.toolTip()
            _hover_hook(tip if self.isEnabled() else "%s (unavailable)" % tip)
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self.update()
        if _hover_hook is not None:
            _hover_hook(None)
        super().leaveEvent(ev)

    def changeEvent(self, ev):
        if ev.type() == QtCore.QEvent.EnabledChange:
            self.update()
        super().changeEvent(ev)


def icon_button(name, tooltip="", px=36, font_px=None, scale=None,
                checkable=False, checked=False, checked_name=None, danger=False):
    """Factory (kept for call-site compatibility).

    scale sizes the icon relative to the button. When omitted it is derived
    from the legacy font_px (font_px/px) or defaults to 0.55. Toolbar/option
    buttons (36px) therefore read bigger than small panel icons (28px).
    """
    if scale is None:
        scale = (font_px / px) if font_px else 0.55
    b = IconButton(name, tooltip, px=px, scale=scale, checked_name=checked_name,
                   danger=danger)
    b.setCheckable(checkable)
    b.setChecked(checked)
    return b
