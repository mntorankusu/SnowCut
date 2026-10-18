"""Dockable property panels: objects, cut order, color group, line, point, svg.

All action buttons are square icon buttons (words in tooltips), PANEL_BTN px in panels.
Field labels sit in QFormLayouts without trailing colons, like the Scan / Mat / Cut panels.
Dynamic lists clamp to 2..5 visible rows so panels stay compact."""
from collections import Counter
from PySide6 import QtWidgets, QtGui, QtCore
from core.doc import normalize_color, GROUP_TOOLS
from core import userdata
from .curve_editor import CurveEditor
from . import icons, image_ops
from .icons import icon_button
from .flowlayout import FlowLayout
from .color_picker import ColorPicker, swatch_pixmap, icon_pixmap
from .draw_tools import MAX_THICKNESS_MM, MAX_RADIUS_MM

PANEL_BTN = 32          # action buttons inside info panels
MUTED = "color: #9a9a9a;"
TOOL_LABELS = {"cut": "Cut", "draw": "Draw", "emboss": "Emboss"}   # GROUP_TOOLS -> UI text
MERGE_SETTING = "confirm/merge_color_groups"


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def group_choices(doc):
    """[(color, label)] of the document's color groups in cut order, for ColorPicker menus."""
    counts = Counter(p.color for p in doc.paths.values())
    return [(c, "%s   %s" % (c, plural(counts[c], "path") if counts[c] else "empty")) for c in doc.group_colors()]


def confirm_merge(parent):
    """Ask before a recolor merges color groups, unless the user turned the question off."""
    st = userdata.settings()
    if str(st.value(MERGE_SETTING, "true")).lower() == "false":
        return True
    box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Question, "Merge Color Groups",
                                "This will merge the affected color groups. Continue?",
                                QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel, parent)
    box.button(QtWidgets.QMessageBox.Ok).setText("Merge")
    skip = QtWidgets.QCheckBox("Don't ask again")
    box.setCheckBox(skip)
    ok = box.exec() == QtWidgets.QMessageBox.Ok
    if ok and skip.isChecked():
        st.setValue(MERGE_SETTING, "false")
    return ok


def selected_path_ids(doc, points=True):
    """Paths touched by the selection: whole paths, segments, objects and (optionally) points."""
    sel = doc.selection
    out = set(sel.path_ids)
    out.update(pid for (pid, _i) in sel.segments)
    if points:
        out.update(pid for (pid, _i) in sel.points)
    for svg_id in sel.svg_ids:
        s = doc.svgs.get(svg_id)
        if s:
            out.update(s.path_ids)
    return {p for p in out if p in doc.paths}


def _form():
    f = QtWidgets.QFormLayout()
    f.setContentsMargins(0, 0, 0, 0)
    return f


def _apply_row(btn):
    """An apply button aligned right under its fields."""
    row = QtWidgets.QHBoxLayout()
    row.addStretch(1)
    row.addWidget(btn)
    return row


def fit_list_height(lw, count, row_h=None):
    """Fix a QListWidget's height to 2..5 rows (min two lines, max five).
    0 or 1 items show 2 empty/content rows. More than 5 items gets a scrollbar.
    """
    if row_h is None:
        row_h = lw.sizeHintForRow(0) if lw.count() else max(22, lw.fontMetrics().lineSpacing() + 10)
    # Count: 0 or 1 -> 2 rows, 2..5 -> count rows, >5 -> 5 rows
    rows = max(2, min(5, int(count)))
    lw.setFixedHeight(int(2 * lw.frameWidth() + rows * row_h + 4))


class MixedSpin(QtWidgets.QAbstractSpinBox):
    """Number field of a property panel: blank with a '(mixed)' placeholder when the selected
    values differ. Arrows, the mouse wheel and Up / Down (Page Up / Down = 10 steps) step it,
    starting from the first selected value when mixed. Nothing is applied until Enter (emits
    `submitted`) or the panel's apply button. Shown values are rounded to `decimals`, but an
    untouched field reports the exact value it was given, so applying another field never
    rounds this one."""
    submitted = QtCore.Signal()

    def __init__(self, step=1.0, decimals=2, lo=-1e6, hi=1e6, parent=None):
        super().__init__(parent)
        self._step, self._dec, self._lo, self._hi = float(step), int(decimals), float(lo), float(hi)
        self._first = None    # stepping from a blank field starts here
        self._shown = None    # (text, exact value) put there by set_values

    def _fmt(self, v):
        s = "%.*f" % (self._dec, v)
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s

    def _clamp(self, v):
        return max(self._lo, min(self._hi, v))

    # -- QLineEdit-like API --
    def setText(self, text):
        self.lineEdit().setText(text)

    def placeholderText(self):
        return self.lineEdit().placeholderText()

    def setPlaceholderText(self, text):
        self.lineEdit().setPlaceholderText(text)

    def set_values(self, vals):
        self.blockSignals(True)
        self._first = vals[0] if vals else None
        if vals and all(abs(v - vals[0]) < 1e-9 for v in vals):
            self.setText(self._fmt(vals[0]))
            self._shown = (self.text(), vals[0])
            self.setPlaceholderText("")
        else:
            self.setText("")
            self._shown = None
            self.setPlaceholderText("(mixed)" if vals else "")
        self.blockSignals(False)

    def edited(self):
        """True once the text differs from what set_values showed (a value typed or stepped)."""
        t = self.text().strip()
        return t != (self._shown[0] if self._shown is not None else "")

    def parsed(self):
        """The value to apply, or None (blank / not a number)."""
        t = self.text().strip()
        if self._shown is not None and t == self._shown[0]:
            return self._shown[1]
        try:
            return self._clamp(float(t)) if t else None
        except ValueError:
            return None

    # -- QAbstractSpinBox --
    def validate(self, text, pos):
        t = text.strip()
        if t in ("", "-", ".", "-."):
            return QtGui.QValidator.Intermediate
        try:
            float(t)
        except ValueError:
            return QtGui.QValidator.Invalid
        return QtGui.QValidator.Acceptable

    def stepEnabled(self):
        flags = QtWidgets.QAbstractSpinBox.StepEnabledFlag
        v = self.parsed()
        if v is None:
            v = self._first
        if v is None:
            return flags.StepUpEnabled | flags.StepDownEnabled
        out = flags.StepNone
        if v < self._hi - 1e-9:
            out |= flags.StepUpEnabled
        if v > self._lo + 1e-9:
            out |= flags.StepDownEnabled
        return out

    def stepBy(self, steps):
        v = self.parsed()
        if v is None:
            v = self._first if self._first is not None else max(self._lo, min(self._hi, 0.0))
        self.setText(self._fmt(self._clamp(round(v + steps * self._step, self._dec))))
        self.setPlaceholderText("")
        self.lineEdit().selectAll()

    def keyPressEvent(self, ev):
        if ev.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            ev.accept()
            self.submitted.emit()
            return
        super().keyPressEvent(ev)


def _tri(check, vals):
    """Set a tristate checkbox from a list of bools. Returns True if mixed."""
    check.blockSignals(True)
    if not vals:
        check.setCheckState(QtCore.Qt.Unchecked)
    elif all(vals):
        check.setCheckState(QtCore.Qt.Checked)
    elif not any(vals):
        check.setCheckState(QtCore.Qt.Unchecked)
    else:
        check.setCheckState(QtCore.Qt.PartiallyChecked)
    check.blockSignals(False)
    return check.checkState() == QtCore.Qt.PartiallyChecked


class ObjectsPanel(QtWidgets.QWidget):
    changed = QtCore.Signal()
    ROW_H = 40

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self.list = QtWidgets.QListWidget()
        self.list.itemClicked.connect(self._row_clicked)
        lay.addWidget(self.list)

    def refresh(self):
        d = self.doc
        self.list.clear()
        ids = list(d.svgs)
        if not ids:
            it = QtWidgets.QListWidgetItem("No objects. Use File › Import to add one.")
            it.setFlags(QtCore.Qt.NoItemFlags)
            self.list.addItem(it)
        for svg_id in ids:
            s = d.svgs[svg_id]
            it = QtWidgets.QListWidgetItem()
            it.setData(QtCore.Qt.UserRole, svg_id)
            it.setSizeHint(QtCore.QSize(0, self.ROW_H))
            if svg_id in d.selection.svg_ids:
                it.setSelected(True)
            self.list.addItem(it)
            row = QtWidgets.QWidget()
            hl = QtWidgets.QHBoxLayout(row)
            hl.setContentsMargins(2, 2, 2, 2)
            tag = {"image": "Image", "pdf": "PDF", "text": "Text"}.get(s.kind, "")
            name = QtWidgets.QLabel(s.label + ('  <span style="color:#9a9a9a">%s</span>' % tag if tag else ""))
            if not s.visible:
                name.setStyleSheet(MUTED)
            if image_ops.is_raster(s):
                name.setToolTip("%s\n%s%s" % (s.source_path, image_ops.size_text(s),
                                               "" if image_ops.source_available(s) else " (file missing)"))
            name.setWordWrap(True)
            hl.addWidget(name, 1)
            eye = icon_button("show" if s.visible else "hide",
                              "Hide" if s.visible else "Show",
                              px=28, font_px=14)
            ghost = icon_button("ghost", "Ghost (show as a faint outline)",
                                px=28, font_px=14, checked_name="ghost_on")
            ghost.setCheckable(True)
            ghost.setChecked(s.ghost)
            dele = icon_button("delete", "Delete", px=28, font_px=14)
            hl.addWidget(eye)
            hl.addWidget(ghost)
            hl.addWidget(dele)
            eye.clicked.connect(lambda _=False, i=svg_id: self._toggle_hide(i))
            ghost.clicked.connect(lambda _=False, i=svg_id: self._toggle_ghost(i))
            dele.clicked.connect(lambda _=False, i=svg_id: self._delete(i))
            self.list.setItemWidget(it, row)
        fit_list_height(self.list, len(ids), self.ROW_H)

    def _row_clicked(self, item):
        svg_id = item.data(QtCore.Qt.UserRole)
        if svg_id and svg_id in self.doc.svgs:
            self.doc.selection.clear()
            self.doc.selection.svg_ids.add(svg_id)
            self.changed.emit()

    def _toggle_hide(self, svg_id):
        s = self.doc.svgs.get(svg_id)
        if s:
            s.visible = not s.visible
            self.changed.emit()

    def _toggle_ghost(self, svg_id):
        s = self.doc.svgs.get(svg_id)
        if s:
            s.ghost = not s.ghost
            self.changed.emit()

    def _delete(self, svg_id):
        self.doc.delete_svg(svg_id)
        self.changed.emit()


class _GroupDelegate(QtWidgets.QStyledItemDelegate):
    """Cut Order row: color square, tool icon, text, and a pause icon at the end when the
    machine stops after the group. Rows holding selected objects get bold text."""
    ROW_H = 26
    SW = 14       # color square
    ICON = 16     # tool / pause icons
    COLOR, TOOL, PAUSE, HAS_SEL = (QtCore.Qt.UserRole + i for i in range(4))

    def sizeHint(self, opt, idx):
        return QtCore.QSize(super().sizeHint(opt, idx).width(), self.ROW_H)

    def paint(self, p, opt, idx):
        o = QtWidgets.QStyleOptionViewItem(opt)
        self.initStyleOption(o, idx)
        text = o.text
        o.text = ""
        selected = bool(opt.state & QtWidgets.QStyle.State_Selected)
        o.state &= ~(QtWidgets.QStyle.State_Selected | QtWidgets.QStyle.State_HasFocus)
        style = o.widget.style() if o.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, o, p, o.widget)   # hover
        r = opt.rect
        cy = r.center().y()
        p.save()
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        if selected:      # the style's own highlight is too faint to show the highlighted group
            hl = QtGui.QColor(icons.ACCENT)
            hl.setAlpha(140)
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(hl)
            p.drawRoundedRect(QtCore.QRectF(r).adjusted(1, 1, -1, -1), 4, 4)
        x = r.left() + 6
        p.drawPixmap(x, cy - self.SW // 2 + 1, swatch_pixmap(idx.data(self.COLOR), self.SW))
        x += self.SW + 6
        tool = idx.data(self.TOOL)
        if tool:
            p.drawPixmap(x, cy - self.ICON // 2 + 1, icon_pixmap(tool, self.ICON))
        x += self.ICON + 6
        right = r.right() - 4
        if idx.data(self.PAUSE):
            right -= self.ICON
            p.drawPixmap(right, cy - self.ICON // 2 + 1, icon_pixmap("pause", self.ICON))
            right -= 4
        f = QtGui.QFont(o.font)
        f.setBold(bool(idx.data(self.HAS_SEL)))
        p.setFont(f)
        p.setPen(o.palette.color(QtGui.QPalette.Text))
        tr = QtCore.QRect(x, r.top(), max(0, right - x), r.height())
        p.drawText(tr, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                   QtGui.QFontMetrics(f).elidedText(text, QtCore.Qt.ElideRight, tr.width()))
        p.restore()


class CutOrderPanel(QtWidgets.QWidget):
    changed = QtCore.Signal()
    active_key_changed = QtCore.Signal(str)

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.active_key = ""
        lay = QtWidgets.QVBoxLayout(self)
        self.list = QtWidgets.QListWidget()
        self.list.setToolTip("Color groups are cut from top to bottom. Drag a group to reorder it.\n"
                             "Click a group to edit its settings.")
        self.list.setItemDelegate(_GroupDelegate(self.list))
        self.list.setDragDropMode(QtWidgets.QListWidget.InternalMove)
        self.list.model().rowsMoved.connect(self._moved)
        self.list.itemClicked.connect(self._clicked)
        lay.addWidget(self.list)
        row = QtWidgets.QHBoxLayout()
        self.add_btn = icon_button("plus", "New color group", px=PANEL_BTN, scale=0.55)
        self.add_btn.clicked.connect(self.add_group)
        self.remove_btn = icon_button("minus", "Remove the highlighted color group (empty groups only)",
                                      px=PANEL_BTN, scale=0.55)
        self.remove_btn.clicked.connect(self.remove_group)
        self.assign_btn = icon_button("assign", "Move the selection into the highlighted color group",
                                      px=PANEL_BTN, scale=0.55)
        self.assign_btn.clicked.connect(self._assign)
        row.addWidget(self.add_btn)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        row.addWidget(self.assign_btn)
        lay.addLayout(row)

    def _sel_colors(self):
        return {self.doc.paths[pid].color for pid in selected_path_ids(self.doc)}

    def set_active_key(self, color):
        self.active_key = color or ""
        self.active_key_changed.emit(self.active_key)

    def refresh(self):
        d = self.doc
        groups = d.group_colors()
        if self.active_key and self.active_key not in groups:
            self.active_key = ""        # recolored, removed or undone away
        sel_colors = self._sel_colors()
        counts = Counter(p.color for p in d.paths.values())
        G = _GroupDelegate
        self.list.blockSignals(True)
        self.list.clear()
        for c in groups:
            cp = d.color_params.get(c)
            tool = cp.tool if cp is not None else "cut"
            pause = bool(d.pause_after.get(c))
            it = QtWidgets.QListWidgetItem("%s   %s" % (c, plural(counts[c], "path") if counts[c] else "empty"))
            it.setData(G.COLOR, c)
            it.setData(G.TOOL, tool)
            it.setData(G.PAUSE, pause)
            it.setData(G.HAS_SEL, c in sel_colors)
            it.setToolTip("%s%s%s" % (TOOL_LABELS.get(tool, tool),
                                      " \u00b7 pauses afterwards" if pause else "",
                                      " \u00b7 holds selected objects" if c in sel_colors else ""))
            self.list.addItem(it)
            if c == self.active_key:
                it.setSelected(True)      # only takes once the item is in the list
        self.list.blockSignals(False)
        has_sel = not d.selection.is_empty()
        self.assign_btn.setEnabled(has_sel and bool(self.active_key))
        self.remove_btn.setEnabled(bool(self.active_key) and not counts[self.active_key])
        fit_list_height(self.list, self.list.count(), _GroupDelegate.ROW_H)

    def add_group(self):
        """A new empty group, highlighted so its settings show and the selection can be moved in."""
        c = self.doc.add_group()
        self.set_active_key(c)
        self.changed.emit()
        return c

    def remove_group(self):
        if self.active_key and self.doc.remove_group(self.active_key):
            self.set_active_key("")
            self.changed.emit()

    def _moved(self, *a):
        colors = [self.list.item(i).data(_GroupDelegate.COLOR) for i in range(self.list.count())]
        tail = [c for c in self.doc.color_order if c not in colors]
        self.doc.color_order = colors + tail
        self.changed.emit()

    def _clicked(self, item):
        self.set_active_key(item.data(_GroupDelegate.COLOR))
        self.refresh()

    def _assign(self):
        if not self.active_key or self.doc.selection.is_empty():
            return
        for pid in selected_path_ids(self.doc):
            self.doc.set_path_color(pid, self.active_key)
        self.changed.emit()


class ColorKeyPanel(QtWidgets.QWidget):
    """Color Group Properties: the group's color, tool, speed, pressure and pause."""
    changed = QtCore.Signal()
    recolored = QtCore.Signal(str)     # the group now has this color

    def __init__(self, doc, get_color, parent=None):
        super().__init__(parent)
        self.doc = doc
        self._get_color = get_color
        lay = QtWidgets.QVBoxLayout(self)
        self.title = QtWidgets.QLabel("")
        lay.addWidget(self.title)
        self.body = QtWidgets.QWidget()
        lay.addWidget(self.body)
        lay = QtWidgets.QVBoxLayout(self.body)
        lay.setContentsMargins(0, 0, 0, 0)
        form = _form()
        self.picker = ColorPicker(lambda: group_choices(self.doc), px=28, title="Color Group Color",
                                  tooltip="Color of every path in this group")
        self.picker.picked.connect(self.set_group_color)
        form.addRow("Color", self.picker)
        self.tool = QtWidgets.QComboBox()
        for t in GROUP_TOOLS:
            self.tool.addItem(QtGui.QIcon(icon_pixmap(t, 16)), TOOL_LABELS[t], t)
        self.tool.setToolTip("Tool used for this color group")
        self.tool.currentIndexChanged.connect(self._tool)
        form.addRow("Tool", self.tool)
        self.speed = QtWidgets.QComboBox()
        self.speed.addItem("Machine setting", None)
        for i in range(10):
            self.speed.addItem(str(i), i)
        self.speed.currentIndexChanged.connect(self._speed)
        form.addRow("Speed", self.speed)
        self.pres = QtWidgets.QComboBox()
        self.pres.addItem("Machine setting", None)
        for i in range(-9, 10):
            self.pres.addItem(str(i), i)
        self.pres.currentIndexChanged.connect(self._pres)
        form.addRow("Pressure", self.pres)
        lay.addLayout(form)
        self.map_check = QtWidgets.QCheckBox("Vary pressure with line thickness")
        self.map_check.setToolTip("Map each point's line thickness to a cutting pressure")
        self.map_check.toggled.connect(self._map)
        lay.addWidget(self.map_check)
        self.curve = CurveEditor(self._get_curve, self._set_curve)
        self.curve.changed.connect(lambda: self.changed.emit())
        self.curve.setVisible(False)
        lay.addWidget(self.curve)
        self.pause = QtWidgets.QCheckBox("Pause after this group")
        self.pause.setToolTip("The machine will stop after cutting this color, for example to change the tool")
        self.pause.toggled.connect(self._pause)
        lay.addWidget(self.pause)
        self._loading = False

    def _color(self):
        return self._get_color()

    def refresh(self):
        c = self._color()
        self._loading = True
        self.body.setVisible(bool(c))
        if not c:
            self.title.setText("No color group selected")
            self.title.setStyleSheet(MUTED)
            self._loading = False
            return
        cp = self.doc.ensure_color(c)
        n = len(self.doc.group_path_ids(c))
        self.title.setText("<b>%s</b> &nbsp;%s" % (c, plural(n, "path")))
        self.title.setStyleSheet("")
        self.picker.set_color(c)
        for combo, val in ((self.tool, cp.tool), (self.speed, cp.speed), (self.pres, cp.pressure)):
            combo.blockSignals(True)
            idx = 0
            for i in range(combo.count()):
                if combo.itemData(i) == val:
                    idx = i
                    break
            combo.setCurrentIndex(idx)
            combo.blockSignals(False)
        self.map_check.blockSignals(True)
        self.map_check.setChecked(bool(cp.use_thickness_map))
        self.map_check.blockSignals(False)
        self.curve.setVisible(bool(cp.use_thickness_map))
        self.curve.refresh()
        self.pause.blockSignals(True)
        self.pause.setChecked(bool(self.doc.pause_after.get(c)))
        self.pause.blockSignals(False)
        self._loading = False

    def _get_curve(self):
        c = self._color()
        return self.doc.ensure_color(c).curve if c else [(0.0, -9), (2.0, 9)]

    def _set_curve(self, curve):
        c = self._color()
        if c:
            self.doc.ensure_color(c).curve = [list(map(float, p)) for p in curve]

    def set_group_color(self, new):
        """Recolor every path of the shown group. Picking another group's color merges the two
        (after confirm_merge), and the group stays shown under its new color."""
        old = self._color()
        new = normalize_color(new)
        if not old or new == old:
            return False
        if new in self.doc.group_colors() and not confirm_merge(self.window()):
            return False
        self.doc.recolor_group(old, new)
        self.recolored.emit(new)
        self.changed.emit()
        return True

    def _tool(self, i):
        if self._loading or not self._color():
            return
        self.doc.ensure_color(self._color()).tool = self.tool.itemData(i)
        self.changed.emit()

    def _speed(self, i):
        if self._loading or not self._color():
            return
        self.doc.ensure_color(self._color()).speed = self.speed.itemData(i)
        self.changed.emit()

    def _pres(self, i):
        if self._loading or not self._color():
            return
        self.doc.ensure_color(self._color()).pressure = self.pres.itemData(i)
        self.changed.emit()

    def _map(self, v):
        if self._loading or not self._color():
            return
        self.doc.ensure_color(self._color()).use_thickness_map = bool(v)
        self.curve.setVisible(bool(v))
        self.changed.emit()

    def _pause(self, v):
        if self._loading or not self._color():
            return
        self.doc.pause_after[self._color()] = bool(v)
        self.changed.emit()


class LinePanel(QtWidgets.QWidget):
    changed = QtCore.Signal()

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        lay = QtWidgets.QVBoxLayout(self)
        self.title = QtWidgets.QLabel("")
        lay.addWidget(self.title)
        cform = _form()
        self.picker = ColorPicker(lambda: group_choices(self.doc), px=28, title="Line Color",
                                  tooltip="Color of the selected lines")
        self.picker.picked.connect(self.set_color)
        cform.addRow("Color", self.picker)
        lay.addLayout(cform)
        self.dash = QtWidgets.QCheckBox("Dashed line (perforation)")
        self.dash.setTristate(True)
        self.dash.toggled.connect(self._dash_toggle)
        lay.addWidget(self.dash)
        form = _form()
        self.on = MixedSpin(0.1, 2, 0.1, 1000.0)
        self.on.setToolTip("Length of each cut, in mm")
        form.addRow("Dash (mm)", self.on)
        self.off = MixedSpin(0.1, 2, 0.1, 1000.0)
        self.off.setToolTip("Length of each uncut gap, in mm")
        form.addRow("Gap (mm)", self.off)
        lay.addLayout(form)
        self.apply = icon_button("apply", "Apply dash and gap lengths", px=PANEL_BTN, scale=0.55)
        self.apply.clicked.connect(self._apply_lengths)
        for w in (self.on, self.off):
            w.submitted.connect(self._apply_lengths)
        lay.addLayout(_apply_row(self.apply))
        self._loading = False

    def _targets(self):
        # points alone do not select lines
        return selected_path_ids(self.doc, points=False)

    def set_color(self, color):
        """Move the selected lines into the color group `color` (made if new)."""
        tg = self._targets()
        if not tg:
            return
        for pid in tg:
            self.doc.set_path_color(pid, color)
        self.changed.emit()

    def refresh(self):
        d = self.doc
        tg = sorted(self._targets())
        self._loading = True
        self.title.setText(plural(len(tg), "path") + " selected")
        self.picker.set_colors(sorted({d.paths[p].color for p in tg}))
        _tri(self.dash, [d.paths[p].dash_enabled for p in tg])
        self.on.set_values([d.paths[p].dash_on_mm for p in tg])
        self.off.set_values([d.paths[p].dash_off_mm for p in tg])
        self._loading = False

    def _dash_toggle(self, v):
        if self._loading:
            return
        # the tristate toggled signal only fires on a user click, and Qt resolves a
        # click on PartiallyChecked to Checked: treat it as set-all True/False by state
        state = self.dash.checkState()
        val = state != QtCore.Qt.Unchecked
        for pid in self._targets():
            self.doc.paths[pid].dash_enabled = val
        self.changed.emit()

    def _apply_lengths(self):
        on, off = self.on.parsed(), self.off.parsed()
        if on is None and off is None:
            return
        for pid in self._targets():
            p = self.doc.paths[pid]
            if on is not None:
                p.dash_on_mm = max(0.1, on)
            if off is not None:
                p.dash_off_mm = max(0.1, off)
        self.changed.emit()


class PointPanel(QtWidgets.QWidget):
    changed = QtCore.Signal()

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        lay = QtWidgets.QVBoxLayout(self)
        self.title = QtWidgets.QLabel("")
        lay.addWidget(self.title)
        form = _form()
        self.thick = MixedSpin(0.1, 2, 0.0, MAX_THICKNESS_MM)
        form.addRow("Thickness (mm)", self.thick)
        self.radius = MixedSpin(0.5, 2, 0.0, MAX_RADIUS_MM)
        self.radius.setToolTip("Rounds the corner at the point with an arc of this radius when it is\n"
                               "drawn and cut. The point itself does not move. 0 = sharp corner.\n"
                               "A radius that does not fit the corner is reduced. The ends of an open\n"
                               "path are never rounded.")
        form.addRow("Corner radius (mm)", self.radius)
        lay.addLayout(form)
        self.apply = icon_button("apply", "Apply", px=PANEL_BTN, scale=0.55)
        self.apply.clicked.connect(self._apply)
        lay.addLayout(_apply_row(self.apply))
        for w in (self.thick, self.radius):
            w.submitted.connect(self._apply)

    def refresh(self):
        refs = self.doc.selected_point_refs()
        self.title.setText(plural(len(refs), "point") + " selected")
        pts = [self.doc.paths[pid].points[i] for (pid, i) in refs
               if pid in self.doc.paths and 0 <= i < len(self.doc.paths[pid].points)]
        self.thick.set_values([pt.thickness for pt in pts])
        self.radius.set_values([pt.radius for pt in pts])

    def _apply(self):
        """Apply the fields that hold a value (a blank / mixed field leaves the points alone)."""
        thick, radius = self.thick.parsed(), self.radius.parsed()
        if thick is None and radius is None:
            return
        for (pid, i) in self.doc.selected_point_refs():
            p = self.doc.paths.get(pid)
            if p is not None and 0 <= i < len(p.points):
                if thick is not None:
                    p.points[i].thickness = thick
                if radius is not None:
                    p.points[i].radius = max(0.0, radius)
        self.changed.emit()


class SvgPanel(QtWidgets.QWidget):
    """Properties of the selected objects: X/Y/Scale/Rotation for every kind, plus (for image / pdf-only
    selections) the raster section: dpi, page, size, outline options. Section edits apply
    immediately to all selected image/pdf objects and regenerate their outlines."""
    changed = QtCore.Signal()
    message = QtCore.Signal(str)   # status-bar text (outline / render problems)

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        self._pdf_pages = {}   # source path -> page count
        lay = QtWidgets.QVBoxLayout(self)
        self.title = QtWidgets.QLabel("")
        self.title.setWordWrap(True)
        lay.addWidget(self.title)
        form = _form()
        for key, name, spin in (("x", "X (mm)", MixedSpin(1.0, 2, -10000.0, 10000.0)),
                                ("y", "Y (mm)", MixedSpin(1.0, 2, -10000.0, 10000.0)),
                                ("scale", "Scale", MixedSpin(0.01, 3, 0.01, 100.0)),
                                ("rotation", "Rotation (°)", MixedSpin(15.0, 2, -360.0, 360.0))):
            form.addRow(name, spin)
            spin.submitted.connect(self._apply)
            setattr(self, "edit_" + key, spin)
        lay.addLayout(form)
        self.edit_rotation.setToolTip("Clockwise, about the object's centre")
        self.apply = icon_button("apply", "Apply position, scale and rotation", px=PANEL_BTN, scale=0.55)
        self.apply.clicked.connect(self._apply)
        lay.addLayout(_apply_row(self.apply))
        self._build_image_section(lay)

    # -- card title --
    def selected_svgs(self):
        return [self.doc.svgs[i] for i in sorted(self.doc.svg_ids_for_selection()) if i in self.doc.svgs]

    def card_title(self):
        return image_ops.title_for(self.selected_svgs())

    def refresh(self):
        svgs = self.selected_svgs()
        self.title.setText(", ".join(s.label for s in svgs) if svgs else "Nothing selected")
        self.edit_x.set_values([s.offset_x_mm for s in svgs])
        self.edit_y.set_values([s.offset_y_mm for s in svgs])
        self.edit_scale.set_values([s.scale for s in svgs])
        self.edit_rotation.set_values([s.rotation for s in svgs])
        self._refresh_image_section()

    def _apply(self):
        """Apply the fields that were changed. The rotation turns each object about its own
        centre, and a position typed with it then places the turned object."""
        def value(spin):
            return spin.parsed() if spin.edited() else None
        x, y, s, r = (value(self.edit_x), value(self.edit_y), value(self.edit_scale),
                      value(self.edit_rotation))
        for svg_id in self.doc.svg_ids_for_selection():
            svg = self.doc.svgs.get(svg_id)
            if svg is None:
                continue
            if r is not None:
                self.doc.set_svg_rotation(svg_id, r)
            if x is not None:
                svg.offset_x_mm = x
            if y is not None:
                svg.offset_y_mm = y
            if s is not None:
                svg.scale = max(0.01, min(100.0, s))
        self.changed.emit()

    # -- image / pdf section --
    @staticmethod
    def _swatch_style(btn, color):
        c = QtGui.QColor(color)
        fg = "#000000" if c.lightness() > 128 else "#ffffff"
        btn.setStyleSheet("background-color: %s; color: %s;" % (c.name(), fg))
        btn.setText(c.name())

    def _build_image_section(self, lay):
        self.img_box = QtWidgets.QWidget()
        self.img_box.setVisible(False)
        form = QtWidgets.QFormLayout(self.img_box)
        form.setContentsMargins(0, 6, 0, 0)
        self.img_size = QtWidgets.QLabel("")
        form.addRow("Size", self.img_size)
        self.dpi_label = QtWidgets.QLabel("DPI")
        self.spin_dpi = QtWidgets.QDoubleSpinBox()
        self.spin_dpi.setRange(30, 1200)
        self.spin_dpi.setDecimals(1)
        self.spin_dpi.setSingleStep(10)
        self.spin_dpi.setToolTip("Image: pixels per inch, which sets its size on the mat.\n"
                                 "PDF: render resolution (the page keeps its size).")
        self.spin_dpi.editingFinished.connect(self._on_dpi)
        form.addRow(self.dpi_label, self.spin_dpi)
        self.page_label = QtWidgets.QLabel("Page")
        page_row = QtWidgets.QHBoxLayout()
        page_row.setContentsMargins(0, 0, 0, 0)
        self.spin_page = QtWidgets.QSpinBox()
        self.spin_page.setRange(1, 1)
        self.spin_page.editingFinished.connect(self._on_page)
        self.page_of = QtWidgets.QLabel("")
        page_row.addWidget(self.spin_page)
        page_row.addWidget(self.page_of)
        page_row.addStretch(1)
        self.page_row_w = QtWidgets.QWidget()
        self.page_row_w.setLayout(page_row)
        form.addRow(self.page_label, self.page_row_w)

        self.chk_outline = QtWidgets.QCheckBox("Cut outline")
        self.chk_outline.setToolTip("Trace the shape and add a closed cut path around it")
        self.chk_outline.clicked.connect(self._on_outline)
        form.addRow(self.chk_outline)
        self.spin_margin = QtWidgets.QDoubleSpinBox()
        self.spin_margin.setRange(-50, 50)
        self.spin_margin.setDecimals(2)
        self.spin_margin.setSingleStep(0.1)
        self.spin_margin.setSuffix(" mm")
        self.spin_margin.setToolTip("Positive values grow the outline, negative values shrink it")
        self.spin_margin.editingFinished.connect(self._on_margin)
        form.addRow("Margin", self.spin_margin)
        key_row = QtWidgets.QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        self.combo_key = QtWidgets.QComboBox()
        for lbl in image_ops.KEY_MODE_LABELS:
            self.combo_key.addItem(lbl)
        self.combo_key.activated.connect(self._on_key_mode)
        self.btn_key = QtWidgets.QPushButton()
        self.btn_key.setToolTip("Background color to ignore (Custom mode)")
        self.btn_key.clicked.connect(self.pick_key_color)
        key_row.addWidget(self.combo_key)
        key_row.addWidget(self.btn_key)
        key_w = QtWidgets.QWidget()
        key_w.setLayout(key_row)
        form.addRow("Background", key_w)
        self.spin_tol = QtWidgets.QSpinBox()
        self.spin_tol.setRange(0, 255)
        self.spin_tol.setToolTip("How far a color may differ from the background color and still count as background")
        self.spin_tol.editingFinished.connect(self._on_tol)
        form.addRow("Tolerance", self.spin_tol)
        self.chk_holes = QtWidgets.QCheckBox("Include holes")
        self.chk_holes.setToolTip("Also cut around holes inside the shape")
        self.chk_holes.clicked.connect(self._on_holes)
        form.addRow(self.chk_holes)
        self.btn_ocolor = ColorPicker(lambda: group_choices(self.doc), px=28, title="Outline Color",
                                      tooltip="Color of the outline path (sets its color group)")
        self.btn_ocolor.picked.connect(self.set_outline_color)
        form.addRow("Outline color", self.btn_ocolor)
        lay.addWidget(self.img_box)

    def _raster_svgs(self):
        """The selected objects when the whole selection is image/pdf, else []."""
        svgs = self.selected_svgs()
        return svgs if svgs and all(image_ops.is_raster(s) for s in svgs) else []

    def _page_count(self, svg):
        if svg.source_path not in self._pdf_pages:
            try:
                from core import raster
                self._pdf_pages[svg.source_path] = max(1, raster.pdf_page_count(svg.source_path))
            except Exception:
                self._pdf_pages[svg.source_path] = max(1, svg.pdf_page + 1)
        return self._pdf_pages[svg.source_path]

    def _refresh_image_section(self):
        rs = self._raster_svgs()
        self.img_box.setVisible(bool(rs))
        if not rs:
            return
        all_pdf = all(s.kind == "pdf" for s in rs)
        quiet = (self.spin_dpi, self.spin_page, self.chk_outline, self.spin_margin, self.combo_key,
                 self.spin_tol, self.chk_holes)
        for w in quiet:
            w.blockSignals(True)
        self.img_size.setText(image_ops.size_text(rs[0]) if len(rs) == 1 else plural(len(rs), "object"))
        self.dpi_label.setText("Render DPI" if all_pdf else "DPI")
        self.spin_dpi.setValue(rs[0].dpi)
        self.page_label.setVisible(all_pdf)
        self.page_row_w.setVisible(all_pdf)
        if all_pdf:
            self.spin_page.setRange(1, max(self._page_count(s) for s in rs))
            self.spin_page.setValue(rs[0].pdf_page + 1)
            self.page_of.setText("of %d" % self._page_count(rs[0]))
        _tri(self.chk_outline, [s.outline_enabled for s in rs])
        self.spin_margin.setValue(rs[0].outline_margin_mm)
        mode = rs[0].color_key_mode
        self.combo_key.setCurrentIndex(image_ops.KEY_MODES.index(mode) if mode in image_ops.KEY_MODES else 0)
        self.spin_tol.setValue(int(rs[0].color_key_tol))
        _tri(self.chk_holes, [s.outline_holes for s in rs])
        for w in quiet:
            w.blockSignals(False)
        self._swatch_style(self.btn_key, rs[0].color_key)
        self.btn_key.setEnabled(any(s.color_key_mode == "custom" for s in rs))
        self.btn_ocolor.set_colors([s.outline_color for s in rs])

    def _edit_rasters(self, change, regen=True):
        """Run change(svg) on every selected image/pdf object (it returns True when it changed
        something), then regenerate those outlines and emit `changed`."""
        errors, touched = [], []
        for s in self._raster_svgs():
            try:
                if change(s):
                    touched.append(s)
            except Exception as e:
                errors.append("%s: %s" % (s.label, e))
        if regen:
            for s in touched:
                err = image_ops.regenerate_outline(self.doc, s.id)
                if err:
                    errors.append(err)
        if errors:
            self.message.emit(". ".join(errors))
        if touched or errors:
            self.changed.emit()

    def _on_dpi(self):
        v = self.spin_dpi.value()

        def f(s):
            if abs(s.dpi - v) < 1e-6:
                return False
            image_ops.apply_dpi(s, v)
            return True
        self._edit_rasters(f)

    def _on_page(self):
        v = self.spin_page.value() - 1

        def f(s):
            if s.kind != "pdf" or s.pdf_page == v:
                return False
            image_ops.apply_pdf_page(s, min(v, self._page_count(s) - 1))
            return True
        self._edit_rasters(f)

    def _on_outline(self, checked):
        def f(s):
            if s.outline_enabled == bool(checked):
                return False
            s.outline_enabled = bool(checked)
            return True
        self._edit_rasters(f)

    def _on_margin(self):
        v = self.spin_margin.value()

        def f(s):
            if abs(s.outline_margin_mm - v) < 1e-9:
                return False
            s.outline_margin_mm = v
            return True
        self._edit_rasters(f)

    def _on_key_mode(self, idx):
        mode = image_ops.KEY_MODES[max(0, min(idx, 2))]

        def f(s):
            if s.color_key_mode == mode:
                return False
            s.color_key_mode = mode
            return True
        self._edit_rasters(f)

    def _on_tol(self):
        v = self.spin_tol.value()

        def f(s):
            if s.color_key_tol == v:
                return False
            s.color_key_tol = v
            return True
        self._edit_rasters(f)

    def _on_holes(self, checked):
        def f(s):
            if s.outline_holes == bool(checked):
                return False
            s.outline_holes = bool(checked)
            return True
        self._edit_rasters(f)

    def set_key_color(self, color):
        c = normalize_color(color)

        def f(s):
            if s.color_key == c:
                return False
            s.color_key = c
            return True
        self._edit_rasters(f)

    def set_outline_color(self, color):
        c = normalize_color(color)

        def f(s):
            if s.outline_color == c:
                return False
            s.outline_color = c
            return True
        self._edit_rasters(f)

    def pick_key_color(self):
        rs = self._raster_svgs()
        if not rs:
            return
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(rs[0].color_key), self, "Background Color")
        if c.isValid():
            self.set_key_color(c.name())

    def pick_outline_color(self):
        if self._raster_svgs():
            self.btn_ocolor.open_dialog()


class DeviceControlPanel(QtWidgets.QWidget):
    """Device actions as a button grid (same icon flow as the Tools dock)."""
    changed = QtCore.Signal()
    panel_toggled = QtCore.Signal(str, bool)   # action, shown (cut / jog / config panels)
    action_requested = QtCore.Signal(str)

    PANEL_ACTIONS = ("cut", "jog", "config", "mat", "scan")
    TOGGLE_ACTIONS = ("mount_usb",)            # checked = on (the main window keeps the state)
    MOUNT_TIP = "Mount the machine's USB drive in Windows"
    ACTIONS = [
        ("feed", "Load / unload mat"),
        ("cut", "Cut"),
        ("scan", "Scan"),
        ("jog", "Manual jog"),
        ("config", "Cut settings"),
        ("mat", "Mat settings"),
        ("mount_usb", MOUNT_TIP),
        ("abort", "Stop"),
    ]

    def __init__(self, main_win=None, parent=None, px=36):
        super().__init__(parent)
        self.main_win = main_win
        self.flow = FlowLayout(spacing=4)
        self.setLayout(self.flow)

        self.buttons = {}
        for act, tip in self.ACTIONS:
            btn = icon_button(act, tip, px=px, scale=0.55,
                              checkable=(act in self.PANEL_ACTIONS or act in self.TOGGLE_ACTIONS),
                              danger=(act == "abort"))
            btn.clicked.connect(lambda checked=False, a=act: self._on_btn_clicked(a))
            self.flow.addWidget(btn)
            self.buttons[act] = btn

    def _on_btn_clicked(self, act):
        if act in self.PANEL_ACTIONS:
            self.panel_toggled.emit(act, self.buttons[act].isChecked())
        else:
            self.action_requested.emit(act)

    def set_panel_active(self, act, active):
        """Reflect a panel's visibility on its (checkable) button."""
        btn = self.buttons.get(act)
        if btn and btn.isChecked() != active:
            btn.blockSignals(True)
            btn.setChecked(active)
            btn.blockSignals(False)

    def set_drive_state(self, state, letter=None):
        """Mount button: checked while mounting, mounted or unmounting, with a tooltip that says
        which."""
        tips = {"mounting": "Mounting the machine's USB drive…",
                "mounted": "USB drive mounted as %s: (click to unmount)" % letter,
                "unmounting": "Unmounting the USB drive…"}
        btn = self.buttons["mount_usb"]
        btn.setToolTip(tips.get(state, self.MOUNT_TIP))
        self.set_panel_active("mount_usb", state != "idle")

    def refresh(self):
        pass


def mm_spin(lo, hi, step, decimals, val, tip):
    w = QtWidgets.QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setSingleStep(step)
    w.setDecimals(decimals)
    w.setSuffix(" mm")
    w.setValue(val)
    w.setToolTip(tip)
    return w


class CutSettingsPanel(QtWidgets.QWidget):
    """Cut settings, opened from Device Control's Machine Configuration
    button: swivel-blade compensation (doc.blade) and which colors use the
    swivel blade (ColorParams.blade). Edits go straight into the document, and
    the Cut panel generates from them."""
    changed = QtCore.Signal()

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.doc = doc
        self._loading = False
        self._color_keys = None
        lay = QtWidgets.QVBoxLayout(self)

        lay.addWidget(QtWidgets.QLabel("<b>Blade compensation</b>"))
        form = QtWidgets.QFormLayout()
        self.mode = QtWidgets.QComboBox()
        for label, key in (("Off", "off"),
                           ("Machine setting (0.50 mm)", "firmware"),
                           ("Custom", "custom")):
            self.mode.addItem(label, key)
        self.mode.setToolTip("Corrects for the swivel blade's tip trailing behind the holder.\n"
                             "Off: cut the path exactly as drawn.\n"
                             "Machine setting: what the machine does for its own 0.50 mm blades.\n"
                             "Custom: your own blade offset and corner tolerances.")
        form.addRow("Mode", self.mode)
        self.offset = mm_spin(0.0, 10.0, 0.05, 2, 0.5,
                              "Distance from the holder's center to the blade tip")
        form.addRow("Blade offset", self.offset)
        self.corner_tol = mm_spin(0.005, 2.0, 0.01, 3, 0.1,
                                  "Corners that would round off by more than this are pivoted.\n"
                                  "Smaller values give sharper corners but more pivots.")
        form.addRow("Corner tolerance", self.corner_tol)
        self.arc_tol = mm_spin(0.001, 0.5, 0.001, 3, 0.005,
                               "How closely pivots follow a true arc. Smaller values use more points.")
        form.addRow("Pivot tolerance", self.arc_tol)
        self.leadin = QtWidgets.QCheckBox("Lead-in cut")
        self.leadin.setToolTip("A short cut in the mat margin before each blade run,\n"
                               "so the blade is already pointing the correct way when the cut starts.")
        form.addRow(self.leadin)
        self.leadin_x = mm_spin(-50.0, 400.0, 1.0, 2, 1.0, "Where the lead-in starts across the mat")
        form.addRow("Lead-in X", self.leadin_x)
        self.leadin_y = mm_spin(-60.0, 400.0, 1.0, 2, -10.47,
                                "Where the lead-in ends (it runs upward from here into the margin)")
        form.addRow("Lead-in end Y", self.leadin_y)
        self.leadin_pres = QtWidgets.QSpinBox()
        self.leadin_pres.setRange(-10, 9)
        self.leadin_pres.setSpecialValueText("Same as cut")
        form.addRow("Lead-in pressure", self.leadin_pres)
        lay.addLayout(form)

        lay.addWidget(QtWidgets.QLabel("<b>Color groups cut with the blade</b>"))
        self.colors = QtWidgets.QListWidget()
        self.colors.setToolTip("Checked groups are cut with the swivel blade and compensated.\n"
                               "Uncheck groups drawn with a pen or another tool.")
        lay.addWidget(self.colors)

        self.mode.currentIndexChanged.connect(self._apply)
        for w in (self.offset, self.corner_tol, self.arc_tol, self.leadin_x, self.leadin_y):
            w.valueChanged.connect(self._apply)
        self.leadin_pres.valueChanged.connect(self._apply)
        self.leadin.toggled.connect(self._apply)
        self.colors.itemChanged.connect(self._apply)
        self.refresh()

    def refresh(self):
        d = self.doc
        bs = d.blade
        self._loading = True
        self.mode.setCurrentIndex(max(0, self.mode.findData(bs.mode)))
        self.offset.setValue(bs.offset_mm)
        self.corner_tol.setValue(bs.corner_tolerance_mm)
        self.arc_tol.setValue(bs.arc_tolerance_mm)
        self.leadin.setChecked(bool(bs.leadin))
        self.leadin_x.setValue(bs.leadin_x_mm)
        self.leadin_y.setValue(bs.leadin_end_y_mm)
        self.leadin_pres.setValue(-10 if bs.leadin_pressure is None else int(bs.leadin_pressure))
        keys = list(d.ordered_colors())
        if keys != self._color_keys:  # rebuild only when the color set changes
            self._color_keys = keys
            self.colors.clear()
            for c in keys:
                it = QtWidgets.QListWidgetItem(QtGui.QIcon(swatch_pixmap(c)), c)
                it.setData(QtCore.Qt.UserRole, c)
                it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
                self.colors.addItem(it)
            fit_list_height(self.colors, len(keys))
        for i in range(self.colors.count()):
            it = self.colors.item(i)
            cp = d.color_params.get(it.data(QtCore.Qt.UserRole))
            it.setCheckState(QtCore.Qt.Checked if (cp is None or cp.blade) else QtCore.Qt.Unchecked)
        self._sync_enabled()
        self._loading = False

    def _sync_enabled(self):
        mode = self.mode.currentData()
        custom, on = mode == "custom", mode != "off"
        for w in (self.offset, self.corner_tol, self.arc_tol):
            w.setEnabled(custom)
        self.leadin.setEnabled(on)
        for w in (self.leadin_x, self.leadin_y, self.leadin_pres):
            w.setEnabled(on and self.leadin.isChecked())

    def _apply(self, *a):
        if self._loading:
            return
        b = self.doc.blade
        b.mode = self.mode.currentData()
        b.offset_mm = self.offset.value()
        b.corner_tolerance_mm = self.corner_tol.value()
        b.arc_tolerance_mm = self.arc_tol.value()
        b.leadin = self.leadin.isChecked()
        b.leadin_x_mm = self.leadin_x.value()
        b.leadin_end_y_mm = self.leadin_y.value()
        lp = self.leadin_pres.value()
        b.leadin_pressure = None if lp == -10 else lp
        for i in range(self.colors.count()):
            it = self.colors.item(i)
            self.doc.ensure_color(it.data(QtCore.Qt.UserRole)).blade = \
                it.checkState() == QtCore.Qt.Checked
        self._sync_enabled()
        self.changed.emit()


class JogPanel(QtWidgets.QWidget):
    jog_requested = QtCore.Signal(float, float)  # dx, dy in mm, (0, 0) = home

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)

        form = _form()
        self.step_combo = QtWidgets.QComboBox()
        for v in (0.1, 1.0, 5.0, 10.0, 25.0):
            self.step_combo.addItem(f"{v:g} mm", v)
        self.step_combo.setCurrentIndex(2)
        form.addRow("Step", self.step_combo)
        lay.addLayout(form)

        grid = QtWidgets.QGridLayout()
        grid.setSpacing(4)

        btn_up = QtWidgets.QPushButton("▲  Y+")
        btn_down = QtWidgets.QPushButton("▼  Y−")
        btn_left = QtWidgets.QPushButton("◀  X−")
        btn_right = QtWidgets.QPushButton("X+  ▶")
        btn_home = QtWidgets.QPushButton("Home")
        btn_home.setToolTip("Move the head to the cut origin (0, 0)")

        btn_up.clicked.connect(lambda: self._jog(0, 1))
        btn_down.clicked.connect(lambda: self._jog(0, -1))
        btn_left.clicked.connect(lambda: self._jog(-1, 0))
        btn_right.clicked.connect(lambda: self._jog(1, 0))
        btn_home.clicked.connect(lambda: self._jog(0, 0, home=True))

        grid.addWidget(btn_up, 0, 1)
        grid.addWidget(btn_left, 1, 0)
        grid.addWidget(btn_home, 1, 1)
        grid.addWidget(btn_right, 1, 2)
        grid.addWidget(btn_down, 2, 1)
        lay.addLayout(grid)

    def _jog(self, sx, sy, home=False):
        if home:
            self.jog_requested.emit(0.0, 0.0)
            return
        step = float(self.step_combo.currentData())
        self.jog_requested.emit(sx * step, sy * step)
