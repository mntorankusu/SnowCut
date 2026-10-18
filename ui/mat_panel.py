"""Mat settings panel: pick a mat profile (Standard 12x12 / 12x24 presets or a saved custom one),
edit its grid units / grid size / margins / machine mat type, and save it as a custom profile.

Edits apply to the canvas live (profile_changed). The built-in presets are read-only: saving an
edited preset asks for a new name. Custom profiles are JSON files in Documents/SnowCut/Mats
(core.mat.MatStore).

Machine mat type: the firmware mat kind the app's Feed (and a scan's automatic load) tells the
machine is loaded. A 24" type also makes scans cover the whole 12x24. "Send to machine"
(spoof_send_requested) sets it on the machine right away, for a mat that is already loaded.

Mat skew (skew_changed): the document's rotation / offset correction for a mat that went in
crooked (core.mat.MatSkew). Not part of the profile.
"""
from dataclasses import replace

from PySide6 import QtWidgets, QtCore
from core import machine
from core import mat as matlib
from .panels import mm_spin

class MatPanel(QtWidgets.QWidget):
    profile_changed = QtCore.Signal(object, bool)   # MatProfile, fit (another profile was picked)
    active_changed = QtCore.Signal(str)             # name of the saved profile now selected
    spoof_send_requested = QtCore.Signal(int)       # mat kind to set on the machine now
    skew_changed = QtCore.Signal(object)            # MatSkew

    def __init__(self, store=None, active=None, parent=None):
        super().__init__(parent)
        self.store = store or matlib.MatStore()
        self._loading = False
        self.saved = self.store.get(active or "") or matlib.preset(matlib.DEFAULT_NAME)
        self.profile = replace(self.saved)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel("<b>Mat profile</b>"))
        self.combo = QtWidgets.QComboBox()
        self.combo.setToolTip("Built-in presets and your saved custom mats.\n"
                              "Custom mats are stored in %s" % self.store.folder)
        lay.addWidget(self.combo)
        row = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("Save")
        self.save_btn.setToolTip("Save the changes to this custom mat")
        self.save_as_btn = QtWidgets.QPushButton("Save As\u2026")
        self.save_as_btn.setToolTip("Save these settings as a new custom mat profile")
        self.revert_btn = QtWidgets.QPushButton("Revert")
        self.revert_btn.setToolTip("Discard unsaved changes")
        self.delete_btn = QtWidgets.QPushButton("Delete")
        self.delete_btn.setToolTip("Delete this custom mat profile")
        for b in (self.save_btn, self.save_as_btn, self.revert_btn, self.delete_btn):
            row.addWidget(b)
        lay.addLayout(row)

        lay.addWidget(QtWidgets.QLabel("<b>Grid</b>"))
        form = QtWidgets.QFormLayout()
        self.units = QtWidgets.QComboBox()
        for key in matlib.UNITS:
            self.units.addItem(matlib.UNIT_LABELS[key], key)
        self.units.setToolTip("Grid cell size: 1 inch or 1 cm. Switching keeps the grid's overall size.")
        form.addRow("Units", self.units)
        self.grid_w = self._grid_spin("Grid width, across the mat")
        form.addRow("Width", self.grid_w)
        self.grid_h = self._grid_spin("Grid height, along the feed direction")
        form.addRow("Height", self.grid_h)
        lay.addLayout(form)

        lay.addWidget(QtWidgets.QLabel("<b>Margins</b>"))
        form = QtWidgets.QFormLayout()
        tip = "Distance from the edge of the printed grid to the edge of the mat"
        self.m_left = mm_spin(0.0, matlib.MAX_MARGIN_MM, 0.5, 1, 20.0, tip)
        self.m_right = mm_spin(0.0, matlib.MAX_MARGIN_MM, 0.5, 1, 20.0, tip)
        self.m_top = mm_spin(0.0, matlib.MAX_MARGIN_MM, 0.5, 1, 60.0, tip + (
            ".\nThe top edge feeds in first. Below %.0f mm the mat is fed in further,\n"
            "by whole grid units, to reach the minimum feed depth." % matlib.MIN_FEED_DEPTH_MM))
        self.m_bottom = mm_spin(0.0, matlib.MAX_MARGIN_MM, 0.5, 1, 60.0, tip)
        form.addRow("Left", self.m_left)
        form.addRow("Right", self.m_right)
        form.addRow("Top", self.m_top)
        form.addRow("Bottom", self.m_bottom)
        lay.addLayout(form)

        lay.addWidget(QtWidgets.QLabel("<b>Machine mat type</b>"))
        row = QtWidgets.QHBoxLayout()
        self.spoof = QtWidgets.QComboBox()
        for k, name in matlib.SPOOF_KINDS.items():
            self.spoof.addItem(name, k)
        self.spoof.setToolTip("The mat type reported to the machine when this mat is loaded.\n"
                              "A 12x24 type lets scans cover the whole 12x24 mat.")
        row.addWidget(self.spoof, 1)
        self.spoof_btn = QtWidgets.QPushButton("Send to machine")
        self.spoof_btn.setToolTip("Set this mat type on the machine now, for a mat that is already loaded")
        row.addWidget(self.spoof_btn)
        lay.addLayout(row)

        skew_title = QtWidgets.QLabel("<b>Mat alignment</b>")
        skew_title.setToolTip("Saved with the project. Line the scan up with the grid. Cuts are\n"
                              "corrected the opposite way so they land where they are drawn.")
        lay.addWidget(skew_title)
        form = QtWidgets.QFormLayout()
        self.skew_angle = QtWidgets.QDoubleSpinBox()
        self.skew_angle.setRange(-matlib.MAX_SKEW_DEG, matlib.MAX_SKEW_DEG)
        self.skew_angle.setDecimals(3)
        self.skew_angle.setSingleStep(0.05)
        self.skew_angle.setSuffix(" \u00b0")
        self.skew_angle.setToolTip("Rotation of the scan about the cut origin (positive = clockwise)")
        form.addRow("Rotation", self.skew_angle)
        self.skew_dx = mm_spin(-matlib.MAX_SKEW_MM, matlib.MAX_SKEW_MM, 0.1, 2, 0.0,
                               "Shift of the scan across the mat (X)")
        form.addRow("Offset X", self.skew_dx)
        self.skew_dy = mm_spin(-matlib.MAX_SKEW_MM, matlib.MAX_SKEW_MM, 0.1, 2, 0.0,
                               "Shift of the scan along the feed (Y)")
        form.addRow("Offset Y", self.skew_dy)
        self.skew_reset = QtWidgets.QPushButton("Reset alignment")
        form.addRow(self.skew_reset)
        lay.addLayout(form)

        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color: #9a9a9a;")
        lay.addWidget(self.info)
        self.warn = QtWidgets.QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("color: #e0a030;")
        lay.addWidget(self.warn)

        self.combo.activated.connect(self._on_pick)
        self.units.currentIndexChanged.connect(self._on_units)
        for w in (self.grid_w, self.grid_h, self.m_left, self.m_right, self.m_top, self.m_bottom):
            w.valueChanged.connect(self._apply)
        self.spoof.currentIndexChanged.connect(self._apply)
        self.spoof_btn.clicked.connect(lambda: self.spoof_send_requested.emit(self.profile.spoof_kind))
        self._skew_loading = False
        for w in (self.skew_angle, self.skew_dx, self.skew_dy):
            w.valueChanged.connect(self._apply_skew)
        self.skew_reset.clicked.connect(lambda: self.set_skew(matlib.MatSkew(), emit=True))
        self.save_btn.clicked.connect(self.save)
        self.save_as_btn.clicked.connect(lambda: self.save_as())
        self.revert_btn.clicked.connect(self.revert)
        self.delete_btn.clicked.connect(lambda: self.delete())
        self._fill_combo()
        self._load_fields()

    @staticmethod
    def _grid_spin(tip):
        w = QtWidgets.QDoubleSpinBox()
        w.setDecimals(2)
        w.setSingleStep(1.0)
        w.setToolTip(tip)
        return w

    # -- state --
    def is_modified(self):
        return self.profile.layout_key() != self.saved.layout_key()

    def _fill_combo(self):
        self._loading = True
        self.combo.clear()
        for p in matlib.PRESETS:
            self.combo.addItem(p.name + " (preset)", p.name)
        custom = self.store.custom()
        if custom:
            self.combo.insertSeparator(self.combo.count())
            for p in custom:
                self.combo.addItem(p.name, p.name)
        self.combo.setCurrentIndex(max(0, self.combo.findData(self.saved.name)))
        self._loading = False

    def _load_fields(self):
        """Profile -> widgets."""
        p = self.profile
        self._loading = True
        self.units.setCurrentIndex(max(0, self.units.findData(p.units)))
        hi = matlib.MAX_GRID_MM / p.unit_mm
        for w, v in ((self.grid_w, p.grid_w), (self.grid_h, p.grid_h)):
            w.setRange(0.5, hi)
            w.setSuffix(" " + p.units)
            w.setValue(v)
        self.m_left.setValue(p.margin_left)
        self.m_right.setValue(p.margin_right)
        self.m_top.setValue(p.margin_top)
        self.m_bottom.setValue(p.margin_bottom)
        self.spoof.setCurrentIndex(max(0, self.spoof.findData(p.spoof_kind)))
        self._loading = False
        self._sync()

    def _sync(self):
        """Buttons, the combo's edited marker and the info text."""
        mod = self.is_modified()
        preset = matlib.is_preset(self.saved.name)
        self.save_btn.setEnabled(mod and not preset)
        self.revert_btn.setEnabled(mod)
        self.delete_btn.setEnabled(not preset)
        i = self.combo.findData(self.saved.name)
        if i >= 0:
            base = self.saved.name + (" (preset)" if preset else "")
            self.combo.setItemText(i, base + (" *" if mod else ""))
        p = self.profile
        pw, ph = p.physical_mm()
        gw, gh = p.grid_mm()
        extra = p.feed_extra_units()
        feed = "Feed depth %.1f mm" % p.feed_depth_mm()
        if extra:
            feed += " (top margin + %d %s)" % (extra, p.units)
        self.info.setText("Mat %.1f \u00d7 %.1f mm (%.2f \u00d7 %.2f in)\nGrid %.1f \u00d7 %.1f mm\n%s"
                          % (pw, ph, pw / 25.4, ph / 25.4, gw, gh, feed)
                          + ("\nUnsaved changes" if mod else ""))
        geo = p.geometry()
        gx0, gy0, gx1, gy1 = geo.grid.rect()
        out = []
        if gx0 < machine.HEAD_X_MIN - 1e-6 or gx1 > machine.HEAD_X_MAX + 1e-6:
            out.append("the grid is wider than the cut area")
        if gy1 > machine.MAT_Y_MAX + 1e-6:
            out.append("the grid is longer than the machine can feed (%.0f mm)" % machine.MAT_Y_MAX)
        self.warn.setText(("Warning: " + " ".join(o[:1].upper() + o[1:] + "." for o in out)) if out else "")
        self.warn.setVisible(bool(out))

    def _emit(self, fit=False):
        self.profile_changed.emit(replace(self.profile), fit)

    # -- edits --
    def _apply(self, *a):
        if self._loading:
            return
        p = self.profile
        p.grid_w, p.grid_h = self.grid_w.value(), self.grid_h.value()
        p.margin_left, p.margin_right = self.m_left.value(), self.m_right.value()
        p.margin_top, p.margin_bottom = self.m_top.value(), self.m_bottom.value()
        p.spoof_kind = int(self.spoof.currentData())
        self._sync()
        self._emit()

    # -- skew (document) --
    def skew(self):
        return matlib.MatSkew(self.skew_angle.value(), self.skew_dx.value(), self.skew_dy.value())

    def set_skew(self, skew, emit=False):
        """Show `skew` (e.g. after undo / project load). emit=True also reports it."""
        self._skew_loading = True
        self.skew_angle.setValue(skew.angle_deg)
        self.skew_dx.setValue(skew.dx)
        self.skew_dy.setValue(skew.dy)
        self._skew_loading = False
        if emit:
            self.skew_changed.emit(self.skew())

    def _apply_skew(self, *a):
        if not self._skew_loading:
            self.skew_changed.emit(self.skew())

    def _on_units(self, _i):
        if self._loading:
            return
        self.profile = self.profile.with_units(self.units.currentData())
        self._load_fields()
        self._emit()

    def _confirm_discard(self):
        if not self.is_modified():
            return True
        r = QtWidgets.QMessageBox.question(
            self, "Mat Profile", 'Discard the unsaved changes to "%s"?' % self.saved.name)
        return r == QtWidgets.QMessageBox.Yes

    def _on_pick(self, idx):
        name = self.combo.itemData(idx)
        if self._loading or not name or name == self.saved.name:
            return
        if not self._confirm_discard():
            self._fill_combo()
            self._sync()
            return
        self.select(name)

    def select(self, name):
        """Switch to a saved profile (preset or custom). Returns False if it does not exist."""
        p = self.store.get(name)
        if p is None:
            return False
        self.saved, self.profile = p, replace(p)
        self._fill_combo()
        self._load_fields()
        self.active_changed.emit(p.name)
        self._emit(fit=True)
        return True

    def revert(self):
        self.profile = replace(self.saved)
        self._load_fields()
        self._emit()

    def save(self):
        """Save over the current custom profile. For a preset this is Save As."""
        if matlib.is_preset(self.saved.name):
            return self.save_as()
        return self._write(self.saved.name)

    def save_as(self, name=None):
        """Save the current settings under a new name (asks when name is None)."""
        if name is None:
            suggestion = "" if matlib.is_preset(self.saved.name) else self.saved.name
            name, ok = QtWidgets.QInputDialog.getText(self, "Save Mat Profile", "Profile name:",
                                                      text=suggestion)
            if not ok:
                return False
            name = name.strip()
            if not name:
                return False
            if matlib.is_preset(name):
                QtWidgets.QMessageBox.warning(self, "Save Mat Profile",
                                              '"%s" is a built-in preset. Choose another name.' % name)
                return False
            if name != self.saved.name and self.store.get(name) is not None:
                r = QtWidgets.QMessageBox.question(self, "Save Mat Profile",
                                                   'Replace the existing mat profile "%s"?' % name)
                if r != QtWidgets.QMessageBox.Yes:
                    return False
        return self._write(name)

    def _write(self, name):
        p = replace(self.profile, name=name)
        try:
            self.store.save(p)
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.warning(self, "Save Mat Profile", str(e))
            return False
        self.saved, self.profile = replace(p), p
        self._fill_combo()
        self._load_fields()
        self.active_changed.emit(name)
        self._emit()
        return True

    def delete(self, confirm=True):
        name = self.saved.name
        if matlib.is_preset(name):
            return False
        if confirm:
            r = QtWidgets.QMessageBox.question(self, "Delete Mat Profile",
                                               'Delete the mat profile "%s"?' % name)
            if r != QtWidgets.QMessageBox.Yes:
                return False
        try:
            self.store.delete(name)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Delete Mat Profile", str(e))
            return False
        self.select(matlib.DEFAULT_NAME)
        return True
