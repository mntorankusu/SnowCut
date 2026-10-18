"""One editing widget per EEPROM field (catalog: pc-driver/cm110_eeprom.py).

A FieldEditor shows the field's current bytes and gives them back with get_bytes(). Bytes the
firmware would not use as they are (erased 0xFF, or out of range: it falls back to its default)
are shown as the default with an "unset" note, and are returned unchanged until the user edits
the widget, so opening and applying never rewrites a byte by accident.
"""
from PySide6 import QtWidgets, QtCore, QtGui

ASCII_RE = QtCore.QRegularExpression(r"[\x20-\x7e]*")


def _shown(f, raw):
    return raw + f.offset


class FieldEditor(QtCore.QObject):
    changed = QtCore.Signal()               # the user edited the value

    def __init__(self, field, parent=None):
        super().__init__(parent)
        self.field = field
        self._orig = bytes(field.size)
        self._touched = False
        self._loading = False
        self.note = QtWidgets.QLabel()
        self.note.setStyleSheet("color: gray; font-style: italic")
        self.edit = self._make()
        self.widget = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(self.widget)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit)
        lay.addWidget(self.note)
        lay.addStretch(1)
        tip = field.note or ""
        if field.default is not None and field.kind not in ("ascii", "bytes"):
            tip += ("\n" if tip else "") + "Default: %s" % self._text_for(field.default)
        tip += ("\n" if tip else "") + "EEPROM %#05x, %d byte%s" % (
            field.addr, field.size, "" if field.size == 1 else "s")
        if field.danger:
            tip += "\nCAUTION: " + field.danger
        self.widget.setToolTip(tip)

    # -- widget construction --
    def _make(self):
        f = self.field
        if f.choices is not None:
            w = QtWidgets.QComboBox()
            for raw, text in sorted(f.choices.items()):
                w.addItem(text, raw)
            w.currentIndexChanged.connect(self._user)
            return w
        if f.kind == "ascii":
            w = QtWidgets.QLineEdit()
            w.setMaxLength(f.size)
            w.setValidator(QtGui.QRegularExpressionValidator(ASCII_RE, w))
            w.textEdited.connect(self._user)
            return w
        if f.kind == "bytes":
            w = QtWidgets.QLineEdit()
            w.setInputMask(" ".join(["HH"] * f.size) + ";_")
            w.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
            w.textEdited.connect(self._user)
            return w
        if f.kind == "f32":
            w = QtWidgets.QDoubleSpinBox()
            w.setDecimals(f.decimals)
            lo = -3.0e38 if f.lo is None else f.lo
            hi = 3.0e38 if f.hi is None else f.hi
            w.setRange(lo, hi)
            w.valueChanged.connect(self._user)
            return self._unit(w)
        lo, hi = _shown(f, f.lo), _shown(f, f.hi)
        if hi > 0x7FFFFFFF or lo < -0x7FFFFFFF:      # u32: beyond QSpinBox's int range
            w = QtWidgets.QDoubleSpinBox()
            w.setDecimals(0)
            w.setRange(lo, hi)
        else:
            w = QtWidgets.QSpinBox()
            w.setRange(lo, hi)
        w.valueChanged.connect(self._user)
        return self._unit(w)

    def _unit(self, w):
        if self.field.unit:
            w.setSuffix(self.field.unit)
        w.setKeyboardTracking(False)
        w.setMinimumWidth(110)
        w.setFocusPolicy(QtCore.Qt.StrongFocus)
        return w

    # -- values --
    def _text_for(self, raw):
        f = self.field
        if f.choices is not None:
            return f.choices.get(raw, str(raw))
        return "%s%s" % (_shown(f, raw), f.unit)

    def _user(self, *_):
        if self._loading:
            return
        self._touched = True
        self.note.setText("")
        self.changed.emit()

    def set_bytes(self, data):
        """Show these bytes (the field's slice of the EEPROM image)."""
        f = self.field
        self._orig, self._touched = bytes(data), False
        self._loading = True
        try:
            note = ""
            if f.kind == "ascii":
                txt = bytes(b for b in self._orig if 0x20 <= b < 0x7F).decode("ascii")
                self.edit.setText(txt)
                if all(b == 0xFF for b in self._orig):
                    note = "unset"
            elif f.kind == "bytes":
                self.edit.setText(" ".join("%02X" % b for b in self._orig))
            else:
                raw = f.get(self._orig, base=f.addr)
                if f.valid(raw):
                    self._show(raw)
                else:
                    self._show(f.default if f.default is not None else self._fallback())
                    note = "unset (reads as %s)" % self._text_for(
                        f.default) if f.default is not None else "unset"
                    if f.kind != "f32" and f.choices is None and raw != 0xFF:
                        note = "out of range (%s): the firmware ignores it" % raw
            self.note.setText(note)
        finally:
            self._loading = False

    def _fallback(self):
        f = self.field
        if f.choices is not None:
            return min(f.choices)
        if f.kind == "f32":                  # neutral calibration: scale 1, offsets 0
            lo = -3.0e38 if f.lo is None else f.lo
            hi = 3.0e38 if f.hi is None else f.hi
            return 1.0 if lo <= 1.0 <= hi and f.lo is not None and f.lo > 0 else min(max(0.0, lo), hi)
        return f.lo

    def _show(self, raw):
        f, w = self.field, self.edit
        if f.choices is not None:
            i = w.findData(raw)
            if i < 0:
                i = 0
            w.setCurrentIndex(i)
        else:
            w.setValue(_shown(f, raw))

    def get_bytes(self):
        """The field's bytes: unchanged until the user edits the widget."""
        f, w = self.field, self.edit
        if not self._touched:
            return self._orig
        if f.kind == "ascii":
            return f.encode(w.text())
        if f.kind == "bytes":
            if not w.hasAcceptableInput():
                return self._orig
            return bytes.fromhex(w.text())
        if f.choices is not None:
            return f.encode(w.currentData())
        v = w.value()
        if f.kind == "f32":
            return f.encode(v)
        return f.encode(int(round(v)) - f.offset)
