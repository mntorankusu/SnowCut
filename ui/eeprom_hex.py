"""Raw hex view of the EEPROM image: 16 bytes per row, every byte editable.

Bytes that differ from what the machine held when it was read are shaded, and hovering a byte
names the catalog field it belongs to. Emits byteEdited(addr, value) for each user edit.
"""
import re
from PySide6 import QtWidgets, QtCore, QtGui

COLS = 16
HEX_RE = re.compile(r"^\s*(?:0x)?([0-9a-fA-F]{1,2})\s*$")


class HexView(QtWidgets.QTableWidget):
    byteEdited = QtCore.Signal(int, int)

    def __init__(self, field_at, parent=None):
        super().__init__(parent)
        self._field_at = field_at
        self._image = b""
        self._loaded = b""
        self._busy = False
        self.setColumnCount(COLS)
        self.setHorizontalHeaderLabels(["%X" % i for i in range(COLS)])
        self.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.horizontalHeader().setDefaultSectionSize(34)
        self.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Fixed)
        self.verticalHeader().setDefaultSectionSize(22)
        self.itemChanged.connect(self._edited)

    def set_image(self, image, loaded=None):
        """Show `image` and shade the bytes that differ from `loaded` (default: no shading)."""
        self._image = bytes(image)
        self._loaded = bytes(loaded) if loaded is not None else self._image
        rows = (len(self._image) + COLS - 1) // COLS
        self._busy = True
        try:
            if self.rowCount() != rows:
                self.setRowCount(rows)
                self.setVerticalHeaderLabels(["%03X" % (r * COLS) for r in range(rows)])
            for a, b in enumerate(self._image):
                self._set_cell(a, b)
            for a in range(len(self._image), rows * COLS):
                self.setItem(a // COLS, a % COLS, None)
        finally:
            self._busy = False

    def _set_cell(self, addr, value):
        r, c = divmod(addr, COLS)
        it = self.item(r, c)
        if it is None:
            it = QtWidgets.QTableWidgetItem()
            it.setTextAlignment(QtCore.Qt.AlignCenter)
            self.setItem(r, c, it)
        it.setText("%02X" % value)
        f = self._field_at(addr)
        it.setToolTip("%#05x  %s" % (addr, f.label if f else "(not in the catalog)"))
        changed = addr < len(self._loaded) and self._loaded[addr] != value
        it.setBackground(QtGui.QBrush(QtGui.QColor("#ffd9a0") if changed else QtGui.QColor(0, 0, 0, 0)))
        it.setForeground(QtGui.QBrush(self.palette().color(QtGui.QPalette.Text)) if changed
                         else QtGui.QBrush(self.palette().color(
                             QtGui.QPalette.Text if f else QtGui.QPalette.PlaceholderText)))

    def _edited(self, it):
        if self._busy:
            return
        addr = it.row() * COLS + it.column()
        m = HEX_RE.match(it.text())
        self._busy = True
        try:
            if addr >= len(self._image) or not m:
                self._set_cell(addr, self._image[addr] if addr < len(self._image) else 0)
                return
            value = int(m.group(1), 16)
            self._set_cell(addr, value)
        finally:
            self._busy = False
        if value != self._image[addr]:
            self._image = self._image[:addr] + bytes([value]) + self._image[addr + 1:]
            self.byteEdited.emit(addr, value)
