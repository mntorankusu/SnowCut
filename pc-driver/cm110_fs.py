r"""cm110_fs.py: host-side file gateway for CM110 (firmware opcode 0xC4).

Firmware does NOT implement this yet. This file defines the wire protocol and
implements the host side as if the device already handled it. Comments marked
DEVICE: tell the firmware author what to do for each opcode.

Transport: USB MSC Bulk-Only (BOT) on EP_OUT 0x01 / EP_IN 0x82, same as
pc-driver/memdump.py. One BOT command = one direction only, so paths are
delivered with a separate data-OUT command (SETPATH) before any op that
needs a path. This keeps every command CBW-compliant.

CDB layout (16 bytes, big-endian fields to match existing 0xC0/0xC1 style):
  [0]    = 0xC4 (FILE gateway)
  [1]    = sub-op (below)
  [2..5]  = arg0 BE32 (meaning per sub-op: dir index / offset / flags / size)
  [6..9]  = arg1 BE32 (length / mode)
  [10]   = handle u8 (0..7 frozen ABI for handle ops, 0 otherwise)
  [11..15]= reserved (0)

Sub-ops:
  0x00 SETPATH  data-OUT N bytes : path bytes, NUL-terminated, max 260 incl NUL
                DEPRECATED on current firmware: the data-OUT phase never returns
                a CSW (device hangs). The host sets the path via 0x0E instead.
  0x0E SETPATH_CHUNK no-data     : path piece carried in the CDB. CDB[2:4]=offset
                BE16, CDB[4]=count (0..11), CDB[5:5+count]=bytes. Writes to
                path_buf+offset and NUL-terminates at +count. Host streams
                ascending offsets, and the last chunk terminates the path. This
                replaces 0x00 because no-data commands complete reliably while
                data-OUT does not (same reason WRITE/push is still blocked).
  0x01 STAT     data-IN  32 bytes: file info for stored path
  0x02 LIST     data-IN  64 bytes: one directory entry (index = arg0)
  0x03 OPEN     data-IN  4 bytes : open stored path, flags=arg0 -> handle u32le
  0x04 READ     data-IN  arg1 bytes: read handle=CDB[10] at offset=arg0
  0x05 WRITE    data-OUT arg1 bytes: write handle=CDB[10] at offset=arg0
  0x06 CLOSE    no-data          : close handle=CDB[10] (flushes)
  0x07 DELETE   no-data          : delete file at stored path
  0x08 MKDIR    no-data          : create dir at stored path
  0x09 RMDIR    no-data          : remove (empty) dir at stored path
  0x0A CLOSEALL no-data          : close all handles, clear path buffer
  0x0B RENAME   no-data          : move stored path -> destination path set by
                0x11 (full path, same drive, may change directory). The
                destination is one-shot: cleared after every RENAME. (Was
                data-OUT, but a failure in data-OUT phase 1 cannot be reported.)
  0x0C TRUNCATE no-data          : shrink handle=CDB[10] to arg0 bytes (REFUSED:
                no verified firmware target)
  0x0D FLUSH    no-data          : fx_media_flush, handle=CDB[10] must be open
  0x11 DSTPATH_CHUNK no-data     : like 0x0E, into RENAME's destination buffer.
                Also the capability probe: firmware without path ops (before
                2026-10-16) refuses it 05/24/00.

STAT payload (32B, little-endian, all 4-byte aligned):
  [0:4]  attr u32le FAT attribute bits (test with &, never ==: 0x10 directory,
         0x01 read-only, 0x02 hidden, etc. may combine, e.g. 0x12. 0x20 archive
         is the normal "plain file" bit. Directory <=> (attr & 0x10) != 0.)
  [4:8]  size u32le (file bytes, 0 for dirs. KB-scale JPEG/FCM, so 32-bit is
         plenty and avoids MIPS unaligned-ld faults. Do NOT put a u64 at
         offset 4. Build with memcpy byte-wise, never by cast.)
  [8:32] reserved zero (DEVICE: put FAT mtime here later, zero for now)
  Missing path: canonical signal is CSW FAILED + sense 05/80/00 (below).
  A GOOD response with attr 0xFFFFFFFF is a deprecated-compat sentinel only.
  New firmware must NOT use it, but the host accepts it as FileNotFoundError fallback.

LIST payload (64B, all 4-byte aligned):
  [0:4]  attr u32le bits (same, and 0xFFFFFFFF + empty name = end of dir)
  [4:8]  size u32le
  [8:12] reserved zero (keeps name 4-aligned)
  [12:64] name NUL-terminated (52B max incl NUL).
  DEVICE: skip "." and ".." (never emit them). Skip (do not truncate, do not
  fail) entries whose names do not fit 52B incl NUL (only possible with long
  file names, and such entries are invisible to the host but still make RMDIR
  fail with the not-empty sense). The enumeration index counts only emitted
  entries. Cache the directory iterator so idx == prev+1 is O(1) (host walks
  0,1,2,... => O(n) total, not O(n^2)). Multi-record packing is a future
  optimization, not v1.

OPEN handle freeze: CDB[10] is one byte and the table has 8 slots, so the
  response stays a 4-byte u32le ONLY for USB alignment. Values 0..7 are valid
  and the low byte equals the CDB[10] value used later. Upper 3 bytes MUST be
  zero. Host rejects anything >= 8. DEVICE: return slot index zero-extended.

OPEN flags (arg0):
  0x01 read, 0x02 write, 0x04 create-if-missing, 0x08 truncate,
  0x10 exclusive (requires create, file/dir already exists -> 05/81/00)
  Validity (else 05/24, host ValueError): at least one of read/write, no bits
  above 0x1F, truncate requires write, exclusive requires create.
  DEVICE: map to fx_file_open modes and keep a table of ~8 FX_FILE objects in
  overlay RAM + one mutex. Check for a free slot BEFORE creating anything.
  Table full -> CSW FAILED sense 05/55 and no side effects. Opening a
  directory -> 05/82/01. Reads require O_READ and writes O_WRITE (else
  05/82/03 wrong-mode, host PermissionError).

READ past EOF: GOOD, not an error. DEVICE: offset >= size -> send 0 bytes,
  residue = want, CSW GOOD. offset+want past EOF -> send size-offset bytes,
  residue = want - sent, GOOD. WinFsp issues past-EOF reads routinely.

WRITE / TRUNCATE range rules (FileX seek clamps at EOF, so the device must
  check, not rely on the seek): WRITE with offset > current size -> 05/21/00
  (no implicit hole-filling. The host layer zero-fills by writing, if it ever
  needs to). TRUNCATE with size > current size -> 05/21/00 (shrink only).

WRITE short-write rule (no data-IN count, because BOT forbids bidirectional
  commands): consume the full OUT payload off the wire, attempt the store,
  then full store -> CSW GOOD residue 0, disk-full -> CSW FAILED with sense
  03/0C/00 or 05/55/00, and the host raises OSError ENOSPC. Never partial-success.

Flush policy (cheap, not every chunk): WRITE chunks MAY buffer in FileX
  cache. DEVICE MUST flush (fx_media_flush) on CLOSE and FLUSH, and MUST make
  STAT/LIST coherent, i.e. flush-if-dirty before answering STAT/LIST while
  any handle is open. Host streams CHUNK pieces, and durability is gated on
  close/flush, so a failed close can mean lost data and is surfaced.

In-use rule: DELETE, RMDIR and RENAME of a path that has an open handle (or,
  for directories, any open handle beneath it) fail with 05/84/00
  (host PermissionError). Nothing is silently detached.

RENAME rules: the firmware's move(old, new) (tools/fs_handler.py fs_move):
  same drive (else 05/24/00), may change directory, files and directories.
  dst already existing (and not the same entry) -> 05/81/00. A case-only
  rename of the same entry is allowed by the library. After RENAME the path
  buffer still holds src.

BOT residue rule. DEVICE MUST (final firmware): every data-IN CSW reports
  residue = requested - actually_sent (short READ at EOF -> residue > 0, CSW
  GOOD). The host below tolerates residue==0 with a short packet only as a
  bring-up concession to imperfect firmware. Final firmware must be strict.

SETPATH serialization and lifetime. The path buffer is connection state:
  SETPATH then the consuming op must be atomic. DEVICE: the BOT task already
  processes one CBW at a time via mailbox, so own the path buffer in BOT-task
  context only. Never touch it from ISR, never allow a second CBW to
  overwrite it mid-op. The buffer PERSISTS (success or failure of the
  consuming op) until the next SETPATH, CLOSEALL, Bulk-Only Reset or bus reset,
  so a host busy-retry of the consuming op needs no second SETPATH.
  On Bulk-Only Reset (0x21/0xFF) AND on USB bus reset: close all handles,
  clear the path buffer (same as CLOSEALL) so a crashed host cannot leak the
  8 slots. HOST: C4Client serializes every SETPATH+op pair under one lock.

Path handling. Host sends a drive-qualified "<D>:\..." path (to_device_path is
  idempotent, fills in DEFAULT_DRIVE when the caller omits the drive, and rejects
  "." / ".." components. C4Client applies it to every path, so raw argv and
  WinFsp paths both work). DRIVE LETTERS (firmware, SCAN_HANDOFF.md s4):
  B:\ = device-internal FileX volume (DEFAULT_DRIVE, always mounted, use this).
  A:\ = external USB-A host drive (needs a drive inserted, and opening it with none
  present makes the firmware block in the USBH mount path and kills the bulk-IN
  endpoint). The device handler passes the full "<D>:\..." path to FileX as-is
  (it does NOT strip the drive), so the drive the host sends selects the volume.
  DEVICE: require a "<letter>:\" prefix (else 05/24). Check MAX_PATH against the
  firmware's FX_MAXIMUM_PATH at build time. If FileX allows fewer than 260,
  lower MAX_PATH or return 05/24 for overlong.

Path encoding: raw bytes round-trip with ascii+surrogateescape (so any byte
  0x80-0xFF survives host str handling). The prototype restricts to an ASCII subset, and
  embedded NULs and non-ASCII characters are rejected host-side with a
  ValueError that says so. If the real encoding turns out to be
  cp932/Shift-JIS, swap the codec in the helpers only. DEVICE passes bytes
  through untranscoded.

Sense table (small, frozen, DEVICE must produce exactly these):
  key/asc/ascq  meaning                            host exception
  00/00/00      success                            --
  02/04/08      busy, long write in progress       BusyError (retryable)
  02/3A/00      medium not present (no drive)      IOError("no medium")
  05/24/00      invalid field in CDB               ValueError
                (bad sub-op / handle / flags / cross-drive rename)
  05/21/00      offset/size out of range           ValueError
  05/2C/00      command sequence error             RuntimeError
                (op needing SETPATH without one)
  05/80/00      file/dir not found (vendor)        FileNotFoundError
  05/81/00      already exists (vendor)            FileExistsError
  05/82/00      wrong type, generic (vendor)       PermissionError
  05/82/01      is a directory (vendor)            IsADirectoryError
  05/82/02      not a directory (vendor)           NotADirectoryError
  05/82/03      handle not open for this mode      PermissionError
  05/83/00      directory not empty (vendor)       OSError errno.ENOTEMPTY
  05/84/00      in use: open handle exists         PermissionError
  03/0C/00 or   write error / no space              OSError errno.ENOSPC
  05/55/00      (table/full)
  04/00/xx      other FAT library error -xx         IOError
  DEVICE: stash this triple for the existing opcode-0x03 REQUEST SENSE
  handler on EVERY 0xC4 failure. Fixed-sense bytes only: byte0=0x70/0x71,
  byte2=key, byte12=ASC, byte13=ASCQ.

Windows drive letter: winfsp_mount.py (DriveFS over this client, ctypes WinFsp
  binding, DriveMount = the app's one-click mount). It uses stat, list, read,
  whole-file rewrites, and delete/mkdir/rmdir/rename where refused_ops() says
  the firmware has them.
"""

import contextlib
import errno
import os
import re
import struct
import sys
import threading
import time
from typing import NoReturn

VID, PID = 0x04F9, 0x2107
EP_OUT, EP_IN = 0x01, 0x82
CHUNK = 0x1000  # <= firmware cap 0x1400, keep in sync with memdump.py

OP_FILE = 0xC4
S_SETPATH, S_STAT, S_LIST, S_OPEN, S_READ, S_WRITE, S_CLOSE = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06
S_DELETE, S_MKDIR, S_RMDIR = 0x07, 0x08, 0x09
S_CLOSEALL, S_RENAME, S_TRUNCATE, S_FLUSH = 0x0A, 0x0B, 0x0C, 0x0D
# 0x0E SETPATH_CHUNK: no-data path upload. The data-OUT SETPATH (0x00) does not
# complete on current firmware (device never returns the CSW), so the path is
# set via a sequence of no-data commands carrying the bytes INSIDE the CDB:
#   CDB[2:4] = offset BE16, CDB[4] = count (0..11), CDB[5:5+count] = path bytes.
S_SETPATH_CHUNK = 0x0E
_PATH_CHUNK = 11  # usable CDB bytes [5..15] per SETPATH_CHUNK command
# 0x11 DSTPATH_CHUNK: the same into RENAME's destination buffer.
S_DSTPATH_CHUNK = 0x11
# 0x0F MOUNT / 0x10 UNMOUNT (no-data, CDB[2] = drive letter): the firmware's own
# fsys_Mount/fsys_UnMount. The firmware keeps A: (the USB drive) UNMOUNTED except
# while one of its own features is using it. Every access is bracketed by
# fsys_Mount("A:") ... fsys_UnMount("A:"), so an idle machine rejects every
# gateway op on A: (stat -> 05/80, opendir -> 05/82/02). UNMOUNT closes all
# gateway handles first. Mount failure -> 02/3A/(-err), unmount -> 05/84/(-err).
S_MOUNT, S_UNMOUNT = 0x0F, 0x10
_MOUNT_TIMEOUT_MS = 15000  # media init on a cold drive can take seconds

# Drive objects (fsys layer). +0x10 = FAT type byte (set by the first mount and
# NOT cleared by unmount, so it is no mount indicator), +0x11 = the drive
# letter, +0x54 bit 7 = mounted (set by the mount core 0x88f7e138, cleared by
# the unmount core 0x88f7deec, verified live: A: 0x01 idle -> 0x81 after
# MOUNT -> 0x01 after UNMOUNT). Read via the 0xC0 memread so the host can tell
# whether A: is already mounted (UI busy, or the maintenance host-check left it
# up): fsys_Mount is NOT idempotent (it re-inits the drive's FAT state), so the host
# must never mount over a live mount. (Reads must be aligned, and a 1-byte 0xC0
# read at +0x11 returns 0.)
OP_MEMREAD = 0xC0
DRIVE_OBJ = {"A": 0x882519f0, "B": 0x88251a68}
DRIVE_FATTYPE_OFF, DRIVE_LETTER_OFF = 0x10, 0x11
DRIVE_FLAGS_OFF, DRIVE_MOUNTED_BIT = 0x54, 0x80
_DRIVE_HEAD = 0x60

O_READ, O_WRITE, O_CREATE, O_TRUNC, O_EXCL = 0x01, 0x02, 0x04, 0x08, 0x10
_O_ALL = O_READ | O_WRITE | O_CREATE | O_TRUNC | O_EXCL

ATTR_DIR_BIT = 0x10
ATTR_NONE = 0xFFFFFFFF  # end-of-dir / deprecated-compat sentinel, see STAT doc
MAX_PATH = 260  # incl NUL. DEVICE: must be <= FX_MAXIMUM_PATH, else lower it
MAX_HANDLES = 8  # frozen ABI: handles 0..7, low byte of u32le response

# Device drive letters (from firmware strings, see SCAN_HANDOFF.md section 4):
#   A:\  = the USB-A drive: the real storage for scans, path/.FCM files and
#          firmware-update files. This is the target for push/pull. It must have
#          a drive inserted (the firmware accesses it from its own scan/update
#          tasks). DEFAULT.
#   B:\  = device-internal function-side FileX volume (diagnostic use only).
# The device 0xC4 handler passes the path straight to FileX without forcing a
# drive, so the drive letter sent here is the volume the firmware opens.
#
# A: is only mounted while a firmware feature is using it. The CLI mounts it around
# each command via C4Client.volume() (see S_MOUNT above).
#
# Firmware state (2026-10-16): TRUNCATE is refused (05/24/00, ValueError): no
# verified firmware target. DELETE/MKDIR/RMDIR/RENAME work from the 2026-10-16
# handler on, and older firmware refuses them too (C4Client.refused_ops() probes).
# Also: OPEN with any write flag is fopen("w") (always creates/truncates,
# O_CREATE/O_TRUNC/O_EXCL are not looked at, and a write handle cannot read).
# winfsp_mount.py works within these limits.
REFUSED_OPS = frozenset(("truncate",))
REFUSED_OPS_OLD = frozenset(("delete", "mkdir", "rmdir", "rename", "truncate"))
DEFAULT_DRIVE = "A"

# READ at/after EOF: the device has no data, so it stalls bulk-IN and fails
# the CSW with this sense (00/00/05 END-OF-DATA DETECTED) = empty read.
SENSE_EOF = (0x00, 0x00, 0x05)
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

_BUSY_RETRIES = 3
_BUSY_DELAYS = (0.1, 0.2, 0.4)


class BusyError(IOError):
    """Device temporarily busy (sense 02/04/08). Safe to retry after ~100ms."""
    pass


def _parse_sense(raw):
    # Fixed-format ONLY (spec): byte0 0x70/0x71, byte2 key, bytes12/13 ASC/Q.
    # Descriptor format (0x72/0x73, key at byte1) is rejected -> None.
    if not raw or len(raw) < 14:
        return None
    if raw[0] not in (0x70, 0x71):
        return None
    return (raw[2] & 0x0F, raw[12], raw[13])


def _raise_for_sense(what, sense_raw) -> NoReturn:
    # Never returns: always raises. Call bare (no `raise` prefix).
    trip = _parse_sense(sense_raw) if sense_raw else None
    s = (" sense=%s" % sense_raw.hex()) if sense_raw else " sense=?"
    if trip is None:
        raise IOError("%s failed%s" % (what, s))
    key, asc, ascq = trip
    if (key, asc, ascq) == (0x02, 0x04, 0x08):
        raise BusyError("%s busy, retry%s" % (what, s))
    if (key, asc) == (0x02, 0x3A):
        raise IOError("%s: no medium%s" % (what, s))
    if (key, asc, ascq) == (0x05, 0x24, 0x00):
        raise ValueError("%s: invalid CDB field%s" % (what, s))
    if (key, asc, ascq) == (0x05, 0x21, 0x00):
        raise ValueError("%s: offset/size out of range%s" % (what, s))
    if (key, asc, ascq) == (0x05, 0x2C, 0x00):
        raise RuntimeError("%s: command sequence error%s" % (what, s))
    if (key, asc) == (0x05, 0x80):
        raise FileNotFoundError("%s%s" % (what, s))
    if (key, asc) == (0x05, 0x81):
        raise FileExistsError("%s%s" % (what, s))
    if (key, asc, ascq) == (0x05, 0x82, 0x01):
        raise IsADirectoryError("%s%s" % (what, s))
    if (key, asc, ascq) == (0x05, 0x82, 0x02):
        raise NotADirectoryError("%s%s" % (what, s))
    if (key, asc, ascq) == (0x05, 0x82, 0x03):
        raise PermissionError("%s: handle not open for this mode%s" % (what, s))
    if (key, asc) == (0x05, 0x82):
        raise PermissionError("%s: wrong file type%s" % (what, s))
    if (key, asc, ascq) == (0x05, 0x83, 0x00):
        raise OSError(errno.ENOTEMPTY, "%s: directory not empty%s" % (what, s))
    if (key, asc, ascq) == (0x05, 0x84, 0x00):
        raise PermissionError("%s: in use (open handle)%s" % (what, s))
    if (key, asc) == (0x05, 0x84):
        raise PermissionError("%s: unmount refused (err -%#x)%s" % (what, ascq, s))
    if (key, asc, ascq) in ((0x03, 0x0C, 0x00), (0x05, 0x55, 0x00)):
        raise OSError(errno.ENOSPC, "%s: no space%s" % (what, s))
    if key == 0x05:
        raise ValueError("%s: illegal request%s" % (what, s))
    raise IOError("%s failed%s" % (what, s))


def _is_dir_attr(attr):
    return (attr & ATTR_DIR_BIT) != 0


def _check_open_flags(flags):
    # Mirrors the device's 05/24 rules so bad flags fail before any I/O.
    if not isinstance(flags, int) or flags < 0 or flags & ~_O_ALL:
        raise ValueError("bad open flags %r" % (flags,))
    if not flags & (O_READ | O_WRITE):
        raise ValueError("open needs O_READ and/or O_WRITE")
    if flags & O_TRUNC and not flags & O_WRITE:
        raise ValueError("O_TRUNC requires O_WRITE")
    if flags & O_EXCL and not flags & O_CREATE:
        raise ValueError("O_EXCL requires O_CREATE")


def _iface(dev):
    # Interface number for the BOT reset control request (cached on dev).
    n = getattr(dev, "_c4_iface", None)
    if n is not None:
        return n
    n = 0
    try:
        n = dev.get_active_configuration()[(0, 0)].bInterfaceNumber
    except Exception:
        pass
    try:
        dev._c4_iface = n
    except Exception:
        pass
    return n


def _dev():
    # NOTE: raises ConnectionError (not SystemExit): this module is also a
    # WinFSP-service library. main() converts it to exit codes.
    # Windows: libusb when a libusb-capable driver is bound (Zadig), otherwise SCSI
    # pass-through on the disk Windows made for it (scsipt, needs administrator).
    import scsipt
    try:
        dev = scsipt.open_machine(VID, PID)
    except scsipt.OpenFailed as f:
        if f.elevation:
            raise ConnectionError("%s: restart as administrator, or install the libusb "
                                  "driver" % f)
        if f.open_error is not None:
            raise ConnectionError("set_configuration failed: %s" % f.open_error)
        raise ConnectionError("device %04X:%04X not found" % (VID, PID))
    reset_recovery(dev)
    # Best-effort: reclaim leaked slots from a previous crashed host.
    # DEVICE also does this on Bulk-Only / bus reset, so this may be redundant.
    try:
        from_usb_closeall(dev)
    except Exception:
        pass
    return dev


def reset_recovery(dev):
    try:
        dev.ctrl_transfer(0x21, 0xFF, 0, _iface(dev), 0, timeout=1000)
    except Exception:
        pass
    for ep in (EP_OUT, EP_IN):
        _clear_halt(dev, ep)


def from_usb_closeall(dev):
    # Raw CLOSEALL without a client (connect-time only). Returns True if the
    # device acknowledged it. A FAILED status (firmware without 0xC4) leaves
    # the pipe in sync, so no reset is needed. A transport error does reset.
    tag = 0x43414C4C
    try:
        dev.write(EP_OUT, _cbw(tag, _cdb(S_CLOSEALL), 0, False), timeout=3000)
        raw = bytes(dev.read(EP_IN, 13, timeout=3000))
    except Exception:
        reset_recovery(dev)
        return False
    if (len(raw) < 13 or raw[:4] != b"USBS"
            or struct.unpack("<I", raw[4:8])[0] != tag):
        reset_recovery(dev)
        return False
    return raw[12] == 0


def _clear_halt(dev, ep):
    try:
        dev.clear_halt(ep)
    except Exception:
        pass


def _cdb(sub, arg0=0, arg1=0, handle=0):
    # DEVICE: dispatch on CDB[0]==0xC4, then CDB[1]==sub-op. arg0/arg1 are
    # BE32 at CDB[2..5]/CDB[6..9], and handle is the single byte CDB[10]
    # (0..7 frozen ABI). Numeric range is enforced here: values that do not
    # fit u32 raise instead of silently wrapping (a >4GiB offset must never
    # read the wrong sector).
    for name, v in (("arg0", arg0), ("arg1", arg1)):
        if not isinstance(v, int) or v < 0 or v > 0xFFFFFFFF:
            raise ValueError("%s out of u32 range: %r" % (name, v))
    if not isinstance(handle, int) or handle < 0 or handle > 0xFF:
        raise ValueError("handle out of u8 range: %r" % handle)
    cdb = bytearray(16)
    cdb[0] = OP_FILE
    cdb[1] = sub & 0xFF
    cdb[2:6] = struct.pack(">I", arg0)
    cdb[6:10] = struct.pack(">I", arg1)
    cdb[10] = handle
    return bytes(cdb)


def _cbw(tag, cdb, xfer_len, direction_in):
    flags = 0x80 if direction_in else 0x00
    return (b"USBC" + struct.pack("<I", tag) + struct.pack("<I", xfer_len)
            + bytes([flags, 0, len(cdb)]) + cdb)


class C4Client:
    """Owns USB transport, CBW tags, sense decoding, SETPATH state, recovery.

    All public methods are thread-safe via one lock: each SETPATH+op pair
    (and each handle op / chunk) runs atomically, so WinFSP threads cannot
    interleave SETPATH buffers. Every path argument goes through
    to_device_path, so raw CLI / WinFSP-style paths are accepted. Use
    MockC4Client (below) with the same method names to test the WinFSP layer
    with no hardware.
    """

    def __init__(self, dev=None):
        self.dev = dev if dev is not None else _dev()
        self._tag = 0x534E4653
        self._lock = threading.Lock()

    # -- low level --
    def _tag_next(self):
        self._tag = (self._tag + 1) & 0xFFFFFFFF
        return self._tag

    def _send_cbw(self, tag, cdb, xfer_len, direction_in):
        # A CBW write failure means the device never saw a command: there is
        # no CSW to wait for, so go straight to reset recovery.
        cbw = _cbw(tag, cdb, xfer_len, direction_in)
        try:
            self.dev.write(EP_OUT, cbw, timeout=3000)
        except Exception as e:
            reset_recovery(self.dev)
            raise IOError("CBW write failed (%s)" % e)

    def _read_csw(self, tag):
        # BOT 6.7.2: on a stalled/timed-out CSW read, clear halt on IN and
        # retry once. If that fails too, do reset recovery. A stale
        # outstanding tag would otherwise poison every later call.
        raw = None
        err = None
        for attempt in (0, 1):
            try:
                raw = bytes(self.dev.read(EP_IN, 13, timeout=5000))
                break
            except Exception as e:
                err = e
                if attempt == 0:
                    _clear_halt(self.dev, EP_IN)
        if raw is None:
            reset_recovery(self.dev)
            raise IOError("CSW read failed (%s)" % err)
        if len(raw) < 13 or raw[:4] != b"USBS":
            reset_recovery(self.dev)
            raise IOError("bad CSW (%d bytes) %r" % (len(raw), raw[:4]))
        rtag, residue = struct.unpack("<II", raw[4:12])
        status = raw[12]
        if rtag != tag:
            reset_recovery(self.dev)
            raise IOError("CSW tag mismatch %#x != %#x" % (rtag, tag))
        if status == 2:  # phase error: pipe out of sync, sense is meaningless
            reset_recovery(self.dev)
            raise IOError("CSW phase error")
        return status, residue

    def _sense(self):
        # DEVICE: keep opcode-0x03 working and stash the sense triple on failure.
        try:
            cdb = bytes([0x03, 0, 0, 0, 0x1B, 0]) + bytes(10)
            tag = self._tag_next()
            self.dev.write(EP_OUT, _cbw(tag, cdb, 0x1B, True), timeout=3000)
            data = bytes(self.dev.read(EP_IN, 0x1B, timeout=3000))
            self._read_csw(tag)
            return data
        except Exception:
            return b""

    def _fail(self, tag, what):
        status, _ = self._read_csw(tag)
        if status == 0:
            return
        _raise_for_sense(what, self._sense())

    def _run_busy_retry(self, fn):
        # Safe because the path buffer persists across a failed op (spec).
        for i in range(_BUSY_RETRIES + 1):
            try:
                return fn()
            except BusyError:
                if i >= _BUSY_RETRIES:
                    raise
                time.sleep(_BUSY_DELAYS[min(i, len(_BUSY_DELAYS) - 1)])

    def _data_out(self, sub, payload, arg0=0, arg1=0, handle=0):
        # DEVICE: flags==0x00. SETPATH: copy to path buffer (NUL-check,
        # MAX_PATH, A:\ prefix or 05/24). WRITE: raw bytes for
        # (handle, offset=arg0, len=arg1). Consume all, store, then CSW GOOD
        # or CSW FAILED + ENOSPC sense. Never partial-success. RENAME dst:
        # payload is the NUL-terminated dst path.
        what = "subop 0x%02x out" % sub

        def once():
            tag = self._tag_next()
            # Built before any I/O: a bad argument raises ValueError here
            # and never reaches the stall handling below.
            cdb = _cdb(sub, arg0, arg1, handle)
            self._send_cbw(tag, cdb, len(payload), False)
            try:
                self.dev.write(EP_OUT, payload, timeout=5000)
            except Exception as e:
                # Early reject: device stalled OUT before taking the payload.
                # Clear the halt, THEN read the CSW + sense it carries --
                # never discard it with a full reset.
                _clear_halt(self.dev, EP_OUT)
                self._fail(tag, what)  # raises the mapped error if FAILED
                raise IOError("%s: payload stall with CSW GOOD (%s)" % (what, e))
            self._fail(tag, what)
        return self._run_busy_retry(once)

    def _failed_in(self, what, exact):
        # Data-IN CSW FAILED: READ at/after EOF (SENSE_EOF) is an empty read,
        # and anything else raises the mapped error.
        sense = self._sense()
        if not exact and _parse_sense(sense) == SENSE_EOF:
            return b""
        _raise_for_sense(what, sense)

    def _data_in(self, sub, want, arg0=0, arg1=0, handle=0, exact=True):
        # DEVICE: flags==0x80. Bulk-IN up to want, then CSW GOOD with
        # residue = want - sent (strict). Short READ at EOF is GOOD+residue.
        # exact=True (STAT/LIST/OPEN structs): short transfer is an error.
        # exact=False (READ only): short is EOF.
        what = "subop 0x%02x in" % sub

        def once():
            tag = self._tag_next()
            cdb = _cdb(sub, arg0, arg1, handle)
            self._send_cbw(tag, cdb, want, True)
            try:
                data = bytes(self.dev.read(EP_IN, want, timeout=5000))
            except Exception:
                # Stall, not timeout-noise: clear IN halt, then read the CSW
                # that follows it so a FAILED op still maps to its sense
                # (e.g. FileNotFoundError) instead of a generic IOError.
                _clear_halt(self.dev, EP_IN)
                try:
                    status, _res = self._read_csw(tag)
                except Exception:
                    reset_recovery(self.dev)
                    raise IOError("%s: stall, CSW unreadable" % what)
                if status != 0:
                    return self._failed_in(what, exact)
                raise IOError("%s: stall with CSW GOOD" % what)
            if len(data) > want:
                reset_recovery(self.dev)
                raise IOError("data-in overflow %d/%d" % (len(data), want))
            # Early-CSW detection applies to fixed structs only. A READ may
            # legitimately return 13 bytes that happen to start with USBS.
            if (exact and len(data) == 13 and data[:4] == b"USBS"
                    and want != 13):
                rtag, _res = struct.unpack("<II", data[4:12])
                if rtag != tag:
                    reset_recovery(self.dev)
                    raise IOError("CSW tag mismatch %#x != %#x" % (rtag, tag))
                if data[12] == 2:
                    reset_recovery(self.dev)
                    raise IOError("CSW phase error")
                if data[12] != 0:
                    return self._failed_in(what, exact)
                raise IOError("%s: missing data phase (got CSW)" % what)
            status, residue = self._read_csw(tag)
            if status != 0:
                return self._failed_in(what, exact)
            # Bring-up tolerance: accept residue==0 with short packet, but
            # never more than want. Final firmware MUST send residue exactly.
            if residue != want - len(data) and residue != 0:
                raise IOError("residue mismatch: want=%d got=%d residue=%d"
                              % (want, len(data), residue))
            if exact and len(data) != want:
                raise IOError("%s: truncated struct %d/%d" % (what, len(data), want))
            return data
        return self._run_busy_retry(once)

    def _no_data(self, sub, arg0=0, arg1=0, handle=0):
        # DEVICE: CLOSE/FLUSH/DELETE/MKDIR/RMDIR/CLOSEALL/TRUNCATE. Flush
        # where the spec says so, then send the CSW.
        what = "subop 0x%02x nodata" % sub

        def once():
            tag = self._tag_next()
            cdb = _cdb(sub, arg0, arg1, handle)
            self._send_cbw(tag, cdb, 0, False)
            self._fail(tag, what)
        return self._run_busy_retry(once)

    def _no_data_raw(self, cdb, what):
        # Send a prebuilt 16-byte CDB as a no-data command (with busy-retry).
        # Used by SETPATH_CHUNK, whose payload rides inside the CDB.
        def once():
            tag = self._tag_next()
            self._send_cbw(tag, cdb, 0, False)
            self._fail(tag, what)
        return self._run_busy_retry(once)

    def _setpath(self, wp, sub=S_SETPATH_CHUNK):
        # Set the device path buffer WITHOUT the broken data-OUT phase: stream
        # the bytes in <=11-byte pieces via no-data SETPATH_CHUNK commands. wp
        # is encode_path output (trailing NUL). The device NUL-terminates after
        # each chunk, so the bytes are sent without a terminator of their own.
        # sub=S_DSTPATH_CHUNK fills RENAME's destination buffer instead.
        body = wp[:-1] if wp[-1:] == b"\x00" else wp
        off, n = 0, len(body)
        first = True
        while off < n or first:
            first = False
            chunk = body[off:off + _PATH_CHUNK]
            cdb = bytearray(16)
            cdb[0] = OP_FILE
            cdb[1] = sub
            cdb[2] = (off >> 8) & 0xFF
            cdb[3] = off & 0xFF
            cdb[4] = len(chunk)
            cdb[5:5 + len(chunk)] = chunk
            self._no_data_raw(bytes(cdb), "setpath_chunk off=%d" % off)
            off += len(chunk)
            if not chunk:
                break  # empty path: one terminating chunk at offset 0

    # -- mount layer --
    def _memread(self, addr, n):
        # 0xC0 vendor RAM read (CDB: [2..5]=addr BE, [7..8]=len BE, 9 bytes).
        tag = self._tag_next()
        cdb = bytes([OP_MEMREAD, 0, (addr >> 24) & 0xFF, (addr >> 16) & 0xFF,
                     (addr >> 8) & 0xFF, addr & 0xFF, 0, (n >> 8) & 0xFF, n & 0xFF])
        self._send_cbw(tag, cdb, n, True)
        try:
            data = bytes(self.dev.read(EP_IN, n, timeout=5000))
        except Exception as e:
            reset_recovery(self.dev)
            raise IOError("memread %#x failed (%s)" % (addr, e))
        status, _ = self._read_csw(tag)
        if status != 0 or len(data) != n:
            raise IOError("memread %#x: status=%d len=%d" % (addr, status, len(data)))
        return data

    def mount_state(self, drive=DEFAULT_DRIVE):
        # -> (mounted: bool, fat_type, raw drive-object head). Read-only.
        drive = drive.upper()[:1]
        with self._lock:
            head = self._memread(DRIVE_OBJ[drive], _DRIVE_HEAD)
        if head[DRIVE_LETTER_OFF] != ord(drive):
            raise IOError("drive object for %s: has letter %#04x. The firmware "
                          "layout differs from this client" % (drive, head[DRIVE_LETTER_OFF]))
        mounted = bool(head[DRIVE_FLAGS_OFF] & DRIVE_MOUNTED_BIT)
        return mounted, head[DRIVE_FATTYPE_OFF], head

    def _drive_cdb(self, sub, drive):
        cdb = bytearray(16)
        cdb[0], cdb[1], cdb[2] = OP_FILE, sub, ord(drive.upper()[:1])
        return bytes(cdb)

    def mount(self, drive=DEFAULT_DRIVE):
        what = "mount %s:" % drive.upper()[:1]
        with self._lock:
            tag = self._tag_next()
            self._send_cbw(tag, self._drive_cdb(S_MOUNT, drive), 0, False)
            # Media init can outlast the normal 5 s CSW wait.
            raw = None
            try:
                raw = bytes(self.dev.read(EP_IN, 13, timeout=_MOUNT_TIMEOUT_MS))
            except Exception as e:
                reset_recovery(self.dev)
                raise IOError("%s: no CSW (%s)" % (what, e))
            if len(raw) < 13 or raw[:4] != b"USBS":
                reset_recovery(self.dev)
                raise IOError("%s: bad CSW" % what)
            if raw[12] != 0:
                _raise_for_sense(what, self._sense())

    def unmount(self, drive=DEFAULT_DRIVE):
        # Closes ALL gateway handles on the device first.
        with self._lock:
            self._no_data_raw(self._drive_cdb(S_UNMOUNT, drive),
                              "unmount %s:" % drive.upper()[:1])

    def send_vendor_cdb(self, cdb):
        # One no-data vendor command (any opcode) under the client lock, so a
        # busy keep-alive thread never interleaves with an FS op's CBW/CSW.
        with self._lock:
            self._no_data_raw(bytes(cdb), "vendor %#04x" % cdb[0])

    def busy(self, msg):
        """Context manager: modal pop-up on the machine while the block runs.
        msg = name from busy.MESSAGES ("saving", ...), firmware message id, or
        custom text (<= 14 UTF-8 bytes). Cosmetic: failures only warn."""
        import busy as _busy
        return _busy.Busy(self.send_vendor_cdb, msg, warn=_warn_once)

    @contextlib.contextmanager
    def volume(self, path, auto=True):
        """Make the volume holding `path` usable for the duration of the block.

        If the firmware already has it mounted (UI busy with it, or the
        maintenance USB-host check left it up) it is used as-is and left alone.
        Otherwise it is mounted here and unmounted afterwards, the same
        bracket the firmware's own features use, so the idle machine is left
        exactly as it was found. Do not use the machine's own USB functions
        while a transfer is running.
        """
        drive = to_device_path(path)[0]
        if not auto or drive not in DRIVE_OBJ:
            yield False
            return
        already, _letter, _t = self.mount_state(drive)
        if already:
            yield False
            return
        self.mount(drive)
        try:
            yield True
        finally:
            try:
                self.unmount(drive)
            except Exception as e:
                print("warning: unmount %s: failed: %s" % (drive, e), file=sys.stderr)

    # -- file ops (SETPATH+op is atomic under the lock) --
    @staticmethod
    def _wp(path):
        return encode_path(to_device_path(path))

    def set_path(self, path):
        # Test hook only: real ops always pair SETPATH with their consumer
        # under the same lock (see below). Do not call this then another op
        # from separate lock acquisitions.
        with self._lock:
            self._setpath(self._wp(path))

    def stat(self, path):
        wp = self._wp(path)
        with self._lock:
            self._setpath(wp)
            # DEVICE: pack with memcpy, never *(u64*)(buf+4).
            raw = self._data_in(S_STAT, 32, exact=True)
        attr, size = struct.unpack("<II", raw[0:8])
        if attr == ATTR_NONE:
            # Deprecated-compat fallback only. The canonical signal is FAILED+05/80/00.
            raise FileNotFoundError(path)
        return attr, size

    def is_dir(self, path):
        return _is_dir_attr(self.stat(path)[0])

    def list_dir(self, path):
        # Collected under lock (a generator would escape it).
        # DEVICE: first/next_full_entry_find, one 64B record per index.
        # NOTE: metadata ops stay path-based (not handle-based): FileX
        # delete/stat are path syscalls. OPEN-first would add a round trip and
        # handle leaks, and it would still need the path buffer (flash saved ~= 0).
        wp = self._wp(path)
        with self._lock:
            self._setpath(wp)
            out, idx = [], 0
            while True:
                raw = self._data_in(S_LIST, 64, arg0=idx, exact=True)
                attr, size = struct.unpack("<II", raw[0:8])
                name = raw[12:64].split(b"\x00", 1)[0]
                if attr == ATTR_NONE or not name:
                    return out
                # Defensive: firmware must already skip these.
                if name in (b".", b".."):
                    idx += 1
                    continue
                out.append((_decode_name(name), attr, size))
                idx += 1

    def open(self, path, flags):
        _check_open_flags(flags)
        wp = self._wp(path)
        with self._lock:
            self._setpath(wp)
            raw = self._data_in(S_OPEN, 4, arg0=flags, exact=True)
        h = struct.unpack("<I", raw)[0]
        if h >= MAX_HANDLES:
            raise IOError("bad handle %d (ABI 0..%d)" % (h, MAX_HANDLES - 1))
        return h

    def read_at(self, handle, offset, size):
        # Short reads are EOF: GOOD + residue. Zero bytes -> stop instead of spinning.
        # Past-EOF reads are GOOD+empty by spec (never 05/21 for EOF).
        # Lock is taken per chunk: handle ops do not depend on the path
        # buffer, so a stat from another thread may slip between chunks.
        out = bytearray()
        while len(out) < size:
            n = min(CHUNK, size - len(out))
            with self._lock:
                # DEVICE: seek + read. EOF short is GOOD + residue.
                blk = self._data_in(S_READ, n, arg0=offset + len(out), arg1=n,
                                    handle=handle, exact=False)
            if not blk:
                break
            out += blk
            if len(blk) < n:
                break
        return bytes(out)

    def write_at(self, handle, offset, data):
        done = 0
        while done < len(data):
            n = min(CHUNK, len(data) - done)
            with self._lock:
                # DEVICE: seek + write (buffering OK), flush rules per spec.
                self._data_out(S_WRITE, data[done:done + n],
                               arg0=offset + done, arg1=n, handle=handle)
            done += n

    def close(self, handle):
        with self._lock:
            self._no_data(S_CLOSE, handle=handle)

    def close_all(self):
        with self._lock:
            self._no_data(S_CLOSEALL)

    def truncate(self, handle, size):
        # Shrink only (spec): growing is 05/21 -> ValueError.
        with self._lock:
            self._no_data(S_TRUNCATE, arg0=size, handle=handle)

    def flush(self, handle):
        with self._lock:
            self._no_data(S_FLUSH, handle=handle)

    def delete(self, path):
        wp = self._wp(path)
        with self._lock:
            self._setpath(wp)
            self._no_data(S_DELETE)

    def mkdir(self, path):
        wp = self._wp(path)
        with self._lock:
            self._setpath(wp)
            self._no_data(S_MKDIR)

    def rmdir(self, path):
        wp = self._wp(path)
        with self._lock:
            self._setpath(wp)
            self._no_data(S_RMDIR)

    def rename(self, src, dst):
        # Both paths go up as no-data chunks (dst into the destination buffer),
        # then RENAME runs as a no-data command.
        ws, wd = self._wp(src), self._wp(dst)
        with self._lock:
            self._setpath(ws)
            self._setpath(wd, S_DSTPATH_CHUNK)
            self._no_data(S_RENAME)

    def refused_ops(self):
        """Ops this firmware refuses: REFUSED_OPS, or REFUSED_OPS_OLD on firmware from
        before the path ops (it refuses the empty DSTPATH_CHUNK probe, which has no
        side effects on firmware that knows it)."""
        cdb = bytearray(16)
        cdb[0], cdb[1] = OP_FILE, S_DSTPATH_CHUNK
        try:
            with self._lock:
                self._no_data_raw(bytes(cdb), "dstpath probe")
        except ValueError:
            return REFUSED_OPS_OLD
        return REFUSED_OPS

    def _close_quiet(self, h):
        try:
            self.close(h)
        except Exception:
            pass

    def pull(self, remote, local):
        attr, size = self.stat(remote)
        if _is_dir_attr(attr):
            raise IsADirectoryError(remote)
        h = self.open(remote, O_READ)
        tmp = local + ".part"
        try:
            try:
                with open(tmp, "wb") as f:
                    off = 0
                    while off < size:
                        blk = self.read_at(h, off, min(CHUNK, size - off))
                        if not blk:
                            raise IOError("truncated: %r ended at %d/%d bytes"
                                          % (remote, off, size))
                        f.write(blk)
                        off += len(blk)
                os.replace(tmp, local)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        finally:
            # Read-only handle: a close error cannot mean lost data here.
            self._close_quiet(h)
        return size

    def push(self, local, remote):
        # Open local FIRST: a bad local path must never truncate the remote.
        with open(local, "rb") as f:
            try:
                h = self.open(remote, O_WRITE | O_TRUNC)
            except FileNotFoundError:
                # Only missing-file falls through to create. Busy/no-medium errors
                # propagate and never truncate.
                h = self.open(remote, O_WRITE | O_CREATE | O_TRUNC)
            body_exc = None
            try:
                off = 0
                while True:
                    blk = f.read(CHUNK)
                    if not blk:
                        break
                    self.write_at(h, off, blk)
                    off += len(blk)
            except BaseException as e:
                body_exc = e
                raise
            finally:
                try:
                    self.close(h)  # durability gate: ENOSPC surfaces here
                except Exception:
                    if body_exc is None:
                        raise
                    # Body already failed: its error wins. The slot may leak, but
                    # the next connect's CLOSEALL / BOT reset reclaims it.
        # Every command can come back GOOD while the data still went astray
        # (an old firmware stored 64 B per 4 KB command): check the result.
        _attr, size = self.stat(remote)
        if size != off:
            raise IOError("push %s: device file is %d bytes, sent %d"
                          % (remote, size, off))
        return off


class MockC4Client:
    """In-memory C4Client double: same method names, no USB.

    Case-preserving but case-insensitive (like FAT). Dirs may be implicit
    (a listed child implies its parents). Thread-safe via one lock. Paths go
    through the same to_device_path/encode_path validation as the real
    client, and failures raise the same shapes as _raise_for_sense:
    FileNotFoundError, FileExistsError, Is/NotADirectoryError,
    PermissionError, ValueError, OSError(ENOSPC/ENOTEMPTY).
    """

    ROOT = DEFAULT_DRIVE.upper() + ":\\"

    def __init__(self):
        self._files = {self.ROOT: None}  # upper-key -> None(dir) | bytearray
        self._case = {self.ROOT: self.ROOT}  # upper-key -> original-case path
        self._handles = {}  # h -> {"key": upper, "buf": bytearray, "flags": int}
        self._lock = threading.Lock()

    # -- helpers (call with lock held where they read state) --
    def _norm(self, p):
        dp = to_device_path(p)
        encode_path(dp)  # same length/NUL/ASCII validation as the wire path
        return dp.upper()

    def _is_implicit_dir(self, key):
        prefix = key.rstrip("\\") + "\\"
        return any(k != key and k.startswith(prefix) for k in self._files)

    def _dir_exists(self, key):
        if key in self._files:
            return self._files[key] is None
        return self._is_implicit_dir(key)

    @staticmethod
    def _parent_key(key):
        parent = key.rpartition("\\")[0]
        # "" or a bare "X:" means that drive's root "X:\".
        if parent == "":
            return MockC4Client.ROOT
        if len(parent) == 2 and parent[1] == ":":
            return parent + "\\"
        return parent

    def _check_parent(self, key, path):
        parent = self._parent_key(key)
        if parent in self._files and self._files[parent] is not None:
            raise NotADirectoryError("parent is a file: %r" % (path,))
        if not self._dir_exists(parent):
            raise FileNotFoundError("parent missing: %r" % (path,))

    def _in_use(self, key):
        under = key.rstrip("\\") + "\\"
        return any(h["key"] == key or h["key"].startswith(under)
                   for h in self._handles.values())

    # -- ops --
    def stat(self, path):
        key = self._norm(path)
        with self._lock:
            if key in self._files:
                if self._files[key] is None:
                    return 0x10, 0
                return 0x20, len(self._files[key])
            if self._is_implicit_dir(key):
                return 0x10, 0
        raise FileNotFoundError(path)

    def is_dir(self, path):
        return _is_dir_attr(self.stat(path)[0])

    def list_dir(self, path):
        key = self._norm(path)
        with self._lock:
            if key in self._files and self._files[key] is not None:
                raise NotADirectoryError(path)
            if not self._dir_exists(key):
                raise FileNotFoundError(path)
            prefix = key.rstrip("\\") + "\\"
            out, seen = [], set()
            for k in list(self._files):
                if k == key or not k.startswith(prefix):
                    continue
                first_up = k[len(prefix):].split("\\", 1)[0]
                if not first_up or first_up in seen:
                    continue
                seen.add(first_up)
                full = prefix + first_up
                if full in self._case:
                    disp = self._case[full].rsplit("\\", 1)[-1]
                else:  # implicit dir: take its spelling from the child
                    child = self._case.get(k, k)
                    disp = child[len(prefix):].split("\\", 1)[0]
                if full in self._files and self._files[full] is not None:
                    out.append((disp, 0x20, len(self._files[full])))
                else:
                    out.append((disp, 0x10, 0))
            return out

    def open(self, path, flags):
        _check_open_flags(flags)
        key = self._norm(path)
        with self._lock:
            slot = next((h for h in range(MAX_HANDLES)
                         if h not in self._handles), None)
            if slot is None:  # checked BEFORE creating anything (spec)
                raise OSError(errno.ENOSPC, "no free handles")
            exists = key in self._files or self._is_implicit_dir(key)
            if flags & O_EXCL and exists:
                raise FileExistsError(path)
            if self._dir_exists(key):
                raise IsADirectoryError(path)
            if key not in self._files:
                if not flags & O_CREATE:
                    raise FileNotFoundError(path)
                self._check_parent(key, path)
                self._files[key] = bytearray()
                self._case[key] = to_device_path(path)
            elif flags & O_TRUNC:
                del self._files[key][:]  # in place: no handle detaches
            self._handles[slot] = {"key": key, "buf": self._files[key],
                                   "flags": flags}
            return slot

    def _handle(self, handle):
        try:
            return self._handles[handle]
        except KeyError:
            raise ValueError("bad handle %r" % (handle,))  # 05/24 analog

    def read_at(self, handle, offset, size):
        with self._lock:
            ent = self._handle(handle)
            if not ent["flags"] & O_READ:
                raise PermissionError("handle not open for read")
            return bytes(ent["buf"][offset:offset + size])

    def write_at(self, handle, offset, data):
        with self._lock:
            ent = self._handle(handle)
            if not ent["flags"] & O_WRITE:
                raise PermissionError("handle not open for write")
            buf = ent["buf"]
            if offset > len(buf):
                raise ValueError("write offset %d past EOF %d" % (offset, len(buf)))
            if offset + len(data) > 64 * 1024 * 1024:
                raise OSError(errno.ENOSPC, "mock full")
            buf[offset:offset + len(data)] = data

    def close(self, handle):
        # Unknown-handle close is ignored here (real firmware: 05/24). It is kept
        # lenient so teardown double-closes do not mask the error under test.
        with self._lock:
            self._handles.pop(handle, None)

    def close_all(self):
        with self._lock:
            self._handles.clear()

    def truncate(self, handle, size):
        with self._lock:
            ent = self._handle(handle)
            if not ent["flags"] & O_WRITE:
                raise PermissionError("handle not open for write")
            buf = ent["buf"]
            if size > len(buf):
                raise ValueError("truncate grows file (%d > %d)" % (size, len(buf)))
            del buf[size:]

    def flush(self, handle):
        with self._lock:
            self._handle(handle)

    def delete(self, path):
        key = self._norm(path)
        with self._lock:
            if self._dir_exists(key):
                raise IsADirectoryError(path)
            if key not in self._files:
                raise FileNotFoundError(path)
            if self._in_use(key):
                raise PermissionError("in use: %r" % (path,))
            del self._files[key]
            self._case.pop(key, None)

    def mkdir(self, path):
        key = self._norm(path)
        with self._lock:
            if key in self._files or self._is_implicit_dir(key):
                raise FileExistsError(path)
            self._check_parent(key, path)
            self._files[key] = None
            self._case[key] = to_device_path(path)

    def rmdir(self, path):
        key = self._norm(path)
        with self._lock:
            if key == self.ROOT:
                raise ValueError("cannot remove root")
            if key in self._files and self._files[key] is not None:
                raise NotADirectoryError(path)
            if not self._dir_exists(key):
                raise FileNotFoundError(path)
            if self._in_use(key):
                raise PermissionError("in use: %r" % (path,))
            prefix = key.rstrip("\\") + "\\"
            if any(k != key and k.startswith(prefix) for k in self._files):
                raise OSError(errno.ENOTEMPTY, "directory not empty: %r" % (path,))
            self._files.pop(key, None)
            self._case.pop(key, None)

    def rename(self, src, dst):
        sk, dk = self._norm(src), self._norm(dst)
        dst_case = to_device_path(dst)
        with self._lock:
            if sk == self.ROOT or dk == self.ROOT:
                raise ValueError("cannot rename root")
            if not (sk in self._files or self._is_implicit_dir(sk)):
                raise FileNotFoundError(src)
            if sk[0] != dk[0]:
                raise ValueError("rename across drives")       # firmware: 05/24
            if dk.startswith(sk + "\\"):
                raise ValueError("cannot move a directory into itself")
            self._check_parent(dk, dst)
            if self._in_use(sk):
                raise PermissionError("in use: %r" % (src,))
            if dk != sk and (dk in self._files or self._is_implicit_dir(dk)):
                raise FileExistsError(dst)
            under = sk + "\\"
            moves = [(k, dk + k[len(sk):]) for k in list(self._files)
                     if k == sk or k.startswith(under)]
            vals = {k: self._files.pop(k) for k, _ in moves}
            cases = {k: self._case.pop(k, None) for k, _ in moves}
            for k, nk in moves:
                self._files[nk] = vals[k]
                oc = cases[k]
                if k == sk:
                    self._case[nk] = dst_case
                elif oc is not None:
                    self._case[nk] = dst_case + oc[len(sk):]

    def refused_ops(self):
        return REFUSED_OPS

    def pull(self, remote, local):
        attr, size = self.stat(remote)
        if _is_dir_attr(attr):
            raise IsADirectoryError(remote)
        h = self.open(remote, O_READ)
        try:
            tmp = local + ".part"
            try:
                with open(tmp, "wb") as f:
                    f.write(self.read_at(h, 0, size))
                os.replace(tmp, local)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        finally:
            self.close(h)
        return size

    def push(self, local, remote):
        with open(local, "rb") as f:
            data = f.read()
        try:
            h = self.open(remote, O_WRITE | O_TRUNC)
        except FileNotFoundError:
            h = self.open(remote, O_WRITE | O_CREATE | O_TRUNC)
        try:
            self.write_at(h, 0, data)
        finally:
            self.close(h)
        return len(data)


def _decode_name(b):
    if isinstance(b, str):
        return b
    return b.decode("ascii", "surrogateescape")


def encode_path(path):
    # str -> bytes + NUL. Strict: must already be <drive>:\-prefixed (use
    # to_device_path first for anything user- or WinFSP-supplied).
    try:
        b = (path.encode("ascii", "surrogateescape")
             if isinstance(path, str) else bytes(path))
    except UnicodeEncodeError as e:
        raise ValueError("path has non-ASCII characters (prototype is "
                         "ASCII-only): %r" % (path,)) from e
    if b"\x00" in b:
        raise ValueError("path contains NUL: %r" % (path,))
    if not (len(b) >= 3 and 0x41 <= (b[0] & ~0x20) <= 0x5A
            and b[1:3] == b":\\"):
        raise ValueError("path must start with <drive>:\\ (got %r)" % (path,))
    if len(b) + 1 > MAX_PATH:
        raise ValueError("path too long (%d >= %d)" % (len(b), MAX_PATH))
    return b + b"\x00"


def to_device_path(path, default_drive=None):
    # Idempotent normalizer. Accepts WinFSP "\d\f", "/d/f", "X:\d\f", "X:/d\f",
    # "X:d\f", bare "X:" (= that drive's root). A path with no drive letter gets
    # default_drive (or module DEFAULT_DRIVE). Collapses duplicate and trailing
    # separators and rejects "." and ".." components (resolve them upstream).
    if isinstance(path, bytes):
        path = path.decode("ascii", "surrogateescape")
    t = path.replace("/", "\\")
    m = _DRIVE_RE.match(t)
    if m:
        drive = t[0].upper()
        t = t[2:]
    else:
        drive = (default_drive or DEFAULT_DRIVE).upper()
    parts = [c for c in t.split("\\") if c != ""]
    for c in parts:
        if c in (".", ".."):
            raise ValueError("'.'/'..' components not supported: %r" % (path,))
    return "%s:\\" % drive + "\\".join(parts) if parts else "%s:\\" % drive


# --- module-level wrappers: per-dev cached clients (lock-preserving) ---
_CLIENTS = {}
_CLIENTS_LOCK = threading.Lock()


def _client_for(dev):
    # Cache one C4Client per usb handle so the SETPATH lock is actually shared.
    # Strong refs: dev objects live for the process, which avoids id() reuse bugs.
    with _CLIENTS_LOCK:
        ent = _CLIENTS.get(id(dev))
        if ent is not None and ent[0] is dev:
            return ent[1]
        c = C4Client(dev)
        _CLIENTS[id(dev)] = (dev, c)
        return c


def set_path(dev, path):
    # Test hook only (see C4Client.set_path). Prefer client methods.
    _client_for(dev).set_path(path)


def stat(dev, path):
    return _client_for(dev).stat(path)


def list_dir(dev, path):
    return iter(_client_for(dev).list_dir(path))


def open_file(dev, path, flags):
    return _client_for(dev).open(path, flags)


def read_at(dev, handle, offset, size):
    return _client_for(dev).read_at(handle, offset, size)


def write_at(dev, handle, offset, data):
    return _client_for(dev).write_at(handle, offset, data)


def close_file(dev, handle):
    _client_for(dev).close(handle)


def delete_file(dev, path):
    _client_for(dev).delete(path)


def make_directory(dev, path):
    _client_for(dev).mkdir(path)


def remove_directory(dev, path):
    _client_for(dev).rmdir(path)


def pull(dev, remote, local):
    return _client_for(dev).pull(remote, local)


def push(dev, local, remote):
    return _client_for(dev).push(local, remote)


# Windows drive-letter mount (WinFsp): see winfsp_mount.py (DriveFS / DriveMount).


_WARNED = set()


def _warn_once(msg):
    # The busy pop-up is cosmetic (and absent on older firmware): say so once.
    key = msg.split(":")[0]
    if key not in _WARNED:
        _WARNED.add(key)
        print("warning: %s" % msg, file=sys.stderr)


# CLI command -> busy pop-up (None = too quick to be worth a pop-up).
_CMD_BUSY = {"pull": "retrieving", "push": "saving", "rm": "deleting", "rmdir": "deleting"}


def _printable(s):
    # surrogateescape'd names would raise on print(), so they are escaped instead.
    return s.encode("ascii", "backslashreplace").decode("ascii")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    auto = "--no-mount" not in argv
    show_busy = "--no-busy" not in argv
    argv = [a for a in argv if a not in ("--no-mount", "--no-busy")]
    usage = {"ls": "<DIR>", "stat": "<PATH>", "pull": "<REMOTE> <LOCAL>",
             "push": "<LOCAL> <REMOTE>", "rm": "<PATH>", "mkdir": "<DIR>",
             "rmdir": "<DIR>", "mv": "<OLD> <NEW>", "mounts": "[DRIVE]",
             "mount": "<DRIVE>", "unmount": "<DRIVE>",
             "busy": "<NAME|0xID|TEXT|off> [SECONDS]"}
    if len(argv) < 1 or argv[0] in ("-h", "--help"):
        print("usage: cm110_fs.py [--no-mount] [--no-busy] " + " | ".join(
            "%s %s" % kv for kv in usage.items()))
        print("  pull/push show a modal busy pop-up on the machine (--no-busy: no pop-up. Without"
              " it the machine's idle UI unmounts A: within seconds, cutting long transfers"
              " short).")
        print("  busy NAME|0xID|TEXT [SECONDS]: show a pop-up (names: %s, TEXT <= 14"
              " bytes UTF-8). Without SECONDS it expires 15 s after the last USB"
              " command. 'busy off' clears it." % ", ".join(sorted(_busy_names())))
        print("  paths default to %s:\\ (the USB-A drive). The drive is mounted for"
              " the command and unmounted after, unless the machine already has"
              " it mounted (--no-mount: never touch the mount state)." % DEFAULT_DRIVE)
        return 1
    cmd = argv[0]
    want = {"ls": 2, "stat": 2, "pull": 3, "push": 3, "rm": 2, "mkdir": 2,
            "rmdir": 2, "mv": 3, "mounts": 1,
            "mount": 2, "unmount": 2, "busy": 2}
    if cmd not in want:
        print("unknown cmd %r" % cmd)
        return 1
    if len(argv) < want[cmd]:
        print("usage: cm110_fs.py %s %s" % (cmd, usage[cmd]))
        return 1
    try:
        c = C4Client()
    except ConnectionError as e:
        print("device error: %s" % e)
        return 2
    try:
        if cmd == "mounts":
            drv = (argv[1] if len(argv) > 1 else DEFAULT_DRIVE).upper()[:1]
            for d in ([drv] if len(argv) > 1 else sorted(DRIVE_OBJ)):
                m, fat, head = c.mount_state(d)
                print("%s: %-11s flags=%#04x fat_type=%#04x  obj@%08x" % (
                    d, "MOUNTED" if m else "not mounted", head[DRIVE_FLAGS_OFF],
                    fat, DRIVE_OBJ[d]))
            return 0
        if cmd == "mount":
            c.mount(argv[1])
            print("mounted %s:" % argv[1].upper()[:1])
            return 0
        if cmd == "unmount":
            c.unmount(argv[1])
            print("unmounted %s:" % argv[1].upper()[:1])
            return 0
        if cmd == "busy":
            return _busy_cmd(c, argv[1], argv[2] if len(argv) > 2 else None)
        target = argv[2] if cmd == "push" else argv[1]
        pop = _CMD_BUSY.get(cmd) if show_busy else None
        # The pop-up also covers the mount: media init can take seconds and a
        # UI drive operation then would be most harmful.
        with (c.busy(pop) if pop else contextlib.nullcontext()):
            with c.volume(target, auto=auto):
                _run_cmd(c, cmd, argv)
    except (OSError, ValueError, RuntimeError) as e:
        print("error: %s" % e)
        return 3
    return 0


def _busy_names():
    import busy as _busy
    return _busy.MESSAGES


def _parse_busy_arg(arg):
    if arg.lower() in ("off", "clear"):
        return None
    if arg in _busy_names():
        return arg
    if re.fullmatch(r"0[xX][0-9a-fA-F]{1,2}", arg):
        return int(arg, 16)
    return arg                                  # custom text


def _busy_cmd(c, arg, seconds):
    import busy as _busy
    msg = _parse_busy_arg(arg)
    if seconds is None:
        c.send_vendor_cdb(_busy.busy_cdb(msg))
        print("busy %s" % ("cleared" if msg is None else "shown: %r" % (msg,)))
        return 0
    with c.busy(msg):
        print("busy shown: %r for %s s (Ctrl+C to stop early)" % (msg, seconds))
        try:
            time.sleep(float(seconds))
        except KeyboardInterrupt:
            pass
    print("busy cleared")
    return 0


def _run_cmd(c, cmd, argv):
    if cmd == "ls":
        for name, attr, size in c.list_dir(argv[1]):
            print("%s %10d %s" % ("<DIR>" if _is_dir_attr(attr) else "     ",
                                  size, _printable(name)))
    elif cmd == "stat":
        attr, size = c.stat(argv[1])
        print("attr=%#x size=%d" % (attr, size))
    elif cmd == "pull":
        c.pull(argv[1], argv[2])
        print("pulled -> %s" % argv[2])
    elif cmd == "push":
        c.push(argv[1], argv[2])
        print("pushed -> %s" % argv[2])
    elif cmd == "rm":
        c.delete(argv[1])
        print("removed %s" % argv[1])
    elif cmd == "mkdir":
        c.mkdir(argv[1])
        print("created %s" % argv[1])
    elif cmd == "rmdir":
        c.rmdir(argv[1])
        print("removed %s" % argv[1])
    elif cmd == "mv":
        c.rename(argv[1], argv[2])
        print("moved %s -> %s" % (argv[1], argv[2]))


if __name__ == "__main__":
    sys.exit(main())