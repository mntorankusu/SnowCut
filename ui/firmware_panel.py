"""The two firmware buttons of the Machine Settings dialog (device/firmware_update.py):
Upload Firmware… (copy a .upf to the machine's USB drive, renaming old ones) and
Install Firmware… (arm the Updater and restart into it).

Both go over the SnowCut USB connection, which the original firmware does not have. When the
machine was found to run the original firmware (tools.firmware == STOCK) the buttons become
Save Firmware to USB Drive… (add the SnowCut features to an original .upf and save the copy,
meant for the top folder of a USB flash drive) and How to Install… (the machine's own update
steps). Nothing is sent over the cable.

The jobs run in a background thread. This panel polls it with a timer, shows progress, and tells
the dialog (busyChanged) so it can lock the rest while the machine is in use. The confirmation
and file-chooser steps are methods so tests can replace them. FirmwareDialog is the panel on its
own (the start-up offer for a machine on the original firmware).
"""
import os
from PySide6 import QtWidgets, QtCore
from device import firmware_update as fw


class FirmwarePanel(QtWidgets.QGroupBox):
    busyChanged = QtCore.Signal(bool)

    def __init__(self, tools, device_busy=None, parent=None):
        super().__init__("Firmware", parent)
        self.tools = tools
        self._running = False
        self.device_busy = device_busy or (lambda: None)
        lay = QtWidgets.QGridLayout(self)
        self.upload_btn = QtWidgets.QPushButton()
        self.arm_btn = QtWidgets.QPushButton()
        self.hint = QtWidgets.QLabel(
            "This machine is running the original Brother firmware, so firmware cannot be sent to it "
            "over the USB cable. Save the firmware to a USB flash drive and install it from "
            "there.")
        self.hint.setWordWrap(True)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.hint, 0, 0, 1, 2)
        lay.addWidget(self.upload_btn, 1, 0)
        lay.addWidget(self.arm_btn, 1, 1)
        lay.addWidget(self.progress, 2, 0, 1, 2)
        lay.addWidget(self.status, 3, 0, 1, 2)
        self.upload_btn.clicked.connect(self.upload)
        self.arm_btn.clicked.connect(self.arm)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self._sync_mode()

    @property
    def busy(self):
        return self.tools.busy

    @property
    def stock(self):
        return self.tools.stock

    def set_firmware(self, kind):
        """What the machine was found to run (fw.SNOWCUT / fw.STOCK)."""
        self.tools.firmware = kind
        self._sync_mode()

    def _sync_mode(self):
        if self.stock:
            self.upload_btn.setText("Save Firmware to USB Drive…")
            self.upload_btn.setToolTip("Add the SnowCut features to an original firmware file "
                                       "(.upf) and save it for a USB flash drive")
            self.arm_btn.setText("How to Install…")
            self.arm_btn.setToolTip("How the machine installs firmware from a USB flash drive")
        else:
            self.upload_btn.setText("Upload Firmware…")
            self.upload_btn.setToolTip("Copy a firmware file (.upf) to the machine's USB drive")
            self.arm_btn.setText("Install Firmware…")
            self.arm_btn.setToolTip("Restart the machine into its firmware updater")
        self.hint.setVisible(self.stock)

    # -- hooks the tests replace --
    def choose_upf(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose Firmware File", "", "Firmware files (*.upf);;All files (*)")
        return path

    def choose_save(self, suggested):
        """Where to save the patched copy (suggested: a USB drive's top folder if one is in)."""
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Firmware to the Top Folder of a USB Drive", suggested,
            "Firmware files (*.upf)")
        if path and not fw.is_upf(path):
            path += ".upf"
        return path

    def inform(self, title, text):
        QtWidgets.QMessageBox.information(self, title, text)

    def confirm(self, title, text):
        return QtWidgets.QMessageBox.question(
            self, title, text, QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No) == QtWidgets.QMessageBox.Yes

    def ask_patch(self, name, target):
        """True = patch it first, False = upload it unchanged, None = cancel."""
        box = QtWidgets.QMessageBox(
            QtWidgets.QMessageBox.Question, "Original Firmware",
            "%s is the original %s firmware.\n\nAdd the SnowCut features before uploading? "
            "SnowCut needs them for file access, machine settings and scanning. "
            "Your original file will not be changed." % (name, target["name"]), parent=self)
        yes = box.addButton("Add and Upload", QtWidgets.QMessageBox.YesRole)
        no = box.addButton("Upload Unchanged", QtWidgets.QMessageBox.NoRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(yes)
        box.exec()
        return True if box.clickedButton() is yes else False if box.clickedButton() is no else None

    # -- actions --
    def _blocked(self):
        why = self.device_busy()
        if why:
            self.status.setText("The machine is busy (%s). Try again when it has finished." % why)
        return bool(why)

    def upload(self):
        if self.stock:
            return self.save_to_drive()
        if self.busy or self._blocked():
            return False
        path = self.choose_upf()
        if not path:
            return False
        try:
            info = fw.check_upf(path)
        except (ValueError, OSError) as e:
            self.status.setText("Not uploaded: %s" % e)
            return False
        target = fw.stock_target(path)
        patch = False
        if target is not None:
            patch = self.ask_patch(os.path.basename(path), target)
            if patch is None:
                return False
        if not self.confirm(
                "Upload Firmware",
                "Copy %s%s (%d KB) to the machine's USB drive?\n\nOther firmware files on "
                "the drive will be renamed so they are not installed. Nothing will be installed until "
                "you choose Install Firmware."
                % (os.path.basename(path), " with SnowCut features" if patch else "",
                   (info["size"] + 1023) // 1024)):
            return False
        return self._begin(self.tools.start_upload, path, patch)

    def save_to_drive(self):
        """Original firmware: add the SnowCut features to an original .upf and save the copy for
        the top folder of a USB flash drive. Nothing goes over the USB cable."""
        if self.busy:
            return False
        path = self.choose_upf()
        if not path:
            return False
        name = os.path.basename(path)
        try:
            fw.check_upf(path)
        except (ValueError, OSError) as e:
            self.status.setText("Not saved: %s" % e)
            return False
        target = fw.stock_target(path)
        if target is None:
            self.status.setText(
                "Not saved: %s is not an original firmware version SnowCut can add its features "
                "to. Choose the original Brother firmware file. If this file already has the "
                "SnowCut features, copy it to the top folder of a USB flash drive yourself."
                % name)
            return False
        if not self.confirm(
                "Save Firmware to USB Drive",
                "%s is the original %s firmware. SnowCut will add its features to a copy. Your "
                "original file will not be changed.\n\nSave the copy in the top folder of a USB "
                "flash drive (not in a folder on it): the machine's own updater will install it from "
                "there.\n\nChoose where to save it now?" % (name, target["name"])):
            return False
        stem = os.path.splitext(name)[0] + "_snowcut.upf"
        drives = fw.usb_drives()
        out = self.choose_save(os.path.join(drives[0], stem) if drives else stem)
        if not out:
            return False
        if os.path.normcase(os.path.abspath(out)) == os.path.normcase(os.path.abspath(path)):
            self.status.setText("Not saved: choose a different name than the original file.")
            return False
        if not fw.is_drive_root(out) and not self.confirm(
                "Not the Top Folder",
                "The machine's updater only finds firmware in the top folder of a USB flash "
                "drive, and %s is not one. You will have to move the file there yourself.\n\n"
                "Save it here anyway?" % os.path.dirname(os.path.abspath(out))):
            return False
        return self._begin(self.tools.start_save, path, out)

    def install_steps(self):
        """Original firmware: the machine installs from a USB flash drive by itself."""
        self.inform("How to Install",
                    "The machine's own updater installs the firmware from a USB flash drive. "
                    "Save the firmware in the top folder of the drive first (Save Firmware to "
                    "USB Drive).\n\n" + fw.INSTALL_STEPS.replace("To install it", "Then"))
        return True

    def arm(self):
        if self.stock:
            return self.install_steps()
        if self.busy or self._blocked():
            return False
        if not self.confirm(
                "Install Firmware",
                "The machine will restart into its firmware updater. Tap Update on its "
                "screen to install the firmware on its USB drive.\n\nTo leave the updater "
                "without updating, switch the machine off, then switch it on while holding Home "
                "and Setting.\n\nRestart the machine now?"):
            return False
        return self._begin(self.tools.start_arm_reboot)

    def _begin(self, start, *args):
        try:
            started = start(*args)
        except (ValueError, OSError) as e:
            self.status.setText("Not started: %s" % e)
            return False
        if not started:
            return False
        self._running = True
        self.progress.setValue(0)
        self.progress.show()
        self.upload_btn.setEnabled(False)
        self.arm_btn.setEnabled(False)
        self.busyChanged.emit(True)
        self.timer.start()
        return True

    def poll(self):
        if not self._running:
            return
        busy, prog, text, result = self.tools.poll()
        self.progress.setValue(int(prog * 1000))
        if text:
            self.status.setText(text)
        if busy:
            return
        self._running = False
        self.timer.stop()
        self.progress.hide()
        self.upload_btn.setEnabled(True)
        self.arm_btn.setEnabled(True)
        if result is not None:
            ok, msg = result
            self.status.setText(msg if ok else "Failed: %s" % msg)
        self.busyChanged.emit(False)

    def wait(self, timeout=None):
        """Block until the running job finishes, then show its result (tests, shutdown)."""
        self.tools.wait(timeout)
        self.poll()


class FirmwareDialog(QtWidgets.QDialog):
    """The firmware panel on its own, with a Close button (start-up offer)."""

    def __init__(self, tools, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Firmware")
        self.resize(560, 220)
        lay = QtWidgets.QVBoxLayout(self)
        self.panel = FirmwarePanel(tools, parent=self)
        lay.addWidget(self.panel)
        lay.addStretch(1)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        self.buttons.rejected.connect(self.reject)
        self.panel.busyChanged.connect(lambda busy: self.buttons.setEnabled(not busy))
        lay.addWidget(self.buttons)

    def start(self):
        """Run the panel's first button now, and close again if the user canceled it before
        anything happened (nothing to show)."""
        if not self.panel.upload() and not self.panel.status.text():
            self.reject()

    def reject(self):
        if self.panel.busy:
            return                          # the file is being written: wait
        super().reject()
