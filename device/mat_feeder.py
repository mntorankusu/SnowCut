"""The app's Feed button: load or unload the mat, in a background thread.

Load is the machine's own mat load run through 0xC3/0x54 LOAD-CUSTOM (pc-driver memdump
mat_load_custom): the mat is fed in and aligned on the mat edge sensor exactly like the
panel's Load button, but the mat-recognition scan is skipped, so mats the scanner does not
recognise (custom / third-party) are accepted. The machine is told the active mat profile's
mat kind, and the Y origin is moved for the profile's feed depth (core.mat: the top margin,
raised by whole grid units to at least 32 mm). The panel's own Load button
is unchanged (it still recognises the stock mats).

Feed with a mat loaded unloads it (event 7), like the panel's button.
API (poll from a QTimer): start(kind, top_margin_mm, scale_y) / poll() -> (busy, message).
"""
from __future__ import annotations
import threading

try:
    import os
    import sys
    _d = os.path.join(os.path.dirname(__file__), "..", "pc-driver")
    if os.path.isdir(_d):
        sys.path.insert(0, _d)
    from memdump import (_dev as fw_dev, close_dev as fw_close, get_matkind as fw_matkind,
                         get_movstate as fw_movstate, mat_load_custom as fw_load,
                         mat_unload as fw_unload, set_matkind as fw_set_matkind,
                         friendly_error, MAT_KINDS, MS_READY)
    import busy as _busy
    HAVE_DRIVER = True
except Exception:
    HAVE_DRIVER = False
    MAT_KINDS = {}
    friendly_error = str


class MatFeeder:
    def __init__(self):
        self._thread = None
        self._result = None          # (ok: bool, message)
        self.action = ""             # "load" / "unload" while busy
        self.detail = ""             # latest progress line from the driver (MovState etc.)

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, kind, top_margin_mm, scale_y=1.0):
        """Load (no mat) or unload (mat loaded). Returns False if already busy."""
        if not HAVE_DRIVER:
            raise RuntimeError("USB support is not available in this installation.")
        if self.busy:
            return False
        self._result = None
        self.detail = ""
        self.action = "feed"
        self._thread = threading.Thread(target=self._run, args=(kind, top_margin_mm, scale_y),
                                        daemon=True)
        self._thread.start()
        return True

    def _run(self, kind, margin, scale_y):
        dev = None
        try:
            dev = fw_dev()
            if fw_movstate(dev) == MS_READY and fw_matkind(dev) != 0:
                self.action = "unload"
                _busy.try_set_busy(dev, "unloading")
                fw_unload(dev)
                self._result = (True, "Mat unloaded")
            else:
                self.action = "load"
                _busy.try_set_busy(dev, "loading")
                got = fw_load(dev, kind, margin, scale_y,
                              log=lambda s: setattr(self, "detail", s.strip()))
                self._result = (True, "Mat loaded (%s, feed depth %.1f mm)"
                                % (MAT_KINDS.get(got, "type %d" % got), margin))
        except Exception as e:
            self._result = (False, "Feed failed: %s" % friendly_error(e))
        finally:
            if dev is not None:
                _busy.try_set_busy(dev, None)
                try:
                    fw_close(dev)
                except Exception:
                    pass

    def poll(self):
        """(busy, message): message is the outcome once, after the thread has finished."""
        if self.busy:
            return True, None
        r, self._result = self._result, None
        return False, (r[1] if r else None)


def send_mat_kind(kind):
    """Tell the machine this mat kind is loaded, right now (0xC3/0x10, RAM only).
    Returns the message for the status bar."""
    if not HAVE_DRIVER:
        raise RuntimeError("USB support is not available in this installation.")
    dev = fw_dev()
    try:
        st = fw_set_matkind(dev, kind)
        if st != 0:
            raise IOError("the machine did not accept the mat type (error %d)" % st)
        return "Mat type set to %s" % MAT_KINDS.get(kind, "type %d" % kind)
    finally:
        fw_close(dev)
