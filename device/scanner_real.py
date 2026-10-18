"""Real scanner client for the CM110 over USB SCSI/BOT.

The machine's MovState task only acts on event messages, and accepts a scan
(event 10) only in state Ready (2) with a mat loaded. start_scan() therefore
runs the same sequence as the panel, one phase per poll() so the GUI never
blocks:
  init:  state 0 (power-on, never touched): event 5 homes the head (and feeds
          out a mat that is sitting in the rollers) -> Ready
  load:  no mat recognised: event 6 = Load button (feed in + recognise) -> Ready
  scan:  event 10 {mode, submode}: 0xb ScanPreparing -> 0xc Scanning ->
          0xd ScanFinishing (mat return) -> Ready, image left in ScanDstBuff
Any recognised mat kind scans: only the panel's Scan button refuses 24x12 or
unknown mats, and the MovState task never checks the kind.

The default is a JPEG (mode 0: the scan engine encodes it, a few MB) at
dpi 300/200/150/100. mode=1 returns the raw planar RGB buffer (~37 MB, slow).

Vendor sub-opcodes (0xC3): 0x50 SCAN_START, 0x51 SCAN_FREE, 0x52 SCAN_STOP
(event 12), 0x53 MOV_EVENT. Status and image are read with 0xC0.
API: start_scan() / poll() / fetch() / cancel().

USB handle: held only while a scan is running (start_scan until poll() reports done or an
error, or cancel()). fetch() / fetch_data() open their own and close it. Only one handle can
have the machine open, so a handle kept after the scan would lock out Feed, Cut and Jog.
"""
from __future__ import annotations
import os
import sys
import time

from core.userdata import documents_dir, user_dir   # noqa: F401  (documents_dir: old import site)

try:
    _d = os.path.join(os.path.dirname(__file__), "..", "pc-driver")
    if os.path.isdir(_d):
        sys.path.insert(0, _d)
    from memdump import (
        _dev as fw_dev,
        close_dev as fw_close,
        scan_start as fw_scan_start,
        scan_start_jpeg as fw_scan_start_jpeg,
        scan_jpeg_restore as fw_scan_jpeg_restore,
        scan_stop as fw_scan_stop,
        scan_fetch as fw_scan_fetch,
        get_scan_status as fw_scan_status,
        get_matkind as fw_matkind,
        mov_event as fw_mov_event,
        load_custom as fw_load_custom,
        mat_edge_delta as fw_mat_edge_delta,
        scan_window as fw_scan_window,
        scan_max_dpi as fw_scan_max_dpi,
        scan_start_custom as fw_scan_start_custom,
        scan_custom_restore as fw_scan_custom_restore,
        scan_start_y_restore as fw_scan_start_y_restore,
        scan_back_max as fw_scan_back_max,
        require_caps as fw_require_caps,
        mat_snapshot as fw_mat_snapshot,
        set_matkind as fw_set_matkind,
        min_top_margin_mm as fw_min_top_margin_mm,
        STOCK_JPEG_QUALITY,
        dump_region as fw_dump_region,
        OP_RAM, MS_POWERON, MS_READY, MS_SCAN_BUSY, MS_SCAN_FAIL,
        EV_INIT, EV_LOAD, MAT_KINDS,
        friendly_error, DeviceNotFound, ProtocolError, FW_HINT,
    )
    import busy as _busy
    import scsipt as _scsipt
    from memdump import _USB_ERRORS
    HAVE_DRIVER = True
    _LINK_ERRORS = (DeviceNotFound, ProtocolError, _scsipt.PassthroughError) + _USB_ERRORS
except Exception:
    HAVE_DRIVER = False
    _LINK_ERRORS = ()
    STOCK_JPEG_QUALITY = 95
    FW_HINT = "the machine's firmware may need updating"

PHASE_TEXT = {"init": "start-up", "load": "mat load", "scan": "scan"}


def scans_dir():
    """Documents/SnowCut/Scans: scans taken while no saved project is open."""
    return user_dir("Scans")


def scan_output_path(project_path="", ext=".jpg"):
    """Where a new scan is saved: the open project's folder if it has been saved and the
    folder still exists, else Documents/SnowCut/Scans. Timestamped, so a new scan never overwrites an
    image an older project still uses as its background."""
    folder = os.path.dirname(os.path.abspath(project_path)) if project_path else ""
    if not (folder and os.path.isdir(folder)):
        folder = scans_dir()
    base = os.path.join(folder, time.strftime("scan_%Y%m%d_%H%M%S"))
    path, n = base + ext, 1
    while os.path.exists(path):
        path, n = "%s_%d%s" % (base, n, ext), n + 1
    return path


def _pop(dev, msg):
    # Modal busy pop-up on the machine (cosmetic: never fails the scan). The
    # scan's own status polls are vendor commands, so they keep it alive. If
    # the app exits unexpectedly, it expires by itself 15 s after the last command.
    if dev is not None:
        _busy.try_set_busy(dev, msg)


class RealScanner:
    STATE_IDLE = "idle"
    STATE_BUSY = "busy"
    STATE_DONE = "done"
    STATE_ERROR = "error"

    ACCEPT_TIMEOUT = 5.0     # s for the MovState task to leave the state a request was posted in
    PHASE_TIMEOUT = {"init": 60.0, "load": 90.0, "scan": 90.0}
    LONG_SCAN_TIMEOUT = 200.0  # a 12x24 scan feeds twice as far
    PHASE_SPAN = {"init": (0.0, 0.05), "load": (0.05, 0.25), "scan": (0.25, 1.0)}

    def __init__(self, dev=None):
        self.dev = dev
        self._borrowed = dev is not None   # a handle passed in belongs to the caller
        self.state = self.STATE_IDLE
        self.progress = 0.0
        self.jpeg_path = ""
        self.override_path = ""
        self.last_error = ""
        self.scan_cal = None       # core.machine.ScanCal read from the unit after fetch()
        self.mat_kind = 0
        self._est_duration = 15.0  # seconds typical scan duration
        self._phase = ""
        self._phase_t0 = 0.0
        self._left = False         # MovState has left the state the request was posted in
        self._posted_in = None
        self._args = (None, 0, 0)
        self._dpi = 300
        self.scan_dpi = 300        # dpi actually requested (24" mats cap it, see _start_scan)
        self._custom_load = None
        self._window = None        # area-7 scan rectangle for a 24" mat, else None
        self._custom = None        # (window, raw, quality, sampling) for a custom scan
        self._custom_token = None  # memdump.scan_start_custom restore token
        self._scan_info = None     # scan status (ptr, len, R/G/B offsets) when done
        self.result_meta = None    # {fmt, dpi, window, width, lines, col0, row0} of the scan
        self._saved_sel = None     # panel ScanToUSB area/resolution to restore after a JPEG scan
        self._image_ptr = 0
        self._image_len = 0

    def start_scan(self, mode=None, submode=0, mat=0, load=True, dpi=300, custom_load=None,
                   window=None, raw=False, quality=STOCK_JPEG_QUALITY, sampling=0):
        """Begin the init/load/scan sequence. mode None/0 = JPEG at `dpi`, 1 = raw.
        `mat` != 0 also overwrites the recognised mat kind (not needed to scan).
        custom_load = (mat kind, feed depth mm, printed->cut Y scale): load a missing mat with
        0xC3/0x54 (no recognition, Y origin on the grid) instead of the panel's Load.
        The JPEG window follows the loaded mat kind: a 24" kind scans the whole 12x24.
        window = (x0, x1, y0, y1) in 0.01 mm scans just that box (core.scan), raw = 8-bit RGB
        instead of JPEG, and quality / sampling = JPEG quality and sampling mode (0xC3/0x55).
        The window may reach past the stock frame (core.scan.scan_request): left/right of it
        uses the full sensor, above it starts the mat less fed in."""
        if not HAVE_DRIVER:
            raise RuntimeError("USB support is not available in this installation.")
        self.state = self.STATE_BUSY
        self.progress = 0.0
        self.last_error = ""
        self._image_ptr = 0
        self._image_len = 0
        self._args = (mode, submode, mat)
        self._load = load
        self._dpi = dpi
        self._custom_load = custom_load
        custom = window is not None or raw or quality != STOCK_JPEG_QUALITY or sampling
        self._custom = (window, raw, quality, sampling) if custom and not mode else None
        self._scan_info = None
        self.result_meta = None

        try:
            self._drop_dev()                 # never reuse a handle from an earlier operation
            self._open()
            _pop(self.dev, "scanning")
            info = fw_scan_status(self.dev)
            ms = info["movstate"]
            if ms == MS_POWERON:
                self._post("init", lambda: fw_mov_event(self.dev, EV_INIT), ms)
            elif ms in MS_SCAN_FAIL:
                self._post("init", lambda: fw_scan_stop(self.dev), ms)
            elif ms != MS_READY:
                raise IOError("The machine is busy (%s). Wait until it has finished, then "
                              "scan again." % info["state_name"])
            else:
                self._after_ready()
        except Exception as e:
            self._fail(e)
            raise IOError(self.last_error) from None

    # -- phases --
    def _post(self, phase, send, from_state):
        st = send()
        if st != 0:
            raise IOError("The machine refused the %s request (status %d)."
                          % (PHASE_TEXT[phase], st))
        self._phase, self._phase_t0 = phase, time.time()
        self._posted_in, self._left = from_state, False

    def _after_ready(self):
        self.mat_kind = fw_matkind(self.dev)
        if self.mat_kind == 0 and self._load:
            if self._custom_load:
                kind, margin, scale = self._custom_load
                fw_require_caps(self.dev, "load_custom")
                if margin < fw_min_top_margin_mm(scale) - 1e-6:
                    raise IOError("A feed depth under %.1f mm cannot be loaded."
                                  % fw_min_top_margin_mm(scale))
                delta = fw_mat_edge_delta(margin, scale)
                self._post("load", lambda: fw_load_custom(self.dev, kind, delta), MS_READY)
            else:
                self._post("load", lambda: fw_mov_event(self.dev, EV_LOAD), MS_READY)
        else:
            self._start_scan()

    def _start_scan(self):
        if self.mat_kind == 0 and not self._args[2]:
            raise IOError("No mat is loaded. Put the mat against the feed rollers and scan again.")
        mode, submode, mat = self._args
        self._window = None
        if not fw_scan_start_y_restore(self.dev):      # left shifted by an interrupted wide scan
            raise IOError("The machine is still busy putting the mat back from the last scan. "
                          "Wait a moment and scan again.")
        if mode:
            send = lambda: fw_scan_start(self.dev, mode=mode, submode=submode, mat=mat)
        elif self._custom:
            from core import scan as scanlib
            window, raw, quality, sampling = self._custom
            window = window or scanlib.kind_window(mat or self.mat_kind)
            back_max = fw_scan_back_max(self.dev)          # the loaded mat's real edge (c44)
            top = scanlib.max_dpi(window, back_max)
            if top is None:
                raise IOError("The scan area is too large for the machine's scan memory.")
            self.scan_dpi = min(self._dpi, top)
            req = scanlib.scan_request(window, self.scan_dpi, back_max)
            short = scanlib.scan_request(window, self.scan_dpi)["back"] - req["back"]
            if short > 4 * (1800 // self.scan_dpi):          # more than the line rounding
                raise IOError("The scan area starts %.1f mm higher than the loaded mat can be "
                              "backed out (its leading edge would leave the feed rollers). Move "
                              "the area down, or reload the mat with this profile's feed depth."
                              % (short * 25.4 / 1800))
            w, h, c0, r0 = req["width"], req["lines"], req["col0"], req["row0"]
            rect, wide, back = req["rect"], req["wide"], req["back"]
            self._window = window
            self._est_duration = max(8.0, 15.0 * h / 3529.0 * self.scan_dpi / 300.0 + 5.0)
            self.result_meta = {"fmt": "raw" if raw else "jpeg", "dpi": self.scan_dpi,
                                "window": window, "width": w, "lines": h, "col0": c0, "row0": r0}

            def send():
                st, self._custom_token = fw_scan_start_custom(
                    self.dev, self.scan_dpi, rect, raw=raw, quality=quality, sampling=sampling,
                    wide=wide, start_back=back)
                return st
        else:
            self._window = fw_scan_window(mat or self.mat_kind)
            self.scan_dpi = min(self._dpi, fw_scan_max_dpi(self._window))
            self._est_duration = 30.0 if self._window else 15.0

            def send():
                st, self._saved_sel = fw_scan_start_jpeg(self.dev, dpi=self.scan_dpi, mat=mat,
                                                         window=self._window)
                return st
        self._post("scan", send, MS_READY)

    def _restore_selection(self):
        if self._custom_token is not None and self.dev is not None:
            try:
                fw_scan_custom_restore(self.dev, self._custom_token)
            except Exception:
                pass
            self._custom_token = None
        if self._saved_sel is not None and self.dev is not None:
            try:
                fw_scan_jpeg_restore(self.dev, self._saved_sel)
            except Exception:
                pass
            self._saved_sel = None

    def _fail(self, err):
        """Record a failure. `err` is a message or an exception. A broken USB link also
        drops the device handle so the next scan reconnects (e.g. after a replug/restart)."""
        self.state = self.STATE_ERROR
        self.last_error = friendly_error(err) if isinstance(err, Exception) else err
        if isinstance(err, _LINK_ERRORS):
            self._saved_sel = None          # cannot restore over a broken link
            self._custom_token = None
        else:
            self._restore_selection()
            _pop(self.dev, None)
        self._drop_dev()

    def _open(self):
        if self.dev is None:
            self.dev = fw_dev()

    def _drop_dev(self):
        """Release the USB handle (the machine is free for other operations)."""
        if self.dev is not None and not self._borrowed:
            fw_close(self.dev)
            self.dev = None

    def _set_progress(self, frac):
        lo, hi = self.PHASE_SPAN[self._phase]
        self.progress = max(self.progress, lo + (hi - lo) * min(1.0, frac))

    def poll(self):
        """Returns (busy: bool, progress: 0..1)."""
        if self.state != self.STATE_BUSY:
            return (False, 1.0 if self.state == self.STATE_DONE else 0.0)
        if not HAVE_DRIVER or self.dev is None:
            self.state = self.STATE_ERROR
            return (False, 0.0)

        try:
            info = fw_scan_status(self.dev)
            ms = info["movstate"]
            el = time.time() - self._phase_t0
            if ms != self._posted_in:
                self._left = True
            if not self._left:
                if el > self.ACCEPT_TIMEOUT:
                    raise IOError("The machine did not start the %s. Check its screen for a "
                                  "message. Otherwise, %s." % (PHASE_TEXT[self._phase], FW_HINT))
                return (True, self.progress)
            limit = self.PHASE_TIMEOUT[self._phase]
            if self._phase == "scan" and getattr(self, "_window", None):
                limit = self.LONG_SCAN_TIMEOUT
            if el > limit:
                raise IOError("The %s took too long (the machine is still in %s)."
                              % (PHASE_TEXT[self._phase], info["state_name"]))

            if ms in MS_SCAN_FAIL:              # the scan, or load's recognition scan
                fw_scan_stop(self.dev)          # event 12: return the mat to Ready
                raise IOError("The scanner reported an error during the %s, and the mat is being "
                              "returned. Check that the mat is inserted straight and try again."
                              % PHASE_TEXT[self._phase])
            if ms != MS_READY:
                if self._phase == "scan":
                    self._set_progress({0x0b: 0.02, 0x0d: 0.95}.get(
                        ms, 0.05 + 0.85 * el / self._est_duration) if ms in MS_SCAN_BUSY else 0.0)
                else:
                    self._set_progress(el / 20.0)
                return (True, self.progress)

            # back in Ready: the phase is complete
            if self._phase in ("init", "load"):
                if self._phase == "load":
                    self.mat_kind = fw_matkind(self.dev)
                    if self.mat_kind == 0:
                        raise IOError("The mat load finished but no mat was detected. Put the "
                                      "mat against the feed rollers and scan again.")
                    if self._custom_load and fw_mat_snapshot(self.dev)["y"] is None:
                        fw_set_matkind(self.dev, 0)     # nothing was fed in: there really is no mat
                        raise IOError("The mat was not fed in: the mat sensor already saw the mat "
                                      "when the load started. Pull the mat back so it only "
                                      "touches the feed rollers, then scan again.")
                    if self._custom_load and self.mat_kind != self._custom_load[0]:
                        raise IOError("The mat loaded as the wrong type, so %s." % FW_HINT)
                    self._start_scan()
                else:
                    self._after_ready()
                return (True, self.progress)
            if not (info["ptr"] and info["len"]):
                raise IOError("The scan finished but the machine returned no image.")
            self._image_ptr, self._image_len = info["ptr"], info["len"]
            self._scan_info = info
            self._restore_selection()
            self._drop_dev()                    # fetch / fetch_data open their own
            self.state = self.STATE_DONE
            self.progress = 1.0
            return (False, 1.0)
        except Exception as e:
            self._fail(e)
            return (False, 0.0)

    def fetch(self, out_path=None):
        if self.state != self.STATE_DONE:
            if self.last_error:
                raise RuntimeError("Scan failed: %s" % self.last_error)
            raise RuntimeError("Scan not complete yet")

        if not self._image_ptr or not self._image_len:
            raise RuntimeError("No image data available from scan")

        target = out_path or self.override_path
        if not target:
            target = scan_output_path()   # never relative to the cwd (e.g. System32)

        try:
            self._open()
        except Exception as e:
            raise RuntimeError("Could not connect to the machine to download the scan: %s"
                               % friendly_error(e)) from None
        _pop(self.dev, "retrieving")
        try:
            fw_scan_fetch(self.dev, self._image_ptr, self._image_len, out=target)   # also frees
            self.jpeg_path = target
            self.scan_cal = self.read_scan_cal()
        except OSError as e:
            if isinstance(e, _LINK_ERRORS):
                self._fail(e)
                raise RuntimeError("Downloading the scan failed: %s" % self.last_error) from None
            raise RuntimeError("Could not save the scan to %s: %s" % (target, e.strerror or e)) from None
        except Exception as e:
            self._fail(e)
            raise RuntimeError("Downloading the scan failed: %s" % self.last_error) from None
        finally:
            _pop(self.dev, None)
            self._drop_dev()

        return self.jpeg_path

    def fetch_data(self, progress_cb=None):
        """Download the finished scan (blocking, safe to call from a worker thread) and free the
        machine's buffer. Returns (bytes, meta): JPEG bytes, or the raw RGB buffer with
        meta["offsets"] = R/G/B byte offsets. meta also has dpi / width / lines / col0 / row0."""
        if self.state != self.STATE_DONE:
            raise RuntimeError("Scan failed: %s" % self.last_error if self.last_error
                               else "Scan not complete yet")
        try:
            self._open()
        except Exception as e:
            raise RuntimeError("Could not connect to the machine to download the scan: %s"
                               % friendly_error(e)) from None
        meta = dict(self.result_meta or {"fmt": "jpeg", "dpi": self.scan_dpi, "col0": 0, "row0": 0})
        info = self._scan_info or {}
        meta["offsets"] = (info.get("roff", 0), info.get("goff", 0), info.get("boff", 0))
        _pop(self.dev, "retrieving")
        try:
            data = fw_scan_fetch(self.dev, self._image_ptr, self._image_len, out=None,
                                 progress_cb=progress_cb)      # also frees the buffer
            self.scan_cal = self.read_scan_cal()
        except Exception as e:
            self._fail(e)
            raise RuntimeError("Downloading the scan failed: %s" % self.last_error) from None
        finally:
            _pop(self.dev, None)
            self._drop_dev()
        return data, meta

    def read_scan_cal(self):
        """The unit's scan->cut adjustment (RAM copy of EEPROM 0x110-0x11c) as core.machine.ScanCal, or None."""
        from core.machine import ScanCal, SCAN_CAL_ADDR
        try:
            return ScanCal.from_ram(fw_dump_region(OP_RAM, SCAN_CAL_ADDR, 32, dev=self.dev))
        except Exception as e:
            self.last_error = "scan calibration read failed: %s" % e
            return None

    def cancel(self):
        if self.state == self.STATE_BUSY and self.dev:
            if self._phase == "scan":
                try:
                    fw_scan_stop(self.dev)      # event 12 (the firmware ignores it outside a scan)
                except Exception:
                    pass
            self._restore_selection()
            _pop(self.dev, None)
            self.state = self.STATE_IDLE
        self._drop_dev()
