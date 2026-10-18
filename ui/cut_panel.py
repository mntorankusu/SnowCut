"""Cut job panels.

CutPanel: canvas preview options -> Generate -> MOTION send with progress, cancel,
pause/resume and dry-run. Cut settings live in doc.blade (job) and ColorParams.blade
(per color: swivel blade vs pen) and are edited in the Cut Settings panel. This panel
only owns the preview/simulation options. Send is only enabled for code generated from
the current settings and document.

CodePreviewPanel: what the CutPanel generated, with detailed statistics (including the
simulated cut error), the canvas preview color key and the G-code. Opened from the
CutPanel's code preview button.

Both are info cards, so the canvas stays fully usable while they are open.
"""
import json
from dataclasses import replace
from PySide6 import QtWidgets, QtCore
from core.gcode import plan_ops_flat, ops_to_gcode, ops_to_steps, stats
from core.blade import compensate_ops
from device.usb_drive import wait_message
from ui.canvas import PREVIEW_COLORS
from ui.icons import icon_button
from ui.panels import mm_spin, PANEL_BTN, MUTED

BTN_PX = PANEL_BTN
STALE = '<span style="color:#ef5350">The drawing or settings changed. Generate again before sending.</span>'


class SendWorker(QtCore.QObject):
    progress = QtCore.Signal(float, str)
    finished = QtCore.Signal(dict)

    def __init__(self, sender, step_ops):
        super().__init__()
        self.sender = sender
        self.step_ops = step_ops

    @QtCore.Slot()
    def run(self):
        try:
            res = self.sender.send(self.step_ops, progress_cb=lambda f, m: self.progress.emit(f, m))
            self.finished.emit(dict(res, error=""))
        except Exception as e:
            self.finished.emit({"sent": 0, "cancelled": True, "error": str(e)})


class CodePreviewPanel(QtWidgets.QWidget):
    """Read-only view filled by CutPanel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        self.stats = QtWidgets.QLabel("Nothing generated yet.")
        self.stats.setStyleSheet(MUTED)
        self.stats.setWordWrap(True)
        self.stats.setTextFormat(QtCore.Qt.RichText)
        lay.addWidget(self.stats)
        lay.addWidget(QtWidgets.QLabel("<b>Preview colors</b>"))
        self.legend = QtWidgets.QLabel()
        self.legend.setWordWrap(True)
        lay.addWidget(self.legend)
        lay.addWidget(QtWidgets.QLabel("<b>G-code</b>"))
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.text.setMinimumHeight(160)
        lay.addWidget(self.text, 1)

    def set_stats(self, html):
        self.stats.setStyleSheet("")
        self.stats.setText(html)

    def set_legend(self, html):
        self.legend.setText(html)

    def set_code(self, text):
        self.text.setPlainText(text)

    def refresh(self):
        pass


class CutPanel(QtWidgets.QWidget):
    open_settings = QtCore.Signal()
    code_toggled = QtCore.Signal(bool)  # code preview button: show / hide that panel
    generated = QtCore.Signal()         # generate() finished (it writes doc.blade): undo history hook

    def __init__(self, doc, sender, canvas=None, code=None, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.sender = sender
        self.canvas = canvas
        self.code = code or CodePreviewPanel()
        self._generated = False
        self._fingerprint = None
        self._thread = None
        self.device_busy = lambda: None   # set by the main window: "why the machine is busy" or None
        self._ops = self._steps = None
        self._preview = None
        self._shown = False
        self._last_comp_offset = None
        bs = doc.blade
        lay = QtWidgets.QVBoxLayout(self)

        # -- actions (words in tooltips / status bar) --
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        self.settings_btn = icon_button("config", "Cut settings", px=BTN_PX)
        self.code_btn = icon_button("code_preview", "Code preview and statistics",
                                    px=BTN_PX, checkable=True)
        self.gen_btn = icon_button("generate", "Generate", px=BTN_PX)
        self.send_btn = icon_button("send", "Send to machine", px=BTN_PX)
        self.pause_btn = icon_button("pause", "Pause", px=BTN_PX, checkable=True,
                                     checked_name="resume")
        self.cancel_btn = icon_button("cancel_send", "Cancel", px=BTN_PX)
        for b in (self.settings_btn, self.code_btn):
            row.addWidget(b)
        row.addStretch(1)
        for b in (self.gen_btn, self.send_btn, self.pause_btn, self.cancel_btn):
            row.addWidget(b)
        lay.addLayout(row)
        self.send_btn.setEnabled(False)
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)

        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(QtCore.Qt.RichText)
        lay.addWidget(self.summary)

        # -- canvas preview options --
        lay.addWidget(QtWidgets.QLabel("<b>Preview on canvas</b>"))
        form = QtWidgets.QFormLayout()
        self.sim_offset = mm_spin(0.0, 10.0, 0.05, 2, bs.sim_offset_mm,
                                  "Blade offset used to simulate the cut result.\n"
                                  "Use a value other than the compensation offset to preview\n"
                                  "the effect of a mis-measured blade.")
        form.addRow("Simulated blade", self.sim_offset)
        lay.addLayout(form)
        self.show_axis = QtWidgets.QCheckBox("Holder path")
        self.show_axis.setToolTip("How the machine moves the blade holder")
        self.show_tip = QtWidgets.QCheckBox("Blade tip")
        self.show_tip.setToolTip("What the blade tip actually cuts, colored by its accuracy")
        self.show_travel = QtWidgets.QCheckBox("Travel moves")
        self.show_travel.setToolTip("Moves with the blade raised")
        self.dim = QtWidgets.QCheckBox("Dim artwork")
        self.show_axis.setChecked(bs.show_axis)
        self.show_tip.setChecked(bs.show_tip)
        self.show_travel.setChecked(bs.show_travel)
        self.dim.setChecked(True)
        for w in (self.show_axis, self.show_tip, self.show_travel, self.dim):
            lay.addWidget(w)

        # -- send --
        lay.addWidget(QtWidgets.QLabel("<b>Send</b>"))
        self.dry = QtWidgets.QCheckBox("Dry run (blade up)")
        self.dry.setToolTip("Run the whole job with the blade raised, without cutting")
        lay.addWidget(self.dry)
        self.bar = QtWidgets.QProgressBar()
        self.bar.hide()            # shown while sending
        lay.addWidget(self.bar)

        # -- wiring --
        self.settings_btn.clicked.connect(self.open_settings)
        self.code_btn.clicked.connect(lambda: self.code_toggled.emit(self.code_btn.isChecked()))
        self.gen_btn.clicked.connect(self.generate)
        self.send_btn.clicked.connect(self._send)
        self.pause_btn.toggled.connect(self._pause_toggled)
        self.cancel_btn.clicked.connect(self._cancel)
        self.dry.toggled.connect(self._on_dry)
        self.sim_offset.valueChanged.connect(self._stale)
        for w in (self.show_axis, self.show_tip, self.show_travel, self.dim):
            w.toggled.connect(self._push_preview)
        self.set_sender(sender)
        self.cut_settings_changed()
        self._show_summary()

    # -- host hooks (main window) --
    def refresh(self):
        pass

    def set_code_active(self, shown):
        if self.code_btn.isChecked() != shown:
            self.code_btn.blockSignals(True)
            self.code_btn.setChecked(shown)
            self.code_btn.blockSignals(False)

    def set_doc(self, doc):
        """A new document: anything generated belongs to the old one."""
        if doc is self.doc:
            return
        self.doc = doc
        self._generated = False
        self._fingerprint = None
        self._ops = self._steps = self._preview = None
        self.send_btn.setEnabled(False)
        self.code.set_code("")
        if self.canvas is not None:
            self.canvas.clear_toolpath_preview()
        self.cut_settings_changed()
        self._show_summary()

    def set_sender(self, sender):
        if self._thread is not None and sender is not self.sender:
            self.sender.cancel()
        self.sender = sender

    def set_shown(self, shown):
        """Panel shown/hidden: the canvas overlay only shows while it is open."""
        self._shown = bool(shown)
        if not self._shown:
            if self.canvas is not None:
                self.canvas.clear_toolpath_preview()
        else:
            self._push_preview()

    def shutdown(self):
        """App closing: stop a running send."""
        if self._thread is not None:
            self.sender.cancel()
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None

    # -- settings --
    def _settings(self):
        """The Cut Settings panel's settings (doc.blade) plus this panel's preview options."""
        return replace(self.doc.blade, sim_offset_mm=self.sim_offset.value(),
                       show_axis=self.show_axis.isChecked(), show_tip=self.show_tip.isChecked(),
                       show_travel=self.show_travel.isChecked())

    def _comp_offset(self):
        bs = self.doc.blade
        return {"off": None, "firmware": 0.50, "custom": bs.offset_mm}.get(bs.mode)

    def cut_settings_changed(self):
        """Called by the main window when the Cut Settings panel edits doc.blade."""
        self._sync_sim_offset()
        self._update_legend()
        self._stale()

    def _sync_sim_offset(self):
        """Keep the simulated blade equal to the compensation offset unless the user changed it."""
        new = self._comp_offset()
        old = self._last_comp_offset
        if new is not None:
            if old is None or abs(self.sim_offset.value() - old) < 1e-9:
                self.sim_offset.blockSignals(True)
                self.sim_offset.setValue(new)
                self.sim_offset.blockSignals(False)
            self._last_comp_offset = new

    def _update_legend(self):
        tol = self._settings().display_tolerance_mm()
        sw = lambda k, t: '<span style="color:%s">&#9632;</span> %s' % (PREVIEW_COLORS[k], t)
        self.code.set_legend("<br>".join([
            sw("drawn", "Drawn path"), sw("axis", "Holder path"), sw("travel", "Travel (blade up)"),
            sw("lead", "Lead-in"),
            sw("ok", "Blade tip within %.3f mm" % (tol / 2)), sw("warn", "Blade tip within %.3f mm" % tol),
            sw("bad", "Blade tip off by more than %.3f mm" % tol)]))

    def _doc_fingerprint(self):
        return json.dumps(self.doc.to_dict(), sort_keys=True, default=str)

    # -- generate --
    def generate(self):
        bs = self._settings()
        self.doc.blade = bs
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            ops, preview = compensate_ops(plan_ops_flat(self.doc), self.doc, bs)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self._ops, self._preview = ops, preview
        self._steps = ops_to_steps(ops, self.doc, dry_run=self.dry.isChecked())
        self.code.set_code(ops_to_gcode(ops, self.doc))
        self._generated = True
        self._fingerprint = self._doc_fingerprint()
        self._show_stats()
        self._push_preview()
        self._update_legend()
        self.send_btn.setEnabled(self._thread is None)
        self.generated.emit()

    def _mode_text(self):
        bs = self.doc.blade
        return {"off": "off", "firmware": "machine setting (0.50 mm)",
                "custom": "%.2f mm" % bs.offset_mm}.get(bs.mode, bs.mode)

    def _show_summary(self, stale=False, msg=None):
        """Short status in the panel. The details are in the code preview."""
        if msg is not None:
            self.summary.setText(msg)
        elif self._ops is None:
            self.summary.setText(STALE if self._fingerprint is not None else
                                 '<span style="color:#9a9a9a">Not generated yet.</span>')
        else:
            st = stats(self._ops)
            text = "%d cuts, %.0f mm total &nbsp;\u00b7&nbsp; compensation %s%s" % (
                st["cuts"], st["cut_mm"], self._mode_text(), " (dry run)" if self.dry.isChecked() else "")
            if stale:
                text = STALE + "<br>" + text
            self.summary.setText(text)

    def _show_stats(self, stale=False):
        self._show_summary(stale)
        if self._ops is None or self._preview is None:
            self.code.set_stats(STALE)
            return
        st, ps = stats(self._ops), self._preview.stats
        bs = self.doc.blade
        lines = []
        if stale:
            lines.append(STALE)
        lines.append("<b>Compensation:</b> %s &nbsp;\u00b7&nbsp; %d moves, %d pivots, %d lead-ins"
                     % (self._mode_text(), len(self._steps), ps.get("arcs", 0), ps.get("leadins", 0)))
        lines.append("<b>Cut:</b> %.1f mm in %d cuts, %d blade downs, %d pauses%s" % (
            st["cut_mm"], st["cuts"], st["downs"], st["pauses"], " (dry run)" if self.dry.isChecked() else ""))
        if bs.sim_offset_mm > 0:
            worst, raw = ps.get("worst", 0.0), ps.get("worst_raw", 0.0)
            tol = self._preview.tol_mm
            col = "#2e7d32" if worst <= tol / 2 else ("#f9a825" if worst <= tol else "#e53935")
            lines.append('<b>Simulated cut</b> (blade %.2f mm): worst <b style="color:%s">%.3f mm</b>, '
                         'mean %.3f mm%s' % (bs.sim_offset_mm, col, worst, ps.get("mean", 0.0),
                                             "" if bs.mode == "off" else
                                             " &nbsp;(uncompensated: %.3f mm)" % raw))
        self.code.set_stats("<br>".join(lines))

    def _push_preview(self, *a):
        if self.canvas is None or self._preview is None or not self._shown:
            return
        self.canvas.set_toolpath_preview(self._preview, dict(
            axis=self.show_axis.isChecked(), tip=self.show_tip.isChecked(),
            travel=self.show_travel.isChecked(), dim=self.dim.isChecked()))

    def _stale(self, *a):
        if self._generated:
            self._generated = False
            self.send_btn.setEnabled(False)
            self._show_stats(stale=True)

    def check_document(self):
        """Called by the main window after edits: invalidate code generated from an older drawing."""
        if self._generated and self._fingerprint != self._doc_fingerprint():
            self._stale()
            self._ops = self._steps = self._preview = None
            self._show_stats()
            if self.canvas is not None:
                self.canvas.clear_toolpath_preview()

    def _on_dry(self, *a):
        if self._ops is not None:
            self._steps = ops_to_steps(self._ops, self.doc, dry_run=self.dry.isChecked())
            self._show_stats(stale=not self._generated)

    # -- send --
    @property
    def sending(self):
        return self._thread is not None

    def _send(self):
        if not self._generated:
            return
        why = self.device_busy()
        if why:
            QtWidgets.QMessageBox.information(self.window(), "Send",
                                              wait_message(why) + ".")
            return
        self.send_btn.setEnabled(False)
        self.gen_btn.setEnabled(False)
        self._set_paused(False)
        self.pause_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.bar.setValue(0)
        self.bar.show()
        self._thread = QtCore.QThread(self)
        self._worker = SendWorker(self.sender, self._steps)
        self._worker.moveToThread(self._thread)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_done)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def _on_progress(self, f, msg):
        self.bar.setValue(int(f * 100))
        self._show_summary(msg=msg)
        self._set_paused(bool(getattr(self.sender, "paused", False)))

    def _on_done(self, res):
        self.bar.hide()
        err = res.get("error", "")
        if err:
            QtWidgets.QMessageBox.warning(self.window(), "Job Stopped",
                                          "The job stopped because of an error.\n\n%s" % err)
        else:
            QtWidgets.QMessageBox.information(
                self.window(), "Job Canceled" if res.get("cancelled") else "Job Complete",
                "The job was canceled." if res.get("cancelled") else "The job is complete.")
        if self._thread:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
        self.gen_btn.setEnabled(True)
        self.send_btn.setEnabled(self._generated)
        self._set_paused(False)
        self.pause_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        if self._ops is not None:
            self._show_stats(stale=not self._generated)
        else:
            self._show_summary()

    def _set_paused(self, paused):
        """Show the pause button's state without acting on it."""
        self.pause_btn.blockSignals(True)
        self.pause_btn.setChecked(paused)
        self.pause_btn.blockSignals(False)
        self.pause_btn.setToolTip("Resume" if paused else "Pause")

    def _pause_toggled(self, paused):
        if paused:
            self.sender.pause_for_tool()
            self._show_summary(msg="Pausing after the current move\u2026")
        else:
            self.sender.resume()
        self.pause_btn.setToolTip("Resume" if paused else "Pause")

    def _cancel(self):
        self.sender.cancel()
