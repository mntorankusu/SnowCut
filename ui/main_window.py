"""Main window: icon toolbar + floating tool options + dockable panel cards.

Panels are PanelCards with ONE custom header shared between docked and
floating states (QDockWidgets never float, so there is no OS chrome
anywhere). Floating cards are plain main-window children positioned by a
FloatManager with proportional anchoring. Card heights fit their content
automatically. A user resize is kept as a minimum until the card is undocked
or redocked.

Two kinds of panel:
- button grids (Tools, Tool Options, Device Control) each own a dock. Their
  icons reflow with the dock's orientation, with at least 2 icons across a vertical
  dock or 2 icons tall in a horizontal one.
- info panels (properties, Manual Jog, Cut, Code Preview, Cut Settings, Mat, Scan) stack in
  DockContainers and are never narrower than 300px. In a horizontal dock they sit side by
  side at their own width (300px unless the user resizes them) instead of stretching.
"""
import os
import sys
from PySide6 import QtWidgets, QtGui, QtCore
from core.doc import Document, PROJECT_EXT, TAB_GAP_MIN_MM, TAB_GAP_MAX_MM
from core import userdata
from core.history import History, DEFAULT_UNDO_LEVELS, clamp_levels, doc_json
from core.svg_import import import_svg
from device.scanner_real import RealScanner, scan_output_path
from device.mat_feeder import MatFeeder, send_mat_kind
from device.usb_drive import UsbDrive, DRIVE_BUSY, WINFSP_URL, wait_message, winfsp_status
from device.machine_settings import UsbSettings as UsbMachineSettings
from device.firmware_update import FirmwareTools
from device import firmware_update as fw
from core import machine
from core import scan as scanlib
import json
import threading
from device.usb_motion import MotionSender, _fw_dev, _fw_motion, _fw_close, _fw_friendly
from .canvas import Canvas, DEFAULT_BEND_STRENGTH, DEFAULT_BEND_FOCUS
from .draw_tools import DRAW_TOOLS, MAX_THICKNESS_MM, MAX_RADIUS_MM
from . import text_ops
from .panels import (ObjectsPanel, CutOrderPanel, ColorKeyPanel, LinePanel, PointPanel, TabPanel, SvgPanel,
                     DeviceControlPanel, JogPanel, CutSettingsPanel, mm_spin, group_choices)
from .cut_panel import CutPanel, CodePreviewPanel
from .color_picker import ColorPicker
from .mat_panel import MatPanel
from .scan_panel import ScanPanel
from .machine_settings_dialog import MachineSettingsDialog
from .firmware_panel import FirmwareDialog
from .about_dialog import AboutDialog, APP_VERSION
from .flowlayout import FlowLayout, DIV, add_flow_items
from . import icons, image_ops
from .icons import icon_button
from .chrome import PanelCard, FloatManager, DropLine, HEADER_H

APP_NAME = "SnowCut"
MAX_RECENT = 10              # File > Open Recent
ICON_PX = 36
ICON_GAP = 4
FLOW_MARGIN = 4
DOCK_FUDGE = 0
OPTIONS_FLOAT_MIN_W = 180   # floating Tool Options: room for its full header on one-icon pages


def apply_theme(app):
    """App-wide look: the panels and canvas chrome are drawn dark, so the native widgets are kept
    dark even when Windows uses the light theme (Qt 6.8+), and checked / selected states use the
    same blue as the icon buttons instead of the system accent colour."""
    if hasattr(QtCore.Qt, "ColorScheme") and hasattr(app.styleHints(), "setColorScheme"):
        app.styleHints().setColorScheme(QtCore.Qt.ColorScheme.Dark)
    pal = app.palette()
    roles = [QtGui.QPalette.Highlight] + ([QtGui.QPalette.Accent] if hasattr(QtGui.QPalette, "Accent") else [])
    for group in (QtGui.QPalette.Active, QtGui.QPalette.Inactive):
        for role in roles:
            pal.setColor(group, role, icons.ACCENT)
    app.setPalette(pal)


def _tools_extent_w(n):
    return n * ICON_PX + max(0, n - 1) * ICON_GAP + 2 * FLOW_MARGIN + DOCK_FUDGE


def _tools_extent_h(n):
    return HEADER_H + n * ICON_PX + max(0, n - 1) * ICON_GAP + 2 * FLOW_MARGIN + DOCK_FUDGE


def _icons_extent(n):
    return _tools_extent_w(n)


def _explicitly_hidden(w):
    """hide() was called (not merely 'not yet shown with its window')."""
    return w.isHidden() and w.testAttribute(QtCore.Qt.WA_WState_ExplicitShowHide)


def _has_point_refs(d):
    """bool(d.selected_point_refs()) without building and sorting the list."""
    sel, paths = d.selection, d.paths
    if sel.points:
        return True
    for pid, _i in sel.segments:
        p = paths.get(pid)
        if p and p.points:
            return True
    for pid in sel.path_ids:
        p = paths.get(pid)
        if p and p.points:
            return True
    for svg_id in sel.svg_ids:
        s = d.svgs.get(svg_id)
        if s and s.kind != "text":     # text points are rebuilt from the letters: not edited
            for pid in s.path_ids:
                p = paths.get(pid)
                if p and p.points:
                    return True
    return False


class DockContainer(QtWidgets.QScrollArea):
    """Scrollable container for docked info cards.
    Keeps panels packed at top with empty space below, and shows a scrollbar
    if contents exceed available height, preventing main window expansion.
    Its minimum width tracks the widest card's minimum (plus a reserved
    scrollbar) so the dock can never be shrunk into clipping its cards.
    In row mode (top/bottom docks) cards run left to right at their own
    widths with the empty space on the right, scrolling horizontally.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setStyleSheet("QScrollArea { border: none; background: #242424; }")
        self.dock = None             # host QDockWidget
        self.layout_changed = None   # set by main window: () -> None
        self.row_mode = False

        self.container = QtWidgets.QWidget()
        # scoped by object name: a bare "background:" would cascade to every label and
        # checkbox in the cards and paint dark bands over the card background
        self.container.setObjectName("dockcontainer")
        self.container.setStyleSheet("QWidget#dockcontainer { background: #242424; }")
        self.vbox = QtWidgets.QVBoxLayout(self.container)
        self.vbox.setContentsMargins(2, 2, 2, 2)
        self.vbox.setSpacing(4)
        self.vbox.addStretch(1)
        self.setWidget(self.container)
        self.container.installEventFilter(self)

    def cards(self):
        """Every card in the stack (shown or hidden), top to bottom."""
        out = []
        for i in range(self.vbox.count()):
            w = self.vbox.itemAt(i).widget()
            if isinstance(w, PanelCard):
                out.append(w)
        return out

    def shown_cards(self):
        return [c for c in self.cards() if not _explicitly_hidden(c)]

    def set_row_mode(self, row):
        row = bool(row)
        if row == self.row_mode:
            return
        self.row_mode = row
        self.vbox.setDirection(QtWidgets.QBoxLayout.LeftToRight if row
                               else QtWidgets.QBoxLayout.TopToBottom)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded if row
                                          else QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff if row
                                        else QtCore.Qt.ScrollBarAsNeeded)
        for c in self.cards():
            self._align(c)
            c.set_row_mode(row)
        self.updateGeometry()

    def _align(self, card):
        # side by side, each card keeps its own height (no stretch to the row)
        self.vbox.setAlignment(card, QtCore.Qt.AlignTop if self.row_mode else QtCore.Qt.Alignment())

    def add_card(self, card, index=None):
        """Stack a card. `index` counts shown cards (None = keep/append)."""
        if card.parent() is self.container:
            if index is None:
                card._in_container = True
                card.set_row_mode(self.row_mode)
                return
            self.vbox.removeWidget(card)
        shown = [c for c in self.shown_cards() if c is not card]
        if index is not None and 0 <= index < len(shown):
            pos = self.vbox.indexOf(shown[index])
        else:
            pos = max(0, self.vbox.count() - 1)  # before the bottom stretch
        card.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)
        self.vbox.insertWidget(pos, card)
        card.dock = self.dock
        card._in_container = True
        self._align(card)
        card.set_row_mode(self.row_mode)
        card.apply_user_height()

    def remove_card(self, card):
        self.vbox.removeWidget(card)
        card._in_container = False
        card.set_row_mode(False)
        card.apply_user_height()

    def content_height(self):
        """Height that shows every stacked card without scrolling."""
        c = self.container
        if self.row_mode:
            w = max(c.width(), c.minimumSizeHint().width())
            sb = (self.horizontalScrollBar().sizeHint().height()
                  if c.minimumSizeHint().width() > self.viewport().width() else 0)
        else:
            w, sb = self.viewport().width(), 0
        h = c.heightForWidth(w) if c.hasHeightForWidth() else -1
        if h < 0:
            h = c.sizeHint().height()
        return h + sb + 2 * self.frameWidth()

    def minimumSizeHint(self):
        f = 2 * self.frameWidth()
        if self.row_mode:
            sb = self.horizontalScrollBar().sizeHint().height()
            return QtCore.QSize(80, self.container.minimumSizeHint().height() + sb + f)
        cw = self.container.minimumSizeHint().width()
        sb = self.verticalScrollBar().sizeHint().width()
        return QtCore.QSize(cw + sb + f, 40)

    def eventFilter(self, obj, ev):
        if obj is getattr(self, "container", None) and ev.type() == QtCore.QEvent.LayoutRequest:
            self.updateGeometry()
            if self.layout_changed is not None:
                self.layout_changed()
        return super().eventFilter(obj, ev)


class _OptionsPage(QtWidgets.QWidget):
    """Tool Options stack page. QStackedLayout sizes itself to the largest of ALL pages,
    so hidden (non-current) pages report nothing and the card fits only the
    current tool's options."""

    def sizeHint(self):
        return QtCore.QSize(0, 0) if self.isHidden() else super().sizeHint()

    def minimumSizeHint(self):
        return QtCore.QSize(0, 0) if self.isHidden() else super().minimumSizeHint()

    def heightForWidth(self, w):
        return 0 if self.isHidden() else super().heightForWidth(w)


DOCK_QSS = """
QMainWindow { background-color: #242424; }
QMainWindow::separator { background: #242424; width: 6px; height: 6px; }
QDockWidget { background: #242424; border: none; margin: 3px; }
QDockWidget QLabel { color: #e8e8e8; }
QDockWidget QCheckBox { color: #e8e8e8; }
QScrollArea { border: none; background: #242424; }
/* NOTE: PanelCard paints its own background/border explicitly (see chrome.py),
   so there is no QWidget#panelcard rule here. It would never paint over it. */
QWidget#headerbar { background-color: #3d3d3d;
                    border-top-left-radius: 5px; border-top-right-radius: 5px; }
"""


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, doc=None):
        super().__init__()
        self.doc = doc or Document()
        self._restoring = False      # undo / redo in progress: refresh_all must not commit history
        self._exiting = False        # File > Exit in progress: closeEvent asks about unsaved changes
        self.history = History(self._stored_undo_levels())
        self.scanner = RealScanner()
        self.sender = MotionSender()
        self._sync_window_title()
        self.resize(1280, 850)
        self.setDockNestingEnabled(True)
        self.setStyleSheet(DOCK_QSS)
        self.canvas = Canvas(self.doc)
        self.setCentralWidget(self.canvas)
        self.cards = {}
        self._view_actions = {}
        self._page_flows = []
        self._last_areas = {}
        self._extra_containers = []  # dock columns created by drag-to-plus
        self._dragging_card = None   # card being header-dragged
        self._drag_area = None       # dock area active for the current drag
        self._sep_snap = None        # dock sizes at a main-window press
        self._last_fit = {}          # loop guard for _fit_all
        self._fit_timer = QtCore.QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_all)
        self._build_panels()
        self._build_tools()
        # In-app floating layer (plain manager, NOT a widget: a full-area
        # overlay widget would need a transparency flag that blanks hit-testing)
        self.overlay = FloatManager(self)
        self._dock_preview = QtWidgets.QRubberBand(
            QtWidgets.QRubberBand.Rectangle, self)
        self._dock_preview.hide()
        self._drop_line = DropLine(self)
        self._drop_line.hide()
        self._plus_btn = icon_button("plus", "Drop here to create a new column",
                                     px=30, scale=0.6)
        self._plus_btn.setParent(self)
        self._plus_btn.hide()
        self._plus_area = None
        self._menus()
        self.coord_lbl = QtWidgets.QLabel("")
        self.coord_lbl.setMinimumWidth(150)
        self.coord_lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.coord_lbl.setToolTip("Pointer position on the mat")
        self.statusBar().addPermanentWidget(self.coord_lbl)
        self.statusBar().setStyleSheet("QStatusBar { color: #c8c8c8; } "
                                       "QStatusBar QLabel { color: #c8c8c8; padding: 0 8px; } "
                                       "QStatusBar::item { border: none; }")
        icons.set_hover_hook(self._hover_status)
        self.statusBar().showMessage("Ready")
        self.canvas.selection_changed.connect(self.refresh_all)
        self.canvas.status.connect(self.statusBar().showMessage)
        self.canvas.cursor_moved.connect(self._on_cursor_moved)
        self.canvas.svg_moved.connect(self.refresh_all)
        self.canvas.svg_dragging.connect(self._on_svg_dragging)
        self.canvas.tool_changed.connect(self._on_tool_changed)
        self.canvas.zoom_changed.connect(self._on_zoom_changed)
        self.canvas.options_changed.connect(self._sync_options)
        for dk in self._all_docks():
            dk.dockLocationChanged.connect(lambda *a: self._update_layout_modes())
            dk.topLevelChanged.connect(lambda *a: self._update_layout_modes())
        self._scan_timer = QtCore.QTimer(self)
        self._scan_timer.timeout.connect(self._scan_poll)
        self._options_placed = False
        self.refresh_all()
        self.history.reset(self.doc)   # the starting document (default colours etc. included)
        self._mark_saved()
        self._sync_undo_actions()
        self._update_layout_modes()
        self.setAcceptDrops(True)

    # -- cards + docks --
    def _all_docks(self):
        docks = [getattr(self, "right_dock", None),
                 getattr(self, "left_dock", None),
                 getattr(self, "tools_dock", None),
                 getattr(self, "options_dock", None),
                 getattr(self, "device_dock", None)]
        docks += [c.dock for c in getattr(self, "_extra_containers", [])]
        return [d for d in docks if d is not None]

    def _containers(self):
        out = [getattr(self, "right_container", None), getattr(self, "left_container", None)]
        return [c for c in out if c is not None] + list(self._extra_containers)

    def _card_container(self, card):
        for cont in self._containers():
            if card.parent() is cont.container:
                return cont
        return None

    def _is_single(self, card):
        """Button-grid card that owns a dedicated QDockWidget (info cards
        stack in containers instead)."""
        return card in (getattr(self, "tools_card", None), getattr(self, "options_card", None),
                        getattr(self, "device_card", None))

    def _grid_flows(self):
        """Icon flows of the plain button-grid cards (Tool Options has its own
        adaptive groups and is handled separately)."""
        return {self.tools_card: self.tools_flow, self.device_card: self.device_panel.flow}

    def _hover_status(self, text):
        """Icon buttons report their name here on hover: no tooltip wait. Leaving the button
        brings back the message it covered."""
        sb = self.statusBar()
        if text:
            if getattr(self, "_status_under_hover", None) is None:
                self._status_under_hover = sb.currentMessage()
            sb.showMessage(text)
        else:
            prev, self._status_under_hover = getattr(self, "_status_under_hover", None), None
            if prev:
                sb.showMessage(prev)
            else:
                sb.clearMessage()

    def _on_cursor_moved(self, pos):
        """Pointer position (canvas mm) for the status bar, or None when it leaves the canvas."""
        self.coord_lbl.setText("" if pos is None else "X %.1f   Y %.1f mm" % pos)

    def _is_shown(self, card):
        if _explicitly_hidden(card):
            return False
        if card.is_floating():
            return True
        return card.dock is None or not _explicitly_hidden(card.dock)

    def _new_container(self):
        """A new (unplaced) dock column for stacking cards."""
        dock = QtWidgets.QDockWidget("", self)
        dock.setWindowTitle("Panels")
        dock.setObjectName("panels_%d" % (len(self._extra_containers) + 1))
        dock.setTitleBarWidget(QtWidgets.QWidget(dock))
        cont = DockContainer()
        dock.setWidget(cont)
        cont.dock = dock
        cont.layout_changed = self._schedule_fit
        dock.dockLocationChanged.connect(lambda *a: self._update_layout_modes())
        self._extra_containers.append(cont)
        cont.dock.hide()  # unplaced: the caller docks and shows it
        return cont

    def _spare_container(self):
        """Reuse a hidden, card-less container before creating a new one."""
        for cont in self._containers():
            if not cont.cards() and _explicitly_hidden(cont.dock):
                return cont
        return self._new_container()

    def _sync_containers(self):
        """Container docks are shown only while they hold a shown card. Hidden ones
        keep their place (and hidden cards) so re-showing restores layout."""
        if self._dragging_card is not None:
            return  # no layout jumps under the cursor mid-drag
        for cont in self._containers():
            want = bool(cont.shown_cards())
            if want and cont.dock.isHidden():
                cont.dock.show()
            elif not want and not _explicitly_hidden(cont.dock):
                cont.dock.hide()

    def _make_card(self, pid, title, content, prefer_float=False, kind="info"):
        card = PanelCard(pid, title, content, kind=kind)
        card._prefer_float = prefer_float
        card._manual_closed = False
        card._manual_open = False
        card._last_container = None
        card.drag_start_request = self._card_drag_start
        card.drag_tick_request = self._card_drag_tick
        card.drag_end_request = self._card_drag_end
        card.toggle_float_request = self.toggle_float
        card.collapse_request = self.toggle_collapse
        card.user_resize_request = self._card_user_resized
        card.user_resize_done = self._card_user_resize_done
        card.installEventFilter(self)
        card.header.collapse_btn.clicked.connect(lambda: self.toggle_collapse(card))
        card.header.float_btn.clicked.connect(lambda: self.toggle_float(card))
        card.header.close_btn.clicked.connect(lambda: self.hide_card(card, manual=True))
        self.cards[pid] = card
        return card

    def _make_dock(self, card, area):
        """Dedicated dock for a button-grid card."""
        card.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)
        dock = QtWidgets.QDockWidget("", self)
        dock.setWindowTitle(card.header.title.text())
        dock.setTitleBarWidget(QtWidgets.QWidget(dock))  # suppress native bar: the app's own header instead
        dock.setWidget(card)
        card.dock = dock
        dock.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetClosable)
        dock.setAllowedAreas(QtCore.Qt.AllDockWidgetAreas)
        self.addDockWidget(area, dock)
        return dock

    def _build_panels(self):
        R = QtCore.Qt.RightDockWidgetArea
        self.right_dock = QtWidgets.QDockWidget("", self)
        self.right_dock.setWindowTitle("Properties")
        self.right_dock.setTitleBarWidget(QtWidgets.QWidget(self.right_dock))
        self.right_container = DockContainer()
        self.right_container.dock = self.right_dock
        self.right_container.layout_changed = self._schedule_fit
        self.right_dock.setWidget(self.right_container)
        self.addDockWidget(R, self.right_dock)

        defs = [("objects", "Objects", ObjectsPanel(self.doc)),
                ("cutorder", "Cut Order", CutOrderPanel(self.doc)),
                ("colorkey", "Color Group Properties", None),  # needs a getter, built below
                ("line", "Line Properties", LinePanel(self.doc)),
                ("point", "Point Properties", PointPanel(self.doc)),
                ("tab", "Tab Properties", TabPanel(self.doc)),
                ("svg", "SVG Properties", SvgPanel(self.doc))]
        self.objects_panel, self.cutorder_panel = defs[0][2], defs[1][2]
        self.colorkey_panel = ColorKeyPanel(self.doc, self._display_color)
        self.line_panel, self.point_panel = defs[3][2], defs[4][2]
        self.tab_panel, self.svg_panel = defs[5][2], defs[6][2]
        panels = {"objects": self.objects_panel, "cutorder": self.cutorder_panel,
                  "colorkey": self.colorkey_panel, "line": self.line_panel,
                  "point": self.point_panel, "tab": self.tab_panel, "svg": self.svg_panel}
        titles = {pid: title for pid, title, _p in defs}
        self._info_panels = panels
        self._panel_dirty = set()      # hidden panels skipped by refresh_all
        self._in_rules = False
        self._drag_pending = False
        for pid, panel in panels.items():
            card = self._make_card(pid, titles[pid], panel)
            self.right_container.add_card(card)
            panel.changed.connect(self.refresh_all)
        self.svg_panel.message.connect(self.statusBar().showMessage)
        self.cutorder_panel.active_key_changed.connect(lambda _k: self.refresh_all())
        self.colorkey_panel.recolored.connect(self._group_recolored)

    def _group_recolored(self, color):
        # the Cut Order keeps the recolored group highlighted (no refresh: changed follows)
        if self.cutorder_panel.active_key:
            self.cutorder_panel.active_key = color

    def _display_color(self):
        if self.cutorder_panel.active_key:
            return self.cutorder_panel.active_key
        cols = []
        for pid in self.doc.selection.path_ids:
            if pid in self.doc.paths:
                cols.append(self.doc.paths[pid].color)
        for (pid, _i) in list(self.doc.selection.segments) + list(self.doc.selection.points):
            if pid in self.doc.paths:
                cols.append(self.doc.paths[pid].color)
        for svg_id in self.doc.selection.svg_ids:
            s = self.doc.svgs.get(svg_id)
            if s:
                for pid in s.path_ids:
                    if pid in self.doc.paths:
                        cols.append(self.doc.paths[pid].color)
        return cols[0] if cols else ""

    # -- float / dock mechanics --
    def toggle_float(self, card):
        self.show_card(card, floating=not card.is_floating())

    def toggle_collapse(self, card):
        cur_w = card.width()
        card.set_collapsed(not card.collapsed)
        if card.is_floating():
            if card.collapsed:
                card.resize(cur_w, HEADER_H)
            else:
                self._contract_card(card)
                if card.width() != cur_w:
                    card.resize(cur_w, card.height())
            if card.overlay is not None:
                card.overlay.store_fraction(card)

    def _drag_float(self, card, global_pos):
        """Float a docked card at a drop point (contracts to minimum)."""
        self.show_card(card, floating=True)
        try:
            pt = self.overlay.mapFromGlobal(global_pos) - QtCore.QPoint(24, 13)
            card.move_clamped(pt)
            self.overlay.store_fraction(card)
        except Exception:
            pass

    # Drag-to-dock: a thin band along each client edge ACTIVATES that dock
    # area. Once active, the whole area (every dock in it, plus the band)
    # is the drop zone until the cursor leaves it. Dragging a docked card
    # starts with its own area already active (reorder in place).
    DOCK_BAND = 72  # activation band thickness (main coords)
    PLUS_PX = 30
    _EDGE_AREAS = (QtCore.Qt.LeftDockWidgetArea, QtCore.Qt.RightDockWidgetArea,
                   QtCore.Qt.TopDockWidgetArea, QtCore.Qt.BottomDockWidgetArea)

    @staticmethod
    def _is_vertical_area(area):
        return area in (QtCore.Qt.LeftDockWidgetArea, QtCore.Qt.RightDockWidgetArea)

    def _zone_rect(self, area):
        """Activation band for an area."""
        r = self.overlay.client_rect()
        b = self.DOCK_BAND
        if area == QtCore.Qt.LeftDockWidgetArea:
            return QtCore.QRect(r.x(), r.y(), b, r.height())
        if area == QtCore.Qt.RightDockWidgetArea:
            return QtCore.QRect(r.right() - b + 1, r.y(), b, r.height())
        if area == QtCore.Qt.TopDockWidgetArea:
            return QtCore.QRect(r.x(), r.y(), r.width(), b)
        return QtCore.QRect(r.x(), r.bottom() - b + 1, r.width(), b)

    def _area_rect(self, area):
        """Full drop zone of an active area: its docks united with the band."""
        rect = self._zone_rect(area)
        for d in self._area_docks(area):
            rect = rect.united(d.geometry())
        return rect.intersected(self.overlay.client_rect())

    def _active_area_at(self, global_pos):
        p = self.mapFromGlobal(global_pos)
        if not self.overlay.client_rect().contains(p):
            return None
        if self._drag_area is not None and self._area_rect(self._drag_area).contains(p):
            return self._drag_area
        for area in self._EDGE_AREAS:
            if self._zone_rect(area).contains(p):
                return area
        return None

    def _plus_rect(self, area):
        """New-column button: right edge (L/R) or bottom edge (T/B) of the
        full drop zone, fully inside it, where addDockWidget appends."""
        r = self._area_rect(area)
        s, m = self.PLUS_PX, 6
        if self._is_vertical_area(area):
            return QtCore.QRect(r.right() - m - s + 1, r.center().y() - s // 2, s, s)
        return QtCore.QRect(r.center().x() - s // 2, r.bottom() - m - s + 1, s, s)

    def _on_plus(self, area, global_pos):
        return (self._plus_area == area and
                self._plus_rect(area).adjusted(-4, -4, 4, 4).contains(self.mapFromGlobal(global_pos)))

    def _hide_drag_feedback(self):
        self._dock_preview.hide()
        self._drop_line.hide()
        self._plus_btn.setDown(False)
        self._plus_btn.hide()
        self._plus_area = None

    def _card_drag_start(self, card):
        self._dragging_card = card
        self._drag_area = None
        if not card.is_floating() and card.dock is not None and card.dock.isVisible():
            area = self.dockWidgetArea(card.dock)
            if area in self._EDGE_AREAS:
                self._drag_area = area

    def _card_drag_tick(self, card, global_pos):
        area = self._active_area_at(global_pos)
        self._drag_area = area
        if area is None:
            self._hide_drag_feedback()
            return
        self._dock_preview.setGeometry(self._area_rect(area))
        self._dock_preview.show()
        self._dock_preview.raise_()
        has_docks = any(not (self._is_single(card) and d is card.dock)
                        for d in self._area_docks(area))
        if has_docks:
            self._plus_btn.setGeometry(self._plus_rect(area))
            self._plus_btn.show()
            self._plus_area = area
        else:
            self._plus_btn.hide()
            self._plus_area = None
        on_plus = has_docks and self._on_plus(area, global_pos)
        self._plus_btn.setDown(on_plus)
        if on_plus:
            self._drop_line.hide()
        else:
            _action, line = self._drop_target(card, area, global_pos)
            self._drop_line.show_at(line)
        if self._plus_btn.isVisible():
            self._plus_btn.raise_()

    def _card_drag_end(self, card, global_pos, was_floating):
        area = self._active_area_at(global_pos)
        action = None
        if area is not None:
            if self._on_plus(area, global_pos):
                action = ("plus",)
            else:
                action, _line = self._drop_target(card, area, global_pos)
        self._hide_drag_feedback()
        self._dragging_card = None
        self._drag_area = None
        if action is not None:
            self._apply_drop(card, area, action)
        elif was_floating:
            self.overlay.store_fraction(card)
        else:
            self._drag_float(card, global_pos)
        self._sync_containers()
        self._schedule_fit()

    # -- slot geometry: which column, which slot, where the line goes --
    def _area_docks(self, area):
        out = []
        for dk in self.findChildren(QtWidgets.QDockWidget):
            try:
                if dk.isVisibleTo(self) and self.dockWidgetArea(dk) == area:
                    out.append(dk)
            except Exception:
                pass
        return out

    def _area_columns(self, area, exclude=None):
        """Visible docks in an area grouped into columns (L/R) or rows (T/B).

        Column mates share (nearly) the same x origin, so they are grouped by x-start
        (not transitive x-overlap, which merges adjacent wide/narrow columns).
        """
        docks = [d for d in self._area_docks(area) if d is not exclude]
        vertical = self._is_vertical_area(area)
        lead = (lambda d: d.x()) if vertical else (lambda d: d.y())
        along = (lambda d: d.y()) if vertical else (lambda d: d.x())
        docks.sort(key=lead)
        groups, cur, cur_v = [], [], None
        for d in docks:
            if cur and abs(lead(d) - cur_v) <= 20:
                cur.append(d)
            else:
                if cur:
                    groups.append(cur)
                cur, cur_v = [d], lead(d)
        if cur:
            groups.append(cur)
        for g in groups:
            g.sort(key=along)
        return groups

    def _drop_target(self, card, area, global_pos):
        """(action, line_rect) for releasing `card` at global_pos in `area`.

        Actions: ("container", cont, index) stacks into a dock container,
        ("dock", prev, next) places a single-card dock between two docks,
        ("newdock", prev, next) makes a container dock between two docks, and
        ("empty",) docks into an area with nothing in it.
        """
        p = self.mapFromGlobal(global_pos)
        vertical = self._is_vertical_area(area)
        single = self._is_single(card)
        cols = self._area_columns(area, exclude=card.dock if single else None)
        if not cols:
            zr = self._area_rect(area)
            line = (QtCore.QRect(zr.x(), zr.y(), zr.width(), 4) if vertical
                    else QtCore.QRect(zr.x(), zr.y(), 4, zr.height()))
            return ("empty",), line

        def lo(r):
            return r.top() if vertical else r.left()

        def hi(r):
            return r.bottom() if vertical else r.right()

        def span(g):  # extent across the stacking axis
            if vertical:
                return min(d.x() for d in g), max(d.geometry().right() for d in g)
            return min(d.y() for d in g), max(d.geometry().bottom() for d in g)

        cross = p.x() if vertical else p.y()

        def cross_dist(g):
            a, b = span(g)
            return 0 if a <= cross <= b else min(abs(cross - a), abs(cross - b))

        col = min(cols, key=cross_dist)
        a, b = span(col)
        main = p.y() if vertical else p.x()

        def bar(pos):
            if vertical:
                return QtCore.QRect(a, pos - 2, b - a + 1, 4)
            return QtCore.QRect(pos - 2, a, 4, b - a + 1)

        if not single:
            hover = next((d for d in col if isinstance(d.widget(), DockContainer)
                          and lo(d.geometry()) <= main <= hi(d.geometry())), None)
            if hover is not None:
                cont = hover.widget()
                cards = [c for c in cont.shown_cards() if c is not card]
                rects = [QtCore.QRect(c.mapTo(self, QtCore.QPoint(0, 0)), c.size()) for c in cards]
                idx = next((i for i, r in enumerate(rects) if r.center().y() > p.y()), len(rects))
                dg = hover.geometry()
                if not rects:
                    y = dg.top() + 4
                elif idx == 0:
                    y = rects[0].top() - 1
                elif idx >= len(rects):
                    y = rects[-1].bottom() + 3
                else:
                    y = (rects[idx - 1].bottom() + rects[idx].top()) // 2 + 1
                y = max(dg.top() + 2, min(y, dg.bottom() - 2))
                return ("container", cont, idx), QtCore.QRect(dg.x(), y - 2, dg.width(), 4)

        idx = next((i for i, d in enumerate(col)
                    if (lo(d.geometry()) + hi(d.geometry())) / 2 > main), len(col))
        prev = col[idx - 1] if idx > 0 else None
        nxt = col[idx] if idx < len(col) else None
        if prev is not None and nxt is not None:
            pos = (hi(prev.geometry()) + lo(nxt.geometry())) // 2 + 1
        elif nxt is not None:
            pos = lo(nxt.geometry()) + 2
        else:
            pos = hi(prev.geometry()) - 1
        line = bar(pos)
        if single:
            return ("dock", prev, nxt), line
        if prev is not None and isinstance(prev.widget(), DockContainer):
            cont = prev.widget()
            return ("container", cont, len([c for c in cont.shown_cards() if c is not card])), line
        if nxt is not None and isinstance(nxt.widget(), DockContainer):
            return ("container", nxt.widget(), 0), line
        return ("newdock", prev, nxt), line

    def _place_dock(self, dock, area, prev, nxt):
        """Put a dock between prev and nxt in one column (either may be None)."""
        ori = QtCore.Qt.Vertical if self._is_vertical_area(area) else QtCore.Qt.Horizontal
        if prev is not None:
            self.splitDockWidget(prev, dock, ori)            # dock right after prev
        elif nxt is not None:
            self.splitDockWidget(nxt, dock, ori)             # dock after nxt...
            self.splitDockWidget(dock, nxt, ori)             # ...then nxt after dock
        else:
            self.addDockWidget(area, dock)

    def _detach_for_dock(self, card):
        """Common prelude for docking a card: leave the float layer/container."""
        if card.is_floating():
            self.overlay.release(card)
        else:
            cont = self._card_container(card)
            if cont is not None:
                cont.remove_card(card)
        card.set_user_height(None)  # (re)docking returns to automatic height
        card.header.set_floating(False)
        card._prefer_float = False
        card._manual_closed = False

    def _apply_drop(self, card, area, action):
        kind = action[0]
        cross = QtCore.Qt.Horizontal if self._is_vertical_area(area) else QtCore.Qt.Vertical
        self._detach_for_dock(card)
        card.hide()
        if self._is_single(card):
            dock = card.dock
            dock.setWidget(card)
            if kind == "plus":
                self.addDockWidget(area, dock, cross)        # new column at the end
            elif kind == "dock":
                self._place_dock(dock, area, action[1], action[2])
            else:
                self.addDockWidget(area, dock)
            dock.show()
        else:
            if kind == "container":
                cont = action[1]
            else:
                cont = self._spare_container()
                if kind == "plus":
                    self.addDockWidget(area, cont.dock, cross)
                elif kind == "newdock":
                    self._place_dock(cont.dock, area, action[1], action[2])
                else:
                    self.addDockWidget(area, cont.dock)
            cont.add_card(card, action[2] if kind == "container" else None)
            card._last_container = cont
            cont.dock.show()
        card.show()
        self._update_layout_modes()  # reflow first: the reset measures the docked layout
        self._collapse_area(area)
        self._sync_view_actions()

    def _collapse_area(self, area):
        """Reset an area's cross-axis extent: grids span 2 icons, info
        containers their minimum (300px cards)."""
        docks = self._area_docks(area)
        if not docks:
            return
        try:
            vertical = self._is_vertical_area(area)
            sizes = []
            for d in docks:
                w = d.widget()
                if isinstance(w, PanelCard) and self._is_single(w):
                    sizes.append(max(_tools_extent_w(2), d.minimumSizeHint().width()) if vertical
                                 else _tools_extent_h(2))
                else:
                    hint = d.minimumSizeHint()
                    sizes.append(hint.width() if vertical else max(hint.height(), self._dock_want_h(d)))
            self.resizeDocks(docks, sizes,
                             QtCore.Qt.Horizontal if vertical else QtCore.Qt.Vertical)
        except Exception:
            pass
        self._schedule_fit()  # heights along the column fit content

    def _collapse_all_areas(self):
        for area in (QtCore.Qt.LeftDockWidgetArea, QtCore.Qt.RightDockWidgetArea,
                     QtCore.Qt.TopDockWidgetArea, QtCore.Qt.BottomDockWidgetArea):
            self._collapse_area(area)

    def _contract_card(self, card):
        """Contract a floating card to its visible content."""
        if card.collapsed:
            card.resize(card.width(), HEADER_H)
            return
        if card is self.options_card:
            if card.is_floating():
                pg = self.opt_stack.currentWidget()
                if pg is not None:
                    # floating: one row of the current page's icons
                    for f in self._page_flows:
                        f.set_vertical(False)
                    pg.layout().activate()
                    pg.updateGeometry()
                    hint = pg.layout()._compact_size()
                    w = max(hint.width(), card.minimumSizeHint().width(), OPTIONS_FLOAT_MIN_W)
                    h = hint.height()
                    if card.overlay is not None:   # wider than the canvas: wrap onto more rows
                        avail = card.overlay.canvas_rect().width()
                        if 0 < avail < w:
                            w = max(OPTIONS_FLOAT_MIN_W, avail)
                            h = pg.layout().heightForWidth(w)
                    card.resize(w, h + HEADER_H)
                    if card.overlay is not None:
                        card.move_clamped(card.pos())
                        card.overlay.store_fraction(card)
                    return
        if card in self._grid_flows() and card.is_floating():
            # 2 icons wide unless the user widened it, and the height follows the flow
            w = card.user_w or _tools_extent_w(2)
            card.resize(w, card.desired_height(w))
            if card.overlay is not None:
                card.move_clamped(card.pos())
                card.overlay.store_fraction(card)
            return
        card.contract()

    def show_card(self, card, floating=None):
        if floating is None:
            floating = getattr(card, "_prefer_float", False)
        was_floating = card.is_floating() if self._is_shown(card) else card._prefer_float
        if bool(floating) != bool(was_floating):
            card.reset_user_size()  # undock/redock: back to automatic size
        card._manual_closed = False
        self._flush_dirty(card)  # fresh content before the card is sized
        if floating:
            cont = self._card_container(card)
            if cont is not None:
                cont.remove_card(card)
                card._last_container = cont
            if self._is_single(card):
                card.dock.hide()
            self.overlay.place(card)
            self._update_layout_modes()
            self._contract_card(card)
            card.raise_()
        else:
            self.overlay.release(card)
            card.header.set_floating(False)
            card._prefer_float = False
            if self._is_single(card):
                card.dock.setWidget(card)
                card.dock.show()
            else:
                cont = self._card_container(card)
                if cont is None:
                    cont = card._last_container
                    if cont not in self._containers():
                        cont = self.right_container
                    cont.add_card(card)
                    card._last_container = cont
                cont.dock.show()
            card.show()
        self._sync_panel_button(card, True)
        self._sync_containers()
        self._update_layout_modes()
        self._sync_view_actions()
        self._schedule_fit()

    def hide_card(self, card, manual=True):
        card._prefer_float = card.is_floating()
        if manual:
            card._manual_closed = True
            card._manual_open = False
        if card.is_floating():
            self.overlay.release(card)
        card.hide()
        if self._is_single(card):
            card.dock.hide()
        self._sync_panel_button(card, False)
        self._sync_containers()
        self._sync_view_actions()
        self._schedule_fit()

    def _sync_view_actions(self):
        for pid, act in self._view_actions.items():
            card = self.cards.get(pid)
            act.blockSignals(True)
            act.setChecked(card is not None and self._is_shown(card))
            act.blockSignals(False)

    # -- left icon toolbar + floating tool options --
    def _opt_btn(self, name, tooltip, checkable=True):
        return icon_button(name, tooltip, px=ICON_PX, scale=0.55,
                           checkable=checkable)

    @staticmethod
    def _muted_label(text):
        """Placeholder text on an options page, one icon tall so the card keeps its height."""
        lbl = QtWidgets.QLabel(text)
        lbl.setStyleSheet("color: #9a9a9a; padding: 0 6px;")
        lbl.setMinimumHeight(ICON_PX)
        return lbl

    def _build_tools(self):
        L = QtCore.Qt.LeftDockWidgetArea

        # tools card/dock (added FIRST so it is at top of Left dock)
        holder = QtWidgets.QWidget()
        self.tools_flow = FlowLayout(spacing=ICON_GAP)
        holder.setLayout(self.tools_flow)
        self.tools_card = self._make_card("tools", "Tools", holder, kind="grid")
        self.tools_dock = self._make_dock(self.tools_card, L)
        self.tool_btns = {}
        for name, tip in (("mouse", "Select"),
                          ("box", "Box select"),
                          ("zoom", "Zoom"),
                          ("hand", "Pan (or hold Space)"),
                          ("transform", "Move and resize"),
                          ("bend", "Bend"),
                          ("tab", "Tab"),
                          ("line", "Line"),
                          ("freehand", "Freehand"),
                          ("curve", "Pen"),
                          ("polygon", "Polygon"),
                          ("rect", "Rectangle"),
                          ("ellipse", "Ellipse"),
                          ("type", "Type")):
            b = icon_button(name, tip, px=ICON_PX, scale=0.55,
                            checkable=True, checked=(name == "mouse"))
            b.clicked.connect(lambda _=False, n=name: self.canvas.set_tool(n))
            self.tools_flow.addWidget(b)
            self.tool_btns[name] = b

        # device control: a button grid like Tools, docked below it
        self.device_panel = DeviceControlPanel(main_win=self, px=ICON_PX)
        self.device_card = self._make_card("device", "Device", self.device_panel,
                                           kind="grid")
        self.device_dock = self._make_dock(self.device_card, L)
        self.device_panel.panel_toggled.connect(self._toggle_device_panel)
        self.device_panel.action_requested.connect(self._on_device_action)

        # spare left container for info cards dropped on the left (starts empty)
        self.left_dock = QtWidgets.QDockWidget("", self)
        self.left_dock.setWindowTitle("Panels (left)")
        self.left_dock.setTitleBarWidget(QtWidgets.QWidget(self.left_dock))
        self.left_container = DockContainer()
        self.left_container.dock = self.left_dock
        self.left_container.layout_changed = self._schedule_fit
        self.left_dock.setWidget(self.left_container)
        self.addDockWidget(L, self.left_dock)
        self.left_dock.hide()

        # manual jog: an info card (floats when opened from Device Control)
        self.jog_panel = JogPanel()
        self.jog_card = self._make_card("jog", "Manual Jog", self.jog_panel, prefer_float=True)
        self.right_container.add_card(self.jog_card)
        self.jog_card.hide()
        self.jog_panel.jog_requested.connect(self._on_jog_move)

        # cut settings: an info card opened by Device Control's Machine
        # configuration button (floats like the jog panel)
        self.cut_panel = CutSettingsPanel(self.doc)
        self.settings_card = self._make_card("cutsettings", "Cut Settings", self.cut_panel,
                                        prefer_float=True)
        self.right_container.add_card(self.settings_card)
        self.settings_card.hide()
        self.cut_panel.changed.connect(self._on_cut_settings_changed)

        # mat settings: the active mat profile drawn on the canvas (Documents/SnowCut/Mats)
        self.mat_panel = MatPanel(active=self._stored_mat_name())
        self.mat_card = self._make_card("mat", "Mat", self.mat_panel, prefer_float=True)
        self.right_container.add_card(self.mat_card)
        self.mat_card.hide()
        self.mat_panel.profile_changed.connect(self.canvas.set_mat_profile)
        self.mat_panel.active_changed.connect(self._store_mat_name)
        self.mat_panel.spoof_send_requested.connect(self._send_mat_kind)
        self.mat_panel.skew_changed.connect(self._on_skew_changed)
        self.mat_panel.set_skew(self.doc.mat_skew)
        self.canvas.set_mat_profile(self.mat_panel.profile)
        self.mat_panel.profile_changed.connect(lambda p, fit: self.scan_panel.set_context(profile=p))
        self.feeder = MatFeeder()

        # scan: what to scan / where the image goes (Device Control's Scan button)
        self.scan_panel = ScanPanel(self._stored_scan_settings())
        self.scan_card = self._make_card("scan", "Scan", self.scan_panel, prefer_float=True)
        self.right_container.add_card(self.scan_card)
        self.scan_card.hide()
        self.scan_panel.set_context(profile=self.mat_panel.profile, cal=self.doc.scan_cal,
                                    skew=self.doc.mat_skew)
        self.scan_panel.overlay_changed.connect(self._on_scan_overlay)
        self.scan_panel.draw_box_toggled.connect(self.canvas.set_scan_box_mode)
        self.scan_panel.settings_changed.connect(self._store_scan_settings)
        self.scan_panel.scan_requested.connect(self._start_panel_scan)
        self.scan_panel.cancel_requested.connect(self._cancel_scan)
        self.canvas.scan_box_drawn.connect(self.scan_panel.set_box_canvas)
        self.canvas.scan_box_mode_changed.connect(self.scan_panel.set_draw_active)
        self._scan_job = None
        # which firmware the machine is running (fw.SNOWCUT / fw.STOCK), None until it has answered
        self.machine_firmware = None
        self._fw_check = None              # FirmwareTools running the start-up check
        self._stock_offered = False
        self._fw_check_timer = QtCore.QTimer(self)
        self._fw_check_timer.setInterval(50)
        self._fw_check_timer.timeout.connect(self._firmware_check_poll)
        self._feed_timer = QtCore.QTimer(self)
        self._feed_timer.timeout.connect(self._feed_poll)
        # Device Control's Mount button: the machine's USB drive as a Windows drive (WinFsp)
        self.usb_drive = UsbDrive()
        self._drive_opened = None          # drive letter already shown in Explorer
        self._drive_timer = QtCore.QTimer(self)
        self._drive_timer.timeout.connect(self._drive_poll)

        # cut job (Device Control's cut button) + its code preview
        self.code_panel = CodePreviewPanel()
        self.code_card = self._make_card("codepreview", "Code Preview", self.code_panel,
                                         prefer_float=True)
        self.right_container.add_card(self.code_card)
        self.code_card.hide()
        self.cut_job = CutPanel(self.doc, self.sender, canvas=self.canvas, code=self.code_panel)
        self.cut_job.device_busy = self._device_busy
        self.job_card = self._make_card("cut", "Cut", self.cut_job, prefer_float=True)
        self.right_container.add_card(self.job_card)
        self.job_card.hide()
        self.cut_job.open_settings.connect(lambda: self._toggle_device_panel("config", True))
        self.cut_job.code_toggled.connect(self._toggle_code_preview)
        self.cut_job.generated.connect(self._commit_history)   # generate() writes doc.blade

        # options card/dock (starts hidden and floats bottom-left on startup)
        self.opt_stack = QtWidgets.QStackedWidget()
        self.options_card = self._make_card("options", "Tool Options", self.opt_stack,
                                           prefer_float=True, kind="grid")
        self.options_dock = self._make_dock(self.options_card, L)
        self.options_dock.hide()
        # Option pages are icon flows exactly like the Tools grid (same
        # buttons, gap and margins). DIV marks a divider between groups.
        self.type_btns, self.accum_btns = [], []
        self.mouse_box_toggle = self._opt_btn("box", "Drag on empty space to box select")
        self.mouse_xform_toggle = self._opt_btn("transform", "Show resize handles")
        self.mouse_box_toggle.clicked.connect(lambda v: self.canvas.set_mouse_box(v))
        self.mouse_xform_toggle.clicked.connect(lambda v: self.canvas.set_mouse_transform(v))
        self.mouse_type_btns = self._type_btns()
        self.mouse_accum_btns = self._accum_btns()
        self.nudge_spin = mm_spin(0.01, 100.0, 0.1, 2, self.canvas.nudge_mm,
                                  "Arrow-key nudge distance (hold Shift for 10×)")
        self.nudge_spin.valueChanged.connect(self.canvas.set_nudge_mm)
        self.nudge_spin.setFixedWidth(2 * ICON_PX + ICON_GAP)  # two grid cells
        self.nudge_spin.setSuffix("")   # the label carries the unit: no room at this width
        self._tool_pages = {}   # tool name -> options page
        self._tool_pages["mouse"] = self._flow_page(
            [self.mouse_box_toggle, DIV] + self.mouse_type_btns + [DIV] +
            self.mouse_accum_btns + [DIV, self.mouse_xform_toggle, DIV, self.anchor_btn_mouse(),
                                     DIV, QtWidgets.QLabel("Nudge (mm)"), self.nudge_spin])
        self._tool_pages["box"] = self._flow_page(self._type_btns() + [DIV] + self._accum_btns())

        self.zoom_out_btn = self._opt_btn("zoomout", "Zoom out", checkable=False)
        self.zoom_in_btn = self._opt_btn("zoomin", "Zoom in", checkable=False)
        self.fit_btn_z = self._opt_btn("fit", "Fit mat to window", checkable=False)
        self.zoom_out_btn.clicked.connect(lambda: self.canvas.set_zoom_relative(self.canvas.zoom_relative() / 1.25))
        self.zoom_in_btn.clicked.connect(lambda: self.canvas.set_zoom_relative(self.canvas.zoom_relative() * 1.25))
        self.fit_btn_z.clicked.connect(self.canvas.fit_view)
        self.zoom_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.zoom_slider.setToolTip("Zoom level")
        self.zoom_slider.setRange(10, 800)
        self.zoom_slider.setValue(100)
        self.zoom_slider.setFixedWidth(2 * ICON_PX + ICON_GAP)  # two grid cells
        self.zoom_slider.setFocusPolicy(QtCore.Qt.NoFocus)
        self.zoom_slider.valueChanged.connect(lambda v: self.canvas.set_zoom_relative(v / 100.0))
        self.zoom_mouse_toggle = self._opt_btn(
            "zoom_mouse", "Zoom toward the pointer instead of the window center")
        self.zoom_mouse_toggle.setChecked(self._stored_zoom_to_mouse())
        self.canvas.set_zoom_to_mouse(self.zoom_mouse_toggle.isChecked())
        self.zoom_mouse_toggle.toggled.connect(self._set_zoom_to_mouse)
        self._tool_pages["zoom"] = self._flow_page(
            [self.zoom_out_btn, self.zoom_in_btn, DIV, self.fit_btn_z, DIV, self.zoom_mouse_toggle,
             DIV, self.zoom_slider])

        self.fit_btn_h = self._opt_btn("fit", "Fit mat to window", checkable=False)
        self.fit_btn_h.clicked.connect(self.canvas.fit_view)
        self._tool_pages["hand"] = self._flow_page([self.fit_btn_h])

        self.anchor_btn = self._opt_btn("anchor", "Resize anchor", checkable=False)
        self.anchor_btn.clicked.connect(self.canvas.cycle_anchor)
        self._tool_pages["transform"] = self._flow_page([self.anchor_btn])
        self.bend_slider = self._pct_slider()
        self.bend_slider.setToolTip("How closely the curve follows the pointer.\n"
                                    "0%: the line bends evenly. 100%: the curve passes through the pointer.")
        self.bend_value = self._pct_label()
        self.canvas.set_bend_strength(self._stored_bend("strength", DEFAULT_BEND_STRENGTH))
        self.bend_slider.setValue(int(round(self.canvas.bend_strength * 100)))
        self.bend_slider.valueChanged.connect(self._set_bend_strength)
        self.bend_value.setText("%d%%" % self.bend_slider.value())
        self.focus_slider = self._pct_slider()
        self.focus_slider.setToolTip("How much dragging near one end bends only that end.\n"
                                     "0%: the whole line always bends. 100%: dragging near an "
                                     "end bends only that end.")
        self.focus_value = self._pct_label()
        self.canvas.set_bend_focus(self._stored_bend("focus", DEFAULT_BEND_FOCUS))
        self.focus_slider.setValue(int(round(self.canvas.bend_focus * 100)))
        self.focus_slider.valueChanged.connect(self._set_bend_focus)
        self.focus_value.setText("%d%%" % self.focus_slider.value())
        self._tool_pages["bend"] = self._flow_page(
            [QtWidgets.QLabel("Pull"), self.bend_slider, self.bend_value, DIV,
             QtWidgets.QLabel("Focus"), self.focus_slider, self.focus_value])
        self.tab_gap_spin = mm_spin(TAB_GAP_MIN_MM, TAB_GAP_MAX_MM, 0.5, 2, self.canvas.tab_gap_mm,
                                    "Gap length of new tabs, measured along the line")
        self.tab_gap_spin.valueChanged.connect(self.canvas.set_tab_gap_mm)
        self.tab_gap_spin.setFixedWidth(2 * ICON_PX + ICON_GAP)  # two grid cells
        self.tab_gap_spin.setSuffix("")   # the label carries the unit
        self._tool_pages["tab"] = self._flow_page([QtWidgets.QLabel("Gap (mm)"), self.tab_gap_spin])
        self._tool_pages["scanbox"] = self._flow_page(
            [self._muted_label("Drawing the scan area")])
        self._build_draw_pages()
        self._build_type_page()
        self.opt_stack.setCurrentIndex(0)

    def _build_draw_pages(self):
        """One options page shared by all drawing tools: color and thickness of new shapes, plus
        Smoothing (freehand), Sides (polygon) and Corner radius (polygon, rect), shown only while
        one of those tools is active."""
        self.draw_color_btn = ColorPicker(lambda: group_choices(self.doc), px=ICON_PX,
                                          title="Stroke Color", tooltip="Stroke color")
        self.draw_color_btn.picked.connect(self.canvas.set_draw_color)
        self.draw_color_btns = [self.draw_color_btn]
        self.thick_spin = mm_spin(0.0, MAX_THICKNESS_MM, 0.1, 2, self.canvas.draw_thickness_mm,
                                  "Line thickness of new shapes")
        self.thick_spin.valueChanged.connect(self.canvas.set_draw_thickness)
        self.thick_spin.setFixedWidth(2 * ICON_PX + ICON_GAP)
        self.thick_spin.setSuffix("")   # the label carries the unit: no room at this width
        self.smooth_spin = mm_spin(0.0, 10.0, 0.05, 2, self.canvas.draw_smooth_mm,
                                   "Higher values give smoother strokes with fewer points")
        self.smooth_spin.valueChanged.connect(self.canvas.set_draw_smooth)
        self.smooth_spin.setFixedWidth(2 * ICON_PX + ICON_GAP)
        self.smooth_spin.setSuffix("")   # the label carries the unit: no room at this width
        self.sides_spin = QtWidgets.QSpinBox()
        self.sides_spin.setRange(3, 32)
        self.sides_spin.setValue(self.canvas.draw_sides)
        self.sides_spin.setToolTip("Number of polygon sides")
        self.sides_spin.valueChanged.connect(self.canvas.set_draw_sides)
        self.sides_spin.setFixedWidth(2 * ICON_PX + ICON_GAP)
        self.radius_spin = mm_spin(0.0, MAX_RADIUS_MM, 0.5, 1, self.canvas.draw_radius_mm,
                                   "Corner radius of new polygons and rectangles\n"
                                   "(0 = sharp corners, limited to what fits each corner)")
        self.radius_spin.valueChanged.connect(self.canvas.set_draw_radius)
        self.radius_spin.setFixedWidth(2 * ICON_PX + ICON_GAP)
        self.radius_spin.setSuffix("")   # the label carries the unit: no room at this width
        smooth_lbl, sides_lbl = QtWidgets.QLabel("Smoothing (mm)"), QtWidgets.QLabel("Sides")
        radius_lbl = QtWidgets.QLabel("Corner radius (mm)")
        self._draw_tool_only = [(("freehand",), (smooth_lbl, self.smooth_spin)),
                                (("polygon",), (sides_lbl, self.sides_spin)),
                                (("polygon", "rect"), (radius_lbl, self.radius_spin))]
        page = self._flow_page([self.draw_color_btn, DIV, QtWidgets.QLabel("Thickness (mm)"),
                                self.thick_spin, DIV, smooth_lbl, self.smooth_spin,
                                DIV, sides_lbl, self.sides_spin, DIV, radius_lbl, self.radius_spin])
        for name in DRAW_TOOLS:
            self._tool_pages[name] = page
        self._sync_draw_options()

    def _sync_draw_options(self):
        c = self.canvas
        if not hasattr(self, "sides_spin"):
            return   # options pages not built yet
        self.draw_color_btn.set_color(c.draw_color)
        for tools, widgets in self._draw_tool_only:
            for w in widgets:
                w.setVisible(c.tool in tools)
        for w, v in ((self.smooth_spin, c.draw_smooth_mm), (self.sides_spin, c.draw_sides),
                     (self.thick_spin, c.draw_thickness_mm), (self.radius_spin, c.draw_radius_mm)):
            if abs(w.value() - v) > 1e-9:
                w.blockSignals(True)
                w.setValue(v)
                w.blockSignals(False)

    # -- Type tool options --
    def _build_type_page(self):
        """Type tool: color, font, size, alignment, the font's real bold / italic styles, and
        faux bold / italic (amounts in % of the font size)."""
        self.type_color_btn = ColorPicker(lambda: group_choices(self.doc), px=ICON_PX,
                                          title="Text Color", tooltip="Text color")
        self.type_color_btn.picked.connect(self._type_color)
        self.font_combo = QtWidgets.QFontComboBox()
        self.font_combo.setFontFilters(QtWidgets.QFontComboBox.ScalableFonts)
        self.font_combo.setFixedWidth(4 * ICON_PX + 3 * ICON_GAP)
        self.font_combo.setToolTip("Font")
        self.font_combo.currentFontChanged.connect(self._type_font)
        self.font_size_spin = QtWidgets.QDoubleSpinBox()
        self.font_size_spin.setRange(0.5, 500.0)
        self.font_size_spin.setDecimals(1)
        self.font_size_spin.setSingleStep(1.0)
        self.font_size_spin.setToolTip("Font size: the height of the em, in mm")
        self.font_size_spin.setFixedWidth(2 * ICON_PX + ICON_GAP)
        self.font_size_spin.valueChanged.connect(lambda v: self.canvas.set_text_style(size_mm=v))
        self.font_size_spin.editingFinished.connect(self._type_refocus)
        self.type_thick_spin = mm_spin(0.0, MAX_THICKNESS_MM, 0.1, 2, self.canvas.draw_thickness_mm,
                                       "Line thickness of the letters\n"
                                       "(the same value as the drawing tools' Thickness)")
        self.type_thick_spin.setSuffix("")
        self.type_thick_spin.setFixedWidth(2 * ICON_PX + ICON_GAP)
        self.type_thick_spin.valueChanged.connect(self.canvas.set_text_thickness)
        self.type_thick_spin.editingFinished.connect(self._type_refocus)
        self.align_btns = {}
        for key, tip in (("left", "Align left"), ("center", "Align center"), ("right", "Align right")):
            b = self._opt_btn("align_" + key, tip)
            b.clicked.connect(lambda _=False, k=key: self._type_set(align=k))
            self.align_btns[key] = b
        self.bold_btn = self._opt_btn("bold", "Bold")
        self.bold_btn.clicked.connect(self._type_bold)
        self.italic_btn = self._opt_btn("italic", "Italic")
        self.italic_btn.clicked.connect(lambda on: self._type_set(font_italic=bool(on)))
        self.faux_bold_btn = self._opt_btn("faux_bold", "Faux bold: grow the outline")
        self.faux_bold_btn.clicked.connect(lambda on: self._type_set(faux_bold_on=bool(on)))
        self.faux_bold_spin = self._pct_spin(0.0, 20.0, 0.5,
                                             "How far faux bold grows the outline, in % of the font size")
        self.faux_bold_spin.valueChanged.connect(lambda v: self.canvas.set_text_style(faux_bold=v / 100.0))
        self.faux_italic_btn = self._opt_btn("faux_italic", "Faux italic: slant the letters")
        self.faux_italic_btn.clicked.connect(lambda on: self._type_set(faux_italic_on=bool(on)))
        self.faux_italic_spin = self._pct_spin(-60.0, 60.0, 1.0, decimals=0, tip=
                                               "How far faux italic shifts the top of the letters\n"
                                               "sideways, in % of the font size")
        self.faux_italic_spin.valueChanged.connect(
            lambda v: self.canvas.set_text_style(faux_italic=v / 100.0))
        self._tool_pages["type"] = self._flow_page(
            [self.type_color_btn, DIV, self.font_combo, DIV, QtWidgets.QLabel("Size (mm)"),
             self.font_size_spin, DIV, QtWidgets.QLabel("Thickness (mm)"), self.type_thick_spin,
             DIV] + list(self.align_btns.values()) +
            [DIV, self.bold_btn, self.italic_btn, DIV, self.faux_bold_btn, self.faux_bold_spin,
             self.faux_italic_btn, self.faux_italic_spin])
        self._sync_type_options()

    def _pct_spin(self, lo, hi, step, tip, decimals=1):
        s = QtWidgets.QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(decimals)
        s.setSingleStep(step)
        s.setSuffix("%")
        s.setToolTip(tip)
        s.setFixedWidth(2 * ICON_PX + ICON_GAP)
        s.editingFinished.connect(self._type_refocus)
        return s

    def _type_refocus(self):
        # typing goes on in the canvas after a button / list / field of the options is used
        if self.canvas.editing_text_id() is not None:
            self.canvas.setFocus(QtCore.Qt.OtherFocusReason)

    def _type_set(self, **changes):
        self.canvas.set_text_style(**changes)
        self._type_refocus()

    def _type_color(self, color):
        self.canvas.set_text_color(color)
        self._type_refocus()

    def _type_font(self, font):
        fam = font.family()
        st = self.canvas.current_text_style()
        if fam == st.font_family:
            return
        base = text_ops.base_style(fam)
        self._type_set(font_family=fam, font_style=base,
                       font_italic=st.font_italic and text_ops.italic_for(fam, base) is not None)

    def _type_bold(self):
        """One other upright style: toggle to it. Several: choose one from a menu."""
        st = self.canvas.current_text_style()
        fam = st.font_family or text_ops.default_family()
        up = text_ops.upright_styles(fam)
        base = text_ops.base_style(fam)
        cur = st.font_style if st.font_style in up else base
        if len(up) == 2:
            choice = up[0] if cur == up[1] else up[1]
        else:
            self._sync_type_options()     # the click toggled the button, but the menu decides
            menu = QtWidgets.QMenu(self)
            chosen = []
            for name in up:
                act = menu.addAction(name)
                act.setCheckable(True)
                act.setChecked(name == cur)
                act.triggered.connect(lambda _=False, n=name: chosen.append(n))
                f = QtGui.QFont(fam)
                f.setStyleName(name)
                act.setFont(f)      # each weight shown in itself
            menu.exec(self.bold_btn.mapToGlobal(QtCore.QPoint(0, self.bold_btn.height())))
            menu.deleteLater()
            if not chosen:
                self._type_refocus()
                return
            choice = chosen[0]
        italic = st.font_italic and text_ops.italic_for(fam, choice) is not None
        self._type_set(font_style=choice, font_italic=italic)

    def _sync_type_options(self):
        if not hasattr(self, "bold_btn"):
            return
        c = self.canvas
        st = c.current_text_style()
        fam = st.font_family or text_ops.default_family()
        self.type_color_btn.set_color(c.current_text_color())
        quiet = [self.font_combo, self.font_size_spin, self.faux_bold_spin, self.faux_italic_spin,
                 self.type_thick_spin]
        for w in quiet:
            w.blockSignals(True)
        if self.font_combo.currentFont().family() != fam:
            self.font_combo.setCurrentFont(QtGui.QFont(fam))
        self.font_size_spin.setValue(st.size_mm)
        self.type_thick_spin.setValue(c.current_text_thickness())
        self.faux_bold_spin.setValue(st.faux_bold * 100.0)
        self.faux_italic_spin.setValue(st.faux_italic * 100.0)
        for w in quiet:
            w.blockSignals(False)
        for key, b in self.align_btns.items():
            b.setChecked(st.align == key)
        up = text_ops.upright_styles(fam)
        base = text_ops.base_style(fam)
        cur = st.font_style if st.font_style in up else base
        self.bold_btn.setEnabled(len(up) > 1)
        self.bold_btn.setChecked(len(up) > 1 and cur != base)
        if len(up) <= 1:
            self.bold_btn.setToolTip("Bold (this font has no other weights)")
        elif len(up) == 2:
            self.bold_btn.setToolTip("Bold: %s" % (up[1] if up[0] == base else up[0]))
        else:
            self.bold_btn.setToolTip("Weight: %s (click to choose)" % cur)
        has_italic = text_ops.italic_for(fam, cur) is not None
        self.italic_btn.setEnabled(has_italic)
        self.italic_btn.setChecked(has_italic and st.font_italic)
        self.italic_btn.setToolTip("Italic" if has_italic else "Italic (this font has no italic for this weight)")
        self.faux_bold_btn.setChecked(st.faux_bold_on)
        self.faux_bold_spin.setEnabled(st.faux_bold_on)
        self.faux_italic_btn.setChecked(st.faux_italic_on)
        self.faux_italic_spin.setEnabled(st.faux_italic_on)

    def anchor_btn_mouse(self):
        self.anchor_btn_m = self._opt_btn("anchor", "Resize anchor", checkable=False)
        self.anchor_btn_m.clicked.connect(self.canvas.cycle_anchor)
        return self.anchor_btn_m

    def _flow_page(self, items):
        page = _OptionsPage()
        flow = FlowLayout(spacing=ICON_GAP)
        flow.pack_groups = True      # a floating bar narrower than its page wraps into full rows
        page.setLayout(flow)
        add_flow_items(flow, items)
        self._page_flows.append(flow)
        self.opt_stack.addWidget(page)
        return page

    def _type_btns(self):
        return [self._mode_btn("points", "Select points"),
                self._mode_btn("segments", "Select segments"),
                self._mode_btn("paths", "Select whole paths")]

    def _accum_btns(self):
        return [self._accum_btn("new", "Replace selection"),
                self._accum_btn("add", "Add to selection"),
                self._accum_btn("subtract", "Subtract from selection"),
                self._accum_btn("intersect", "Intersect with selection")]

    def _mode_btn(self, mode, tip):
        b = icon_button(mode, tip, px=ICON_PX, scale=0.55, checkable=True)
        b.clicked.connect(lambda _=False, m=mode: self.canvas.set_box_mode(m))
        self.type_btns.append(b)
        return b

    def _accum_btn(self, mode, tip):
        b = icon_button(mode, tip, px=ICON_PX, scale=0.55, checkable=True)
        b.clicked.connect(lambda _=False, m=mode: self.canvas.set_select_mode(m))
        self.accum_btns.append(b)
        return b

    def _sync_options(self):
        c = self.canvas
        for name, b in self.tool_btns.items():
            b.blockSignals(True)
            b.setChecked(name == c.tool)
            b.blockSignals(False)
        for b, m in zip(self.type_btns, ["points", "segments", "paths"] * 2):
            b.blockSignals(True)
            b.setChecked(m == c.box_mode)
            b.blockSignals(False)
        for b, m in zip(self.accum_btns, ["new", "add", "subtract", "intersect"] * 2):
            b.blockSignals(True)
            b.setChecked(m == c.select_mode)
            b.blockSignals(False)
        for tgl, val in ((self.mouse_box_toggle, c.mouse_box_enabled),
                         (self.mouse_xform_toggle, c.mouse_transform_enabled)):
            tgl.blockSignals(True)
            tgl.setChecked(val)
            tgl.blockSignals(False)
        # unavailable mouse-tool actions stay in place, drawn inactive
        for b in self.mouse_type_btns + self.mouse_accum_btns:
            b.setEnabled(c.mouse_box_enabled)
        self.anchor_btn_m.setEnabled(c.mouse_transform_enabled)
        if abs(self.nudge_spin.value() - c.nudge_mm) > 1e-9:
            self.nudge_spin.blockSignals(True)
            self.nudge_spin.setValue(c.nudge_mm)
            self.nudge_spin.blockSignals(False)
        self._sync_draw_options()
        self._sync_type_options()
        anchor_tip = "Resize from: %s (click to change)" % c.anchor_label().lower()
        self.anchor_btn.setToolTip(anchor_tip)
        self.anchor_btn_m.setToolTip(anchor_tip)
        pg = self.opt_stack.currentWidget()
        if pg is not None:
            if hasattr(pg, "layout") and pg.layout() is not None:
                pg.layout().activate()
            pg.updateGeometry()
        if self.options_card.is_floating() and self.options_card.isVisible():
            self._contract_card(self.options_card)

    def _on_tool_changed(self, name):
        self.opt_stack.setCurrentWidget(self._tool_pages[name])
        self.opt_stack.updateGeometry()  # fit the new page, not the largest
        if not self.options_card.isVisible():
            self.show_card(self.options_card, floating=self.options_card._prefer_float)
        self._sync_options()

    def _on_zoom_changed(self, f):
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(int(max(10, min(800, f * 100))))
        self.zoom_slider.blockSignals(False)

    def _update_layout_modes(self):
        # button grids: icons fill across a vertical dock and wrap down. In a
        # horizontal dock they fill down and wrap across
        for card, flow in self._grid_flows().items():
            dock = card.dock
            area = self.dockWidgetArea(dock)
            horiz = (not card.is_floating() and
                     area in (QtCore.Qt.TopDockWidgetArea, QtCore.Qt.BottomDockWidgetArea))
            flow.set_vertical(horiz)
            self._cap_dock(dock, horiz)
            key = (card.panel_id, area)
            if not card.is_floating() and self._last_areas.get(card.panel_id) != key:
                self._last_areas[card.panel_id] = key
                self._default_span(dock, horiz)
        # Tool Options pages flow like the other grids: across a side dock (and
        # when floating), down a top/bottom dock.
        docked = not self.options_card.is_floating() and self.options_dock.isVisible()
        o_area = self.dockWidgetArea(self.options_dock)
        o_tb = o_area in (QtCore.Qt.TopDockWidgetArea, QtCore.Qt.BottomDockWidgetArea)
        for f in self._page_flows:
            f.set_vertical(docked and o_tb)
        if not self.options_card.is_floating() and self.options_dock.isVisible():
            self.options_card.layout().activate()
            self._cap_dock(self.options_dock, o_tb)
            key = ("options", o_area)
            if self._last_areas.get("options") != key:
                self._last_areas["options"] = key
                self._default_span(self.options_dock, o_tb)
        # info containers: left-to-right cards in top/bottom docks
        for cont in self._containers():
            cont.set_row_mode(self.dockWidgetArea(cont.dock) in
                              (QtCore.Qt.TopDockWidgetArea, QtCore.Qt.BottomDockWidgetArea))

    def _cap_dock(self, dock, horiz):
        """Button-grid dock floor: 2 icons across a vertical dock, 2 icons
        tall in a horizontal one (or more if the content cannot fit)."""
        content = dock.widget().minimumSizeHint() if dock.widget() is not None else QtCore.QSize()
        if horiz:
            dock.setMinimumHeight(max(_tools_extent_h(2), content.height()))
            dock.setMinimumWidth(80)
        else:
            dock.setMinimumWidth(max(_tools_extent_w(2), content.width()))
            dock.setMinimumHeight(_tools_extent_h(1))
        dock.setMaximumHeight(16777215)
        dock.setMaximumWidth(16777215)

    def _default_span(self, dock, horiz):
        try:
            if horiz:
                self.resizeDocks([dock], [dock.minimumHeight()], QtCore.Qt.Vertical)
            else:
                self.resizeDocks([dock], [dock.minimumWidth()], QtCore.Qt.Horizontal)
        except Exception:
            pass
        self._schedule_fit()

    # -- float-manager geometry: client area minus menu/status, proportional --
    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._layout_overlay()
        self._schedule_fit()

    # -- automatic panel heights --
    # Every card wants its content's natural height (or the user's override,
    # if larger). Container cards get that from their stack layout, floating
    # cards are resized, and single-card docks are fitted along their dock
    # column here, with a container (or the last dock) absorbing the rest.
    def _schedule_fit(self):
        if hasattr(self, "_fit_timer"):
            self._fit_timer.start(0)

    def eventFilter(self, obj, ev):
        if isinstance(obj, PanelCard):
            t = ev.type()
            if t == QtCore.QEvent.LayoutRequest:
                obj.sync_grips()
                obj.update()  # a grid's painted body follows its content
                self._schedule_fit()
            elif t == QtCore.QEvent.Resize and ev.size().width() != ev.oldSize().width():
                self._schedule_fit()  # reflowed content may need another height
        return super().eventFilter(obj, ev)

    def event(self, ev):
        # Dock separator drags are handled by QMainWindow itself. The dock sizes are
        # recorded around the press so that a user-set height can be kept.
        t = ev.type()
        if t == QtCore.QEvent.MouseButtonPress and ev.button() == QtCore.Qt.LeftButton:
            self._sep_snap = {d: d.size() for d in self._all_docks() if d.isVisible()}
        res = super().event(ev)
        if t == QtCore.QEvent.MouseButtonRelease and self._sep_snap is not None:
            snap, self._sep_snap = self._sep_snap, None
            QtCore.QTimer.singleShot(0, lambda: self._record_dock_resize(snap))
        return res

    def _record_dock_resize(self, snap):
        for d, old in snap.items():
            if not d.isVisible() or d.height() == old.height():
                continue
            card = d.widget()
            if isinstance(card, PanelCard) and not card.is_floating():
                need = card.natural_height(card.width())
                card.set_user_height(card.height() if card.height() > need + 1 else None)
        self._schedule_fit()

    def _card_user_resized(self, card):
        if card.is_floating():
            card.move_clamped(card.pos())
        elif self._is_single(card):
            self._fit_all(force=True)

    def _card_user_resize_done(self, card):
        if card.is_floating() and card.overlay is not None:
            card.overlay.store_fraction(card)
        self._schedule_fit()

    def _dock_want_h(self, dock):
        w = dock.widget()
        if isinstance(w, PanelCard):
            h = w.desired_height(w.width()) + max(0, dock.height() - w.height())
        elif isinstance(w, DockContainer):
            h = w.content_height() + max(0, dock.height() - w.height())
        else:
            return dock.height()
        # never ask below the dock's floor: Qt would take the shortfall
        # proportionally out of the other docks in the column
        return max(h, dock.minimumHeight(), dock.minimumSizeHint().height())

    def _fit_all(self, force=False):
        if self._sep_snap is not None or self._dragging_card is not None:
            return
        for card in self.cards.values():
            if card.is_floating() and card.isVisible() and card is not self.options_card:
                want = card.desired_height(card.width())
                if abs(card.height() - want) > 1:
                    card.resize(card.width(), want)
                    card.move_clamped(card.pos())
        if force:
            self._last_fit.clear()
        client_h = self.overlay.client_rect().height() if hasattr(self, "overlay") else self.height()
        for area in self._EDGE_AREAS:
            vertical = self._is_vertical_area(area)
            for col in self._area_columns(area):
                if vertical:
                    if len(col) < 2:
                        continue
                    conts = [d for d in col if isinstance(d.widget(), DockContainer)]
                    absorber = conts[-1] if conts else col[-1]
                    cur = [d.height() for d in col]
                    want = [None if d is absorber else self._dock_want_h(d) for d in col]
                    rest = max(40, sum(cur) - sum(w for w in want if w is not None))
                    sizes = [rest if w is None else w for w in want]
                    docks = col
                else:
                    # a top/bottom row's height: fit info cards, leave the
                    # button grids (sized by their own span rules) alone
                    fit = [d for d in col if not (isinstance(d.widget(), PanelCard) and
                                                  self._is_single(d.widget()))]
                    if not fit:
                        continue
                    h = min(max(self._dock_want_h(d) for d in fit), int(client_h * 0.4))
                    docks, cur, sizes = [col[0]], [col[0].height()], [h]
                if sizes == cur:
                    continue
                key = tuple(id(d) for d in docks)
                if self._last_fit.get(key) == (tuple(sizes), tuple(cur)):
                    continue  # asked for this already, and the constraints won
                self._last_fit[key] = (tuple(sizes), tuple(cur))
                try:
                    self.resizeDocks(docks, sizes, QtCore.Qt.Vertical)
                except Exception:
                    pass
        # dock columns may have moved the canvas: re-anchor floating cards and
        # keep them above any dock created since they were placed
        if hasattr(self, "overlay"):
            self._layout_overlay()

    def _layout_overlay(self):
        self.overlay.relayout()
        for card in self.overlay.cards:
            if card.is_floating() and card.isVisible():
                card.raise_()

    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._options_placed:
            self._options_placed = True
            QtCore.QTimer.singleShot(0, self._float_options_initial)

    def _float_options_initial(self):
        self._layout_overlay()
        self.show_card(self.options_card, floating=True)
        self._contract_card(self.options_card)
        c = self.options_card
        c._outside_canvas = False
        c._anchored_to_canvas = True
        # Position at bottom-left inside canvas
        cr = self.overlay.canvas_rect()
        target_x = cr.x() + 16
        target_y = cr.bottom() - c.height() - 16
        c.move_clamped(QtCore.QPoint(target_x, target_y), canvas_bound=True)
        self.overlay.store_fraction(c)
        self._collapse_all_areas()

    # -- menus --
    def _menus(self):
        m = self.menuBar()
        f = m.addMenu("&File")
        f.addAction("&New Project", QtGui.QKeySequence.New, self.new_proj)
        f.addAction("&Open Project…", QtGui.QKeySequence.Open, self.load_proj)
        self.recent_menu = f.addMenu("Open &Recent")
        self.recent_menu.aboutToShow.connect(self._sync_recent_menu)
        self._sync_recent_menu()
        f.addAction("&Save Project", QtGui.QKeySequence.Save, self.save_proj)
        f.addAction("Save Project &As…", QtGui.QKeySequence("Ctrl+Shift+S"), self.save_proj_as)
        f.addSeparator()
        f.addAction("&Import…", QtGui.QKeySequence("Ctrl+I"), self.import_dialog)
        f.addAction("Set &Background Image…", self.set_bg)
        f.addSeparator()
        f.addAction("E&xit", QtGui.QKeySequence("Ctrl+Q"), self.exit_app)

        e = m.addMenu("&Edit")
        # A focused QLineEdit / QSpinBox keeps its own Ctrl+Z / Ctrl+Y (it accepts the shortcut
        # override), so these only reach the document when no text field has the focus.
        self.act_undo = QtGui.QAction("&Undo", self)
        self.act_undo.setShortcut(QtGui.QKeySequence("Ctrl+Z"))
        self.act_undo.triggered.connect(self.undo)
        e.addAction(self.act_undo)
        self.act_redo = QtGui.QAction("&Redo", self)
        self.act_redo.setShortcuts([QtGui.QKeySequence("Ctrl+Y"), QtGui.QKeySequence("Ctrl+Shift+Z")])
        self.act_redo.triggered.connect(self.redo)
        e.addAction(self.act_redo)
        e.addSeparator()
        # While text is being typed on the canvas, Ctrl+A/C/X/V edit the text instead (the
        # canvas accepts their shortcut override).
        e.addAction("Cu&t", QtGui.QKeySequence.Cut, self.cut_objects)
        e.addAction("&Copy", QtGui.QKeySequence.Copy, self.copy_objects)
        e.addAction("&Paste", QtGui.QKeySequence.Paste, self.paste_objects)
        e.addAction("&Duplicate", QtGui.QKeySequence("Ctrl+D"), self.duplicate_objects)
        e.addAction("&Delete", self.canvas.delete_selected)
        e.addSeparator()
        e.addAction("Rotate Ri&ght", QtGui.QKeySequence("Ctrl+R"), lambda: self.canvas.rotate_selected(90.0))
        e.addAction("Rotate &Left", QtGui.QKeySequence("Ctrl+Shift+R"),
                    lambda: self.canvas.rotate_selected(-90.0))
        e.addAction("Flip &Horizontal", QtGui.QKeySequence("Ctrl+Shift+H"),
                    lambda: self.canvas.flip_selected(False))
        e.addAction("Flip &Vertical", QtGui.QKeySequence("Ctrl+Shift+V"),
                    lambda: self.canvas.flip_selected(True))
        e.addSeparator()
        e.addAction("Select &All", QtGui.QKeySequence.SelectAll, self.select_all)
        self._sync_undo_actions()

        view = m.addMenu("&View")
        self._view_menu = view
        view.addAction("&Fit Mat to Window", QtGui.QKeySequence("Ctrl+0"), self.canvas.fit_view)
        view.addSeparator()
        for pid, card in self.cards.items():
            act = QtGui.QAction(card.header.title.text(), self)
            act.setCheckable(True)
            act.triggered.connect(lambda _=False, c=card: self._view_toggle(c))
            view.addAction(act)
            self._view_actions[pid] = act

        mach = m.addMenu("&Machine")
        self.act_machine_settings = mach.addAction("Machine &Settings…", self.machine_settings)
        # Without a libusb driver the machine is reached through Windows' own USB storage
        # driver, which only an administrator may use: a small elevated helper (one UAC prompt)
        # does that for this normal, non-elevated app (see pc-driver/scsipt.py).
        self.act_admin = mach.addAction("Connect with &Administrator Rights…",
                                        self.connect_admin_helper)
        self.act_admin.setToolTip("Windows requires administrator rights for access to the machine")
        mach.aboutToShow.connect(self._sync_admin_action)
        self._sync_admin_action()

        hlp = m.addMenu("&Help")
        hlp.addAction("&About SnowCut", self.show_about)

    def show_about(self):
        AboutDialog(self).exec()

    # -- Edit: whole objects --
    CLIP_MIME = "application/x-snowcut-objects"
    PASTE_STEP_MM = 5.0

    def _selected_objects(self):
        return self.doc.svg_ids_for_selection()

    def copy_objects(self):
        """Edit > Copy: the selected objects (whole) to the clipboard. Returns the count."""
        ids = self._selected_objects()
        data = self.doc.copy_objects(ids)
        if data is None:
            self.statusBar().showMessage("Nothing selected to copy")
            return 0
        mime = QtCore.QMimeData()
        mime.setData(self.CLIP_MIME, json.dumps(data, default=str).encode("utf-8"))
        QtWidgets.QApplication.clipboard().setMimeData(mime)
        self._paste_count = 0
        n = len(data["svgs"])
        self.statusBar().showMessage("Copied %d object%s" % (n, "" if n == 1 else "s"))
        return n

    def cut_objects(self):
        """Edit > Cut: copy the selected objects, then delete them."""
        ids = self._selected_objects()
        if not self.copy_objects():
            return
        self.canvas.cancel_transient()
        self.doc.selection.clear()
        self.doc.selection.svg_ids.update(ids)
        self.canvas.delete_selected()
        self._paste_count = -1     # the first paste goes back where the objects were

    def paste_objects(self):
        """Edit > Paste: copies of the objects on the clipboard, each paste a little further
        down and right of the originals."""
        mime = QtWidgets.QApplication.clipboard().mimeData()
        if mime is None or not mime.hasFormat(self.CLIP_MIME):
            self.statusBar().showMessage("Nothing to paste")
            return []
        try:
            data = json.loads(bytes(mime.data(self.CLIP_MIME)).decode("utf-8"))
        except ValueError:
            return []
        self._paste_count = getattr(self, "_paste_count", 0) + 1
        return self._paste(data, self._paste_count * self.PASTE_STEP_MM, "Pasted")

    def duplicate_objects(self):
        """Edit > Duplicate: copies of the selected objects, placed a little down and right."""
        data = self.doc.copy_objects(self._selected_objects())
        if data is None:
            self.statusBar().showMessage("Nothing selected to duplicate")
            return []
        return self._paste(data, self.PASTE_STEP_MM, "Duplicated")

    def _paste(self, data, d, verb):
        self.canvas.cancel_transient()
        ids = self.doc.paste_objects(data, d, d)
        if ids:
            self.refresh_all()
            self.statusBar().showMessage("%s %d object%s" % (verb, len(ids),
                                                             "" if len(ids) == 1 else "s"))
        return ids

    def select_all(self):
        """Edit > Select All: every visible object."""
        self.canvas.cancel_transient()
        self.doc.select_all()
        self.canvas.selection_changed.emit()

    # -- drag and drop: files dropped on the window are imported --
    def _dropped_files(self, ev):
        md = ev.mimeData()
        if not md.hasUrls():
            return []
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile()]

    def dragEnterEvent(self, ev):
        files = self._dropped_files(ev)
        if any(f.lower().endswith(PROJECT_EXT) or image_ops.file_kind(f) for f in files):
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dropEvent(self, ev):
        files = self._dropped_files(ev)
        ev.acceptProposedAction()
        # a dialog opened from inside the drop would block the source application
        QtCore.QTimer.singleShot(0, lambda: self.open_files(files))

    def open_files(self, files):
        """Open a dropped project, or import dropped drawings, images and PDFs."""
        projects = [f for f in files if f.lower().endswith(PROJECT_EXT)]
        if projects:
            self.open_project(projects[0])
            return
        for f in files:
            if image_ops.file_kind(f):
                self.import_file(f)

    @staticmethod
    def _admin_helper_needed():
        """True when the machine is visible only through Windows' USB storage driver and no
        elevated helper is connected yet."""
        if sys.platform != "win32":
            return False
        try:
            import scsipt
            from memdump import VID, PID
            if scsipt.is_admin() or scsipt.broker_alive():
                return False
            # a disk Windows made for the machine means no libusb-capable driver is bound
            return scsipt.present(VID, PID)
        except Exception:
            return False

    def _sync_admin_action(self):
        self.act_admin.setVisible(self._admin_helper_needed())

    def connect_admin_helper(self, reason=None, ask=False):
        """Machine > Connect with Administrator Rights: start the elevated USB helper (a UAC
        prompt). `ask`: explain and confirm first (start-up offer). -> True when connected."""
        try:
            import scsipt
        except ImportError:
            return False
        if scsipt.is_admin() or scsipt.broker_alive():
            scsipt.allow_broker(True)
            return True
        if ask:
            r = QtWidgets.QMessageBox.question(
                self, "Administrator Rights",
                (reason + "\n\n" if reason else "") +
                "Windows requires administrator rights for SnowCut to access the machine. "
                "Windows will ask you to allow this.\n\nContinue?")
            if r != QtWidgets.QMessageBox.Yes:
                return False
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            scsipt.start_broker()
        except scsipt.AccessDenied:
            self._device_status("Not connected: administrator rights were not given.")
            return False
        except Exception as e:
            self._device_status("Could not connect with administrator rights: %s" % e)
            return False
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        scsipt.allow_broker(True)          # restart it by itself should it ever stop
        self._sync_admin_action()
        self._device_status("Connected.")
        if self.machine_firmware is None:
            self.check_machine_firmware()  # now that SnowCut can reach it
        return True

    def offer_admin_helper(self):
        """At start-up: the machine is connected through Windows' USB storage driver, which only
        an administrator can use -> offer the helper. Quiet otherwise."""
        if not self._admin_helper_needed():
            return
        self.connect_admin_helper("The machine is connected, but SnowCut cannot access it yet.",
                                  ask=True)

    def startup_machine_check(self):
        """At start-up: offer administrator rights if Windows needs them (granting them starts the
        firmware check), else ask the machine right away which firmware it is running."""
        if self._admin_helper_needed():
            self.offer_admin_helper()
        else:
            self.check_machine_firmware()

    def check_machine_firmware(self):
        """Ask the machine in the background whether it is running the SnowCut firmware (0xC2 PING:
        no data and no effect: open, ask, release). Quiet when it cannot be reached."""
        if self._fw_check is not None or self._device_busy():
            return False
        tools = FirmwareTools()
        if not tools.start_detect():
            return False
        self._fw_check = tools
        self._fw_check_timer.start()
        return True

    def _firmware_check_poll(self):
        busy, _p, _t, result = self._fw_check.poll()
        if busy:
            return
        self._fw_check_timer.stop()
        self._fw_check = None
        if result is not None and result[0]:
            self._firmware_found(result[1])

    def _firmware_found(self, kind):
        self.machine_firmware = kind
        if kind == fw.STOCK and not self._stock_offered:
            self._stock_offered = True     # once per session (Machine Settings has it too)
            self.offer_stock_firmware()

    def ask_stock_firmware(self):
        return QtWidgets.QMessageBox.question(
            self, "Original Firmware",
            "The machine is running the original vendor firmware, so SnowCut cannot communicate "
            "with it over the USB cable.\n\nSnowCut can automatically patch an original firmware "
            "file (.upf) to enable communication. You can install the patched update from a USB flash drive "
            "directly on the machine. Choose the firmware file now?\n\nYou can also do this later "
            "in Machine > Machine Settings.") == QtWidgets.QMessageBox.Yes

    def offer_stock_firmware(self):
        """The machine is running the original firmware: offer to patch an original .upf and save it
        for a USB flash drive (nothing can be sent over the cable)."""
        if not self.ask_stock_firmware():
            return False
        dlg = FirmwareDialog(FirmwareTools(firmware=fw.STOCK), parent=self)
        QtCore.QTimer.singleShot(0, dlg.start)
        dlg.exec()
        dlg.deleteLater()
        return True

    def machine_settings(self):
        """Machine > Machine Settings: the machine's own settings."""
        why = self._device_busy()
        if why:
            self._device_status(wait_message(why))
            return
        tools = FirmwareTools(firmware=self.machine_firmware)
        dlg = MachineSettingsDialog(UsbMachineSettings(), device_busy=self._device_busy,
                                    firmware=tools, parent=self)
        dlg.exec()
        if tools.firmware is not None:
            self.machine_firmware = tools.firmware     # the settings read tells which
        dlg.deleteLater()

    def _view_toggle(self, card):
        if self._is_shown(card):
            self.hide_card(card, manual=True)
        else:
            card._manual_closed = False
            card._manual_open = True  # stays open until closed, whatever the selection
            self.show_card(card)
        self._sync_view_actions()

    def closeEvent(self, ev):
        # only a close the user asked for (title bar, Alt+F4, File > Exit) offers to save
        if (ev.spontaneous() or self._exiting) and not self.maybe_save():
            self._exiting = False
            ev.ignore()
            return
        if hasattr(self, "cut_job"):
            self.cut_job.shutdown()
        if getattr(self, "usb_drive", None) is not None and self.usb_drive.busy:
            failed = self.usb_drive.shutdown()
            if failed:
                QtWidgets.QMessageBox.warning(
                    self, "USB drive", "These files could not be saved to the machine:\n\n"
                    + "\n".join(failed))
        super().closeEvent(ev)

    def _flush_dirty(self, card):
        pid = card.panel_id
        if pid in self._panel_dirty and not self._in_rules:
            p = self._info_panels[pid]
            p.doc = self.doc
            p.refresh()
            self._panel_dirty.discard(pid)

    def _on_svg_dragging(self):
        # drag: only the SVG panel's x/y/scale change, coalesced to one update per event-loop turn
        if not self._drag_pending:
            self._drag_pending = True
            QtCore.QTimer.singleShot(0, self._flush_drag)

    def _flush_drag(self):
        self._drag_pending = False
        if self._is_shown(self.cards["svg"]):
            self.svg_panel.doc = self.doc
            self.svg_panel.refresh()

    def refresh_all(self):
        d = self.doc
        d.promote_text_selection()     # text objects are selected whole
        rules = {
            "objects": not bool(d.selection.path_ids or d.selection.segments or d.selection.points
                                 or d.selection.tabs),
            "cutorder": True,
            "colorkey": (not d.selection.is_empty()) or bool(self.cutorder_panel.active_key),
            "line": bool(d.selection.path_ids or d.selection.segments or d.selection.svg_ids),
            "point": _has_point_refs(d),
            "tab": bool(d.selection.tabs),
            "svg": bool(d.svg_ids_for_selection()),
        }
        self._in_rules = True  # the refresh loop below covers cards shown here
        try:
            for pid, want in rules.items():
                card = self.cards[pid]
                if getattr(card, "_manual_closed", False):
                    continue
                shown = self._is_shown(card)
                if want and not shown:
                    self.show_card(card, floating=getattr(card, "_prefer_float", False))
                elif not want and shown and not card._manual_open:
                    self.hide_card(card, manual=False)
        finally:
            self._in_rules = False
        for pid, p in self._info_panels.items():
            p.doc = d
            # hidden cards skip the work and refresh when next shown. The Color Key
            # always runs (refresh calls doc.ensure_color)
            if pid == "colorkey" or self._is_shown(self.cards[pid]):
                p.refresh()
                self._panel_dirty.discard(pid)
            else:
                self._panel_dirty.add(pid)
        self.cut_panel.doc = d
        self.cut_panel.refresh()
        if hasattr(self, "mat_panel"):
            self.mat_panel.set_skew(d.mat_skew)     # undo / project load
        if hasattr(self, "scan_panel") and (self.scan_panel.cal != d.scan_cal
                                            or self.scan_panel.skew != d.mat_skew):
            self.scan_panel.set_context(cal=d.scan_cal, skew=d.mat_skew)
        # floating cards keep minimum size as their content changes (e.g. a
        # toggled subgroup would otherwise overflow invisibly, clipped and unresponsive)
        for card in self.cards.values():
            if card.is_floating() and card.isVisible():
                self._contract_card(card)
                self.overlay.store_fraction(card)
        self._sync_options()
        self._sync_card_titles()
        self._sync_view_actions()
        self.canvas.refresh()
        if hasattr(self, "cut_job"):
            self.cut_job.check_document()
        if not self._restoring:
            self._commit_history()    # last: the panel refreshes above may normalise the document

    # -- undo / redo --
    def _stored_undo_levels(self):
        try:
            return clamp_levels(self._settings().value("undo/levels", DEFAULT_UNDO_LEVELS))
        except Exception:
            return DEFAULT_UNDO_LEVELS

    def undo_levels(self):
        """How many undo steps are kept."""
        return self.history.max_levels

    def set_undo_levels(self, n):
        """Change the number of undo steps live (older steps are dropped at once) and remember it
        for the next start.  Returns the value in effect (clamped)."""
        n = self.history.set_max_levels(n)
        try:
            self._settings().setValue("undo/levels", n)
        except Exception:
            pass
        self._sync_undo_actions()
        return n

    def _commit_history(self):
        """Record the document's current state as an undo step if it changed since the last one.
        Called at the end of refresh_all and after the edits that bypass it."""
        key = self.canvas.history_coalesce_key
        self.canvas.history_coalesce_key = None
        self.history.commit(self.doc, key)
        self._sync_undo_actions()
        self._sync_window_title()

    def _sync_undo_actions(self):
        if hasattr(self, "act_undo"):
            self.act_undo.setEnabled(self.history.can_undo())
            self.act_redo.setEnabled(self.history.can_redo())

    def undo(self):
        return self._history_step(redo=False)

    def redo(self):
        return self._history_step(redo=True)

    def _history_step(self, redo):
        cv = self.canvas
        if cv._drag_kind is not None:     # mid-gesture: the document is in flux
            return False
        if cv._draw_state is not None:
            # a shape is being drawn: Ctrl+Z steps back inside the shape (last vertex), the
            # document history is untouched, and redo has nothing to do until the shape is finished
            if not redo and cv.draw_undo_step():
                self.statusBar().showMessage("Undo (drawing)")
                return True
            if redo:
                return False
        if not (self.history.can_redo() if redo else self.history.can_undo()):
            self.statusBar().showMessage("Nothing to redo" if redo else "Nothing to undo")
            return False
        cv.cancel_transient()
        self._restoring = True
        try:
            ok = self.history.redo(self.doc) if redo else self.history.undo(self.doc)
            self.refresh_all()
        finally:
            self._restoring = False
        if ok:
            self.history.rebase(self.doc)   # whatever the refresh normalised is not an edit
        self._sync_undo_actions()
        self.statusBar().showMessage("Redo" if redo else "Undo")
        return ok

    def _sync_window_title(self):
        name = os.path.basename(self.doc.file_path) if self.doc.file_path else "Untitled"
        self.setWindowTitle("%s%s - %s" % (os.path.splitext(name)[0],
                                           "*" if self.is_modified() else "", APP_NAME))

    # -- unsaved changes --
    def _mark_saved(self):
        """The document as it is now is what is on disk (or a new, empty project)."""
        self._saved_json = doc_json(self.doc)
        self._sync_window_title()

    def is_modified(self):
        """True when the document differs from the last save (or the new / opened project)."""
        saved = getattr(self, "_saved_json", None)
        base = getattr(getattr(self, "history", None), "_base", None)
        return saved is not None and base is not None and base[0] != saved

    def _ask_save(self):
        """Save / Discard / Cancel for unsaved changes (replaceable in tests)."""
        name = os.path.splitext(os.path.basename(self.doc.file_path))[0] if self.doc.file_path \
            else "Untitled"
        return QtWidgets.QMessageBox.question(
            self, "Unsaved Changes", "Save the changes to %s?" % name,
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard
            | QtWidgets.QMessageBox.Cancel, QtWidgets.QMessageBox.Save)

    def maybe_save(self):
        """Before the document is replaced or the app closes: offer to save unsaved changes.
        Returns False when the user cancels."""
        if not self.is_modified():
            return True
        r = self._ask_save()
        if r == QtWidgets.QMessageBox.Save:
            return self.save_proj()
        return r == QtWidgets.QMessageBox.Discard

    def exit_app(self):
        self._exiting = True       # closeEvent asks about unsaved changes
        self.close()

    def _sync_card_titles(self):
        """The object-properties card is retitled by the selected object kinds."""
        title = self.svg_panel.card_title()
        card = self.cards["svg"]
        if card.header.title.text() != title:
            card.header.title.setText(title)
        act = self._view_actions.get("svg")
        if act is not None and act.text() != title:
            act.setText(title)

    # -- file ops --
    # Import (File menu): SVG, raster images, PDF
    def _show_error(self, title, text):
        QtWidgets.QMessageBox.warning(self, title, text)

    def _ask_pdf_page(self, path, count):
        """Which page of a multi-page PDF to import (0-based), or None when cancelled."""
        n, ok = QtWidgets.QInputDialog.getInt(
            self, "Import PDF", "Page (1 - %d):" % count, 1, 1, count)
        return n - 1 if ok else None

    def _settings(self):
        # Documents/SnowCut/settings.ini (tests redirect it with SNOWCUT_USER_DIR)
        return userdata.settings()

    def _stored_zoom_to_mouse(self):
        try:
            return str(self._settings().value("view/zoom_to_mouse", "false")).lower() == "true"
        except Exception:
            return False

    def _pct_slider(self):
        s = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        s.setRange(0, 100)
        s.setFixedWidth(2 * ICON_PX + ICON_GAP)
        s.setFocusPolicy(QtCore.Qt.NoFocus)
        return s

    @staticmethod
    def _pct_label():
        lbl = QtWidgets.QLabel("")
        lbl.setFixedWidth(lbl.fontMetrics().horizontalAdvance("100%") + 4)
        return lbl

    def _stored_bend(self, name, default):
        try:
            return float(self._settings().value("tools/bend_" + name, default))
        except Exception:
            return default

    def _store_bend(self, name, value):
        try:
            self._settings().setValue("tools/bend_" + name, value)
        except Exception:
            pass

    def _set_bend_strength(self, pct):
        self.canvas.set_bend_strength(pct / 100.0)
        self.bend_value.setText("%d%%" % pct)
        self._store_bend("strength", self.canvas.bend_strength)

    def _set_bend_focus(self, pct):
        self.canvas.set_bend_focus(pct / 100.0)
        self.focus_value.setText("%d%%" % pct)
        self._store_bend("focus", self.canvas.bend_focus)

    def _set_zoom_to_mouse(self, on):
        self.canvas.set_zoom_to_mouse(on)
        self.statusBar().showMessage("Zoom toward the %s" % ("pointer" if on else "window center"))
        try:
            self._settings().setValue("view/zoom_to_mouse", "true" if on else "false")
        except Exception:
            pass

    def _stored_mat_name(self):
        try:
            return str(self._settings().value("mat/active", "") or "")
        except Exception:
            return ""

    def _store_mat_name(self, name):
        try:
            self._settings().setValue("mat/active", name)
        except Exception:
            pass

    def import_dialog(self):
        start = ""
        try:
            start = str(self._settings().value("import_dir", "") or "")
        except Exception:
            pass
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Import", start, image_ops.IMPORT_FILTER)
        if not path:
            return
        try:
            self._settings().setValue("import_dir", os.path.dirname(path))
        except Exception:
            pass
        self.import_file(path)

    def import_file(self, path, page=None):
        """Import an SVG, image (png/jpg/jpeg/bmp) or PDF by file extension (case-insensitive).
        page: 0-based PDF page, or None to ask (multi-page PDFs only).  Returns the new svg id, or None
        when cancelled / failed (failures are reported through _show_error)."""
        kind = image_ops.file_kind(path)
        name = os.path.basename(path)
        if kind is None:
            self._show_error("Import", "Unsupported file type: %s" % name)
            return None
        try:
            if kind == "svg":
                paths = import_svg(path, tol=0.1, from_file=True)
                svg_id = self.doc.add_svg(name, paths, offset=(10.0, 10.0))
                msg = "Imported %s (%d path%s)" % (name, len(paths), "" if len(paths) == 1 else "s")
            elif kind == "image":
                svg_id = image_ops.add_image_object(self.doc, path, "image")
                s = self.doc.svgs[svg_id]
                msg = "Imported %s (%d \u00d7 %d px, %.0f dpi)" % (name, s.px_w, s.px_h, s.dpi)
            else:
                from core import raster
                if not raster.pdf_available():
                    raise RuntimeError("PDF import requires the pypdfium2 package (pip install pypdfium2).")
                if page is None:
                    count = raster.pdf_page_count(path)
                    page = 0 if count <= 1 else self._ask_pdf_page(path, count)
                    if page is None:
                        return None
                svg_id = image_ops.add_image_object(self.doc, path, "pdf", page=int(page))
                msg = "Imported %s, page %d" % (name, int(page) + 1)
        except Exception as e:
            self._show_error("Import", "Could not import %s:\n%s" % (name, e))
            return None
        if kind != "svg":
            err = image_ops.regenerate_outline(self.doc, svg_id)   # does nothing unless enabled
            if err:
                msg += " (%s)" % err
        self.doc.selection.clear()
        self.doc.selection.svg_ids.add(svg_id)
        self.refresh_all()
        self.statusBar().showMessage(msg)
        return svg_id

    def set_bg(self):
        """Embed a full-mat scan image from disk as the canvas background (placed like a scan
        of the whole mat: its dpi is worked out from its width)."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Set Background Image", "",
                                                        "Images (*.jpg *.jpeg *.png)")
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Set Background Image", str(e))
            return
        img = QtGui.QImage.fromData(data)
        if img.isNull():
            QtWidgets.QMessageBox.warning(self, "Set Background Image",
                                          "%s is not a readable image." % os.path.basename(path))
            return
        info = img.dotsPerMeterX() * 0.0254 if img.dotsPerMeterX() > 0 else None
        dpi = machine.image_dpi(img.width(), img.height(), info)
        c0, r0 = scanlib.wide_origin(img.width(), dpi) if dpi in scanlib.DPIS else (0, 0)
        self.doc.set_scan_image(data, os.path.splitext(path)[1].lower() or ".jpg", dpi, c0, r0)
        self.refresh_all()

    def save_proj(self):
        """File > Save Project: save in place, or ask where for a new (or old .json) project.
        Returns True when saved."""
        if self.doc.file_path and self.doc.file_path.lower().endswith(PROJECT_EXT):
            return self._save_to(self.doc.file_path)
        return self.save_proj_as()

    def save_proj_as(self):
        start = self.doc.file_path or os.path.join(userdata.user_dir(), "Untitled" + PROJECT_EXT)
        if not start.lower().endswith(PROJECT_EXT):    # an old .json project is saved as .sncproj
            start = os.path.splitext(start)[0] + PROJECT_EXT
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Project", start, "SnowCut project (*%s)" % PROJECT_EXT)
        if not path:
            return False
        if not path.lower().endswith(PROJECT_EXT):
            path += PROJECT_EXT
        return self._save_to(path)

    def _save_to(self, path):
        try:
            self.doc.save(path)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Save Project", "The project could not be saved.\n\n%s" % e)
            return False
        self._mark_saved()
        self._add_recent(path)
        self.statusBar().showMessage("Saved %s" % os.path.basename(path))
        return True

    def new_proj(self):
        """File > New Project: an empty project that keeps this machine's calibration."""
        if not self.maybe_save():
            return
        old, doc = self.doc, Document()
        doc.area_w_mm, doc.area_h_mm = old.area_w_mm, old.area_h_mm
        doc.steps_x, doc.steps_y = old.steps_x, old.steps_y
        doc.scan_cal, doc.mat_skew, doc.blade = old.scan_cal, old.mat_skew, old.blade
        self._set_document(doc)
        self.statusBar().showMessage("New project")

    def _set_document(self, doc):
        """Replace the document: no undo steps, nothing unsaved."""
        self.doc = doc
        self.cut_job.set_doc(self.doc)
        self.canvas.cancel_transient()
        self.canvas.doc = self.doc
        self._restoring = True      # the new document is not an edit of the old one
        try:
            self.refresh_all()
        finally:
            self._restoring = False
        self.history.reset(self.doc)
        self._sync_undo_actions()
        self._mark_saved()

    def load_proj(self):
        if not self.maybe_save():
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open Project", os.path.dirname(self.doc.file_path) if self.doc.file_path else "",
            "SnowCut projects (*%s *.json);;All files (*)" % PROJECT_EXT)
        if not path:
            return
        try:
            self.load_project(path)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Open Project", "The project could not be opened.\n\n%s" % e)

    def open_project(self, path):
        """Open a project (a dropped file, a recent project, the command line) after offering
        to save unsaved changes.  Returns True when opened."""
        if not self.maybe_save():
            return False
        try:
            self.load_project(path)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Open Project", "The project could not be opened.\n\n%s" % e)
            return False
        return True

    # -- File > Open Recent (Documents/SnowCut/settings.ini, most recent first) --
    def recent_projects(self):
        """The recent project paths that still exist. Missing files are dropped from the list."""
        try:
            paths = json.loads(self._settings().value("file/recent", "") or "[]")
        except Exception:
            paths = []
        paths = [p for p in paths if isinstance(p, str)]
        kept = [p for p in paths if os.path.isfile(p)]
        if kept != paths:
            self._store_recent(kept)
        return kept

    def _store_recent(self, paths):
        try:
            self._settings().setValue("file/recent", json.dumps(paths[:MAX_RECENT]))
        except Exception:
            pass

    def _add_recent(self, path):
        path = os.path.abspath(path)
        key = os.path.normcase(path)
        self._store_recent([path] + [p for p in self.recent_projects() if os.path.normcase(p) != key])

    def clear_recent(self):
        self._store_recent([])
        self._sync_recent_menu()

    def _sync_recent_menu(self):
        menu = self.recent_menu
        menu.clear()
        paths = self.recent_projects()
        fm = menu.fontMetrics()
        for i, p in enumerate(paths, 1):
            text = fm.elidedText(p, QtCore.Qt.ElideMiddle, 60 * fm.averageCharWidth())
            act = menu.addAction("%s%d  %s" % ("&" if i < 10 else "", i, text.replace("&", "&&")))
            act.setStatusTip(p)
            act.triggered.connect(lambda _=False, p=p: self.open_recent(p))
        if not paths:
            menu.addAction("No Recent Projects").setEnabled(False)
        menu.addSeparator()
        clear = menu.addAction("&Clear Recent Projects", self.clear_recent)
        clear.setEnabled(bool(paths))

    def open_recent(self, path):
        if not os.path.isfile(path):
            QtWidgets.QMessageBox.warning(
                self, "Open Recent", "%s could not be found and has been removed from the list."
                % os.path.basename(path))
            self.recent_projects()      # drops it
            return False
        return self.open_project(path)

    def load_project(self, path):
        """Load a project file.  Image / pdf outlines are regenerated from their source files. An
        object whose file is missing keeps its stored outline paths (and a status warning)."""
        doc = Document.load(path)
        warns = [w for w in image_ops.regenerate_all(doc) if not w.startswith("source file missing")]
        self._set_document(doc)
        self._add_recent(path)
        missing = [s.label for s in self.doc.svgs.values()
                   if image_ops.is_raster(s) and not image_ops.source_available(s)]
        if missing:
            warns.append("Source file missing: %s (using the saved outline)" % ", ".join(missing))
        if warns:
            self.statusBar().showMessage(". ".join(w[:1].upper() + w[1:] for w in dict.fromkeys(warns)))
        else:
            self.statusBar().showMessage("Opened %s" % os.path.basename(path))

    # -- scan --
    # -- scan (Scan panel) --
    def _stored_scan_settings(self):
        try:
            return scanlib.ScanSettings.from_dict(json.loads(self._settings().value("scan/settings", "") or "{}"))
        except Exception:
            return scanlib.ScanSettings()

    def _store_scan_settings(self, settings):
        try:
            self._settings().setValue("scan/settings", json.dumps(settings.to_dict()))
        except Exception:
            pass

    def _on_scan_overlay(self, ov):
        if self._is_shown(self.scan_card):
            self.canvas.set_scan_overlay(ov)

    def _scan_status(self, text):
        self._device_status(text)

    def _device_busy(self):
        """What is using the machine right now ("scan", "mat feed", "cut job"), or None.
        Only one operation can have the USB device open at a time."""
        drive = getattr(self, "usb_drive", None)
        if drive is not None and drive.busy:
            return DRIVE_BUSY                  # the mounted drive holds the machine's USB handle
        if getattr(self, "_scan_job", None) is not None:
            return "scan"
        if getattr(self, "feeder", None) is not None and self.feeder.busy:
            return "mat feed"
        if getattr(self, "cut_job", None) is not None and self.cut_job.sending:
            return "cut job"
        if getattr(self, "_fw_check", None) is not None:
            return "firmware check"
        return None

    def _start_panel_scan(self, settings, window):
        """Scan `window` with the panel's settings."""
        if self._scan_job is not None:
            return
        why = self._device_busy()
        if why:
            self._scan_failed(wait_message(why) + ".")
            return
        try:
            self.scanner.start_scan(dpi=settings.dpi, custom_load=self._custom_load_args(),
                                    window=window, raw=settings.fmt == "png",
                                    quality=settings.quality, sampling=settings.sampling_mode())
        except Exception as e:
            self._scan_failed(str(e))
            return
        self._scan_job = {"settings": settings, "thread": None, "box": {}, "cancelled": False}
        self.scan_panel.set_busy(True, "Starting scan\u2026", 0.0)
        self._scan_status("Scanning…")
        self._scan_timer.start(200)

    def _scan_poll(self):
        job = self._scan_job
        if job is None:
            self._scan_timer.stop()
            return
        th, box = job["thread"], job["box"]
        if th is not None:                       # downloading / converting
            if th.is_alive():
                done, total = box.get("progress", (0, 1))
                self.scan_panel.set_busy(True, "Downloading\u2026 %.0f%%" % (100.0 * done / max(1, total)),
                                         0.8 + 0.2 * done / max(1, total))
                return
            self._scan_timer.stop()
            self._scan_job = None
            if not job["cancelled"]:
                self._finish_scan(job["settings"], box)
            return
        busy, f = self.scanner.poll()
        if busy:
            self.scan_panel.set_busy(True, "Scanning\u2026 %.0f%%" % (f * 100), 0.8 * f)
            return
        if self.scanner.state != getattr(self.scanner, "STATE_DONE", "done"):
            self._scan_timer.stop()
            self._scan_job = None
            self._scan_failed(getattr(self.scanner, "last_error", "") or "The scan did not finish.")
            return
        raw = job["settings"].fmt == "png"
        scanner = self.scanner

        def work():
            try:
                data, meta = scanner.fetch_data(
                    progress_cb=lambda d, t: box.__setitem__("progress", (d, t)))
                if raw:
                    rgb = scanlib.raw_to_rgb(data, meta["width"], meta["lines"], meta["offsets"])
                    data, ext = scanlib.rgb_to_png(rgb, meta["dpi"]), ".png"
                else:
                    ext = ".jpg"
                box["result"] = (data, ext, meta)
            except Exception as e:
                box["error"] = str(e)
        job["thread"] = threading.Thread(target=work, daemon=True)
        job["thread"].start()
        self.scan_panel.set_busy(True, "Downloading\u2026", 0.8)

    def _cancel_scan(self):
        job = self._scan_job
        if job is None:
            return
        job["cancelled"] = True
        if job["thread"] is None:               # still scanning: stop the machine
            self._scan_timer.stop()
            self._scan_job = None
            try:
                self.scanner.cancel()
            except Exception:
                pass
        self.scan_panel.set_busy(False)
        self._scan_status("Scan canceled")

    def _scan_failed(self, msg):
        self.scan_panel.set_busy(False)
        self._scan_status("Scan failed")
        QtWidgets.QMessageBox.warning(self, "Scan", msg)

    @staticmethod
    def _unique_path(path):
        root, ext = os.path.splitext(path)
        n = 1
        while os.path.exists(path):
            path, n = "%s_%d%s" % (root, n, ext), n + 1
        return path

    def _finish_scan(self, settings, box):
        self.scan_panel.set_busy(False)
        if "error" in box:
            self._scan_failed(box["error"])
            return
        data, ext, meta = box["result"]
        notes = []
        if ext == ".jpg":
            info = scanlib.jpeg_info(data)
            want = (2, 2) if settings.subsampling else (1, 1)
            if info and info[2] != want:
                notes.append("color detail is %s than requested"
                             % ("lower" if info[2] > want else "higher"))
        if meta.get("dpi") != settings.dpi:
            notes.append("scanned at %d dpi (the area is too large for %d)" % (meta["dpi"], settings.dpi))
        saved = None
        if settings.dest in ("file", "both"):
            target = settings.path or scan_output_path(self.doc.file_path, ext)
            if os.path.splitext(target)[1].lower() not in (".jpg", ".jpeg", ".png"):
                target += ext
            target = self._unique_path(target)       # never overwrite an earlier scan
            try:
                with open(target, "wb") as f:
                    f.write(data)
                saved = target
            except OSError as e:
                notes.append("could not save %s: %s" % (target, e.strerror or e))
        if settings.dest in ("canvas", "both"):
            cal = getattr(self.scanner, "scan_cal", None)
            if cal is not None:      # place it with this unit's own scan->cut calibration
                self.doc.scan_cal = cal
                self.scan_panel.set_context(cal=cal)
            self.doc.set_scan_image(data, ext, meta["dpi"], meta["col0"], meta["row0"])
            self.refresh_all()
        msg = "Scan complete (%d \u00d7 %d px, %d dpi)" % (meta["width"], meta["lines"], meta["dpi"])
        if saved:
            msg += ", saved to %s" % saved
        if notes:
            msg += ". " + ". ".join(n[:1].upper() + n[1:] for n in notes)
        self._scan_status(msg)

    def _device_panel_cards(self):
        """Device Control buttons that open/close an info panel."""
        return {"jog": self.jog_card, "config": self.settings_card, "cut": self.job_card,
                "mat": self.mat_card, "scan": self.scan_card}

    def _sync_panel_button(self, card, shown):
        """A card was shown/hidden: update the buttons that toggle it."""
        if card is getattr(self, "job_card", None):
            self.cut_job.set_shown(shown)  # toolpath overlay only while Cut is open
        if card is getattr(self, "code_card", None):
            self.cut_job.set_code_active(shown)
        if card is getattr(self, "scan_card", None):     # box overlay only while Scan is open
            self.canvas.set_scan_overlay(self.scan_panel.overlay() if shown else None)
            if not shown:
                self.canvas.set_scan_box_mode(False)
        if not hasattr(self, "device_panel"):
            return
        for act, c in self._device_panel_cards().items():
            if c is card:
                self.device_panel.set_panel_active(act, shown)

    def _place_floating_panel(self, card):
        """Open floating panels in a row along the canvas's top-left."""
        cr = self.overlay.canvas_rect()
        x = cr.x() + 24
        for other in (self.jog_card, self.settings_card, self.mat_card, self.scan_card, self.job_card,
                      self.code_card):
            if other is not card and other.is_floating() and self._is_shown(other):
                x = max(x, other.geometry().right() + 12)
        card.move_clamped(QtCore.QPoint(x, cr.y() + 60))
        self.overlay.store_fraction(card)

    def _toggle_code_preview(self, show):
        card = self.code_card
        if not show:
            self.hide_card(card)
        elif self._is_shown(card):
            card.raise_()
        else:
            self.show_card(card, floating=card._prefer_float)
            if card.is_floating():
                self._place_floating_panel(card)

    def _toggle_device_panel(self, act, show):
        card = self._device_panel_cards()[act]
        if not show:
            self.hide_card(card)
            return
        if self._is_shown(card):
            card.raise_()
            self._sync_panel_button(card, True)
            return
        self.show_card(card, floating=card._prefer_float)
        if card.is_floating():
            self._place_floating_panel(card)

    # -- mat: feed, machine mat type, skew --
    def _custom_load_args(self):
        """(mat kind, feed depth mm, printed->cut Y scale) for 0xC3/0x54, from the mat profile.
        The feed depth is the top margin, raised by whole grid units to at least 32 mm."""
        p = self.mat_panel.profile
        return (p.spoof_kind, p.feed_depth_mm(), machine.MAT_SCALE_Y)

    def _device_status(self, text):
        self.statusBar().showMessage(text)

    def _feed(self):
        if self.feeder.busy:
            self._device_status("The mat is still being fed")
            return
        why = self._device_busy()
        if why:
            self._device_status(wait_message(why))
            return
        try:
            self.feeder.start(*self._custom_load_args())
        except Exception as e:
            self._device_status("Mat feed failed: %s" % e)
            return
        self._device_status("Feeding mat\u2026")
        self._feed_timer.start(200)

    def _feed_poll(self):
        busy, msg = self.feeder.poll()
        if busy:
            text = {"load": "Loading mat\u2026", "unload": "Unloading mat\u2026"}.get(self.feeder.action,
                                                                                    "Feeding mat\u2026")
            if self.feeder.detail:
                text += " (%s)" % self.feeder.detail
            self._device_status(text)
            return
        self._feed_timer.stop()
        if msg:
            self._device_status(msg)

    def _send_mat_kind(self, kind):
        why = self._device_busy()
        if why:
            self._device_status(wait_message(why))
            return
        try:
            self._device_status(send_mat_kind(kind))
        except Exception as e:
            self._device_status("Could not set the mat type: %s" % _fw_friendly(e))

    def _on_skew_changed(self, skew):
        self.doc.mat_skew = skew
        self.scan_panel.set_context(skew=skew)
        self.canvas.refresh()
        self.cut_job.check_document()     # cuts generated with the old skew are stale
        self._commit_history()

    def _on_cut_settings_changed(self):
        self.cut_job.cut_settings_changed()
        self._commit_history()    # the panel wrote doc.blade without going through refresh_all

    @staticmethod
    def _jog_command(**kw):
        """One G0 travel. The USB handle is released even if it fails."""
        dev = _fw_dev()
        try:
            return _fw_motion(dev, 0x00, **kw)
        finally:
            _fw_close(dev)

    def _on_jog_move(self, dx_mm, dy_mm):
        why = self._device_busy()
        if why:
            self.statusBar().showMessage(wait_message(why))
            return
        if dx_mm == 0 and dy_mm == 0:
            # Home
            try:
                st = self._jog_command(X=0, Y=0)
            except Exception as e:
                self.statusBar().showMessage("Could not move home: %s" % _fw_friendly(e))
                return
            if st != 0:   # refused: the machine is still moving
                self.statusBar().showMessage("The machine is busy")
                return
            self.statusBar().showMessage("Moved to home position")
        else:
            try:
                st = self._jog_command(X=int(round(dx_mm * self.doc.steps_x)),
                                       Y=int(round(dy_mm * self.doc.steps_y)), flags=1)  # rel=1
            except Exception as e:
                self.statusBar().showMessage("Could not move: %s" % _fw_friendly(e))
                return
            if st != 0:
                self.statusBar().showMessage("The machine is busy")
                return
            self.statusBar().showMessage("Moved X %+.1f mm, Y %+.1f mm" % (dx_mm, dy_mm))

    # -- USB drive (Mount button) --
    def _toggle_drive(self):
        """One click: mount the machine's USB drive as a drive letter and open it in Explorer.
        Click again to unmount."""
        d = self.usb_drive
        if d.state == "mounted":
            pending = d.pending_files()
            if pending and QtWidgets.QMessageBox.question(
                    self, "Unmount USB drive",
                    "These files are still being written:\n\n%s\n\nUnmount anyway? They will be "
                    "saved to the machine as they are now." % "\n".join(pending)
            ) != QtWidgets.QMessageBox.StandardButton.Yes:
                self.device_panel.set_drive_state(d.state, d.letter)
                return
            d.stop()
            self._device_status("Unmounting the USB drive…")
        elif d.state == "idle":
            ok, reason = winfsp_status()
            if not ok:
                self.device_panel.set_drive_state(d.state)
                self._winfsp_missing(reason)
                return
            why = self._device_busy()
            if why:
                self.device_panel.set_drive_state(d.state)
                self._device_status(wait_message(why))
                return
            d.start()
            self._device_status("Mounting the machine's USB drive…")
        # mounting / unmounting: the click just re-syncs the button
        self.device_panel.set_drive_state(d.state, d.letter)
        self._drive_timer.start(200)

    def _winfsp_missing(self, reason):
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Information)
        box.setWindowTitle("Mount USB drive")
        box.setText("Mounting the machine's USB drive requires WinFsp.")
        box.setInformativeText(reason)
        get = box.addButton("Open download page", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is get:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(WINFSP_URL))

    def _drive_poll(self):
        d = self.usb_drive
        for is_error, msg in d.poll():
            self._device_status(msg)
            if is_error:
                QtWidgets.QMessageBox.warning(self, "USB drive", msg)
        self.device_panel.set_drive_state(d.state, d.letter)
        if d.state == "mounted" and self._drive_opened != d.letter:
            self._drive_opened = d.letter
            try:
                os.startfile(d.letter + ":\\")
            except OSError:
                pass
        if d.state == "idle":
            self._drive_opened = None
            self._drive_timer.stop()

    def _on_device_action(self, act):
        if act == "feed":
            self._feed()      # load (custom mat, no recognition) / unload
        elif act == "mount_usb":
            self._toggle_drive()
        elif act == "abort":
            self.sender.cancel()
            if self._scan_job is not None:
                self._cancel_scan()
            self.statusBar().showMessage("Stopped")
