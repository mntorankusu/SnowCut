"""Color picker for cut colors: a swatch joined to a drop-down arrow.

Clicking the swatch opens the color dialog, and the arrow lists the document's color groups so an
existing one can be picked directly (plus "Other Color…" for the dialog). Painted like
icons.IconButton (same frame, hover and disabled look) so it sits in the icon grids.
Several colors (a mixed selection) show as stripes, and no color shows an empty, crossed swatch.
"""
from PySide6 import QtWidgets, QtGui, QtCore
from . import icons

ARROW_W = 16


def swatch_pixmap(color, px=14):
    """A small rounded color square with a dark outline (menus, lists, combo boxes)."""
    dpr = 2
    pm = QtGui.QPixmap(px * dpr, px * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setBrush(QtGui.QColor(color))
    p.setPen(QtGui.QPen(QtGui.QColor("#9a9a9a"), 1))
    p.drawRoundedRect(QtCore.QRectF(0.5, 0.5, px - 1, px - 1), 3, 3)
    p.end()
    return pm


def icon_pixmap(name, px=16):
    """An app icon (icons.ICONS name) rendered for lists and combo boxes."""
    dpr = 2
    pm = QtGui.QPixmap(px * dpr, px * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(QtCore.Qt.transparent)
    rend = icons.renderer(name)
    if rend is not None:
        p = QtGui.QPainter(pm)
        rend.render(p, QtCore.QRectF(0, 0, px, px))
        p.end()
    return pm


class ColorPicker(QtWidgets.QWidget):
    """picked(color) fires with "#RRGGBB" when the user chooses a color (dialog or group list).
    groups() returns [(color, label)] for the drop-down. The widget never changes its own swatch, since
    the owner calls set_colors() from its refresh."""
    picked = QtCore.Signal(str)

    def __init__(self, groups, px=32, title="Color", tooltip="Color", parent=None):
        super().__init__(parent)
        self._groups = groups
        self._px = px
        self._title = title
        self._tip = tooltip
        self._colors = []
        self._hover = None            # "swatch" | "arrow" | None
        self.setFixedSize(px + ARROW_W, px)
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self._sync_tip()

    # -- API --
    def set_colors(self, colors):
        out = []
        for c in colors:
            c = QtGui.QColor(c).name().upper()
            if c not in out:
                out.append(c)
        if out != self._colors:
            self._colors = out
            self._sync_tip()
            self.update()

    def set_color(self, color):
        self.set_colors([color] if color else [])

    def color(self):
        """The color shown, or None when there is none or several."""
        return self._colors[0] if len(self._colors) == 1 else None

    def colors(self):
        return list(self._colors)

    def choose(self, color):
        """Pick `color` as if the user chose it."""
        c = QtGui.QColor(color)
        if c.isValid():
            self.picked.emit(c.name().upper())

    def open_dialog(self):
        start = QtGui.QColor(self._colors[0] if self._colors else "#000000")
        c = QtWidgets.QColorDialog.getColor(start, self.window(), self._title)
        if c.isValid():
            self.choose(c.name())

    def build_menu(self):
        menu = QtWidgets.QMenu(self)
        groups = list(self._groups() or [])
        if groups:
            head = menu.addAction("Color groups")
            head.setEnabled(False)
        else:
            none = menu.addAction("No color groups yet")
            none.setEnabled(False)
        for color, label in groups:
            act = menu.addAction(QtGui.QIcon(swatch_pixmap(color)), label)
            act.setCheckable(True)
            act.setChecked(color in self._colors)
            act.triggered.connect(lambda _=False, c=color: self.choose(c))
        menu.addSeparator()
        menu.addAction("Other Color…", self.open_dialog)
        return menu

    def open_menu(self):
        menu = self.build_menu()
        menu.exec(self.mapToGlobal(QtCore.QPoint(0, self.height())))

    # -- internals --
    def _sync_tip(self):
        if not self._colors:
            state = "none"
        elif len(self._colors) == 1:
            state = self._colors[0]
        else:
            state = "mixed"
        self.setToolTip("%s (%s)" % (self._tip, state))

    def _part(self, x):
        return "arrow" if x >= self._px else "swatch"

    def mouseMoveEvent(self, ev):
        part = self._part(ev.position().x())
        if part != self._hover:
            self._hover = part
            self.update()
        super().mouseMoveEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() != QtCore.Qt.LeftButton or not self.isEnabled():
            return super().mousePressEvent(ev)
        if self._part(ev.position().x()) == "arrow":
            self.open_menu()
        else:
            self.open_dialog()

    def enterEvent(self, ev):
        self.update()
        if icons._hover_hook is not None:
            icons._hover_hook(self.toolTip())
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = None
        self.update()
        if icons._hover_hook is not None:
            icons._hover_hook(None)
        super().leaveEvent(ev)

    def changeEvent(self, ev):
        if ev.type() == QtCore.QEvent.EnabledChange:
            self.update()
        super().changeEvent(ev)

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        enabled = self.isEnabled()
        r = QtCore.QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(icons.IDLE_BG if enabled else QtCore.Qt.transparent)
        p.drawRoundedRect(r, 5, 5)
        if enabled and self._hover == "arrow":
            p.setBrush(icons.HOVER_BG)
            p.save()
            p.setClipRect(QtCore.QRectF(self._px, 0, ARROW_W, self.height()))
            p.drawRoundedRect(r, 5, 5)
            p.restore()
        if not enabled:
            p.setOpacity(0.35)
        # swatch
        inset = max(4, self._px // 6)
        sw = QtCore.QRectF(inset, inset, self._px - 2 * inset, self.height() - 2 * inset)
        path = QtGui.QPainterPath()
        path.addRoundedRect(sw, 3, 3)
        if self._colors:
            p.save()
            p.setClipPath(path)
            w = sw.width() / len(self._colors)
            for i, c in enumerate(self._colors):
                p.fillRect(QtCore.QRectF(sw.left() + i * w, sw.top(), w + 0.5, sw.height()),
                           QtGui.QColor(c))
            p.restore()
        else:
            p.setPen(QtGui.QPen(QtGui.QColor("#9a9a9a"), 1))
            p.drawLine(sw.bottomLeft(), sw.topRight())
        hover_sw = enabled and self._hover == "swatch"
        p.setBrush(QtCore.Qt.NoBrush)
        p.setPen(QtGui.QPen(QtGui.QColor(icons.INK if hover_sw else "#9a9a9a"), 1))
        p.drawPath(path)
        # arrow
        s = 12
        rend = icons.renderer("chev_down")
        box = QtCore.QRectF(self._px + (ARROW_W - s) / 2 - 2, (self.height() - s) / 2, s, s)
        if rend is not None:
            rend.render(p, box)
        p.setOpacity(1.0)
        p.setPen(QtGui.QPen(icons.OUTLINE, 1.5))
        p.drawRoundedRect(r, 5, 5)
        p.end()
