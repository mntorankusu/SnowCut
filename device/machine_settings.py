"""Machine > Machine Settings: everything in the machine's settings EEPROM.

The catalog of fields (names, ranges, defaults, sources) is pc-driver/cm110_eeprom.py. The
dialog edits a copy of the EEPROM's used area (0x000..0x5FF). Apply writes only the bytes
that changed, through vendor opcode 0xC9 (both chips, each byte read back). Fields that the
0xC5 SETTINGS opcode also knows (`Field.live`) are then put into RAM as well, so they take effect
at once. Everything else takes effect after the machine restarts.

UsbSettings opens the machine for each read / write and releases it (one handle at a time on
Windows). The dialog takes any backend with the same API:
  read() -> bytes (USED_END long)
  write(old, new) -> bytes as read back
Firmware without 0xC9 makes read() raise NeedsFirmware: StockFirmware when the machine is running the
original firmware (0xC2 PING refused too), which takes the firmware from a USB flash drive.
"""
from __future__ import annotations

try:
    import os
    import sys
    _d = os.path.join(os.path.dirname(__file__), "..", "pc-driver")
    if os.path.isdir(_d):
        sys.path.insert(0, _d)
    import cm110_eeprom as eep
    from memdump import _dev as _fw_dev, close_dev as _fw_close, friendly_error  # noqa
    from memdump import snowcut_firmware as _snowcut
    import cm110_settings as _cs
    HAVE_DRIVER = True
except Exception:
    import cm110_eeprom as eep            # catalog only (no pyusb)
    HAVE_DRIVER = False
    _cs = _fw_dev = _fw_close = _snowcut = None
    friendly_error = str

FIELDS, BY_NAME, GROUPS = eep.FIELDS, eep.BY_NAME, eep.GROUPS
SIZE = eep.USED_END


class NeedsFirmware(IOError):
    """The machine's firmware has no 0xC9 EEPROM handler."""


class StockFirmware(NeedsFirmware):
    """The machine is running the original firmware: no SnowCut opcodes at all, so firmware cannot be
    sent over USB either. It is installed from a USB flash drive."""


OLD_FIRMWARE = ("This machine's firmware is older than this version of SnowCut and does not let "
                "it read the settings. Upload and install the current firmware with SnowCut "
                "features (Firmware buttons below).")
STOCK_FIRMWARE = ("This machine is running the original Brother firmware, which does not let SnowCut "
                  "read its settings or send it files over the USB cable. To add the SnowCut "
                  "features, choose Save Firmware to USB Drive below: SnowCut will add them to an "
                  "original firmware file (.upf), and you can then install it from a USB flash drive with "
                  "the machine's own updater.")


def changed_fields(old, new):
    return [f for f in FIELDS if old[f.addr:f.end] != new[f.addr:f.end]]


class UsbSettings:
    """The machine's EEPROM over USB. Each call opens and releases the machine."""

    def __init__(self, open_dev=None, close_dev=None, driver=None, settings=None, probe=None):
        self._open = open_dev or _fw_dev
        self._close = close_dev or _fw_close
        self._eep = driver or eep
        self._cs = settings or _cs
        self._probe = probe or _snowcut

    def _session(self):
        if self._open is None:
            raise RuntimeError("USB support is not available in this installation.")
        return self._open()

    def _read(self, dev):
        try:
            return self._eep.read(dev, 0, SIZE)
        except self._eep.EepromRejected:
            if self._stock(dev):
                raise StockFirmware(STOCK_FIRMWARE) from None
            raise NeedsFirmware(OLD_FIRMWARE) from None

    def _stock(self, dev):
        """True when the machine refuses 0xC2 PING too (original firmware), on the same handle."""
        try:
            return self._probe is not None and not self._probe(dev)
        except Exception:
            return False

    def read(self):
        dev = self._session()
        try:
            return self._read(dev)
        finally:
            self._close(dev)

    def write(self, old, new):
        """Write the bytes of `new` that differ from what the machine holds now (re-read,
        so a stale `old` cannot undo someone else's change), refresh RAM for live fields,
        and return the image read back. Raises IOError if it does not match `new`."""
        dev = self._session()
        try:
            cur = self._read(dev)
            target = bytearray(cur)
            for a, d in eep.diff_runs(old, new):         # only what the user changed
                target[a:a + len(d)] = d
            self._eep.write_changes(dev, cur, target)
            for f in changed_fields(cur, target):
                if f.live is not None and self._cs is not None:
                    raw = f.get(target)
                    # RAM only (flag 2): the EEPROM already holds the raw byte.
                    self._cs.set_setting(dev, f.live, f.live_value(raw), commit_eeprom=False)
            got = self._read(dev)
        finally:
            self._close(dev)
        bad = [f.label for f in changed_fields(got, target)]
        if bad or got != bytes(target):
            raise IOError("the machine did not keep %s" % (", ".join(bad) or "every byte"))
        return got


def describe_error(e):
    """One sentence for the dialog."""
    if isinstance(e, NeedsFirmware):
        return str(e)
    if isinstance(e, eep.EepromRejected):
        return "The machine refused the settings request (%s)." % e
    return friendly_error(e)
