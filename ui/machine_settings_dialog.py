"""Machine > Machine Settings: every value in the machine's settings EEPROM, one tab per group
(Settings, Emboss, Foil, ...), a raw hex tab for bytes the catalog does not name, and the
firmware update buttons. Catalog: pc-driver/cm110_eeprom.py. Backend: device/machine_settings.py.

The dialog edits a copy of the EEPROM's used area (`image`). Apply writes only the bytes that
differ from what was read (`loaded`). `device_busy()` (the main window's) is checked before each
USB access, so the dialog never opens the machine while a scan, feed, cut or mount holds it.
Without the 0xC9 firmware the field tabs stay disabled, but the firmware buttons still work. A read
also tells which firmware the machine is running: on the original one the firmware buttons switch to
saving the firmware for a USB flash drive (ui/firmware_panel.py).
"""
from PySide6 import QtWidgets, QtCore, QtGui
from device import machine_settings as ms
from device import firmware_update as fw
from .eeprom_editors import FieldEditor
from .eeprom_hex import HexView
from .firmware_panel import FirmwarePanel

GROUP_INTRO = {
    "Settings": "The machine's Settings screen. Speed and pressure take effect immediately. "
                "Other settings take effect after the machine restarts.",
    "Emboss": "Settings for the embossing kit.",
    "Foil": "Settings for the foil kit.",
    "Cut area & calibration": "Factory calibration. Wrong values make cuts land in the wrong "
                              "place. Note the current values before changing anything.",
    "Sensors & service": "Service settings. Most take effect after the machine restarts.",
    "Counters": "Usage counters.",
    "Factory": "Set at the factory. Wrong values can stop the machine from starting.",
    "Updater": "Settings the firmware updater reads when it starts. To update the firmware, "
               "use the Firmware buttons below instead.",
}
TAB_TITLE = {"Cut area & calibration": "Calibration", "Sensors & service": "Service",
             "Fill patterns": "Fill"}       # short, so all tabs fit without scroll buttons
CAUTION_GROUPS = ("Cut area & calibration", "Factory", "Updater")


class MachineSettingsDialog(QtWidgets.QDialog):
    def __init__(self, backend, device_busy=None, firmware=None, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.device_busy = device_busy or (lambda: None)
        self.loaded = None                  # bytes as last read from the machine
        self.image = bytearray(ms.SIZE)     # what the widgets currently say
        self.editors = {}                   # field name -> FieldEditor
        self._labels = {}                   # field name -> QLabel
        self._owner = [ms.eep.field_at(a) for a in range(ms.SIZE)]   # byte -> catalog field
        self.setWindowTitle("Machine Settings")
        self.resize(760, 680)
        lay = QtWidgets.QVBoxLayout(self)

        self.tabs = QtWidgets.QTabWidget()
        for group in ms.GROUPS:
            self.tabs.addTab(self._build_tab(group), TAB_TITLE.get(group, group))
        self.hex = HexView(lambda a: self._owner[a] if a < len(self._owner) else None)
        self.hex.byteEdited.connect(self._hex_edited)
        raw = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(raw)
        hint = QtWidgets.QLabel("The machine's settings memory, byte by byte. Shaded bytes "
                                "have unsaved changes. Edit with care.")
        hint.setWordWrap(True)
        rl.addWidget(hint)
        rl.addWidget(self.hex)
        self.tabs.addTab(raw, "Raw (hex)")
        lay.addWidget(self.tabs, 1)

        self.firmware = FirmwarePanel(firmware, device_busy=self.device_busy, parent=self) \
            if firmware is not None else None
        if self.firmware is not None:
            self.firmware.busyChanged.connect(self._firmware_busy)
            lay.addWidget(self.firmware)

        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Apply
            | QtWidgets.QDialogButtonBox.Cancel)
        self.reload_btn = self.buttons.addButton("Reload", QtWidgets.QDialogButtonBox.ResetRole)
        self.reload_btn.setToolTip("Read the settings from the machine again, discarding "
                                   "unsaved changes")
        self.buttons.accepted.connect(self._ok)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QtWidgets.QDialogButtonBox.Apply).clicked.connect(self.apply)
        self.reload_btn.clicked.connect(self.reload)
        lay.addWidget(self.buttons)
        self._set_loaded(None)

    # -- building --
    def _build_tab(self, group):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        intro = QtWidgets.QLabel(GROUP_INTRO.get(group, ""))
        intro.setWordWrap(True)
        if group in CAUTION_GROUPS:
            intro.setStyleSheet("color: #b00020")
        outer.addWidget(intro)
        body = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(body)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        for f in ms.FIELDS:
            if f.group != group:
                continue
            ed = FieldEditor(f, self)
            ed.changed.connect(lambda ed=ed: self._editor_changed(ed))
            lab = QtWidgets.QLabel(f.label)
            if f.danger:
                lab.setStyleSheet("color: #b00020")
            self.editors[f.name] = ed
            self._labels[f.name] = lab
            form.addRow(lab, ed.widget)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        outer.addWidget(scroll, 1)
        return page

    # -- image <-> widgets --
    def _editor_changed(self, ed):
        f = ed.field
        self.image[f.addr:f.end] = ed.get_bytes()
        self._refresh_marks()

    def _hex_edited(self, addr, value):
        self.image[addr] = value
        f = self._owner[addr]
        if f is not None:
            self.editors[f.name].set_bytes(self.image[f.addr:f.end])
        self._refresh_marks()

    def _show_image(self):
        for ed in self.editors.values():
            ed.set_bytes(self.image[ed.field.addr:ed.field.end])
        self._refresh_marks()

    def _refresh_marks(self):
        changed = {f.name for f in ms.changed_fields(self.loaded, self.image)} \
            if self.loaded is not None else set()
        for name, lab in self._labels.items():
            font = lab.font()
            font.setBold(name in changed)
            lab.setFont(font)
        self.hex.set_image(self.image, self.loaded)
        self._sync_buttons()

    def values(self):
        """The EEPROM image the widgets describe."""
        return bytes(self.image)

    def set_values(self, image):
        self.image[:] = image
        self._show_image()

    def pending(self):
        """[label] of what Apply would write: catalog fields, then loose bytes as '0xADDR'."""
        if self.loaded is None:
            return []
        out = [f.label for f in ms.changed_fields(self.loaded, self.image)]
        for a in range(len(self.image)):
            if self.image[a] != self.loaded[a] and self._owner[a] is None:
                out.append("%#05x" % a)
        return out

    def _set_loaded(self, image):
        self.loaded = bytes(image) if image is not None else None
        for i in range(self.tabs.count()):
            self.tabs.widget(i).setEnabled(image is not None)
        if image is not None:
            self.image[:] = image
            self._show_image()
        self._sync_buttons()

    def _sync_buttons(self, *_):
        dirty = bool(self.pending())
        self.buttons.button(QtWidgets.QDialogButtonBox.Apply).setEnabled(dirty)
        self.buttons.button(QtWidgets.QDialogButtonBox.Ok).setEnabled(self.loaded is not None)

    # -- machine I/O --
    def _blocked(self):
        why = self.device_busy()
        if why:
            self.status.setText("The machine is busy (%s). Try again when it has finished." % why)
        return bool(why)

    def _io(self, fn, *args):
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            return fn(*args)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def reload(self):
        """Read the EEPROM from the machine. Returns False if that failed."""
        if self._blocked():
            return False
        try:
            image = self._io(self.backend.read)
        except Exception as e:
            self._set_loaded(None)
            if isinstance(e, ms.NeedsFirmware):        # the machine answered, so the firmware is known
                self._firmware_is(fw.STOCK if isinstance(e, ms.StockFirmware) else fw.SNOWCUT)
            self.status.setText("Could not read the settings: %s" % ms.describe_error(e))
            return False
        self._firmware_is(fw.SNOWCUT)
        self._set_loaded(image)
        self.status.setText("Read from the machine.")
        return True

    def confirm_caution(self, fields):
        """True to go ahead with changes to fields marked dangerous in the catalog."""
        text = "\n\n".join("%s: %s" % (f.label, f.danger) for f in fields)
        return QtWidgets.QMessageBox.warning(
            self, "Change service values?",
            "You are changing values that the machine depends on:\n\n%s\n\nWrite them anyway?"
            % text, QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No) == QtWidgets.QMessageBox.Yes

    def apply(self):
        """Write the changed bytes. Returns False if nothing could be confirmed."""
        todo = self.pending()
        if not todo:
            return True
        risky = [f for f in ms.changed_fields(self.loaded, self.image) if f.danger]
        if risky and not self.confirm_caution(risky):
            return False
        if self._blocked():
            return False
        try:
            got = self._io(self.backend.write, self.loaded, bytes(self.image))
        except Exception as e:
            self.status.setText("Could not save the settings: %s" % ms.describe_error(e))
            self.reload_btn.setFocus()
            return False
        self._set_loaded(got)
        self.status.setText("Saved %d change%s to the machine. Most settings will take effect after the "
                            "machine is restarted." % (len(todo), "" if len(todo) == 1 else "s"))
        return True

    def _ok(self):
        if self.apply():
            self.accept()

    # -- firmware buttons --
    def _firmware_is(self, kind):
        if self.firmware is not None:
            self.firmware.set_firmware(kind)

    def _firmware_busy(self, busy):
        self.tabs.setEnabled(not busy and self.loaded is not None)
        self.buttons.setEnabled(not busy)
        if not busy:
            self._sync_buttons()

    def reject(self):
        if self.firmware is not None and self.firmware.busy:
            return                          # the machine is being written to: wait
        super().reject()

    def showEvent(self, ev):
        super().showEvent(ev)
        if self.loaded is None and not getattr(self, "_shown_once", False):
            self._shown_once = True
            QtCore.QTimer.singleShot(0, self.reload)
