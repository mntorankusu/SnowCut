"""cm110_eeprom.py: the machine's settings EEPROM over USB (vendor opcode 0xC9), and a
catalog of what each byte means.

Transport (tools/eeprom_handler.py): READ up to 4 KB from the main or sub chip, WRITE 1..8
bytes per command to both chips with read-back. The EEPROM is 16 KB, and everything the
firmware uses lies below 0x600 (USED_END), apart from four chip-test bytes.

Catalog (FIELDS): every byte the v1819W main firmware reads or writes, found by scanning it
for EEPROM accesses. Sources, cited per field in `note`:
  - the firmware's EEPROM item table at 0x89020988 (52 entries {label, size, address, mask,
    default}). `label` is the firmware's own name where that table has one.
  - the code that loads and range-checks each value (ranges = what the firmware accepts,
    and anything else makes it fall back to `default`).
  - test-mode screens (labels such as "Cutter Sensor", "Total time check").
Multi-byte values are big-endian unless the kind says "le". Values the firmware never
range-checks get a raw 0..max range. Bytes not in the catalog are still editable as hex.

Most values are copied to RAM at boot, so a change applies after a restart. `live` names
the 0xC5 SETTINGS key that also updates RAM at once (pc-driver/cm110_settings.py).

  python cm110_eeprom.py dump [start] [length] [--sub] [-o file]
  python cm110_eeprom.py get <name|addr>
  python cm110_eeprom.py set <name|addr> <value>
"""
import struct
import sys

OP_EEPROM = 0xC9
SUB_READ, SUB_WRITE = 0x00, 0x01
MAGIC = b"EE"
EEP_SIZE = 0x4000
USED_END = 0x600
MAX_READ = 0x1000
READ_CHUNK = 0x200
MAX_WRITE = 8
EP_OUT, EP_IN = 0x01, 0x82
ERASED = 0xFF


class EepromRejected(IOError):
    """The machine answered 0xC9 with a failed status: firmware without the EEPROM handler
    (05/20 or 05/24), a bad request, or a write that did not read back (04/44)."""


# -- transport --
_tag = [0x45450000]


def _cbw(cdb, length, data_in):
    _tag[0] = (_tag[0] + 1) & 0xFFFFFFFF
    return _tag[0], (b"USBC" + struct.pack("<II", _tag[0], length)
                     + bytes([0x80 if data_in else 0x00, 0, 16]) + bytes(cdb))


def _csw(dev, tag, timeout):
    csw = bytes(dev.read(EP_IN, 13, timeout=timeout))
    if len(csw) < 13 or csw[:4] != b"USBS" or struct.unpack("<I", csw[4:8])[0] != tag:
        raise IOError("bad CSW %r" % csw)
    return csw[12]


def _sense(dev):
    cdb = bytes([0x03, 0, 0, 0, 18, 0]) + bytes(10)
    tag, cbw = _cbw(cdb, 18, True)
    dev.write(EP_OUT, cbw, timeout=3000)
    raw = bytes(dev.read(EP_IN, 18, timeout=3000))
    _csw(dev, tag, 3000)
    return (raw[2] & 0x0F, raw[12], raw[13]) if len(raw) >= 14 else (0, 0, 0)


def _reject(dev, what):
    try:
        key, asc, ascq = _sense(dev)
        detail = "sense %02X/%02X/%02X" % (key, asc, ascq)
    except Exception:
        key, asc, detail = 0, 0, "no sense"
    if key == 4 and asc == 0x44:
        raise EepromRejected("%s: the EEPROM did not keep the value (%s)" % (what, detail))
    raise EepromRejected("%s refused (%s)" % (what, detail))


def read_cdb(addr, n, chip=0):
    cdb = bytearray(16)
    cdb[0], cdb[1] = OP_EEPROM, SUB_READ
    cdb[2:4] = struct.pack(">H", addr)
    cdb[4:6] = struct.pack(">H", n)
    cdb[6] = chip
    return bytes(cdb)


def write_cdb(addr, data):
    if not 1 <= len(data) <= MAX_WRITE:
        raise ValueError("write 1..%d bytes per command" % MAX_WRITE)
    cdb = bytearray(16)
    cdb[0], cdb[1] = OP_EEPROM, SUB_WRITE
    cdb[2:4] = struct.pack(">H", addr)
    cdb[4] = len(data)
    cdb[5:7] = MAGIC
    cdb[8:8 + len(data)] = data
    return bytes(cdb)


def _check_range(addr, n):
    if n < 0 or addr < 0 or addr + n > EEP_SIZE:
        raise ValueError("EEPROM range %#x+%#x is outside 0..%#x" % (addr, n, EEP_SIZE))


def read(dev, addr, n, chip=0, progress=None):
    """n bytes from `addr` (chip 0 = main, the one the firmware uses, 1 = sub)."""
    _check_range(addr, n)
    out = bytearray()
    while len(out) < n:
        k = min(READ_CHUNK, n - len(out))
        a = addr + len(out)
        tag, cbw = _cbw(read_cdb(a, k, chip), k, True)
        dev.write(EP_OUT, cbw, timeout=3000)
        try:
            data = bytes(dev.read(EP_IN, k, timeout=20000))
        except Exception:
            # Refused (old firmware or bad range): the device stalls the data phase,
            # then sends a failed CSW.
            dev.clear_halt(EP_IN)
            _csw(dev, tag, 3000)
            _reject(dev, "EEPROM read %#x" % a)
        if _csw(dev, tag, 5000) != 0:
            _reject(dev, "EEPROM read %#x" % a)
        if len(data) != k:
            raise IOError("EEPROM read %#x: got %d of %d bytes" % (a, len(data), k))
        out += data
        if progress:
            progress(len(out), n)
    return bytes(out)


def write(dev, addr, data, progress=None):
    """Write `data` at `addr` (both chips), 8 bytes per command, each byte verified."""
    data = bytes(data)
    _check_range(addr, len(data))
    done = 0
    while done < len(data):
        blk = data[done:done + MAX_WRITE]
        tag, cbw = _cbw(write_cdb(addr + done, blk), 0, False)
        dev.write(EP_OUT, cbw, timeout=3000)
        if _csw(dev, tag, 10000) != 0:
            _reject(dev, "EEPROM write %#x" % (addr + done))
        done += len(blk)
        if progress:
            progress(done, len(data))


def write_changes(dev, old, new, base=0, progress=None):
    """Write the bytes where `new` differs from `old` (same length, both starting at
    `base`), as runs of up to 8 bytes. Returns the number of bytes written."""
    runs = diff_runs(old, new)
    total = sum(len(d) for _a, d in runs)
    done = 0
    for a, d in runs:
        write(dev, base + a, d)
        done += len(d)
        if progress:
            progress(done, total)
    return total


def diff_runs(old, new, gap=0):
    """[(offset, bytes)] covering every byte where new != old, each run <= MAX_WRITE."""
    if len(old) != len(new):
        raise ValueError("images differ in length")
    out, i = [], 0
    while i < len(new):
        if new[i] == old[i]:
            i += 1
            continue
        j = i + 1
        while j < len(new) and j - i < MAX_WRITE and new[j] != old[j]:
            j += 1
        out.append((i, bytes(new[i:j])))
        i = j
    return out


# -- catalog --
class Field:
    """One EEPROM value. kind: u8 u16 u32 u16le u32le f32 ascii bytes.
    choices: {raw: text}, lo/hi: accepted range, offset: shown = raw + offset."""

    SIZES = {"u8": 1, "u16": 2, "u32": 4, "u16le": 2, "u32le": 4, "f32": 4}

    def __init__(self, addr, kind, name, label, group, lo=None, hi=None, choices=None,
                 default=None, offset=0, unit="", note="", live=None, danger=None,
                 size=None, decimals=6, live_bool=False):
        self.addr, self.kind, self.name, self.label, self.group = addr, kind, name, label, group
        self.size = size or self.SIZES[kind]
        self.choices = choices
        self.lo, self.hi = lo, hi
        if kind not in ("ascii", "bytes", "f32") and hi is None and choices is None:
            self.lo, self.hi = 0, (1 << (8 * self.size)) - 1
        self.default, self.offset, self.unit = default, offset, unit
        self.note, self.live, self.danger, self.decimals = note, live, danger, decimals
        self.live_bool = live_bool        # RAM holds 0/1 for this byte (any nonzero is on)

    def live_value(self, raw):
        """The value 0xC5 stores in RAM for raw EEPROM value `raw`."""
        return int(raw != 0) if self.live_bool else raw

    @property
    def end(self):
        return self.addr + self.size

    def get(self, image, base=0):
        b = bytes(image[self.addr - base:self.end - base])
        if self.kind in ("ascii", "bytes"):
            return b
        if self.kind == "f32":
            return struct.unpack(">f", b)[0]
        return int.from_bytes(b, "little" if self.kind.endswith("le") else "big")

    def encode(self, value):
        if self.kind == "ascii":
            raw = value.encode("ascii") if isinstance(value, str) else bytes(value)
            if len(raw) > self.size:
                raise ValueError("%s: at most %d characters" % (self.name, self.size))
            return raw + bytes([ERASED]) * (self.size - len(raw))
        if self.kind == "bytes":
            raw = bytes(value)
            if len(raw) != self.size:
                raise ValueError("%s: %d bytes" % (self.name, self.size))
            return raw
        if self.kind == "f32":
            return struct.pack(">f", float(value))
        v = int(value)
        if not 0 <= v < 1 << (8 * self.size):
            raise ValueError("%s: %d does not fit in %d bytes" % (self.name, v, self.size))
        return v.to_bytes(self.size, "little" if self.kind.endswith("le") else "big")

    def put(self, image, value, base=0):
        image[self.addr - base:self.end - base] = self.encode(value)

    def erased(self, image, base=0):
        return all(b == ERASED for b in image[self.addr - base:self.end - base])

    def valid(self, value):
        """True if the firmware would use `value` as it is (not fall back to the default)."""
        if self.kind in ("ascii", "bytes"):
            return True
        if self.kind == "f32":
            return value == value and (self.lo is None or self.lo <= value <= self.hi)
        if self.choices is not None and self.hi is None:
            return value in self.choices
        return self.lo <= value <= self.hi


ONOFF = {0: "Off", 1: "On"}
ONOFF_FF = {0: "Off", 1: "On", ERASED: "On (unset)"}
SNS = {0xAA: "Disabled (0xAA)", ERASED: "Enabled"}
_C, _E, _F, _P, _A, _S, _N, _X, _U = ("Settings", "Emboss", "Foil", "Fill patterns",
                                      "Cut area & calibration", "Sensors & service",
                                      "Counters", "Factory", "Updater")
LANGS = {1: "English", 2: "Deutsch (German)", 3: "Fran\u00e7ais (French)",
         4: "Nederlands (Dutch)", 5: "Italiano (Italian)", 6: "Espa\u00f1ol (Spanish)",
         7: "\u0420\u0443\u0441\u0441\u043a\u0438\u0439 (Russian)",
         8: "\u65e5\u672c\u8a9e (Japanese)", 9: "Portugu\u00eas (Portuguese)",
         10: "\u7b80\u4f53\u4e2d\u6587 (Simplified Chinese)"}

FIELDS = [
    # -- the Settings screen (SettingsMode.c, boot loader FUN_88d3eb30) --
    Field(0x14, "u8", "cut_speed", "Cut speed", _C, 1, 5, default=3, live=0x01,
          note="Boot: 1..5, else 3."),
    Field(0x15, "u8", "cut_pressure", "Cut pressure", _C, 1, 19, default=10, offset=-10,
          live=0x02, note="Shown as -9..+9 (raw 1..19, 10 = middle). Boot: else 10."),
    Field(0x16, "u8", "draw_speed", "Draw speed", _C, 1, 5, default=3, live=0x03),
    Field(0x17, "u8", "draw_pressure", "Draw pressure", _C, 1, 19, default=10, offset=-10,
          live=0x04),
    Field(0x10, "u8", "unit", "Units", _C, choices={0: "mm", 1: "inch", ERASED: "inch (unset)"},
          live=0x07, live_bool=True,
          note="Boot: any nonzero byte is inch (FUN_88d3c4b4 divides 0.01 mm by 100 for mm)."),
    Field(0x12, "u8", "sound", "Buzzer sound", _C, choices=ONOFF_FF, default=1, live=0x0B,
          live_bool=True,
          note="Key beep. The beep routine FUN_88e0124c is skipped when it is 0. Any nonzero value is on."),
    Field(0x22, "u8", "auto_off", "Auto shutdown", _C, choices={0: "Off", ERASED: "On"},
          live=0x06, live_bool=True, note="Auto power-off switch (FUN_88cdae6c). The panel writes 0xFF / 0, "
          "and any nonzero byte is on."),
    Field(0x18, "u8", "auto_off_hours", "Auto shutdown after", _C, 1, 4, default=1, unit=" h",
          live=0x05, note="Timeout = base (0x24) x this. Boot: 1..4, else 1."),
    Field(0x23, "u8", "language", "Language", _C, choices=LANGS, default=1, live=0x09,
          note="Boot: 1..10, else 1. The firmware's item table labels this byte \"Pattern "
          "interval\", but the boot code and the panel's language buttons use it as the "
          "language. A new language takes effect after a restart."),
    Field(0x19, "u8", "seam_allowance", "Seam allowance", _C, 0, 6,
          note="Firmware item table label. Boot: 0..6, else 4 (mm) or 2 (inch)."),
    Field(0x11, "u8", "background", "Background", _C, choices={0: "0", 1: "1", 2: "2"},
          default=0, live=0x08, note="Firmware item table label. The panel offers 0/1/2 "
          "(0xFF reads as 0). It selects 25.0 / 178.0 in the layout code FUN_88d262e4."),
    Field(0x21, "u8", "opening_screen", "Opening screen", _C, choices=ONOFF_FF, default=1,
          note="Firmware item table label. Boot: nonzero is on."),
    Field(0x0F, "u8", "cut_thrown_away", "Cut Thrown Away", _C, 1, 4, default=4,
          note="Firmware item table label. Picks the mat area for test cuts so wear is "
          "spread (SWIVEL_COMPENSATION.md). Boot: 1..4, else 4."),
    Field(0x1A, "u8", "scan_size", "Scan size", _C, default=0,
          note="Firmware item table label (FUN_88d1f238)."),
    Field(0x1B, "u8", "scan_resolution", "Scan resolution", _C, default=0,
          note="Firmware item table label (FUN_88d1f778)."),
    Field(0x1C, "u8", "recognition_mode", "Recognition mode", _C, default=0,
          note="Firmware item table label (FUN_88d284ec)."),
    Field(0x13, "u8", "language_code", "Language code (0x13)", _C, 0, 9,
          note="Written to the factory line-check file as \"Language\" (FUN_88d81e6c). "
          "Boot: 0..9 (9 only on some hardware), else 0. Its setter FUN_88d3dbbc also "
          "reloads text tables. Takes effect after a restart."),

    # -- emboss kit (FUN_88d97e44, FUN_88d97fa4, FUN_88d98cd8/d18, FUN_88d9650c) --
    Field(0x85, "u8", "emboss_enable", "Emboss Enable", _E,
          choices={0: "Off", 1: "On", ERASED: "Region default"},
          note="Gates the emboss menus. 0xFF uses the region table at 0x880132c0."),
    Field(0x86, "u8", "emboss_tool", "Emboss Tool Select", _E, 0, 1, default=0),
    Field(0x87, "u8", "emboss_inline0", "Emboss Inline Distance0", _E, 0, 30, default=10),
    Field(0x88, "u8", "emboss_inline1", "Emboss Inline Distance1", _E, 0, 30, default=12),
    Field(0x89, "u8", "emboss_speed", "Emboss Speed", _E, 1, 5, default=1),
    Field(0x8A, "u8", "emboss_pressure", "Emboss Pressure", _E, 1, 19, default=14),
    Field(0x8D, "u8", "emboss_adj_x", "Emboss Adj Xpos", _E, 1, 81, default=41, offset=-41,
          note="Shown as -40..+40 (raw 41 = no offset)."),
    Field(0x8E, "u8", "emboss_adj_y", "Emboss Adj Ypos", _E, 1, 81, default=41, offset=-41),
    Field(0x8B, "u8", "emboss_firstset", "Emboss FirstSet OnOff", _E, 0, 1, default=0),
    Field(0x8C, "u8", "emboss_firstset_adj", "Emboss FirstSet AdjustNo", _E, 1, 15, default=8),
    Field(0xEE, "u8", "emboss_firstset_cuts", "Emboss FirstSet TestCutNum", _E, default=0),

    # -- foil kit (FUN_88d96dc0, FUN_88d96570) --
    Field(0x9B, "u8", "foil_enable", "Foil Enable", _F,
          choices={0: "Off", 1: "On", ERASED: "Region default"}),
    Field(0x9C, "u8", "foil_glue_qty", "Foil Glue Quantity", _F, 1, 11, default=6),
    Field(0x9D, "u8", "foil_glue_pressure", "Foil Glue Pressure", _F, 1, 19, default=10),
    Field(0x9E, "u8", "foil_paste_scale", "Foil Glue Paste Scale", _F, 1, 4, default=1),
    Field(0x9F, "u8", "foil_press_pressure", "Foil Press Pressure", _F, 1, 19, default=10),

    # -- fill patterns (FUN_88ce9938) --
    Field(0x150, "u8", "fill_pattern", "Fill Pattern No", _P, 0, 4, default=0),
    Field(0x151, "u8", "addline_num", "AddLine Num", _P, 0, 2, default=0),
    Field(0x152, "u16", "addline_pitch", "AddLine Pitch", _P, 1, 99, default=3),
] + [
    f for i, (pd, dd) in enumerate(((3, 175), (50, 0), (50, 0), (50, 0), (50, 0)))
    for f in (Field(0x154 + 4 * i, "u16", "fill%d_pitch" % (i + 1), "Fill Pattern%d Pitch" % (i + 1),
                    _P, 1, 999, default=pd),
              Field(0x156 + 4 * i, "u16", "fill%d_dir" % (i + 1), "Fill Pattern%d Direction" % (i + 1),
                    _P, 0, 359, default=dd, unit="\u00b0"))
] + [
    Field(0x16A, "u16", "fill_foil_dir", "Fill PatternFoil Direction", _P, 0, 359, default=265,
          unit="\u00b0"),

    # -- cut area and calibration --
    Field(0x100, "u32", "cut_area_left", "Cut Area (Left)", _A, 0, 60360, default=0,
          unit=" x0.01 mm", note="User cut area rectangle (COORDINATE_MAPPING.md)."),
    Field(0x104, "u32", "cut_area_top", "Cut Area (Top)", _A, 0, 60360, default=0,
          unit=" x0.01 mm"),
    Field(0x108, "u32", "cut_area_right", "Cut Area (Right)", _A, 0, 60360, default=29667,
          unit=" x0.01 mm"),
    Field(0x10C, "u32", "cut_area_bottom", "Cut Area (Bottom)", _A, 0, 60360, default=29880,
          unit=" x0.01 mm"),
    Field(0x110, "f32", "scancut_theta", "Scan-cut adjust: angle", _A, -0.35, 0.35, unit=" rad",
          danger="Factory scan-to-cut calibration.",
          note="FUN_88d2756c: |angle| <= 0.35, else 0. Set by test mode 32/35."),
    Field(0x114, "f32", "scancut_x", "Scan-cut adjust: X offset", _A, -3.0, 3.0, unit=" mm",
          danger="Factory scan-to-cut calibration."),
    Field(0x118, "f32", "scancut_y", "Scan-cut adjust: Y offset", _A, -3.0, 3.0, unit=" mm",
          danger="Factory scan-to-cut calibration."),
    Field(0x11C, "f32", "scancut_xscale", "Scan-cut adjust: X scale", _A, 0.9975, 1.0025,
          danger="Factory scan-to-cut calibration."),
    Field(0x140, "f32", "scancut2_theta", "Scan-cut adjust 2: angle", _A, -0.35, 0.35,
          unit=" rad", note="Second set (FUN_88d2731c), written by the same calibration."),
    Field(0x144, "f32", "scancut2_x", "Scan-cut adjust 2: X offset", _A, -3.0, 3.0, unit=" mm"),
    Field(0x148, "f32", "scancut2_y", "Scan-cut adjust 2: Y offset", _A, -3.0, 3.0, unit=" mm"),
    Field(0x14C, "f32", "scancut2_xscale", "Scan-cut adjust 2: X scale", _A, 0.9975, 1.0025),
    Field(0x50, "u8", "head_x_trim", "Head (X) position trim", _A, 0, 200, offset=-100,
          note="FUN_88db8c84: (v - 100) x 4 steps added to a head constant, and >200 = 0."),
    Field(0x51, "u8", "leadin_depth", "Lead-in depth", _A, 0, 200, offset=-95,
          note="v - 95. Above 200, the firmware uses 5 (SWIVEL_COMPENSATION.md)."),
    Field(0x52, "u8", "blade_trim", "Blade height trim", _A, 0, 200, offset=-100,
          note="Added to the cutter down-table: v - 100 (only if <= 200)."),
    Field(0x53, "u8", "pen_trim", "Pen height trim", _A, 0, 200, offset=-100,
          note="Same for pens."),
    Field(0x00, "u16", "tp_min_x", "Touch panel: min X", _A, danger="Touch screen "
          "calibration: wrong values can make the touch screen unusable.",
          note="Factory line-check \"Tp Min X\" (FUN_88d81e6c, FUN_88e01f38)."),
    Field(0x02, "u16", "tp_den_x", "Touch panel: X scale", _A, danger="Touch screen "
          "calibration: wrong values can make the touch screen unusable."),
    Field(0x04, "u16", "tp_min_y", "Touch panel: min Y", _A, danger="Touch screen "
          "calibration: wrong values can make the touch screen unusable."),
    Field(0x06, "u16", "tp_den_y", "Touch panel: Y scale", _A, danger="Touch screen "
          "calibration: wrong values can make the touch screen unusable."),

    # -- sensors and service (test mode 25 "Sensor off" etc.) --
    Field(0x3A, "u8", "sensor_cutter", "Cutter sensor", _S, choices=SNS,
          note="Test mode 25. 0xAA disables it, and 1 is cleared at boot (FUN_88db8c84)."),
    Field(0x3B, "u8", "sensor_head", "Head (X) sensor", _S, choices=SNS,
          note="Test mode 25. 0xAA disables the X home sensor."),
    Field(0x3C, "u8", "sensor_mat", "Mat (Y) sensor", _S, choices=SNS,
          note="Test mode 25. 0xAA fakes mat presence (COORDINATE_MAPPING.md)."),
    Field(0x43, "u8", "sensor_matkind", "Mat kind recognition", _S,
          choices={0xAA: "Skip (0xAA)", ERASED: "Normal"},
          note="Test mode 25. 0xAA skips mat recognition."),
    Field(0x3D, "u8", "cartridge_sensor_off", "Cartridge sensor off", _S,
          choices={1: "On (use kinds below)", ERASED: "Off"},
          note="BLD_Init_BladeDetect FUN_88db7ef4 (MAT_TOOL_SCANNER_NOTES.md)."),
    Field(0x3E, "u8", "cartridge_blade", "Cartridge override: blade kind", _S,
          choices={3: "3 Standard", 4: "4 Deep", ERASED: "Unset"}),
    Field(0x3F, "u8", "cartridge_pen", "Cartridge override: draw kind", _S,
          choices={1: "1", 2: "2", ERASED: "Unset"}),
    Field(0x5D, "u8", "assert_mode", "Crash handling", _S,
          choices={0: "Error report, wait for a key", ERASED: "Watchdog reset"},
          note="fcmAssert (FUN_88db122c): 0 shows an error report and waits. Any other "
          "value stops feeding the watchdog so the machine resets (0xC8 REBOOT relies on "
          "that path)."),
    Field(0x24, "u8", "auto_off_base", "Auto shutdown unit", _S,
          choices={1: "Minutes (factory)", ERASED: "Hours"},
          note="Boot: 1 makes the auto shutdown base 60 s, anything else 3600 s."),
    Field(0x20, "u8", "flag_20", "Flag 0x20", _S, choices={1: "1", ERASED: "0xFF"},
          note="Toggled 1 / 0xFF by a hidden key sequence (FUN_88d940a8). Use unknown."),
    Field(0x0B, "u8", "flag_0b", "Flag 0x0B", _S, note="== 1 sets 0x880139c8 at task start "
          "(FUN_88db0b6c). Use unknown."),
    Field(0x0C, "u8", "flag_0c", "Flag 0x0C", _S, note="== 1 is passed to FUN_88dafbe0 "
          "(FUN_88ce703c). Cleared to 0xFF by the factory ship step."),
    Field(0x37, "u8", "flag_37", "Flag 0x37", _S, note="== 0 sets settings +0x64 at boot."),
    Field(0x38, "u8", "time_38", "Timer 0x38", _S, 0, 200, unit=" x10",
          note="FUN_88d7dbdc: v x 10 (above 200: 30)."),
    Field(0x39, "u8", "time_39", "Timer 0x39", _S, 0, 200, unit=" x10",
          note="FUN_88d7dc34: v x 10 (above 200: 300)."),
    Field(0x40, "u8", "flag_40", "Flag 0x40", _S, note="== 1 sets 0x88012a3c."),
    Field(0x41, "u8", "select_41", "Select 0x41", _S, note="1 / 2 / other pick one of three "
          "setups (FUN_88ce6de0)."),
    Field(0x42, "u8", "flag_42", "Flag 0x42", _S, note="== 1 with 0x41 (FUN_88ce6de0)."),
    Field(0x44, "u8", "flag_44", "Flag 0x44", _S, note="== 1 turns a cut-state check off "
          "(FUN_88dc1da4)."),
    Field(0x45, "u8", "param_45", "Parameter 0x45", _S, unit=" x100",
          note="FUN_88c81554: v x 100 when valid, else 400."),
    Field(0x46, "u8", "param_46", "Parameter 0x46", _S, unit=" x100", note="Else 100."),
    Field(0x47, "u8", "param_47", "Parameter 0x47", _S, unit=" x100", note="Else 0."),
    Field(0x48, "u8", "flag_48", "Flag 0x48", _S, note="== 1 sets 0x88012a44."),
    Field(0x5E, "u8", "flag_5e", "Flag 0x5E", _S, note="== 0xFF sets 0x88013c20 (mat init)."),
    Field(0x5F, "u8", "cis_type", "Scanner CIS type", _S,
          choices={1: "FB112I_01", ERASED: "HQ24E_J01"},
          note="Shading adjust screen (FUN_88d8ac70) names the sensor from this.",
          danger="Must match the fitted scanner sensor."),
    Field(0x120, "u8", "scan_debug", "Scan debug", _S, choices={0: "On", ERASED: "Off"},
          note="FUN_88dc7f60 / FUN_88d91af4."),
    Field(0x121, "u8", "scan_save_usb", "Save scan data to USB", _S,
          choices={1: "On", ERASED: "Off"},
          note="Writes A:\\ScanImage\\ScanImage_NNNNN.bin after scans (FUN_88dc4be4)."),
    Field(0x122, "u8", "flag_122", "Flag 0x122", _S, choices={1: "On", ERASED: "Off"}),
    Field(0x124, "u16", "scan_save_index", "Scan data file number", _S, 0, 0xFFFE),
    Field(0x13C, "u8", "param_13c", "Parameter 0x13C", _S, default=4, note="Boot: 0xFF -> 4."),
    Field(0x13D, "u8", "param_13d", "Parameter 0x13D", _S, default=25, note="Boot: 0xFF -> 25."),
    Field(0x13E, "u8", "param_13e", "Parameter 0x13E", _S, default=10, note="Boot: 0xFF -> 10."),
    Field(0x13F, "u8", "param_13f", "Parameter 0x13F", _S, note="Scan-cut adjust screen "
          "(FUN_88d8ef94)."),
    Field(0x1C0, "u8", "select_1c0", "Select 0x1C0", _S, 0, 2, note="0..2 (FUN_88d3fd70)."),
    Field(0x1C1, "u8", "select_1c1", "Select 0x1C1", _S, 0, 2, note="0..2 (FUN_88d3fdb0)."),
    Field(0x1C2, "u8", "flag_1c2", "Flag 0x1C2", _S, note="Cleared to 0xFF by the factory "
          "ship step."),
    Field(0x5B8, "u16", "threshold_5b8", "Threshold 0x5B8", _S,
          note="FUN_88de6858 compares a measured value with it, and 0xFFFF means 35000."),
    Field(0x7D, "u8", "code_7d", "Code 0x7D", _S, note="Returned by FUN_88ce70d8 unless 0xFF."),

    # -- counters (maintenance.c, test mode "Total time check") --
] + [
    Field(base + 4 * i, "u32", "%s_%d" % (name, i + 1), "%s (slot %d)" % (label, i + 1), _N,
          note=("The firmware keeps this counter in 4 words, slot n holding values from "
                "%d x n. The count is the last slot whose value is in its range." % step)
          if i == 0 else "")
    for name, label, base, step in (("cut_count", "Cut count", 0xA0, 100000),
                                    ("scan_count", "Scan count", 0xB0, 100000),
                                    ("total_time", "Total time", 0xC0, 1500000),
                                    ("total_time2", "Total time 2", 0xF0, 1500000))
    for i in range(4)
] + [
    Field(0x130, "u32", "save_usb", "Save File USB", _N, 0, 999999, default=1),
    Field(0x134, "u32", "save_mem", "Save File MEM", _N, 0, 999999, default=1),
    Field(0x138, "u32", "save_jpg", "Save File JPG", _N, 0, 999999, default=1),
    Field(0x550, "u32", "save_canvas", "Save File CANVAS", _N, 0, 999999, default=1),

    # -- factory --
    Field(0x25, "ascii", "serial", "Serial number", _X, size=9,
          danger="The machine's serial number."),
    Field(0x09, "u8", "spec_no", "Spec No", _X, note="Factory line-check \"Speck No\".",
          danger="Factory model data."),
    Field(0x0A, "u8", "grade_no", "Grade No", _X, danger="Factory model data."),
    Field(0x4B, "u32le", "spec_info", "Spec info", _X, note="specInfo.c: 8 decimal digits "
          "(little-endian), used when 0x4F == 1.", danger="Factory model data."),
    Field(0x4F, "u8", "spec_info_valid", "Spec info valid", _X, choices={1: "Yes", ERASED: "No"},
          danger="Factory model data."),
    Field(0x388, "ascii", "model_code", "Model code", _X, size=6, danger="Factory model data."),
    Field(0x3A0, "ascii", "board_code", "Code 0x3A0", _X, size=5, danger="Factory model data."),
    Field(0x6F, "u8", "shipped", "Shipped", _X, choices={1: "Yes", ERASED: "No"},
          note="Set by the factory ship step (FUN_88d82640), which also resets settings to "
          "defaults."),
    Field(0x7E, "u16", "signature", "EEPROM signature", _X, choices={0xAA55: "Valid (AA55)"},
          danger="If main's signature is not AA55 at boot, the firmware copies the whole sub "
          "EEPROM over it."),
    Field(0xD0, "bytes", "restart_words", "Restart records (words)", _X, size=20,
          note="restartMain.c: 10 u16 values."),
    Field(0xE4, "bytes", "restart_bytes", "Restart records (bytes)", _X, size=10),
    Field(0x61, "bytes", "process_61", "Process check 0x61", _X, size=7,
          note="test_13_ProcessChk."),
    Field(0x70, "bytes", "process_70", "Process check 0x70", _X, size=10),
    Field(0x90, "bytes", "process_90", "Process check 0x90", _X, size=8),

    # -- updater (BOOTLOADER.md, BOOT_UPDATER.md) --
    Field(0x30, "u8", "upd_pending", "Update pending", _U, choices={1: "Armed", ERASED: "Idle"},
          note="1: the next power-on starts the Updater. Use the Arm button instead."),
    Field(0x31, "u8", "upd_error", "Last update error", _U),
    Field(0x32, "u8", "upd_line_mode", "Line-upgrade mode", _U,
          choices={1: "Line mode", ERASED: "Normal"},
          danger="Line mode makes the Updater rewrite the recovery updater (bank 0). "
          "Never set it with a normal .upf."),
    Field(0x33, "u8", "upd_bank", "Updater bank", _U,
          choices={0: "Bank 0 (factory)", 1: "Bank 1"},
          note="The bootloader starts this updater bank, and a successful update toggles it. "
          "Bank 0 is never rewritten, so it is the safe choice for normal updates."),
    Field(0x34, "u16le", "upf_id", "Last .upf id", _U),
]
BY_NAME = {f.name: f for f in FIELDS}
GROUPS = [_C, _E, _F, _P, _A, _S, _N, _X, _U]
CHIP_TEST = (0x7FF, 0xFFF, 0x1FFF, 0x3FFF)   # single bytes seen in the dump (chip size probe)


def field_at(addr):
    for f in FIELDS:
        if f.addr <= addr < f.end:
            return f
    return None


def default_image():
    """A plausible image for the simulator: erased, with the firmware's defaults."""
    img = bytearray([ERASED]) * USED_END
    for f in FIELDS:
        if f.default is not None:
            f.put(img, f.default)
    for f, v in ((BY_NAME["unit"], 0), (BY_NAME["seam_allowance"], 4), (BY_NAME["sound"], 1),
                 (BY_NAME["auto_off"], ERASED), (BY_NAME["signature"], 0xAA55),
                 (BY_NAME["upd_bank"], 0), (BY_NAME["shipped"], 1), (BY_NAME["serial"], "SIM000001")):
        f.put(img, v)
    return img


def _check_overlaps():
    seen = {}
    for f in FIELDS:
        for a in range(f.addr, f.end):
            assert a not in seen, "EEPROM %#x in %s and %s" % (a, seen[a], f.name)
            seen[a] = f.name
        assert f.end <= USED_END, f.name


_check_overlaps()


# -- CLI --
def _open():
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from memdump import _dev
    return _dev()


def _resolve(s):
    if s in BY_NAME:
        return BY_NAME[s]
    a = int(s, 0)
    return field_at(a) or Field(a, "u8", "raw_%x" % a, "raw", "")


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sp = p.add_subparsers(dest="cmd")
    d = sp.add_parser("dump")
    d.add_argument("start", nargs="?", default="0")
    d.add_argument("length", nargs="?", default=hex(USED_END))
    d.add_argument("--sub", action="store_true")
    d.add_argument("-o", "--out")
    g = sp.add_parser("get")
    g.add_argument("field")
    s = sp.add_parser("set")
    s.add_argument("field")
    s.add_argument("value")
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 1
    from memdump import close_dev
    dev = _open()
    try:
        if a.cmd == "dump":
            start, n = int(a.start, 0), int(a.length, 0)
            data = read(dev, start, n, chip=1 if a.sub else 0)
            if a.out:
                open(a.out, "wb").write(data)
            for o in range(0, n, 16):
                print("%04x  %s" % (start + o, data[o:o + 16].hex(" ")))
        elif a.cmd == "get":
            f = _resolve(a.field)
            img = read(dev, f.addr, f.size)
            print("%s @%#x = %r%s" % (f.name, f.addr, f.get(img, f.addr),
                                      "" if f.valid(f.get(img, f.addr)) else "  (firmware "
                                      "uses its default)"))
        else:
            f = _resolve(a.field)
            v = a.value if f.kind == "ascii" else (float(a.value) if f.kind == "f32"
                                                   else int(a.value, 0))
            write(dev, f.addr, f.encode(v))
            print("%s @%#x <- %r (applies after a restart)" % (f.name, f.addr, v))
    finally:
        close_dev(dev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
