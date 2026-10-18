"""Scan panel (Device Control's Scan button): what to scan and where the image goes.

- Area: a box in the mat's grid units, counted from the grid's top-left corner, clamped to what
  the scanner can physically reach (core.scan.scan_limits: the sensor's width, from where the mat
  can be backed off to given the profile's feed depth, down to the mat's trailing edge). "Draw
  box" lets the user drag it on the canvas, and "Whole area" resets it.
- Full sensor width: widens what the box may cover to the scanner's whole CIS (~5.8 mm more on
  each side, using the vendor firmware's 0x55 option, used only when the box needs it).
- Resolution: the machine's ScanToUSB resolutions (300 / 200 / 150 / 100 dpi). One that would
  not fit the machine's scan memory for the box is marked and blocks the scan.
- Format: JPEG (the machine's encoder, with quality % and chroma subsampling) or PNG (raw 8-bit RGB
  from the machine, saved losslessly).
- Destination: apply to the canvas (embedded in the project), save to a file, or both. The path
  field and Browse are only enabled when a file is written.

The panel holds no device code: it emits scan_requested(ScanSettings, window) and the main window
runs the scan. set_context() gives it the mat profile, the machine's ScanCal, the mat skew and
the mat kind. overlay() / overlay_changed describe the box for the canvas.
"""
import os
from dataclasses import replace

from PySide6 import QtWidgets, QtCore
from core import scan as scanlib
from core import mat as matlib
from core.machine import ScanCal

FILTERS = {"jpeg": "JPEG image (*.jpg *.jpeg)", "png": "PNG image (*.png)"}
EXTS = {"jpeg": ".jpg", "png": ".png"}


class ScanPanel(QtWidgets.QWidget):
    scan_requested = QtCore.Signal(object, object)    # ScanSettings, window (x0, x1, y0, y1)
    cancel_requested = QtCore.Signal()
    overlay_changed = QtCore.Signal(object)            # canvas overlay dict
    draw_box_toggled = QtCore.Signal(bool)
    settings_changed = QtCore.Signal(object)           # ScanSettings (to persist)

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings or scanlib.ScanSettings()
        self.profile = matlib.preset(matlib.DEFAULT_NAME)
        self.geo = self.profile.geometry()
        self.cal, self.skew, self.kind = ScanCal(), matlib.MatSkew(), 2
        self._loading = False
        self._busy = False

        lay = QtWidgets.QVBoxLayout(self)
        area = QtWidgets.QLabel("<b>Area</b>")
        area.setToolTip("Measured from the top-left corner of the mat's grid")
        lay.addWidget(area)
        grid = QtWidgets.QGridLayout()
        self.f_x, self.f_y, self.f_w, self.f_h = (self._spin() for _ in range(4))
        for i, (name, w) in enumerate((("Left", self.f_x), ("Top", self.f_y),
                                       ("Width", self.f_w), ("Height", self.f_h))):
            grid.addWidget(QtWidgets.QLabel(name), i // 2, (i % 2) * 2)
            grid.addWidget(w, i // 2, (i % 2) * 2 + 1)
        lay.addLayout(grid)
        row = QtWidgets.QHBoxLayout()
        self.draw_btn = QtWidgets.QPushButton("Draw box")
        self.draw_btn.setCheckable(True)
        self.draw_btn.setToolTip("Drag a box on the canvas to choose the area to scan")
        self.all_btn = QtWidgets.QPushButton("Whole area")
        self.all_btn.setToolTip("Everything the scanner can reach on this mat")
        row.addWidget(self.draw_btn)
        row.addWidget(self.all_btn)
        lay.addLayout(row)
        self.wide = QtWidgets.QCheckBox("Full sensor width")
        self.wide.setToolTip("Scan about 5.8 mm further on each side than the machine's own\n"
                             "scans, using the scanner's full width")
        lay.addWidget(self.wide)

        form = QtWidgets.QFormLayout()
        self.dpi = QtWidgets.QComboBox()
        for d in scanlib.DPIS:
            self.dpi.addItem("%d dpi" % d, d)
        form.addRow("Resolution", self.dpi)
        fmt_row = QtWidgets.QHBoxLayout()
        self.fmt_jpeg = QtWidgets.QRadioButton("JPEG")
        self.fmt_png = QtWidgets.QRadioButton("PNG (lossless)")
        self.fmt_png.setToolTip("Full quality, but larger and slower to download than JPEG")
        fmt_row.addWidget(self.fmt_jpeg)
        fmt_row.addWidget(self.fmt_png)
        form.addRow("Format", fmt_row)
        self.quality = QtWidgets.QSpinBox()
        self.quality.setRange(1, 100)
        self.quality.setSuffix(" %")
        self.quality.setToolTip("Higher quality makes larger files. The machine's own scans use 95%.")
        form.addRow("JPEG quality", self.quality)
        self.subsample = QtWidgets.QCheckBox("Reduce color detail")
        self.subsample.setToolTip("Store color at half resolution for smaller files, as the\n"
                                  "machine's own scans do. Turn off for sharper color edges.")
        form.addRow(self.subsample)
        lay.addLayout(form)

        lay.addWidget(QtWidgets.QLabel("<b>Destination</b>"))
        dest_row = QtWidgets.QHBoxLayout()
        self.dest_canvas = QtWidgets.QRadioButton("Canvas")
        self.dest_canvas.setToolTip("Place the scan on the canvas (saved in the project)")
        self.dest_file = QtWidgets.QRadioButton("File")
        self.dest_both = QtWidgets.QRadioButton("Both")
        self.dest_group = QtWidgets.QButtonGroup(self)
        for b in (self.dest_canvas, self.dest_file, self.dest_both):
            self.dest_group.addButton(b)
            dest_row.addWidget(b)
        lay.addLayout(dest_row)
        path_row = QtWidgets.QHBoxLayout()
        self.path = QtWidgets.QLineEdit()
        self.path.setPlaceholderText("Automatic file name")
        self.browse = QtWidgets.QPushButton("Browse\u2026")
        path_row.addWidget(self.path, 1)
        path_row.addWidget(self.browse)
        lay.addLayout(path_row)

        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color: #9a9a9a;")
        lay.addWidget(self.info)
        self.warn = QtWidgets.QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("color: #e0a030;")
        lay.addWidget(self.warn)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(True)
        self.progress.hide()
        lay.addWidget(self.progress)
        row = QtWidgets.QHBoxLayout()
        self.scan_btn = QtWidgets.QPushButton("Scan")
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        row.addWidget(self.scan_btn, 1)
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)

        for w in (self.f_x, self.f_y, self.f_w, self.f_h):
            w.editingFinished.connect(self._on_box_edit)
        self.draw_btn.toggled.connect(self.draw_box_toggled)
        self.all_btn.clicked.connect(self.reset_box)
        self.wide.toggled.connect(self._on_wide)
        self.dpi.currentIndexChanged.connect(self._on_change)
        self.quality.valueChanged.connect(self._on_change)
        self.subsample.toggled.connect(self._on_change)
        for b in (self.fmt_jpeg, self.fmt_png):
            b.toggled.connect(self._on_fmt)
        self.dest_group.buttonToggled.connect(lambda *a: self._on_change())
        self.path.editingFinished.connect(self._on_change)
        self.browse.clicked.connect(self._browse)
        self.scan_btn.clicked.connect(self._scan)
        self.cancel_btn.clicked.connect(self.cancel_requested)
        self._load()

    @staticmethod
    def _spin():
        w = QtWidgets.QDoubleSpinBox()
        w.setRange(-1000.0, 1000.0)
        w.setKeyboardTracking(False)
        return w

    # -- context --
    def set_context(self, profile=None, cal=None, skew=None, kind=None):
        """Mat profile (units, grid), the machine's ScanCal, the mat skew, the mat kind."""
        if profile is not None:
            self.profile, self.geo = profile, profile.geometry()
            kind = profile.spoof_kind if kind is None else kind
        if cal is not None:
            self.cal = cal
        if skew is not None:
            self.skew = skew
        if kind is not None:
            self.kind = kind
        self.settings.box = scanlib.clamp_box(self.settings.box, self.geo, self.cal, self.skew,
                                              self.limits())
        self._load()

    # -- settings <-> widgets --
    def _load(self):
        s, p = self.settings, self.profile
        self._loading = True
        s.box = scanlib.clamp_box(s.box, self.geo, self.cal, self.skew, self.limits())
        u = p.unit_mm
        for w, v in zip((self.f_x, self.f_y, self.f_w, self.f_h), s.box):
            w.setDecimals(3 if p.units == "in" else 2)
            w.setSingleStep(0.125 if p.units == "in" else 0.5)
            w.setSuffix(" " + p.units)
            w.setValue(v / u)
        self.dpi.setCurrentIndex(max(0, self.dpi.findData(s.dpi)))
        (self.fmt_png if s.fmt == "png" else self.fmt_jpeg).setChecked(True)
        self.quality.setValue(s.quality)
        self.subsample.setChecked(s.subsampling)
        self.wide.setChecked(s.wide)
        {"canvas": self.dest_canvas, "file": self.dest_file, "both": self.dest_both}[s.dest].setChecked(True)
        self.path.setText(s.path)
        self._loading = False
        self._sync()

    def _read(self):
        s = self.settings
        s.dpi = int(self.dpi.currentData())
        s.fmt = "png" if self.fmt_png.isChecked() else "jpeg"
        s.quality = self.quality.value()
        s.subsampling = self.subsample.isChecked()
        s.wide = self.wide.isChecked()
        s.dest = ("file" if self.dest_file.isChecked() else
                  "both" if self.dest_both.isChecked() else "canvas")
        s.path = self.path.text().strip()

    def _on_change(self, *a):
        if self._loading:
            return
        self._read()
        self._sync()
        self.settings_changed.emit(replace(self.settings))

    def _on_wide(self, checked):
        if self._loading:
            return
        s = self.settings
        was_all = self._is_whole_area(s.box)          # a whole-area box follows the new limits
        self._read()
        self.set_box(None if was_all else s.box)

    def _is_whole_area(self, box):
        """Is `box` the whole area under the current settings?"""
        if box is None:
            return True
        full = scanlib.clamp_box(None, self.geo, self.cal, self.skew, self.limits())
        return all(abs(a - b) < 0.01 for a, b in zip(box, full))

    def _on_fmt(self, checked):
        if self._loading or not checked:
            return
        self._read()
        root, ext = os.path.splitext(self.settings.path)
        if ext.lower() in (".jpg", ".jpeg", ".png"):        # keep the path's type in step
            self.settings.path = root + EXTS[self.settings.fmt]
            self.path.setText(self.settings.path)
        self._sync()
        self.settings_changed.emit(replace(self.settings))

    def _on_box_edit(self):
        if self._loading:
            return
        u = self.profile.unit_mm
        box = tuple(w.value() * u for w in (self.f_x, self.f_y, self.f_w, self.f_h))
        self.set_box(box if box[2] > 0 and box[3] > 0 else None)

    def set_box(self, box):
        """Box in printed mm from the grid corner (None = whole area), clamped and shown."""
        self.settings.box = scanlib.clamp_box(box, self.geo, self.cal, self.skew, self.limits())
        self._load()
        self.settings_changed.emit(replace(self.settings))

    def set_box_canvas(self, rect):
        """A box drawn on the canvas (canvas mm x0, y0, x1, y1)."""
        self.draw_btn.setChecked(False)
        self.set_box(scanlib.box_from_canvas(rect, self.geo))

    def reset_box(self):
        self.set_box(None)

    def set_draw_active(self, on):
        if self.draw_btn.isChecked() != on:
            self.draw_btn.blockSignals(True)
            self.draw_btn.setChecked(on)
            self.draw_btn.blockSignals(False)

    # -- derived --
    def back_max(self):
        """Y units a scan may start behind the stock start, for the profile's feed depth."""
        return scanlib.back_max_units(scanlib.matedge_units(self.profile.feed_depth_mm()))

    def limits(self, wide=None):
        """What the scanner can reach (frame 0.01 mm), with or without the full sensor."""
        return scanlib.scan_limits(self.geo, self.cal, self.skew,
                                   self.settings.wide if wide is None else wide,
                                   scanlib.matedge_units(self.profile.feed_depth_mm()))

    def window(self):
        return scanlib.box_to_window(scanlib.box_canvas(self.settings.box, self.geo),
                                     self.cal, self.skew, self.limits())

    def overlay(self):
        win = self.window()
        ov = {"area": scanlib.scannable_box(self.cal, self.skew, self.limits()),
              "box": scanlib.box_canvas(self.settings.box, self.geo)}
        if win is not None:
            dpi = min(self.settings.dpi, scanlib.max_dpi(win, self.back_max()) or self.settings.dpi)
            ov["actual"] = scanlib.window_outline(win, dpi, self.cal, self.skew, self.back_max())
        return ov

    def _sync(self):
        s = self.settings
        jpeg = s.fmt == "jpeg"
        self.quality.setEnabled(jpeg)
        self.subsample.setEnabled(jpeg)
        to_file = s.dest in ("file", "both")
        self.path.setEnabled(to_file)
        self.browse.setEnabled(to_file)
        win = self.window()
        problems = []
        if win is None:
            self.info.setText("")
            problems.append("The area is outside what the scanner can reach.")
        else:
            top = scanlib.max_dpi(win, self.back_max())
            for i in range(self.dpi.count()):
                d = self.dpi.itemData(i)
                fits = top is not None and d <= top
                self.dpi.setItemText(i, "%d dpi" % d + ("" if fits else " (area too large)"))
            r = scanlib.scan_request(win, s.dpi, self.back_max())
            w, h = r["width"], r["lines"]
            mm_w, mm_h = w * 25.4 / s.dpi, h * 25.4 / s.dpi
            raw = w * h * 3
            extra = []
            if r["wide"]:
                extra.append("full sensor")
            self.info.setText("%d \u00d7 %d px \u00b7 %.1f \u00d7 %.1f mm%s\n%s" % (
                w, h, mm_w, mm_h, " \u00b7 " + ", ".join(extra) if extra else "",
                "PNG \u00b7 about %.0f MB to download" % (raw / 1e6) if s.fmt == "png"
                else "JPEG \u00b7 quality %d%%%s" % (s.quality, " \u00b7 reduced color detail"
                                                    if s.subsampling else "")))
            self.info.setToolTip("The mat will be fed in %.1f mm less so that the scan can reach the top of the area"
                                 % (r["back"] * 25.4 / 1800) if r["back"] else "")
            if top is None or s.dpi > top:
                problems.append("The area is too large to scan at %d dpi%s."
                                % (s.dpi, ". Use %d dpi or a smaller area" % top if top else ""))
        self.warn.setText(" ".join(problems))
        self.warn.setVisible(bool(problems))
        self.scan_btn.setEnabled(not problems and not self._busy)
        self.overlay_changed.emit(self.overlay())

    # -- actions --
    def _browse(self):
        s = self.settings
        start = s.path or os.path.join(os.path.expanduser("~"), "scan" + EXTS[s.fmt])
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Scan As", start, FILTERS[s.fmt])
        if path:
            if os.path.splitext(path)[1].lower() not in (".jpg", ".jpeg", ".png"):
                path += EXTS[s.fmt]
            self.path.setText(path)
            self._on_change()

    def _scan(self):
        self._read()
        win = self.window()
        if win is None:
            return
        self.scan_requested.emit(replace(self.settings), win)

    def set_busy(self, busy, text="", frac=None):
        """Show scan progress. busy=False hides the bar and re-enables Scan."""
        self._busy = busy
        self.cancel_btn.setEnabled(busy)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setValue(int(1000 * (frac or 0.0)))
            self.progress.setFormat(text or "%p%")
        self._sync()
