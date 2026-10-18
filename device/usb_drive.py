"""The app's Mount button: the machine's USB drive as a Windows drive letter (WinFsp).

pc-driver winfsp_mount.DriveMount does the work, and this module wraps it in a background thread (the
machine-side mount can take seconds, unmount saves pending files) with the MatFeeder-style
poll API for a QTimer. While mounted, the mount owns the machine's only USB handle, so the
main window's _device_busy() reports DRIVE_BUSY and Scan / Feed / Cut / Jog wait for unmount.

API: start() / stop() -> bool (False while busy), poll() -> [(is_error, message)], state,
letter, pending_files(), shutdown() (blocking stop, for app exit).
"""
from __future__ import annotations
import threading

try:
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc-driver"))
    import winfsp_mount as _wm
    from memdump import friendly_error
    HAVE_DRIVER = True
except Exception:
    _wm = None
    HAVE_DRIVER = False
    friendly_error = str

WINFSP_URL = "https://winfsp.dev/rel/"
DRIVE_BUSY = "USB drive"            # _device_busy() reason while mounted / mounting
IDLE, MOUNTING, MOUNTED, UNMOUNTING = "idle", "mounting", "mounted", "unmounting"


def wait_message(why):
    """Status text for "the machine is in use by `why`"."""
    if why == DRIVE_BUSY:
        return "Unmount the machine's USB drive first"
    return "Wait until the %s has finished" % why


def winfsp_status():
    """(ok, reason): WinFsp installed and loadable."""
    if not HAVE_DRIVER:
        return False, "USB support is not available in this installation."
    return _wm.winfsp_available()


def _plain(e):
    if HAVE_DRIVER and isinstance(e, _wm.WinFspMissing):
        return str(e)
    return friendly_error(e)


def _default_mount(**kw):
    if not HAVE_DRIVER:
        raise RuntimeError("USB support is not available in this installation.")
    return _wm.DriveMount(**kw)


class UsbDrive:
    def __init__(self, mount_factory=None):
        self._factory = mount_factory or _default_mount
        self._mount = None
        self._thread = None
        self._msgs = []
        self._lock = threading.Lock()
        self.state = IDLE
        self.letter = None

    def _say(self, text, error=False):
        with self._lock:
            self._msgs.append((error, text))

    @property
    def busy(self):
        """Mounted, or on the way in or out: the machine's USB handle is taken."""
        return self.state != IDLE

    def start(self):
        if self.state != IDLE:
            return False
        self.state = MOUNTING
        self._thread = threading.Thread(target=self._run_start, daemon=True)
        self._thread.start()
        return True

    def _run_start(self):
        try:
            m = self._factory(on_error=lambda path, e: self._say(
                                  "Could not save %s to the machine: %s" % (path, _plain(e)),
                                  error=True),
                              on_lost=self._lost)
            self.letter = m.start()
            self._mount = m
            self.state = MOUNTED
            self._say("USB drive mounted as %s:" % self.letter)
        except Exception as e:
            self._mount, self.letter = None, None
            self._say("Could not mount the USB drive: %s" % _plain(e), error=True)
            self.state = IDLE              # after the message: the poll timer stops at IDLE

    def _lost(self, msg):
        self._say("%s. Unmounting the USB drive." % msg, error=True)
        self.stop()

    def pending_files(self):
        m = self._mount
        return m.pending_files() if m is not None else []

    def stop(self):
        if self.state != MOUNTED:
            return False
        self.state = UNMOUNTING
        self._thread = threading.Thread(target=self._run_stop, daemon=True)
        self._thread.start()
        return True

    def _run_stop(self):
        m, self._mount = self._mount, None
        letter = self.letter
        failed = []
        try:
            failed = m.stop() if m is not None else []
        except Exception as e:
            self._say("Unmount problem: %s" % _plain(e), error=True)
        if failed:
            self._say("USB drive unmounted, but %d file(s) were NOT saved: %s"
                      % (len(failed), ", ".join(failed)), error=True)
        else:
            self._say("USB drive %s: unmounted" % letter if letter else "USB drive unmounted")
        self.letter = None
        self.state = IDLE

    def shutdown(self, timeout=60.0):
        """Blocking: unmount (waiting for a mount in progress first). -> files not saved."""
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        m, self._mount = self._mount, None
        self.state, self.letter = IDLE, None
        return m.stop() if m is not None else []

    def poll(self):
        with self._lock:
            out, self._msgs = self._msgs, []
        return out
