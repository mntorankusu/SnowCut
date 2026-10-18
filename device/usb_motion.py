"""MOTION (0xC3) streaming with progress/cancel/pause.

The firmware runs the job the way the machine's own cut job does
(tools/vendor_handler.py):
  G0  coordinated travel (lcs_MoveTarget) at speed 9.
  G1  a point on the locus queue: consecutive G1s run back to back, so the
      host streams them while the machine cuts. Feed 0..9 = LCS speed level
      (None -> the machine's own speed setting for the group's tool). A new
      feed waits for the queue to drain.
  M3  lower the tool to the blade-table depth for the pressure (signed -9..9,
      None -> the machine's own pressure setting for the group's tool, and the depth
      is trimmed by the machine's EEPROM).
  Unset ("Machine setting") speed and pressure are read from the machine's
  settings EEPROM (cut 0x14/0x15, draw 0x16/0x17, emboss 0x89/0x8A) once per
  job. The TOOL step before each color group says which pair applies. If the
  EEPROM cannot be read the firmware's own defaults apply (speed level 5, 0).
  M5  raise the tool.
Every command is non-blocking. Anything that would disturb motion in flight
(including G1 on a full queue) is refused with a busy sense and retried here
(memdump.motion_retry). Dry run skips M3/M5 (traverse only).
"""
from __future__ import annotations
import struct
import threading
import time

try:
    import sys, os
    _d = os.path.join(os.path.dirname(__file__), "..", "pc-driver")
    if os.path.isdir(_d):
        sys.path.insert(0, _d)
    from memdump import motion as _fw_motion, _dev as _fw_dev, close_dev as _fw_close  # noqa
    from memdump import friendly_error as _fw_friendly  # noqa
    from memdump import locus_state as _fw_locus_state, motion_retry as _fw_motion_retry  # noqa
    import cm110_eeprom as _eep
    import busy as _busy
    import buttons as _buttons
    HAVE_FW = True
    # _fw_dev raises memdump.DeviceNotFound (an IOError) when the machine is absent.
except Exception:
    HAVE_FW = False

    def _fw_dev():
        raise IOError("USB support is not available in this installation.")
    _fw_friendly = lambda e: str(e)
    _fw_motion = lambda *a, **k: None
    _fw_close = lambda dev: None
    _fw_locus_state = lambda dev: None
    _fw_motion_retry = lambda *a, **k: None
    _eep = None


def pressure_to_z(pres):
    """M3 CDB[10]: signed pressure -9..9 as a byte (None -> 0, table middle)."""
    p = 0 if pres is None else max(-9, min(9, int(round(pres))))
    return p & 0xFF


# The Settings screen stores speed 1..5 and pressure 1..19 (raw 10 = middle, shown -9..+9).
# The firmware's blade/pen/emboss tables turn speed 1..5 into LCS levels 1,3,5,7,8 and
# pressure raw into the signed M3 value raw - 10 (tables at 0x8903a450.., 0x8903a600..).
SPEED_LEVELS = (1, 3, 5, 7, 8)
# tool -> (speed addr, pressure addr, firmware fallback speed, fallback pressure raw)
SETTING_ADDRS = {"cut": (0x14, 0x15, 3, 10), "draw": (0x16, 0x17, 3, 10),
                 "emboss": (0x89, 0x8A, 1, 14)}


def machine_defaults(read):
    """{tool: (LCS speed level, signed pressure)} from the machine's own settings.

    read(addr, n) returns n bytes of the settings EEPROM. A byte outside the range the
    firmware accepts (e.g. erased 0xFF) means it uses its fallback, and so does this function."""
    out = {}
    for tool, (sa, pa, sdef, pdef) in SETTING_ADDRS.items():
        spd, pres = read(sa, 1)[0], read(pa, 1)[0]
        spd = spd if 1 <= spd <= 5 else sdef
        pres = pres if 1 <= pres <= 19 else pdef
        out[tool] = (SPEED_LEVELS[spd - 1], pres - 10)
    return out


class MotionSender:
    """Streams step-ops [(G0,x,y)|(G1,x,y,feed,pres)|(M3,pres)|(M5,)|(PAUSE,color)].

    While a real job is running, the machine shows a modal busy pop-up (busy_msg, a
    pc-driver/busy.py message name or <=14-byte text) so nobody uses its touch
    UI mid-job. At a tool-change PAUSE it shows PAUSE_MSG instead.

    The machine's Start/Stop button acts as play/pause (firmware button hook,
    pc-driver/buttons.py): pressed mid-job it pauses before the next step, and
    pressed while paused (tool change or button pause) it resumes, same as the
    GUI's Resume. Without that firmware it degrades to GUI-only resume.
    """

    PAUSE_MSG = "Change tool"
    USER_PAUSE_MSG = "Paused"
    KEEPALIVE_S = 3.0   # ping while waiting (the pop-up expires after 15 s idle)
    BUTTON_POLL_S = 0.1  # paused: how often to read the Start/Stop button
    RUN_BUTTON_S = 0.25  # running: read the button at most this often
    IDLE_POLL_S = 0.02   # how often to re-read the axis states while waiting
    IDLE_TIMEOUT_S = 120.0   # queue drain / busy retries

    def __init__(self, busy_msg="operating"):
        self.busy_msg = busy_msg
        self._btn = None   # buttons.Buttons while a real job is running
        self._tool = "cut"        # tool of the current color group (TOOL steps)
        self._defaults = {}       # {tool: (speed level, pressure)}, empty -> firmware defaults
        self._cancel = threading.Event()
        self._resume = threading.Event()
        self._resume.set()

    def cancel(self):
        self._cancel.set()
        self._resume.set()

    def pause_for_tool(self):
        self._resume.clear()

    @property
    def paused(self):
        """True while a job is waiting for resume() (GUI pause, tool change or machine button)."""
        return not self._resume.is_set()

    def resume(self):
        self._resume.set()

    def send(self, step_ops, progress_cb=None):
        self._cancel.clear()
        try:
            dev = _fw_dev()
        except Exception as e:
            raise IOError(str(e)) from None
        pop = None
        next_btn = 0.0
        try:
            # The pop-up shares this thread's raw handle, so no keep-alive thread
            # (keepalive=None): motion commands refresh it, _wait_resume pings.
            pop = _busy.Busy(lambda cdb: _busy.send_raw(dev, cdb), self.busy_msg,
                             keepalive=None)
            self._btn = self._open_buttons(dev)
            pop.__enter__()
            self._tool = "cut"
            self._defaults = self._read_defaults(dev, step_ops)
            total = len(step_ops)
            for i, op in enumerate(step_ops):
                if self._cancel.is_set():
                    self._abort(dev)
                    return {"sent": i, "cancelled": True}
                if self._btn is not None and time.time() >= next_btn:
                    next_btn = time.time() + self.RUN_BUTTON_S
                    if self._start_pressed() and self._resume.is_set():
                        self._resume.clear()           # machine Start/Stop: pause
                        if progress_cb:
                            progress_cb(i / max(1, total), "Paused. Press Resume, or "
                                        "Start/Stop on the machine, to continue.")
                # blocks while paused (GUI pause_for_tool or the machine button)
                self._wait_resume(dev, pop, self.USER_PAUSE_MSG)
                if op[0] == "PAUSE":
                    self._wait_idle(dev)              # queued points finish first
                    self._resume.clear()
                    if progress_cb:
                        progress_cb(i / max(1, total), "Paused for a tool change (%s). Press "
                                    "Resume, or Start/Stop on the machine, to continue." % op[1])
                    self._wait_resume(dev, pop, self.PAUSE_MSG)
                    if self._cancel.is_set():
                        self._abort(dev)
                        return {"sent": i, "cancelled": True}
                    continue
                if op[0] == "TOOL":
                    self._tool = op[1]
                    continue
                self._send_one(dev, op)
                if progress_cb:
                    progress_cb((i + 1) / max(1, total),
                                "Running\u2026 %d%%" % (100 * (i + 1) // max(1, total)))
            self._wait_idle(dev)                      # last queued points
            return {"sent": total, "cancelled": False}
        except Exception:
            self._abort(dev)
            raise
        finally:
            self._btn = None
            try:
                if pop is not None:
                    pop.__exit__(None, None, None)
            finally:
                _fw_close(dev)         # release the machine for Scan / Feed / Jog

    def _read_defaults(self, dev, step_ops):
        """The machine's own speed/pressure per tool, if this job leaves any unset."""
        unset = any((op[0] == "G1" and op[3] is None) or (op[0] == "M3" and op[1] is None)
                    for op in step_ops)
        if dev is None or _eep is None or not unset:
            return {}
        try:
            return machine_defaults(lambda a, n: _eep.read(dev, a, n))
        except Exception:
            return {}       # old firmware without the EEPROM opcode: the firmware's defaults

    def _open_buttons(self, dev):
        if dev is None:
            return None
        try:
            return _buttons.Buttons(lambda: _buttons.read_block_raw(dev))
        except Exception:
            return None                 # older firmware: GUI-only resume

    def _start_pressed(self):
        try:
            return self._btn.pressed_since(_buttons.START) > 0
        except Exception:
            self._btn = None            # stop trying, but keep the job running
            return False

    def _wait_resume(self, dev, pop, msg):
        # Wait for resume() or the machine's Start/Stop, and show msg while paused.
        # Button reads are vendor commands, so they also keep the pop-up alive.
        # Without the button hook, a ping is sent instead.
        if self._resume.is_set() or dev is None:
            self._resume.wait()
            return
        if pop is not None:
            pop.update(msg)
        if self._btn is not None:
            self._start_pressed()       # ignore the press that caused the pause
        last_ping = time.time()
        while True:
            period = self.BUTTON_POLL_S if self._btn is not None else self.KEEPALIVE_S
            if self._resume.wait(period):
                break
            if self._btn is not None:
                if self._start_pressed():
                    self._resume.set()
                    break
            elif time.time() - last_ping >= self.KEEPALIVE_S:
                last_ping = time.time()
                try:
                    _busy.ping_raw(dev)
                except Exception:
                    pass
        if pop is not None and not self._cancel.is_set():
            pop.update(self.busy_msg)

    def _wait_idle(self, dev):
        # G1 points run from the machine's queue after the command returns, so
        # wait for XY (and Z) to settle before a pause and at the end of a job.
        deadline = time.time() + self.IDLE_TIMEOUT_S
        while True:
            st = _fw_locus_state(dev)
            busy = [name for k, name, moving in (("head_state", "head", (1, 3, 4)),
                                                  ("mat_state", "mat", (1, 3, 4)),
                                                  ("z_state", "blade", (1, 4))) if st[k] in moving]
            if not busy:
                return
            if time.time() >= deadline:
                raise IOError("The machine did not finish moving (%s still busy after %.0f s). "
                              "Switch it off and on again."
                              % (" and ".join(busy), self.IDLE_TIMEOUT_S))
            time.sleep(self.IDLE_POLL_S)

    def _abort(self, dev):
        """Cancel/error: stop XY now (LSTOP drops queued points), raise the tool."""
        if dev is None:
            return
        for sub in (0x03, 0x41):              # LSTOP, M05
            try:
                _fw_motion_retry(dev, sub, timeout=5.0)
            except Exception:
                pass

    def _send_one(self, dev, op):
        G00, G01, M03, M05 = 0x00, 0x01, 0x40, 0x41
        cmd = lambda sub, **kw: _fw_motion_retry(dev, sub, timeout=self.IDLE_TIMEOUT_S,
                                                 poll=self.IDLE_POLL_S, **kw)
        if op[0] == "G0":
            cmd(G00, X=int(op[1]), Y=int(op[2]))
        elif op[0] == "G1":
            # pressure changes arrive as their own M3 ops (core/gcode.py)
            _, x, y, feed, _pres = op
            if feed is None:
                feed = self._defaults.get(self._tool, (5, 0))[0]
            cmd(G01, X=int(x), Y=int(y), feed=int(feed))
        elif op[0] == "M3":
            pres = op[1]
            if pres is None:
                pres = self._defaults.get(self._tool, (5, 0))[1]
            cmd(M03, Z=pressure_to_z(pres))
        elif op[0] == "M5":
            cmd(M05)
