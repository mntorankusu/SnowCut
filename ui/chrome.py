"""In-app panel chrome: custom header, panel card, overlay layer.

No OS-level windows anywhere: floating panels are plain child widgets of an
Overlay that covers the main-window client area. The SAME HeaderBar instance
travels with the content between docked and floating states, so the header
(and its dock/float icon) is pixel-identical in both. Card heights are
automatic (content-fitted) with an optional user override that acts as a
minimum. Docking or undocking clears the override.

Floating cards keep proportional positions: each card stores its top-left as
fractions of the overlay size. On resize they are re-anchored (right stays
right, left stays left) and clamped inside.

PanelCard paints its own background/border explicitly (never via stylesheet)
so floating cards are opaque on every backend.
"""
from PySide6 import QtWidgets, QtGui, QtCore
from .icons import IconButton

HEADER_H = 26
CARD_BG = QtGui.QColor("#2f2f2f")
CARD_BORDER = QtGui.QColor("#181818")


class _ElidedLabel(QtWidgets.QLabel):
    """Title label that ends in an ellipsis when it does not fit (text() stays complete)."""

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        r = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), QtCore.Qt.ElideRight, r.width())
        self.style().drawItemText(p, r, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, self.palette(),
                                  self.isEnabled(), text, self.foregroundRole())
        p.end()


class HeaderBar(QtWidgets.QWidget):
    """Custom window header: [dock/float] title [collapse] [close]. Also the drag handle.
    Narrow headers (a two-icon toolbar) keep only the title and close button, but floating and
    docking still work by dragging or double-clicking the header."""

    COMPACT_W = 150   # below this width the float / collapse buttons are hidden

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("headerbar")
        self.setFixedHeight(HEADER_H)
        self._card = None
        self._drag_off = None
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(4, 3, 3, 3)
        lay.setSpacing(3)
        self.float_btn = IconButton("float", "Float panel", px=20, scale=0.6)
        self.title = _ElidedLabel(title)
        self.title.setObjectName("paneltitle")
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.collapse_btn = IconButton("chev_up", "Collapse panel", px=20, scale=0.6)
        self.close_btn = IconButton("close", "Close panel (reopen it from the View menu)",
                                    px=20, scale=0.55)
        lay.addWidget(self.float_btn)
        lay.addWidget(self.title, 1)
        lay.addWidget(self.collapse_btn)
        lay.addWidget(self.close_btn)
        self._press_global = None
        self._pending = False
        self._dragging = False
        self._drag_off = None
        self._was_floating = False
        self._compact = None

    def minimumSizeHint(self):
        return QtCore.QSize(68, HEADER_H)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        compact = self.width() < self.COMPACT_W
        if compact != self._compact:
            self._compact = compact
            self.float_btn.setVisible(not compact)
            self.collapse_btn.setVisible(not compact)
            self.setToolTip(self.title.text() if compact else "")

    def set_card(self, card):
        self._card = card

    def set_floating(self, floating):
        self.float_btn.set_icon_name("dock" if floating else "float")
        self.float_btn.setToolTip("Dock panel" if floating else "Float panel")

    def set_collapsed(self, collapsed):
        self.collapse_btn.set_icon_name("chev_down" if collapsed else "chev_up")
        self.collapse_btn.setToolTip("Expand panel" if collapsed else "Collapse panel")

    # Dragging the header moves a floating panel and floats a docked one on
    # drop (in-app, docks never become OS windows). Dragging to a main-window
    # edge docks there with a live rubber-band preview (via drag_tick/end
    # callbacks on the card), and double-click toggles float/dock. Other presses
    # fall through to Qt.
    def mousePressEvent(self, ev):
        card = self._card
        if card is None or ev.button() != QtCore.Qt.LeftButton:
            ev.ignore()
            return
        self._was_floating = bool(card.is_floating() and card.overlay is not None)
        self._press_global = ev.globalPosition().toPoint()
        self._pending = True
        self._dragging = False
        if self._was_floating:
            self._drag_off = card.pos() - card.window().mapFromGlobal(self._press_global)
        else:
            self._drag_off = QtCore.QPoint(-card.width() // 2, -HEADER_H // 2)
        ev.accept()

    def mouseMoveEvent(self, ev):
        card = self._card
        if card is None or not (ev.buttons() & QtCore.Qt.LeftButton):
            ev.ignore()
            return
        gp = ev.globalPosition().toPoint()
        if not self._dragging:
            if not self._pending:
                ev.ignore()
                return
            if (gp - self._press_global).manhattanLength() <= 8:
                ev.ignore()
                return
            self._dragging = True
            self._pending = False
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.ClosedHandCursor)
            start = getattr(card, "drag_start_request", None)
            if start is not None:
                start(card)
            if not self._was_floating:
                win = card.window()
                if hasattr(win, "show_card"):
                    win.show_card(card, floating=True)
                    self._was_floating = True
                    self._drag_off = QtCore.QPoint(-card.width() // 2, -HEADER_H // 2)
        if card.is_floating() and card.overlay is not None and self._drag_off is not None:
            p = card.window().mapFromGlobal(gp) + self._drag_off
            card.move_clamped(p)
        tick = getattr(card, "drag_tick_request", None)
        if tick is not None:
            tick(card, gp)
        ev.accept()

    def mouseReleaseEvent(self, ev):
        card = self._card
        was_drag = self._dragging
        self._dragging = False
        self._pending = False
        self._drag_off = None
        if was_drag:
            try:
                QtWidgets.QApplication.restoreOverrideCursor()
            except Exception:
                pass
            if card is not None:
                cb = getattr(card, "drag_end_request", None)
                if cb is not None:
                    cb(card, ev.globalPosition().toPoint(), self._was_floating)
                    ev.accept()
                    return
        ev.ignore()

    def mouseDoubleClickEvent(self, ev):
        card = self._card
        cb = getattr(card, "toggle_float_request", None) if card is not None else None
        if card is not None and cb is not None and ev.button() == QtCore.Qt.LeftButton:
            self._pending = False
            self._dragging = False
            self._drag_off = None
            try:
                QtWidgets.QApplication.restoreOverrideCursor()
            except Exception:
                pass
            cb(card)
            ev.accept()
            return
        ev.ignore()


class _ResizeGrip(QtWidgets.QWidget):
    """Invisible drag handle on a card edge. mode "h": bottom edge (height),
    "w": right edge (width, info cards in a horizontal dock), "wh": bottom-
    right corner (floating cards). Dragging sets the card's user overrides."""

    def __init__(self, card, mode="h"):
        super().__init__(card)
        self._card = card
        self._mode = mode
        self._start = None
        self.setCursor({"h": QtCore.Qt.SizeVerCursor, "w": QtCore.Qt.SizeHorCursor,
                        "wh": QtCore.Qt.SizeFDiagCursor}[mode])

    def mousePressEvent(self, ev):
        if ev.button() != QtCore.Qt.LeftButton:
            ev.ignore()
            return
        self._start = (ev.globalPosition().toPoint(), self._card.size())
        ev.accept()

    def mouseMoveEvent(self, ev):
        if self._start is None:
            ev.ignore()
            return
        p0, s0 = self._start
        d = ev.globalPosition().toPoint() - p0
        w = s0.width() + d.x() if "w" in self._mode else None
        h = s0.height() + d.y() if "h" in self._mode else None
        self._card.user_resize(w, h)
        ev.accept()

    def mouseReleaseEvent(self, ev):
        if self._start is None:
            ev.ignore()
            return
        self._start = None
        cb = self._card.user_resize_done
        if cb is not None:
            cb(self._card)
        ev.accept()


class PanelCard(QtWidgets.QWidget):
    """Header + content unit that moves between a QDockWidget and the Overlay.

    Two kinds: "grid" (icon-button grids that reflow with dock orientation,
    with docks sized by the main window) and "info" (property panels:
    never narrower than INFO_MIN_W, and in a horizontal dock they keep that
    width, or the user's, instead of stretching).

    Height is automatic: the card wants exactly what its content needs
    (natural_height). A user resize (bottom-edge grip, or a dock separator
    drag) stores user_h, which acts as a minimum on top of that, but the card
    still grows past it when content needs more. user_w is the user's width
    (floating cards, info cards in a horizontal dock). Docking/undocking
    clears both.
    """

    GRIP = 5
    INFO_MIN_W = 300

    def __init__(self, panel_id, title, content, parent=None, kind="info"):
        super().__init__(parent)
        self.setObjectName("panelcard")
        self.panel_id = panel_id
        self.kind = kind
        self.header = HeaderBar(title)
        self.header.set_card(self)
        self.content = content
        self.dock = None       # host QDockWidget when docked
        self.overlay = None    # host Overlay when floating
        self._rel = (0.05, 0.55)
        self.collapsed = False
        self.user_h = None          # user override (minimum) height, or None
        self.user_w = None          # user width, or None
        self._in_container = False  # stacked inside a DockContainer
        self._row_mode = False      # ...whose cards run left to right
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum)
        self.drag_start_request = None    # set by main window: (card) -> None
        self.drag_tick_request = None     # set by main window: (card, global_pos) -> None
        self.drag_end_request = None      # set by main window: (card, global_pos, was_floating) -> None
        self.toggle_float_request = None  # set by main window: (card) -> None
        self.user_resize_request = None   # set by main window: (card) -> None, while grip-dragging
        self.user_resize_done = None      # set by main window: (card) -> None, grip released
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.header, 0)
        lay.addWidget(self.content, 1)
        self._grip = _ResizeGrip(self, "h")
        self._wgrip = _ResizeGrip(self, "w")
        self._corner = _ResizeGrip(self, "wh")
        self.apply_width_rule()
        self.sync_grips()

    def is_floating(self):
        return self.overlay is not None

    # -- automatic height --
    def natural_height(self, w=None):
        """Height that shows all content at width w (no user override)."""
        if self.collapsed:
            return HEADER_H
        if w is None or w <= 0:
            w = max(self.width(), self.minimumSizeHint().width())
        lay = self.layout()
        if lay.hasHeightForWidth():
            return lay.totalHeightForWidth(w)
        return lay.totalSizeHint().height()

    def desired_height(self, w=None):
        h = self.natural_height(w)
        if self.user_h and not self.collapsed:
            h = max(h, self.user_h)
        return h

    def set_user_height(self, h):
        self.user_h = int(h) if h else None
        self.apply_user_height()

    def reset_user_size(self):
        """Back to automatic size (on undock/redock)."""
        self.user_w = None
        self.set_user_height(None)
        self.apply_width_rule()

    def apply_user_height(self):
        """Pin the override as a real minimum where a layout sizes the card
        (dock container stack, floating). Single-card docks are fitted by the
        main window instead, so a separator drag can still shrink them."""
        pin = self.user_h if (self.user_h and not self.collapsed and
                              (self.is_floating() or self._in_container)) else 0
        if self.minimumHeight() != pin:
            self.setMinimumHeight(pin)

    def set_row_mode(self, row):
        self._row_mode = bool(row)
        self.apply_width_rule()
        self.sync_grips()

    def apply_width_rule(self):
        """Info cards: >= INFO_MIN_W always, and in a left-to-right stack exactly
        their width (the user's, else the minimum) instead of stretching."""
        if self.kind != "info":
            return
        if self._in_container and self._row_mode:
            self.setFixedWidth(max(self.INFO_MIN_W, self.user_w or self.INFO_MIN_W))
        else:
            self.setMinimumWidth(self.INFO_MIN_W)
            self.setMaximumWidth(16777215)

    def user_resize(self, w=None, h=None):
        """Grip drag. h becomes the height override unless content already
        needs it, and w is kept as the user width."""
        floating = self.is_floating()
        if w is not None:
            w = max(w, self.minimumSizeHint().width(),
                    self.INFO_MIN_W if self.kind == "info" else 0)
            self.user_w = w
            self.apply_width_rule()
        cur_w = w if (w is not None and floating) else self.width()
        if h is not None:
            need = self.natural_height(cur_w)
            self.set_user_height(h if h > need + 1 else None)
        if floating:
            self.resize(cur_w, self.desired_height(cur_w))
        else:
            self.updateGeometry()
        cb = self.user_resize_request
        if cb is not None:
            cb(self)

    def body_height(self):
        """Painted height. A docked button grid that is the last dock in its
        column gets the column's leftover space but only draws down to its
        content so the spare space reads as empty dock background."""
        if self.kind == "grid" and not self.is_floating() and not self.collapsed:
            return min(self.height(), max(HEADER_H, self.desired_height(self.width())))
        return self.height()

    def sync_grips(self):
        show = not self.collapsed
        g = self.GRIP
        corner = 12 if (show and self.is_floating()) else 0
        wgrip = (show and not self.is_floating() and self.kind == "info" and
                 self._in_container and self._row_mode)
        self._grip.setGeometry(6, self.body_height() - g, max(0, self.width() - 12 - corner), g)
        self._grip.setVisible(show)
        self._wgrip.setGeometry(self.width() - g, HEADER_H, g, max(0, self.height() - HEADER_H - g))
        self._wgrip.setVisible(wgrip)
        self._corner.setGeometry(self.width() - corner, self.height() - corner, corner, corner)
        self._corner.setVisible(bool(corner))
        for gr in (self._grip, self._wgrip, self._corner):
            gr.raise_()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.sync_grips()

    def set_collapsed(self, collapsed):
        """Collapse to header-only, keeping dock order / float position."""
        self.collapsed = bool(collapsed)
        self.content.setVisible(not self.collapsed)
        self.header.set_collapsed(self.collapsed)
        self.apply_user_height()
        self.sync_grips()
        if self.collapsed and self.is_floating():
            self.resize(self.width(), HEADER_H)

    def paintEvent(self, ev):
        # Explicit (not stylesheet) so the card is opaque in every state,
        # docked or floating, on every platform backend.
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(0, 0, self.width(), self.body_height()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(CARD_BG)
        p.setPen(QtGui.QPen(CARD_BORDER, 1))
        p.drawRoundedRect(r, 6, 6)
        p.end()

    def contract(self):
        if self.collapsed:
            self.resize(self.width(), HEADER_H)
            return
        # minimum width (info cards: INFO_MIN_W) unless the user widened it
        w = max(self.user_w or 0, self.minimumSizeHint().width(),
                self.INFO_MIN_W if self.kind == "info" else 0)
        self.resize(w, self.desired_height(w))
        if self.overlay is not None:
            r = self.overlay.client_rect()
            w = min(self.width(), max(60, r.width() - 8))
            h = min(self.height(), max(60, r.height() - 8))
            self.resize(w, h)
            self.move_clamped(self.pos())

    def move_clamped(self, top_left, canvas_bound=False):
        """Move to main-window top_left, clamped inside bounds."""
        ov = self.overlay
        if ov is None:
            self.move(top_left)
            return
        r = ov.canvas_rect() if (canvas_bound or (getattr(self, "_anchored_to_canvas", True) and not getattr(self, "_outside_canvas", False))) else ov.client_rect()
        w = min(self.width(), max(0, r.width()))
        h = min(self.height(), max(0, r.height()))
        x = max(r.x(), min(top_left.x(), max(r.x(), r.right() - w + 1)))
        y = max(r.y(), min(top_left.y(), max(r.y(), r.bottom() - h + 1)))
        self.move(x, y)


class DropLine(QtWidgets.QWidget):
    """Bright insertion line showing exactly where a dragged panel will slot
    in (drawn between the two panels it will land between)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setVisible(False)

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setBrush(QtGui.QColor("#ffb300"))
        p.setPen(QtCore.Qt.NoPen)
        p.drawRoundedRect(QtCore.QRectF(self.rect()), 2, 2)
        p.end()

    def show_at(self, rect):
        """Position (main-window coords) as a horizontal or vertical bar."""
        self.setGeometry(rect)
        self.show()
        self.raise_()


class FloatManager(QtCore.QObject):
    """Positions floating cards as plain main-window children.

    Deliberately NOT a widget: the previous full-area Overlay QWidget needed
    WA_TransparentForMouseEvents to let clicks reach the canvas/docks, but
    that flag blanks the whole subtree from hit-testing (proven: widgetAt
    over a floating button returned None while docked buttons hit fine),
    so floating panels could be seen but never clicked. With cards parented
    directly to the main window there is no occluding layer at all: normal
    hit-testing, painting, and focus everywhere.

    Cards keep proportional positions: top-left stored as fractions of the
    client rect (main rect minus menu/status). On resize they re-anchor
    (right stays right, left stays left) and clamp inside.
    """

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.cards = []

    def canvas_rect(self):
        """Canvas geometry in main-window coordinates."""
        try:
            if hasattr(self.main, "canvas") and self.main.canvas is not None:
                cg = self.main.canvas.geometry()
                if cg.isValid() and cg.width() > 50 and cg.height() > 50:
                    return cg
        except Exception:
            pass
        return self.client_rect()

    def client_rect(self):
        r = self.main.rect()
        try:
            r.setTop(self.main.menuBar().height())
            r.setBottom(r.bottom() - self.main.statusBar().height())
        except Exception:
            pass
        return r

    # geometry helpers used by PanelCard/HeaderBar drag code.
    # All card coordinates are in CLIENT coordinates (client_rect origin).
    # move_clamped converts to parent (main-window) coordinates.
    def width(self):
        return max(1, self.client_rect().width())

    def height(self):
        return max(1, self.client_rect().height())

    def mapFromGlobal(self, p):
        return self.main.mapFromGlobal(p)

    def card_pos(self, card):
        """Card top-left in main-window coordinates."""
        return card.pos()

    def place(self, card, rel=None):
        """Adopt a card: contract to minimum, position by fraction (or cascade)."""
        if card not in self.cards:
            self.cards.append(card)
        card.overlay = self
        card.setParent(self.main)
        card.apply_user_height()
        card.sync_grips()
        card.contract()
        cr = self.canvas_rect()
        if rel is None:
            n = len([c for c in self.cards if c.isVisible()])
            rel = (0.04 + 0.03 * (n % 5), 0.55)
        target_x = cr.x() + int(max(0.0, min(0.95, rel[0])) * cr.width())
        target_y = cr.y() + int(max(0.0, min(0.95, rel[1])) * cr.height())
        card.move_clamped(QtCore.QPoint(target_x, target_y), canvas_bound=True)
        self.store_fraction(card)  # after the move: clamping may have adjusted it
        card.header.set_floating(True)
        card.show()
        card.raise_()

    def store_fraction(self, card):
        # Anchor relative to canvas if inside canvas, else client_rect
        cr = self.canvas_rect()
        card_rect = card.geometry()
        if cr.intersects(card_rect) and not getattr(card, "_outside_canvas", False):
            card._anchored_to_canvas = True
            card._rel = ((card.x() - cr.x()) / max(1, cr.width()),
                         (card.y() - cr.y()) / max(1, cr.height()))
        else:
            card._anchored_to_canvas = False
            card._outside_canvas = True
            r = self.client_rect()
            card._rel = ((card.x() - r.x()) / max(1, r.width()),
                         (card.y() - r.y()) / max(1, r.height()))

    def relayout(self):
        """Re-anchor every card proportionally after a resize and clamp it inside."""
        for card in self.cards:
            if card.parent() is not self.main:
                continue
            fx, fy = card._rel
            if getattr(card, "_anchored_to_canvas", True) and not getattr(card, "_outside_canvas", False):
                cr = self.canvas_rect()
                target_x = cr.x() + int(fx * cr.width())
                target_y = cr.y() + int(fy * cr.height())
                card.move_clamped(QtCore.QPoint(target_x, target_y), canvas_bound=True)
            else:
                r = self.client_rect()
                card.move_clamped(QtCore.QPoint(r.x() + int(fx * r.width()), r.y() + int(fy * r.height())))

    def release(self, card):
        if card in self.cards:
            self.cards.remove(card)
        card.overlay = None
        card.apply_user_height()
        card.sync_grips()
