"""Machine Settings > Upload firmware / Arm updater and reboot.

Upload: copy a .upf to the root of the machine's USB drive (A:) over the 0xC4 file gateway.
Every .upf already in the root is renamed first ("X.upf" -> "X.upf.old", or "X.upf.old2",
"X.upf.old3", ... if that name is taken), so the Updater finds only the new file. The file's
checksum (header[0:4] = little-endian sum of the rest) is checked before anything on the
machine is touched.

Arm and reboot: 0xC8 ARM (EEPROM 0x30 = 1) then REBOOT (watchdog reset): the machine starts
the Updater, which updates from the drive once Update is tapped on its screen
(pc-driver/updater.py, BOOT_UPDATER.md).

Patching: a stock firmware image (recognised by SHA-256) can be patched with the SnowCut
modifications before it is uploaded (tools/apply_patch.py + patches/cm110_v1819w.sncpatch.json,
which holds only SnowCut's own bytes), then the boot logo / splash PNGs in replacement/ are swapped in
(tools/splash_handler.py). The patched copy goes to a temporary folder and is uploaded as
"<name>_snowcut.upf". The original file is never modified. tools/, patches/, replacement/ and
pc-driver/ are the app's own copies, so pc-app works on its own.

Original firmware: uploading and arming go over the SnowCut USB connection (vendor opcodes),
which only the SnowCut firmware answers. detect() asks the machine with 0xC2 PING (no data, no
effect) which one it is running. On the original firmware the patched copy is instead saved to a file
(start_save) for the user to put in the top folder of a USB flash drive, and the machine's own
updater installs it from there (INSTALL_STEPS).

All run in a background thread. poll() from a QTimer returns (busy, progress 0..1, text, result),
result = (ok, message) once, after the thread has finished (start_detect: (ok, kind)).
"""
from __future__ import annotations
import os
import re
import json
import struct
import sys
import threading

try:
    _d = os.path.join(os.path.dirname(__file__), "..", "pc-driver")
    if os.path.isdir(_d):
        sys.path.insert(0, _d)
    from memdump import _dev as _fw_dev, close_dev as _fw_close, friendly_error  # noqa
    from memdump import snowcut_firmware as _snowcut
    import cm110_fs as _fs
    import updater as _upd
    HAVE_DRIVER = True
except Exception:
    HAVE_DRIVER = False
    _fs = _upd = _fw_dev = _fw_close = _snowcut = None
    friendly_error = str

try:
    _r = os.path.join(os.path.dirname(__file__), "..")
    if os.path.isdir(os.path.join(_r, "tools")) and _r not in sys.path:
        sys.path.insert(0, _r)
    from tools import apply_patch as _ap
    from tools.upf import Upf as _Upf
    from tools.splash_handler import apply_splash_replacements as _splash
    HAVE_PATCH = True
except Exception:
    HAVE_PATCH = False
    _ap = _Upf = _splash = None

PATCH_NAME = "cm110_v1819w.sncpatch.json"
ROOT = "A:\\"
UPF_HEADER = 0x100
UPF_ID_OFF = 0x56
SNOWCUT, STOCK = "snowcut", "stock"         # detect(): what the machine is running
MIN_DRIVE = 64 << 20                        # smaller removable disks (the machine's own) skipped
INSTALL_STEPS = ("To install it: put the USB flash drive into the machine's USB port, switch "
                 "the machine off, then switch it on while holding the Feed button. Tap Update "
                 "on its screen. When the update has finished, switch the machine off and on "
                 "again.")


def check_upf(path):
    """{size, upf_id} of a valid .upf, else ValueError saying what is wrong."""
    data = open(path, "rb").read()
    if len(data) <= UPF_HEADER:
        raise ValueError("%s is too small to be a firmware file" % os.path.basename(path))
    stored = struct.unpack("<I", data[:4])[0]
    actual = sum(data[4:]) & 0xFFFFFFFF
    if stored != actual:
        raise ValueError("%s is damaged or is not a firmware file for this machine."
                         % os.path.basename(path))
    return {"size": len(data), "upf_id": struct.unpack_from("<H", data, UPF_ID_OFF)[0]}


def _app_dir():
    return getattr(sys, "_MEIPASS", None) or os.path.join(os.path.dirname(__file__), "..")


def patch_file():
    """Path of the SnowCut patch (pc-app/patches, or next to the exe when frozen)."""
    return os.path.normpath(os.path.join(_app_dir(), "patches", PATCH_NAME))


def splash_dir():
    """Folder of replacement boot logo / splash PNGs (pc-app/replacement)."""
    return os.path.normpath(os.path.join(_app_dir(), "replacement"))


def stock_target(path):
    """The patch target (dict with "name") if `path` is an unmodified stock firmware that the
    SnowCut patch supports, else None (other firmware, already patched, or no patch)."""
    if not HAVE_PATCH or not os.path.isfile(patch_file()):
        return None
    patch = json.load(open(patch_file(), encoding="utf-8"))
    try:
        return _ap.find_target(patch, open(path, "rb").read())
    except _ap.PatchError:
        return None


def patch_upf(path, out):
    """Write a copy of stock firmware `path` with the SnowCut patch applied to `out`. The
    result is checked against the hash the patch was made with, then the PNGs in
    splash_dir() (if any) replace the boot images. Raises ValueError."""
    target = stock_target(path)
    if target is None:
        raise ValueError("%s is not a stock firmware this patch supports" % os.path.basename(path))
    try:
        u = _Upf(path)
        _ap.apply_to_upf(u, target)
        if _ap._sha(u.bytes_with_checksum()) != target["output_sha256"]:
            raise _ap.PatchError("the patched image does not match what the patch expects")
        _splash(u, splash_dir())
        u.save(out)
    except (_ap.PatchError, ValueError, RuntimeError) as e:
        raise ValueError(str(e)) from None
    return check_upf(out)


def is_upf(name):
    return name.lower().endswith(".upf")


def is_drive_root(path):
    """True when file `path` is in the top folder of a drive (where the updater looks)."""
    folder = os.path.dirname(os.path.abspath(path))
    return os.path.splitdrive(folder)[1] in ("\\", "/")


def usb_drives():
    """Top folders of the removable drives (USB flash drives) Windows shows, e.g. ["E:\\"].
    Disks smaller than MIN_DRIVE are left out: the machine's own USB disk is one."""
    if sys.platform != "win32":
        return []
    import ctypes
    k32 = ctypes.windll.kernel32
    out = []
    mask = k32.GetLogicalDrives()
    for i in range(26):
        root = "%s:\\" % chr(65 + i)
        if not mask & (1 << i) or k32.GetDriveTypeW(root) != 2:     # DRIVE_REMOVABLE
            continue
        total = ctypes.c_ulonglong(0)
        if k32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), None) and \
                total.value >= MIN_DRIVE:
            out.append(root)
    return out


def renames(names):
    """[(old, new)] for every .upf in `names` (one directory): new = old + ".old", or
    ".oldN" (N = 2, 3, ...) when that name exists or is already taken by another rename."""
    taken = {n.lower() for n in names}
    out = []
    for n in sorted(names, key=str.lower):
        if not is_upf(n):
            continue
        new, k = n + ".old", 1
        while new.lower() in taken:
            k += 1
            new = "%s.old%d" % (n, k)
        taken.add(new.lower())
        out.append((n, new))
    return out


class _Job:
    """One background operation with progress (MatFeeder-style poll API)."""

    def __init__(self):
        self._thread = None
        self._result = None
        self.progress = 0.0
        self.text = ""

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def _start(self, fn, *args):
        if self.busy:
            return False
        self._result, self.progress, self.text = None, 0.0, ""
        self._thread = threading.Thread(target=self._run, args=(fn,) + args, daemon=True)
        self._thread.start()
        return True

    def _run(self, fn, *args):
        try:
            self._result = (True, fn(*args))
        except Exception as e:
            self._result = (False, friendly_error(e) if HAVE_DRIVER else str(e))

    def wait(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    def poll(self):
        """(busy, progress, text, result), where result is (ok, message) once at the end."""
        if self.busy:
            return True, self.progress, self.text, None
        r, self._result = self._result, None
        return False, self.progress, self.text, r


class FirmwareTools(_Job):
    """Upload and arm/reboot, or (original firmware) patch to a file for a USB flash drive.
    client_factory(dev) -> a cm110_fs client, probe(dev) -> True on the SnowCut firmware.
    `firmware` is what the machine was found to run (SNOWCUT / STOCK), None until known."""

    def __init__(self, open_dev=None, close_dev=None, client_factory=None, updater=None,
                 probe=None, firmware=None):
        super().__init__()
        self._open = open_dev or _fw_dev
        self._close = close_dev or _fw_close
        self._client = client_factory or (lambda dev: _fs.C4Client(dev))
        self._upd = updater if updater is not None else _upd
        self._probe = probe or _snowcut
        self.firmware = firmware

    @property
    def stock(self):
        return self.firmware == STOCK

    # -- which firmware --
    def _detect(self):
        if self._open is None or self._probe is None:
            raise RuntimeError("USB support is not available in this installation.")
        dev = self._open()
        try:
            self.firmware = SNOWCUT if self._probe(dev) else STOCK
        finally:
            self._end(dev)
        return self.firmware

    def detect(self):
        """SNOWCUT or STOCK (also kept in .firmware), or None if the machine cannot be asked."""
        try:
            return self._detect()
        except Exception:
            return None

    def start_detect(self):
        return self._start(self._detect)

    # -- original firmware: patch to a file for a USB flash drive --
    def start_save(self, path, out):
        """Write `path` with the SnowCut features added to `out` (stock images only)."""
        check_upf(path)
        if stock_target(path) is None:
            raise ValueError("%s is not a firmware version SnowCut can add its features to"
                             % os.path.basename(path))
        return self._start(self._save_job, path, out)

    def _save_job(self, path, out):
        part = out + ".part"                 # not a .upf: the updater never sees a half file
        self.text = "Adding SnowCut features to %s…" % os.path.basename(path)
        self.progress = 0.05
        try:
            info = patch_upf(path, part)
            self.progress = 0.9
            moved = []
            if is_drive_root(out):           # the updater must find only the new file
                folder, name = os.path.dirname(os.path.abspath(out)), os.path.basename(out)
                names = [n for n in os.listdir(folder) if n.lower() != name.lower()
                         and os.path.isfile(os.path.join(folder, n))]
                moved = renames(names)
                for old, new in moved:
                    self.text = "Renaming %s to %s…" % (old, new)
                    os.rename(os.path.join(folder, old), os.path.join(folder, new))
            os.replace(part, out)
        finally:
            if os.path.exists(part):
                os.remove(part)
        self.progress = 1.0
        msg = "Saved %s (%d KB) with the SnowCut features" % (
            os.path.basename(out), (info["size"] + 1023) // 1024)
        if moved:
            msg += ". Renamed %s" % ", ".join("%s → %s" % m for m in moved)
        return msg + ". " + INSTALL_STEPS

    # -- device sessions --
    def _session(self):
        if self._open is None:
            raise RuntimeError("USB support is not available in this installation.")
        dev = self._open()
        return dev, self._client(dev)

    def _end(self, dev):
        if dev is not None and self._close is not None:
            self._close(dev)

    # -- upload --
    def start_upload(self, path, patch=False):
        """Upload `path`. With patch=True, first apply the SnowCut patch (stock images only)."""
        check_upf(path)                      # before the thread: a bad file never starts
        if patch and stock_target(path) is None:
            raise ValueError("%s is not a firmware version SnowCut can add its features to"
                             % os.path.basename(path))
        return self._start(self._upload_job, path, patch)

    def _upload_job(self, path, patch):
        if not patch:
            return self._upload(path, os.path.basename(path))
        import tempfile
        stem = os.path.splitext(os.path.basename(path))[0]
        with tempfile.TemporaryDirectory(prefix="snowcut_fw_") as td:
            out = os.path.join(td, stem + "_snowcut.upf")
            self.text = "Adding SnowCut features to %s…" % os.path.basename(path)
            patch_upf(path, out)
            return self._upload(out, os.path.basename(out))

    def _upload(self, path, name):
        size = os.path.getsize(path)
        dev, fs = self._session()
        try:
            vol = fs.volume(ROOT) if hasattr(fs, "volume") else _null()
            pop = fs.busy("saving") if hasattr(fs, "busy") else _null()
            with vol, pop:
                self.text = "Looking for old firmware files\u2026"
                names = [n for n, attr, _s in fs.list_dir(ROOT) if not attr & 0x10]
                moved = renames(names)
                for old, new in moved:
                    self.text = "Renaming %s to %s\u2026" % (old, new)
                    fs.rename(ROOT + old, ROOT + new)
                self.text = "Uploading %s\u2026" % name
                self._write(fs, path, ROOT + name, size)
        finally:
            self._end(dev)
        self.progress = 1.0
        msg = "Uploaded %s (%d KB) to the machine's USB drive" % (name, (size + 1023) // 1024)
        if moved:
            msg += ". Renamed %s" % ", ".join("%s \u2192 %s" % m for m in moved)
        return msg + ". Choose Install Firmware to install it."

    def _write(self, fs, local, remote, size):
        chunk = getattr(_fs, "CHUNK", 0x1000)
        h = fs.open(remote, _fs.O_WRITE | _fs.O_CREATE | _fs.O_TRUNC)
        ok = False
        try:
            with open(local, "rb") as f:
                off = 0
                while True:
                    blk = f.read(chunk)
                    if not blk:
                        break
                    fs.write_at(h, off, blk)
                    off += len(blk)
                    self.progress = off / max(1, size)
            ok = True
        finally:
            try:
                fs.close(h)                  # flushes, and ENOSPC surfaces here
            except Exception:
                if ok:
                    raise
        _attr, got = fs.stat(remote)
        if got != size:
            raise IOError("the copy on the machine is incomplete (%d of %d bytes)" % (got, size))

    # -- updater --
    def upf_on_stick(self):
        """Names of the .upf files in the drive's root (what the Updater will see)."""
        dev, fs = self._session()
        try:
            vol = fs.volume(ROOT) if hasattr(fs, "volume") else _null()
            with vol:
                return [n for n, attr, _s in fs.list_dir(ROOT) if not attr & 0x10 and is_upf(n)]
        finally:
            self._end(dev)

    def start_arm_reboot(self):
        return self._start(self._arm_reboot)

    def _arm_reboot(self):
        if self._open is None:
            raise RuntimeError("USB support is not available in this installation.")
        dev = self._open()
        try:
            st0 = self._upd.status(dev)
            if st0["version"] < getattr(self._upd, "REBOOT_MIN_VERSION", 0):
                raise IOError("this machine's firmware cannot be restarted from SnowCut. "
                              "Install the firmware with SnowCut features first")
            if st0["line_mode"] == 1:
                raise IOError("Line mode is on, so the updater would also overwrite the backup "
                              "updater. Turn it off on the Updater tab first")
            self.text = "Preparing the updater\u2026"
            st, sense = self._upd.set_pending(dev, True)
            if st != 0:
                raise IOError("the machine did not accept the update request (error %s)" % (sense,))
            if not self._upd.status(dev)["armed"]:
                raise IOError("the machine did not keep the update request")
            self.text = "Restarting the machine\u2026"
            st, sense = self._upd.reboot(dev)
            if st != 0:
                self._upd.set_pending(dev, False)      # do not leave it armed for later
                raise IOError("the machine did not restart (error %s), so the update was canceled"
                              % (sense,))
        finally:
            self._close(dev)
        self.progress = 1.0
        return ("The machine is restarting into its firmware updater. Tap Update on its "
                "screen. The machine will restart with the new firmware when the update is done.")


class _null:
    def __enter__(self):
        return False

    def __exit__(self, *a):
        return False
