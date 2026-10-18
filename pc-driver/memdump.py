"""memdump.py: dump CM110 RAM/flash over USB via the vendor memory-read command.

Requires firmware flashed with SNC0001V1819W_memdump.upf, which installs a
vendor SCSI handler (opcodes 0xC0 = read RAM, 0xC1 = read flash, and both read an
absolute 32-bit address since RAM and flash are memory-mapped).

CDB (9 bytes): [0]=opcode  [2..5]=addr big-endian  [7..8]=length big-endian
Transfer: bulk-OUT CBW -> bulk-IN data (length bytes) -> bulk-IN CSW (13 bytes).
Max 0x1400 (5120) bytes per command. This tool uses 4096.

Usage:
  python memdump.py probe                         # sanity: read 16 B @0x84060000 (boot vector)
  python memdump.py read  <addr_hex> <len_hex>    # hex-dump a region to stdout
  python memdump.py flash [out.bin] [--restart]   # dump all 16 MB flash (0x84000000, 0x1000000),
                                                  # streams to out.bin and RESUMES from its length
  python memdump.py ram   <addr_hex> <size_hex> [out.bin]
  python memdump.py dump  <op_hex> <addr_hex> <size_hex> <out.bin>
  python memdump.py dumpscan <out.bin> [--size <size_hex>] [--slot <name>] [--info] [--force]
                                              [--addr <hex>] [--heap-table <hex>] [--raw]
                                              # resolve ScanDstBuff handle -> data ptr
                                              # via FUN_88d4ce38 replica and dump it.
                                              # slots: ScanDstBuff (0x8801413C),
                                              #   ScanWork0..3 (0x88013028+0x24/0x28/0x2C/0x30).
                                              # --info only prints handles/pointers.
                                              # default size = heap remaining capped to 0x2200000.
                                              # --force dumps even outside RAM window (torn-read debug).
                                              # --addr dumps a manual pointer, skipping deref.
                                              # --raw snapshots slot/heap-table words without deref.
                                              # mid-scan the heap mutates: expect torn reads and
                                              # partial dumps (tolerant reader keeps what it got).

Motion (0xC3, main-firmware vendor handler):
  python memdump.py ping                      # no-data dispatch test
  python memdump.py g0 <X> <Y> [rel]          # rapid XY move (steps), max speed
  python memdump.py g1 <X> <Y> [feed] [rel]   # cut XY move (steps), feed 0..9
  python memdump.py m3 [pressure]             # tool DOWN to the blade-table depth (-9..9, default 0)
  python memdump.py m3 raw <v>                # tool DOWN to Z_base + 4*v (no table, no trim)
  python memdump.py m5                        # tool UP
   python memdump.py feed                      # run feeder (mode-3, works without a mat)
   python memdump.py ww <addr_hex> <val_hex>  # 0xC3/0x11 guarded RAM word write
   python memdump.py motion <subop_hex> [X Y Z feed flags mode]
   python memdump.py tool [status]             # tool holder kind, switches, sensor-off flag, op mode
   python memdump.py tool off [std|deep] [orig|general]
                                              # tool override: ignore holder switches, kind follows
                                              # the selected mode (RAM only, cleared at power-off)
   python memdump.py tool auto                 # back to switch-based holder detection
   python memdump.py tool kind <n>             # force kind n now (re-derived at next job start)
  (X/Y are signed STEP counts, 'rel' = relative to current position. Start small.)

Locus (EXPERIMENTAL true 2-axis coordinated moves, 0xC3 sub-ops 0x02/0x03):
  python memdump.py lstate                    # head/mat state, X/Y, limits, speed, timer ids
  python memdump.py lmove <X> <Y> [speed] [abs]  # coordinated move (default relative, speed 5),
                                              # polls until done, Ctrl-C/timeout -> lstop
  python memdump.py lstop                     # emergency stop (timer 4 off, states -> 0)
  (~70.8 steps/mm X, ~70.4 steps/mm Y. pc-driver/locus_test.py runs a guided test.)

Scanner (0xC3 / 0xC0, main-firmware vendor handler):
  python memdump.py scan [out.jpg] [--dpi 300|200|150|100] [--raw] [--mode M] [--mat K] [--no-load]
                                              # init/load if needed, post scan, poll, fetch, free.
                                              # Default: JPEG (mode 0, full 12x12, scan-engine
                                              # encoded). --raw: mode 1 uncompressed planar RGB
                                              # (~37 MB, R/G/B plane offsets in scanstatus).
                                              # Any loaded mat kind scans (no override needed),
                                              # and --mat K additionally overwrites the kind.
                                              # --wide: whole CIS width (3632 px @300 vs 3504,
                                              # ~5.4 mm more each side, needs scan_wide in caps)
                                              # and 64 lines more above and below (the mat
                                              # starts 5.5 mm less fed in, put back after)
  python memdump.py scanstatus                          # display current scan state and buffer pointer
  python memdump.py scanstop                            # abort running scan (event 12)
  python memdump.py scanfree                            # free scan buffer
  python memdump.py movstate                  # MovState task state + recognised mat kind
  python memdump.py scaninit                  # power-on state 0 -> Ready (homes, EJECTS a mat)
  python memdump.py matload / matunload       # like the Load/Unload buttons (load = recognise)
  python memdump.py matloadcustom <kind> [top_margin_mm]
  python memdump.py caps                      # does the flashed vendor firmware have 0x54 / 0x55?
  python memdump.py matdiag                   # MovState, mat kind, mat mover, Y counter, Z state
                                              # load WITHOUT recognition as mat kind 1..8 (0xC3/0x54),
                                              # Y origin moved for a grid top_margin from the edge (60)
  python memdump.py movevent <ev> [p0] [p1]   # post a raw MovState event 0..14 (0xC3/0x53)
  MovState only changes on events (5 init, 6 load, 7 unload, 10 scan, 12 abort),
  and scans are accepted in Ready (2) only. Never write MovState directly.

Settings (0xC5, main-firmware vendor handler):
  python memdump.py settings                            # retrieve and display all device settings (64B block)
  python memdump.py getsetting <key>                    # retrieve individual setting by name or key ID
  python memdump.py setsetting <key> <val> [--no-eeprom]# set setting (updates RAM + commits to EEPROM)
"""
import sys, struct, time, contextlib
try:
    import usb.core, usb.util
    _USB_ERRORS = (usb.core.USBError,)
except ImportError:                 # no pyusb: the Windows pass-through path still works
    usb = None
    _USB_ERRORS = ()
import scsipt

VID, PID = 0x04F9, 0x2107
EP_OUT, EP_IN = 0x01, 0x82
CHUNK = 0x1000          # 4096 (<= firmware cap 0x1400)
OP_RAM, OP_FLASH = 0xC0, 0xC1
# what to tell the user when the machine lacks a SnowCut firmware feature
FW_HINT = "the machine's firmware may need updating (Machine > Machine Settings > Firmware)"

class DeviceNotFound(IOError):
    """The machine is not on the USB bus, or USB cannot be opened at all."""

class NeedsElevation(DeviceNotFound):
    """The machine is there, but the libusb-free route needs an administrator process."""

class ProtocolError(IOError):
    """The reply was not a valid mass-storage (BOT) status block."""

class CommandRejected(IOError):
    """The machine answered, but with a failed status for the command."""

def _dev():
    """Open the machine. Raises DeviceNotFound (an IOError, which the CLI prints before exiting with 1).
    The caller owns the handle and must close_dev() it when its operation is done: on Windows
    only one handle can have the machine open, so a handle left open blocks every later
    operation (use `with device() as dev:` where possible)."""
    try:
        dev = scsipt.open_machine(VID, PID)
    except scsipt.OpenFailed as f:
        if f.elevation:
            raise NeedsElevation(
                "the machine is connected, but Windows needs administrator rights to reach it. "
                "Choose Machine > Connect with Administrator Rights") from None
        if f.open_error is not None:
            raise DeviceNotFound("the machine was found but could not be opened (%s). Another "
                                 "program may be using it"
                                 % _usb_reason(f.open_error)) from None
        raise DeviceNotFound(
            "machine not found. Check that it is switched on and the USB cable "
            "is connected") from None
    reset_recovery(dev)
    return dev

@contextlib.contextmanager
def device():
    """`with device() as dev:` opens the machine for one operation and always releases it."""
    dev = _dev()
    try:
        yield dev
    finally:
        close_dev(dev)

def _usb_reason(e):
    """The useful part of a pyusb/libusb error ('... win error: <text>' on Windows)."""
    t = str(getattr(e, "strerror", None) or e)
    if "win error:" in t:
        t = t.split("win error:", 1)[1]
    return t.strip().rstrip(".") or e.__class__.__name__

def friendly_error(e):
    """One plain sentence for an exception from talking to the machine."""
    if isinstance(e, DeviceNotFound):
        t = str(e) or "machine not found"
        return t[0].upper() + t[1:] + "."
    if isinstance(e, ProtocolError):
        return ("The machine sent an unexpected reply (%s). Check that no other program is "
                "using it, then try again." % e)
    if isinstance(e, CommandRejected):
        return "The machine refused the command (%s): %s." % (e, FW_HINT)
    if isinstance(e, scsipt.PassthroughStall):
        return "The machine refused the command: %s." % FW_HINT
    if isinstance(e, scsipt.AccessDenied):
        return ("Windows needs administrator rights to reach the machine. Choose "
                "Machine > Connect with Administrator Rights.")
    timeout_types = tuple(t for t in (getattr(usb.core, "USBTimeoutError", None)
                                      if usb else None, scsipt.PassthroughTimeout) if t)
    if isinstance(e, timeout_types) or (isinstance(e, _USB_ERRORS + (scsipt.PassthroughError,))
                                        and "timeout" in str(e).lower()):
        return ("The machine stopped responding. If it restarted or was switched off, wait "
                "until it has started up and try again.")
    if isinstance(e, _USB_ERRORS + (scsipt.PassthroughError,)):
        return ("Lost the USB connection to the machine (%s). Check the cable, reconnect it "
                "if needed, and try again." % _usb_reason(e))
    return str(e) or e.__class__.__name__

def close_dev(dev):
    """Release the interface and close the OS handle. Drop the Device object afterwards:
    pyusb silently reopens a disposed device on its next transfer."""
    if dev is not None:
        if getattr(dev, "passthrough", False):     # in-process or via the elevated helper
            dev.close()
            return
        try: usb.util.dispose_resources(dev)
        except Exception: pass

def reset_recovery(dev):
    """BOT mass-storage reset + clear halts on both bulk endpoints."""
    try: dev.ctrl_transfer(0x21, 0xFF, 0, 0, 0, timeout=1000)   # Bulk-Only Reset
    except Exception: pass
    for ep in (EP_OUT, EP_IN):
        try: dev.clear_halt(ep)
        except Exception: pass

def inquiry(dev):
    """Standard INQUIRY (works on any firmware). Returns the 36-byte response."""
    cdb = bytes([0x12,0,0,0,0x24,0]) + bytes(10)
    cbw = (b'USBC' + struct.pack('<I',0x494e5100) + struct.pack('<I',36)
           + bytes([0x80,0,6]) + cdb)
    dev.write(EP_OUT, cbw, timeout=3000)
    data = bytes(dev.read(EP_IN, 36, timeout=3000))
    try: dev.read(EP_IN, 13, timeout=3000)
    except Exception: pass
    return data

def _cbw(tag, opcode, addr, length):
    cdb = bytes([opcode, 0,
                 (addr >> 24) & 0xff, (addr >> 16) & 0xff, (addr >> 8) & 0xff, addr & 0xff,
                 0, (length >> 8) & 0xff, length & 0xff])
    cdb += bytes(16 - len(cdb))
    return (b'USBC' + struct.pack('<I', tag) + struct.pack('<I', length)
            + bytes([0x80, 0, 9]) + cdb)      # flags=data-IN, LUN=0, CDBlen=9

def _read_chunk(dev, tag, opcode, addr, length):
    dev.write(EP_OUT, _cbw(tag, opcode, addr, length), timeout=3000)
    data = bytes(dev.read(EP_IN, length, timeout=5000))
    csw = bytes(dev.read(EP_IN, 13, timeout=3000))
    if csw[:4] != b'USBS':
        raise ProtocolError("bad CSW signature %r" % csw[:4])
    status = csw[12]
    rtag = struct.unpack('<I', csw[4:8])[0]
    if rtag != tag:
        raise ProtocolError("CSW tag mismatch %#x != %#x" % (rtag, tag))
    if status != 0:
        raise CommandRejected("status %d for a read at %08x" % (status, addr))
    return data

def _no_data_cmd(dev, tag, opcode, b2=0, val16=0):
    """Send a no-data-phase vendor command (0xC2 ping / 0xC3 move). Returns CSW status."""
    cdb = bytes([opcode, 0, b2 & 0xff, 0, 0, 0, 0, (val16 >> 8) & 0xff, val16 & 0xff]) + bytes(7)
    cbw = (b'USBC' + struct.pack('<I', tag) + struct.pack('<I', 0)   # dCBWDataTransferLength = 0
           + bytes([0x00, 0, 9]) + cdb)                              # flags=0 (no data)
    dev.write(EP_OUT, cbw, timeout=3000)
    csw = bytes(dev.read(EP_IN, 13, timeout=5000))
    if csw[:4] != b'USBS':
        raise ProtocolError("bad CSW signature %r" % csw[:4])
    return csw[12]

OP_PING = 0xC2

def snowcut_firmware(dev):
    """True when the machine is running firmware with the SnowCut vendor handler, False on the original
    firmware. Sends 0xC2 PING: no data phase and no effect. The vendor handler answers it with
    success, and the original firmware with ILLEGAL REQUEST (failed CSW). Other errors (no reply,
    lost connection) are raised: they say nothing about the firmware."""
    try:
        return _no_data_cmd(dev, 0x50494e47, OP_PING) == 0
    except scsipt.PassthroughStall:
        return False

# --- tool holder detection (Mechanism\bladeDetect.c), see MAT_TOOL_SCANNER_NOTES.md ---
ADDR_TOOL_KIND   = 0x881cc7e4   # current tool kind (getter FUN_88db8094)
ADDR_TOOL_SNSOFF = 0x881cc7ec   # 1 = "Cartridge Sensor Off": switches ignored, kind follows mode
ADDR_TOOL_BLADE  = 0x881cc7f0   # kind used for CUT while sensor-off (3 std / 4 deep)
ADDR_TOOL_PEN    = 0x881cc7f4   # kind used for DRAW while sensor-off (1 original / 2 general)
ADDR_OP_MODE     = 0x880125ac   # byte: operation mode selected in the UI
TOOL_KINDS = {0: "none (no holder)", 1: "Original Pen", 2: "General (universal) Pen",
              3: "Standard blade 0.50mm", 4: "Deep blade 0.50mm", 5: "Emboss",
              6: "Foil glue pen (universal holder)", 7: "Foil transfer tool (universal holder)",
              8: "Unknown"}
OP_MODES = {0: "none/other", 1: "cut", 2: "draw", 3: "emboss", 4: "emboss (region)",
            5: "foil: glue", 6: "foil: transfer"}

def ram_write32(dev, addr, val):
    """0xC3/0x11 guarded RAM word write (word-aligned 0x88000000..0x8BFFFFFF only)."""
    cdb = bytearray(16)
    cdb[0] = 0xC3; cdb[1] = 0x11
    cdb[2:6] = struct.pack(">I", addr & 0xffffffff)
    cdb[6:10] = struct.pack(">I", val & 0xffffffff)
    cbw = b'USBC' + struct.pack('<I', 0x57573131) + struct.pack('<I', 0) + bytes([0x00, 0, 16]) + bytes(cdb)
    dev.write(EP_OUT, cbw, timeout=3000)
    csw = bytes(dev.read(EP_IN, 13, timeout=5000))
    return csw[12] if len(csw) >= 13 else -1

def tool_status(dev):
    b = _read_chunk(dev, 0x544f4f4c, OP_RAM, ADDR_TOOL_KIND, 0x24)
    w = lambda o: struct.unpack(">I", b[o:o+4])[0]
    mode = _read_chunk(dev, 0x4d4f4445, OP_RAM, ADDR_OP_MODE, 4)[0]
    kind = w(0x00)
    print("tool kind      : %d  %s" % (kind, TOOL_KINDS.get(kind, "?")))
    print("op mode        : %d  %s" % (mode, OP_MODES.get(mode, "?")))
    print("switch polling : %d" % w(0x04))
    print("sensor-off     : %d  %s" % (w(0x08), "(OVERRIDE: kind follows mode)" if w(0x08) else "(holder switches)"))
    print("sensor-off cut : %d  %s" % (w(0x0c), TOOL_KINDS.get(w(0x0c), "?")))
    print("sensor-off draw: %d  %s" % (w(0x10), TOOL_KINDS.get(w(0x10), "?")))
    print("switches raw   : SW1=%d SW2=%d SW3=%d   (1/1/1 = no holder, 0/1/1 = standard blade)"
          % (b[0x14], b[0x15], b[0x16]))

def motion(dev, subop, X=0, Y=0, Z=0, feed=0, flags=0, mode=0, timeout=30000):
    """Send a 0xC3 MOTION command (no data phase). Returns CSW status (0=OK)."""
    cdb = bytearray(16)
    cdb[0] = 0xC3
    cdb[1] = subop & 0xff
    cdb[2:6]  = struct.pack(">i", X)
    cdb[6:10] = struct.pack(">i", Y)
    cdb[10] = Z & 0xff
    cdb[11] = feed & 0xff
    cdb[12] = flags & 0xff
    cdb[13] = mode & 0xff
    tag = 0x4d4f544e
    cbw = b'USBC' + struct.pack('<I', tag) + struct.pack('<I', 0) + bytes([0x00, 0, 16]) + bytes(cdb)
    dev.write(EP_OUT, cbw, timeout=5000)
    csw = bytes(dev.read(EP_IN, 13, timeout=timeout))
    if csw[:4] != b'USBS':
        raise ProtocolError("bad CSW signature %r" % csw[:4])
    return csw[12]

# motion sub-opcodes. G00 = coordinated travel at speed 9. G01 = queue a point
# on the device's locus queue (runs back to back with the previous one). M03 =
# lower the tool to the cut depth for a signed pressure (-9..9) via the blade
# table (flags M3_RAW: CDB[10] is a raw depth instead). M05 = raise the tool.
G00, G01, G04, G28, G90, G91, G92, M00, M03, M05 = (0x00,0x01,0x04,0x10,0x20,0x21,0x22,0x30,0x40,0x41)
LMOVE, LSTOP, AXMOVE = (0x02, 0x03, 0x06)
M3_RAW = 0x02                    # MOTION flags bit1
SENSE_BUSY = (0x02, 0x04, 0x07)  # motion refused: something is still moving, retry

def request_sense(dev):
    """(key, asc, ascq) from REQUEST SENSE (fixed format), or None."""
    cdb = bytes([0x03, 0, 0, 0, 0x12, 0]) + bytes(10)
    tag = 0x53454e53
    cbw = (b'USBC' + struct.pack('<I', tag) + struct.pack('<I', 0x12)
           + bytes([0x80, 0, 6]) + cdb)
    dev.write(EP_OUT, cbw, timeout=3000)
    data = bytes(dev.read(EP_IN, 0x12, timeout=3000))
    csw = bytes(dev.read(EP_IN, 13, timeout=3000))
    if csw[:4] != b'USBS' or len(data) < 14 or data[0] not in (0x70, 0x71):
        return None
    return (data[2] & 0x0F, data[12], data[13])

class MotionBusyTimeout(IOError):
    pass

def motion_retry(dev, subop, timeout=60.0, poll=0.01, **kw):
    """motion() that retries while the firmware refuses with SENSE_BUSY (the
    previous move, Z move or a full locus queue). Raises CommandRejected for
    any other failure, MotionBusyTimeout if still busy after `timeout` s."""
    deadline = time.time() + timeout
    while True:
        st = motion(dev, subop, **kw)
        if st == 0:
            return
        sense = request_sense(dev)
        if sense != SENSE_BUSY:
            raise CommandRejected("MOTION 0x%02x failed (CSW %d, sense %s)" % (subop, st, sense))
        if time.time() >= deadline:
            raise MotionBusyTimeout("MOTION 0x%02x: machine still busy after %.0f s" % (subop, timeout))
        time.sleep(poll)
SCAN_START, SCAN_FREE, SCAN_STOP = (0x50, 0x51, 0x52)

# --- motion state, read via 0xC0 from the tool-Z + head/mat mechanism block ---
LOCUS_BLOCK = 0x88013ab8
LOCUS_LEN   = 0x198          # 0x88013ab8..0x88013c50
LOCUS_FIELDS = {             # name: absolute address (all s32/u32 BE)
    "z_state": 0x88013ab8, "curZ": 0x88013abc, "zmax": 0x88013b04, "zmin": 0x88013b08,
    "z_base": 0x88013b10, "z_down": 0x88013b14, "z_up": 0x88013b1c,
    "head_state": 0x88013b20, "curX": 0x88013b24, "head_tmr": 0x88013b44,
    "xmax": 0x88013b70, "xmin": 0x88013b74, "lcs_tmr": 0x88013b9c,
    "speed": 0x88013bb8, "lcs_mode": 0x88013bbc,
    "mat_state": 0x88013bd8, "curY": 0x88013bdc,
    "ymax": 0x88013c3c, "ymin": 0x88013c40, "y_default": 0x88013c48,
}
AXIS_STATE = {0: "0 (USB idle)", 1: "1 (busy)", 2: "2 (ready)", 3: "3 (chaining)", 4: "4 (MOVING)"}

def locus_state(dev):
    """Snapshot of head/mat state, positions, limits and locus vars."""
    blk = _read_chunk(dev, 0x4c535441, OP_RAM, LOCUS_BLOCK, LOCUS_LEN)
    out = {}
    for k, a in LOCUS_FIELDS.items():
        out[k] = struct.unpack(">i", blk[a - LOCUS_BLOCK:a - LOCUS_BLOCK + 4])[0]
    return out

def _busy(st):
    """Something moves: XY (3 = locus chaining) or the tool Z axis."""
    return (st["head_state"] in (1, 3, 4) or st["mat_state"] in (1, 3, 4)
            or st["z_state"] in (1, 4))

def print_locus_state(st, prefix=""):
    cy = "UNKNOWN(0x80000000)" if st["curY"] == -2**31 else str(st["curY"])
    print("%shead=%s mat=%s  X=%d Y=%s  speed=%d  X[%d..%d] Y[%d..%d]  tmr head=%d lcs=%d" % (
        prefix, AXIS_STATE.get(st["head_state"], st["head_state"]),
        AXIS_STATE.get(st["mat_state"], st["mat_state"]), st["curX"], cy, st["speed"],
        st["xmin"], st["xmax"], st["ymin"], st["ymax"], st["head_tmr"], st["lcs_tmr"]))
    print("%sZ=%s pos=%d  up=%d down=%d base=%d  Z[%d..%d]" % (
        prefix, AXIS_STATE.get(st["z_state"], st["z_state"]), st["curZ"], st["z_up"],
        st["z_down"], st["z_base"], st["zmin"], st["zmax"]))

def locus_stop(dev):
    return motion(dev, LSTOP)

def locus_move(dev, X, Y, speed=5, rel=True, mode=0, timeout=30.0, poll=0.1, verbose=True):
    """EXPERIMENTAL coordinated XY move (0xC3/0x02). Non-blocking on the device:
    the host polls the axis states until the move ends. Ctrl-C / timeout -> LSTOP.
    Returns (finished_ok, start_state, end_state)."""
    st0 = locus_state(dev)
    if _busy(st0):
        raise RuntimeError("axis busy before move: head=%d mat=%d" % (st0["head_state"], st0["mat_state"]))
    if verbose: print_locus_state(st0, "  before: ")
    status = motion(dev, LMOVE, X=X, Y=Y, feed=speed, flags=1 if rel else 0, mode=mode)
    if status != 0:
        raise IOError("LMOVE CSW status %d" % status)
    st = locus_state(dev)
    if verbose: print_locus_state(st, "  issued: ")
    if not _busy(st):
        if verbose: print("  (axes not moving right after LMOVE: zero-length/clamped move, or the move was rejected)")
        return True, st0, st
    t0 = time.time(); last = None
    try:
        while _busy(st):
            if time.time() - t0 > timeout:
                print("  TIMEOUT after %.1fs -> LSTOP" % timeout); locus_stop(dev)
                return False, st0, locus_state(dev)
            time.sleep(poll)
            st = locus_state(dev)
            if verbose and (st["curX"], st["curY"]) != last:
                last = (st["curX"], st["curY"])
                print("    t=%5.2fs  X=%d Y=%d  head=%d mat=%d" % (time.time() - t0, st["curX"], st["curY"],
                                                                st["head_state"], st["mat_state"]))
    except KeyboardInterrupt:
        print("  Ctrl-C -> LSTOP"); locus_stop(dev)
        return False, st0, locus_state(dev)
    if verbose: print_locus_state(st, "  done %.2fs: " % (time.time() - t0))
    return True, st0, st

ADDR_MOVSTATE    = 0x881CC814   # MovState task state (see MOVSTATE_NAMES). NEVER write it, since
                                # the task only moves on messages (0xC3/0x53 MOV_EVENT)
ADDR_MATKIND     = 0x88092d28   # recognised mat kind (MAT_KINDS)
ADDR_SCAN_STRUCT = 0x88014134   # struct movStScan:
                                # +0x00: mode (u32)
                                # +0x04: submode (u32)
                                # +0x08: ScanDstBuff handle (u32)
                                # +0x0C: FCMQ handle (u32)
                                # +0x10: Image buffer pointer (u32)
                                # +0x14: Image length / file size (u32)
                                # +0x18: R offset (u32)
                                # +0x1C: G offset (u32)
                                # +0x20: B offset (u32)
                                # +0x24: steps fed during the scan (u32)

# MovState task (FUN_88dbda34): one handler per state. Transitions happen only
# when the task receives an event message (state x event matrix @0x88013c68).
MOVSTATE_NAMES = {
    0x00: "PowerOn (uninitialised: only init/stop accepted)",
    0x01: "Error/Halted",
    0x02: "Ready",
    0x03: "Init/Home",
    0x04: "MatLoad+Recognise",
    0x05: "MatFeed",
    0x06: "MatUnload",
    0x07: "MatFeed2",
    0x08: "Cutting",
    0x09: "CutEnd",
    0x0a: "Return",
    0x0b: "ScanPreparing",
    0x0c: "Scanning",
    0x0d: "ScanFinishing (mat return)",
    0x0e: "ScanError (waiting for abort)",
    0x0f: "ScanCancelled (waiting for abort)",
    0x10: "TestMove",
    0x11: "Stop",
}
MS_POWERON, MS_READY = 0x00, 0x02
MS_SCAN_BUSY = (0x0b, 0x0c, 0x0d)
MS_SCAN_FAIL = (0x0e, 0x0f)

# MovState events (u32 at msg+0, payload words at +4/+8)
EV_INIT, EV_LOAD, EV_UNLOAD, EV_RECOGNISE = 5, 6, 7, 8
EV_CUT, EV_SCAN, EV_TESTMOVE, EV_ABORT, EV_STOP = 9, 10, 11, 12, 14
MOV_EVENT = 0x53

MAT_KINDS = {0: "none", 1: "LowTack 12x12", 2: "Standard 12x12", 3: "LowTack 24x12",
             4: "Standard 24x12", 5: "Scan 12x12", 6: "Scan Adjust", 7: "Test Mode 1",
             8: "Scan 24x12", 9: "Unknown"}

def scan_start(dev, mode=1, submode=0, mat=0):
    """Post a scan request (event 10) to the MovState task (CDB[0]=0xC3, CDB[1]=0x50).

    Only acts in MovState Ready (2), so use scan_prepare() first. `mat` != 0 also
    overwrites the recognised mat kind. That is not needed to scan (the MovState
    task never checks it, and only the panel's Scan button does) and it changes what
    a later cut job sees, so the default leaves it alone. Returns CSW status.
    """
    cdb = bytearray(16)
    cdb[0] = 0xC3
    cdb[1] = SCAN_START
    cdb[2] = mode & 0xff
    cdb[3] = submode & 0xff
    cdb[4] = mat & 0xff
    tag = 0x5343414E  # 'SCAN'
    cbw = b'USBC' + struct.pack('<I', tag) + struct.pack('<I', 0) + bytes([0x00, 0, 16]) + bytes(cdb)
    dev.write(EP_OUT, cbw, timeout=5000)
    csw = bytes(dev.read(EP_IN, 13, timeout=5000))
    if csw[:4] != b'USBS':
        raise ProtocolError("bad CSW signature %r" % csw[:4])
    return csw[12]

def mov_event(dev, event, p0=0, p1=0):
    """Post MovState event `event` (0..14) with payload words p0/p1 (bytes) via 0xC3/0x53."""
    cdb = bytearray(16)
    cdb[0] = 0xC3; cdb[1] = MOV_EVENT
    cdb[2] = event & 0xff; cdb[3] = p0 & 0xff; cdb[4] = p1 & 0xff
    cbw = b'USBC' + struct.pack('<I', 0x4d455654) + struct.pack('<I', 0) + bytes([0x00, 0, 16]) + bytes(cdb)
    dev.write(EP_OUT, cbw, timeout=5000)
    csw = bytes(dev.read(EP_IN, 13, timeout=5000))
    if csw[:4] != b'USBS':
        raise ProtocolError("bad CSW signature %r" % csw[:4])
    return csw[12]

# --- custom-mat load (0xC3/0x54 LOAD-CUSTOM, tools/vendor_handler.py) ---
# The panel's Load runs a short recognition scan of the printed mat marks and the UI then
# refuses kind 9 (unknown). LOAD-CUSTOM runs the same load state (feed in, align the leading
# edge on the mat sensor, park at c48) but skips the recognition scan and sets the given kind.
# It also moves the mat-edge Y reference (c44, -35 mm) by `edge_delta` Y-counter units, so a
# mat whose printed grid is not 60 mm from its leading edge still gets Y = 0 on its grid.
LOAD_CUSTOM = 0x54
MAT_FACTOR_Y = 56844            # FUN_88dbc770 Y factor: 0.01 mm -> pre-x4 steps
STD_TOP_MARGIN_MM = 60.0        # leading edge -> printed grid on the stock mats
# Descriptor table 0x88ffb3e8 (+0x1c): Y length of the mat's area in 0.01 mm
MAT_Y_LEN = {1: 29880, 2: 29880, 3: 60360, 4: 60360, 5: 29880, 8: 60360}
MAT_X_LEN = 29667               # every mat: 296.67 mm (= the full CIS width at 300 dpi)
SPOOFABLE = (1, 2, 3, 4, 5, 8)

class MatAlreadyLoaded(IOError):
    pass

class FirmwareTooOld(IOError):
    pass

# --- what the flashed vendor firmware supports ---
# Unknown 0xC3 sub-ops are accepted and ignored (CSW success), so an older image would silently
# ignore 0x54 / 0x55. Each feature also patches a call site in the main firmware, which runs from
# RAM (0x88c30fc8..), so reading those 4 bytes back (0xC0, plain RAM) tells which image is in.
VENDOR_CAVE = 0x843e2200
_HOOKS = {"load_custom": ((0x88dc4938, 0x20), (0x88dc4964, 0x28)),     # 0x54 LOAD-CUSTOM
          "scan_opts": ((0x88dc7ea4, 0x30), (0x88dfdf10, 0x38)),       # 0x55 SCAN-OPTS
          "scan_wide": ((0x88ddd290, 0x40),)}                          # 0x55 bit1 full width

def _jal_word(target):
    return 0x0C000000 | ((target >> 2) & 0x3FFFFFF)

def firmware_caps(dev):
    """{"load_custom": bool, "scan_opts": bool, "scan_wide": bool} for the vendor firmware
    running now."""
    rd = _UsbReader(dev, OP_RAM)
    return {name: all(rd.u32(site) == _jal_word(VENDOR_CAVE + vec) for site, vec in sites)
            for name, sites in _HOOKS.items()}

def require_caps(dev, *names):
    caps = firmware_caps(dev)
    missing = [n for n in names if not caps.get(n)]
    if missing:
        raise FirmwareTooOld("The machine's vendor firmware is older than this app (no %s). "
                             "Flash the current SNC*_vendor.upf." % ", ".join(missing))
    return caps

# --- mat load state, for progress and diagnosis ---
ADDR_MAT_STATE = 0x88013bd8     # mat mover state: 0 idle/reset, 1 load/eject run, 2 positioned, 4 moving
ADDR_MAT_Y     = 0x88013bdc     # Y counter (x4 steps), 0x80000000 = no edge reference
ADDR_MATEDGE_Y = 0x88013c44     # Y the edge sensor assigns to the leading edge (-35 mm stock)
ADDR_MAT_PARK  = 0x88013c4c     # post-load position (-6.25 mm)
ADDR_Z_STATE   = 0x88013ab8
ADDR_LOAD_REC  = 0x89499628 + 0x500    # 0x54 record: magic, tick, kind
Y_UNKNOWN = 0x80000000

def mat_snapshot(dev):
    rd = _UsbReader(dev, OP_RAM)
    s32 = lambda v: v - (1 << 32) if v & 0x80000000 else v
    y = rd.u32(ADDR_MAT_Y)
    return {"movstate": rd.u32(ADDR_MOVSTATE), "kind": rd.u32(ADDR_MATKIND),
            "mat_state": rd.u32(ADDR_MAT_STATE), "y": None if y == Y_UNKNOWN else s32(y),
            "edge_y": s32(rd.u32(ADDR_MATEDGE_Y)), "park_y": s32(rd.u32(ADDR_MAT_PARK)),
            "z_state": rd.u32(ADDR_Z_STATE), "load_rec": rd.u32(ADDR_LOAD_REC)}

def describe_snapshot(sn):
    y = "unknown" if sn["y"] is None else "%.2f mm" % (sn["y"] / 4.0 * MAT_FACTOR_Y / 1e6)
    return ("MovState 0x%02x (%s), mat kind %d, mat mover %d, Y %s, Z %d"
            % (sn["movstate"], movstate_name(sn["movstate"]), sn["kind"], sn["mat_state"], y,
               sn["z_state"]))

# After the edge search the load moves on to c4c (-6.25 mm), i.e. c4c - c44 further in. With
# c44 moved by the top-margin delta that distance must stay positive (the mover runs it forward),
# so the top margin cannot be much under 60 - 28.8 mm.
STOCK_EDGE_UNITS = -2464        # 4 * FUN_88dbc770(-3500, 56844): the stock c44
STOCK_PARK_UNITS = -440         # 4 * FUN_88dbc770(-626, 56844):  c4c

def min_top_margin_mm(scale_y=1.0):
    return STD_TOP_MARGIN_MM - (STOCK_PARK_UNITS - STOCK_EDGE_UNITS - 4) / 4.0 * MAT_FACTOR_Y / 1e6 / scale_y

def mat_edge_delta(top_margin_mm, scale_y=1.0):
    """Y-counter units to add to the mat-edge Y for a mat whose grid starts `top_margin_mm`
    from its leading edge (stock mats: 60). Positive = the edge is closer to Y = 0. Rounded to
    whole motor steps (4 units), the counter's resolution after the edge search.
    `scale_y` converts printed mm to cut-frame mm (pc-app core.machine.MAT_SCALE_Y)."""
    mm = (STD_TOP_MARGIN_MM - top_margin_mm) * scale_y
    return 4 * int(round(mm * 100 * 10000 / MAT_FACTOR_Y))

def load_custom(dev, kind, edge_delta=0):
    """Post 0xC3/0x54. Returns the CSW status: 0 = posted, 1 with sense 02/04/07 = not in
    Ready (nothing armed). The load itself runs asynchronously in the MovState task."""
    if kind not in range(1, 9):
        raise ValueError("mat kind must be 1..8")
    cdb = bytearray(16)
    cdb[0] = 0xC3; cdb[1] = LOAD_CUSTOM; cdb[2] = kind
    cdb[6:10] = struct.pack(">i", int(edge_delta))
    cbw = b'USBC' + struct.pack('<I', 0x4c4f4144) + struct.pack('<I', 0) + bytes([0x00, 0, 16]) + bytes(cdb)
    dev.write(EP_OUT, cbw, timeout=5000)
    csw = bytes(dev.read(EP_IN, 13, timeout=5000))
    if csw[:4] != b'USBS':
        raise ProtocolError("bad CSW signature %r" % csw[:4])
    return csw[12]

def set_matkind(dev, kind):
    """Overwrite the mat kind the firmware believes is loaded (0xC3/0x10). RAM only."""
    cdb = bytes([0xC3, 0x10, kind & 0xff]) + bytes(13)
    cbw = b'USBC' + struct.pack('<I', 0x5345544d) + struct.pack('<I', 0) + bytes([0x00, 0, 16]) + cdb
    dev.write(EP_OUT, cbw, timeout=3000)
    csw = bytes(dev.read(EP_IN, 13, timeout=5000))
    if csw[:4] != b'USBS':
        raise ProtocolError("bad CSW signature %r" % csw[:4])
    return csw[12]

def mat_load_custom(dev, kind, top_margin_mm=STD_TOP_MARGIN_MM, scale_y=1.0, timeout=90.0, log=None,
                    accept_timeout=5.0):
    """Load a mat without recognising it, as `kind`, with the Y origin on its grid.
    Brings a power-on (state 0) machine to Ready first (that homes the head and ejects a
    mat already in the rollers). Raises MatAlreadyLoaded if a mat is loaded, FirmwareTooOld if
    the vendor firmware lacks 0x54. Checks afterwards that the mat really was fed in and
    aligned (a Y reference exists). If not, the mat kind is reset and an error explains why.
    Returns the mat kind the machine reports afterwards."""
    log = log or (lambda s: None)
    require_caps(dev, "load_custom")
    if top_margin_mm < min_top_margin_mm(scale_y) - 1e-6:
        raise ValueError("A top margin under %.1f mm is not supported: the load would have to "
                         "back the mat out after finding its edge." % min_top_margin_mm(scale_y))
    ms = get_movstate(dev)
    if ms == MS_POWERON:
        log("MovState 0 (uninitialised) -> INIT (homes head, ejects a mat in the rollers)")
        mov_event(dev, EV_INIT)
        ms = wait_movstate(dev, MS_READY, timeout, leave_first=MS_POWERON, log=log)
    elif ms in MS_SCAN_FAIL:
        scan_stop(dev)
        ms = wait_movstate(dev, MS_READY, timeout, log=log)
    elif ms != MS_READY:
        ms = wait_movstate(dev, MS_READY, timeout, log=log)
    if get_matkind(dev) != 0:
        raise MatAlreadyLoaded("A mat is already loaded. Unload it first.")
    delta = mat_edge_delta(top_margin_mm, scale_y)
    log("LOAD-CUSTOM kind %d (%s), mat-edge Y %+d units (top margin %.1f mm)"
        % (kind, MAT_KINDS.get(kind, "?"), delta, top_margin_mm))
    st = load_custom(dev, kind, delta)
    if st != 0:
        raise CommandRejected("the machine refused the mat load (CSW %d, sense %s). It may be "
                              "busy" % (st, request_sense(dev)))
    # the MovState task must take the request (Ready -> 4 load) within a few seconds
    t0 = time.time()
    while get_movstate(dev) == MS_READY:
        if time.time() - t0 > accept_timeout:
            raise IOError("The machine did not start the mat load (%s)."
                          % describe_snapshot(mat_snapshot(dev)))
        time.sleep(0.1)
    try:
        wait_movstate(dev, MS_READY, timeout, log=log)
    except TimeoutError:
        raise IOError("The mat load did not finish in %.0f s (%s)."
                      % (timeout, describe_snapshot(mat_snapshot(dev)))) from None
    sn = mat_snapshot(dev)
    log("load finished: " + describe_snapshot(sn))
    got = sn["kind"]
    if sn["y"] is None:
        # The edge search never ran (mtm_SetPulseMotorDrive refuses a mode-2 load while the mat
        # sensor already sees material: "line 0x11e"), so nothing moved and Y has no origin.
        if got != 0:
            set_matkind(dev, 0)
        raise IOError("The mat was not fed in: the mat sensor already saw the mat when the load "
                      "started. Pull the mat back so it only touches the feed rollers, then "
                      "load again.")
    if got == 0:
        raise IOError("The mat load finished but no mat was detected. Put the mat against the "
                      "feed rollers and try again.")
    if got != kind:
        raise IOError("The mat loaded as the wrong type (%s instead of %s), so %s."
                      % (MAT_KINDS.get(got, "?"), MAT_KINDS.get(kind, "?"), FW_HINT))
    return got

def mat_unload(dev, timeout=60.0, log=None):
    """Feed the mat out, like the Unload button (event 7). Returns the final MovState."""
    log = log or (lambda s: None)
    ms = get_movstate(dev)
    if ms != MS_READY:
        raise IOError("The machine is busy (%s)." % movstate_name(ms))
    mov_event(dev, EV_UNLOAD)
    return wait_movstate(dev, MS_READY, timeout, leave_first=MS_READY, log=log)

def scan_window(kind):
    """Area-7 scan rectangle (x0, x1, y0, y1 in 0.01 mm) for a mat kind, or None for the
    stock full 12x12 window (area 0)."""
    ylen = MAT_Y_LEN.get(kind, 29880)
    if ylen <= 29880:
        return None
    return (0, MAT_X_LEN, 0, ylen)

def scan_max_dpi(window):
    """Highest JPEG dpi whose scan buffer fits heap 3 (74.4 MB, ~70 MB usable). The firmware
    allocates width * 3 * (lines + 1000) bytes, so a 24" window only fits at 200 dpi or less."""
    if window is None:
        return 300
    for dpi in (300, 200, 150, 100):
        w = int(round((window[1] - window[0]) / 2540.0 * dpi))
        lines = int(round((window[3] - window[2]) / 2540.0 * dpi))
        if w * 3 * (lines + 1000) <= 70 * 1024 * 1024:
            return dpi
    return 100

def scan_free(dev):
    """Release ScanDstBuff in firmware heap (CDB[0]=0xC3, CDB[1]=0x51). Skipped mid-scan."""
    return motion(dev, SCAN_FREE)

def scan_stop(dev):
    """Abort a running scan / release a failed one (event 12, ignored outside 0xc/0xe/0xf)."""
    return motion(dev, SCAN_STOP)

def get_movstate(dev):
    return _UsbReader(dev, OP_RAM).u32(ADDR_MOVSTATE)

def get_matkind(dev):
    return _UsbReader(dev, OP_RAM).u32(ADDR_MATKIND)

def movstate_name(ms):
    return MOVSTATE_NAMES.get(ms, "Unknown(0x%02x)" % ms)

def wait_movstate(dev, want, timeout=60.0, poll=0.2, leave_first=None, log=None):
    """Poll MovState until it is in `want` (int or tuple). If `leave_first` is set,
    the state must first differ from it, so a request that is still queued does
    not count as done. Returns the final state or raises TimeoutError."""
    want = (want,) if isinstance(want, int) else tuple(want)
    t0, left, last = time.time(), leave_first is None, None
    while time.time() - t0 < timeout:
        ms = get_movstate(dev)
        if ms != last and log:
            log("  MovState 0x%02x %s" % (ms, movstate_name(ms)))
        last = ms
        if ms != leave_first:
            left = True
        if left and ms in want:
            return ms
        time.sleep(poll)
    raise TimeoutError("MovState stuck at 0x%02x (%s), wanted %s"
                       % (last, movstate_name(last), "/".join("0x%02x" % w for w in want)))

def scan_prepare(dev, load=True, timeout=90.0, log=None):
    """Bring the MovState task to Ready (2) with a mat loaded, the way the panel does.

    * PowerOn (0): post INIT (event 5), the same as touching the opening screen.
      It homes the head and, if the mat sensor sees a mat, FEEDS IT OUT. Then Ready.
    * No mat recognised and load=True: post LOAD (event 6), the same as the Load
      button. It feeds the mat in and runs mat recognition (a short mode-2 scan),
      then returns to Ready.
    Any mat kind is acceptable afterwards, since the scan request itself never checks it.
    Returns (movstate, matkind).
    """
    log = log or (lambda s: None)
    ms = get_movstate(dev)
    if ms == MS_POWERON:
        log("MovState 0 (uninitialised) -> INIT (homes head, ejects a mat in the rollers)")
        mov_event(dev, EV_INIT)
        ms = wait_movstate(dev, MS_READY, timeout, leave_first=MS_POWERON, log=log)
    elif ms in MS_SCAN_FAIL:
        log("MovState 0x%02x left over from a failed scan -> ABORT" % ms)
        scan_stop(dev)
        ms = wait_movstate(dev, MS_READY, timeout, log=log)
    elif ms != MS_READY:
        ms = wait_movstate(dev, MS_READY, timeout, log=log)
    kind = get_matkind(dev)
    if load and kind == 0:
        log("no mat recognised -> LOAD (feed in + recognise)")
        mov_event(dev, EV_LOAD)
        ms = wait_movstate(dev, MS_READY, timeout, leave_first=MS_READY, log=log)
        kind = get_matkind(dev)
        if kind == 0:
            raise IOError("mat load finished without a mat (is one against the rollers?)")
    log("ready, mat kind %d (%s)" % (kind, MAT_KINDS.get(kind, "?")))
    return ms, kind

def get_scan_status(dev, retries=2):
    """Query live MovState and scan struct over USB. Returns status dict."""
    rd = _UsbReader(dev, OP_RAM)
    movstate = rd.u32(ADDR_MOVSTATE)
    data = rd.read(ADDR_SCAN_STRUCT, 40)
    mode, submode, handle, fcmq, ptr, length, roff, goff, boff, lines = struct.unpack(">10I", data[:40])
    return {
        "movstate": movstate,
        "state_name": movstate_name(movstate),
        "mode": mode,
        "submode": submode,
        "handle": handle,
        "fcmq": fcmq,
        "ptr": ptr,
        "len": length,
        "roff": roff,
        "goff": goff,
        "boff": boff,
        "lines": lines,
    }

def trim_jpeg_overrun(jpeg):
    """Drop the extra MCU row the scan engine encodes past the declared height.

    The engine's JPEGs have restart markers every MCU row (DRI = MCUs per row) and
    one more row of entropy data than SOF's height needs (3529 lines -> 222 rows of
    16, 221 needed), so decoders warn "N extraneous bytes before marker 0xd9".
    Cutting at the restart marker that starts the first unneeded interval and
    ending with EOI is lossless. Anything that does not parse as that layout
    (no DRI, progressive, too few markers) is returned unchanged.
    """
    try:
        i, sof, dri, scan = 2, None, 0, None
        while i + 4 <= len(jpeg) and jpeg[i] == 0xFF:
            m = jpeg[i + 1]
            seg = struct.unpack(">H", jpeg[i + 2:i + 4])[0]
            if m == 0xC0 or m == 0xC1:
                h, w, nc = struct.unpack(">HHB", jpeg[i + 5:i + 10])
                hs = max(jpeg[i + 11 + 3 * k] >> 4 for k in range(nc))
                vs = max(jpeg[i + 11 + 3 * k] & 15 for k in range(nc))
                sof = (w, h, hs, vs)
            elif m == 0xDD:
                dri = struct.unpack(">H", jpeg[i + 4:i + 6])[0]
            elif m == 0xDA:
                scan = i + 2 + seg
                break
            i += 2 + seg
        if not (sof and dri and scan):
            return jpeg
        w, h, hs, vs = sof
        mcus = -(-w // (8 * hs)) * -(-h // (8 * vs))
        intervals = -(-mcus // dri)
        n, j = 0, scan
        while True:
            j = jpeg.find(b"\xff", j, len(jpeg) - 1)
            if j < 0:
                return jpeg
            if 0xD0 <= jpeg[j + 1] <= 0xD7:
                n += 1
                if n == intervals:      # this marker starts the first interval nobody needs
                    return jpeg[:j] + b"\xff\xd9"
            j += 2 if jpeg[j + 1] != 0xFF else 1
    except (struct.error, IndexError, ValueError):
        return jpeg

def scan_fetch(dev, ptr, length, out=None, progress_cb=None):
    """Read the scan output from firmware RAM, free the buffer, save to file.

    A JPEG stream is kept from its SOI. Planar RGB (R/G/B offsets in the scan
    struct, no SOI at the start) is saved unchanged, rather than cut at a random
    ff d8 pair inside the pixels.
    """
    if not ptr or not length:
        raise ValueError("invalid scan buffer ptr=0x%08x len=%d" % (ptr, length))
    raw = dump_region(OP_RAM, ptr, length, dev=dev, progress_cb=progress_cb)
    soi = raw.find(b"\xff\xd8\xff", 0, 64)
    data = trim_jpeg_overrun(raw[soi:]) if soi != -1 else raw
    if soi == -1:
        print("  [!] no JPEG SOI at the buffer start: saving raw scan data", file=sys.stderr)
    try:
        scan_free(dev)
    except Exception as e:
        print("  [!] scan_free failed: %s" % e, file=sys.stderr)
    if out:
        with open(out, "wb") as f:
            f.write(data)
        print("wrote %d bytes -> %s" % (len(data), out))
    return data

# JPEG output. Only scan mode 0 (the panel's ScanToUSB, builder FUN_88dc5964) sets
# request byte +0x36 = 2, which makes the scan task run the JPEG engine
# (FUN_88dfb07c -> 0x883770e6 == 2 -> FUN_88dfad74). Every other mode returns raw
# planar RGB (mode 1: 3504 x 3529 x 3 = ~37 MB). Mode 0 takes its window from the
# ScanToUSB screen's selections: area 0 = full 12x12, resolution index below.
# Widths are multiples of 16 (JPEG MCUs): 3504 / 2336 / 1744 / 1168 px, X offset 0.
ADDR_SCAN_AREA = 0x881cb660     # FUN_88d1e8bc: 0 = 12x12, 1..6 presets, 7 custom (0x88012ea8..)
ADDR_SCAN_RES  = 0x881cb664     # FUN_88d1e8cc: resolution index
ADDR_SCAN_RECT = 0x88012ea8     # FUN_88d1f904: area-7 rectangle x0, x1, y0, y1 (0.01 mm)
SCAN_JPEG_DPI  = {300: 0, 200: 1, 150: 2, 100: 3}

def scan_start_jpeg(dev, dpi=300, mat=0, window=None):
    """Post a JPEG scan (event 10, mode 0 at `dpi`) via 0xC3/0x53.

    window None = the full 12x12 (area 0). Otherwise (x0, x1, y0, y1) in 0.01 mm: the
    panel's custom area (area 7, builder FUN_88dc5964), e.g. scan_window(4) for a 24" mat.
    Row 0 is then at y0: skip lines = (y0 + 3 mm) * dpi, but at least the stock 35/23/17/11
    (with y0 = 0 that is 35 at 300 dpi as usual, and one line more than area 0 at 200 dpi).

    Mode 0 must go through MOV_EVENT: 0xC3/0x50 maps mode 0 to 1. Returns
    (CSW status, saved panel selection). Pass the latter to scan_jpeg_restore()
    once the scan has finished.
    """
    if dpi not in SCAN_JPEG_DPI:
        raise ValueError("JPEG scan dpi must be one of %s" % sorted(SCAN_JPEG_DPI))
    rd = _UsbReader(dev, OP_RAM)
    saved = (rd.u32(ADDR_SCAN_AREA), rd.u32(ADDR_SCAN_RES),
             tuple(rd.u32(ADDR_SCAN_RECT + 4 * i) for i in range(4)))
    if mat:
        motion(dev, 0x10, X=mat)
    writes = [(ADDR_SCAN_AREA, 0 if window is None else 7), (ADDR_SCAN_RES, SCAN_JPEG_DPI[dpi])]
    if window is not None:
        writes += [(ADDR_SCAN_RECT + 4 * i, int(v)) for i, v in enumerate(window)]
    for addr, val in writes:
        st = ram_write32(dev, addr, val)
        if st != 0:
            raise IOError("writing the scan window selection failed (CSW %d)" % st)
    return mov_event(dev, EV_SCAN, 0, 0), saved

# --- custom scans: window, JPEG quality / sampling, raw output (0xC3/0x55) ---
# Quality: the scan JPEG encoder takes its quant tables from one of four fixed pairs picked by
# request byte 0x3b (FUN_88dfe8f8). ScanToUSB uses index 2 = the IJG quality-95 pair at
# 0x89043b80 (luma) / 0x89043bc0 (chroma), 64 bytes each in natural order. The encoder copies
# the pair into its registers with the CPU at every scan start (FUN_88df5d9c, +0x400/+0x500),
# so rewriting that RAM copy (0xC3/0x11 word writes) before a scan sets any IJG quality.
# scan_custom_restore() puts the stock q95 pair back.
JPEG_QSLOT = 0x89043b80
STOCK_JPEG_QUALITY = 95
SCAN_OPTS = 0x55
SOPT_RAW = 0x01
SOPT_WIDE = 0x02
_IJG_LUMA = (16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55, 14, 13, 16, 24, 40, 57,
             69, 56, 14, 17, 22, 29, 51, 87, 80, 62, 18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55,
             64, 81, 104, 113, 92, 49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100,
             103, 99)
_IJG_CHROMA = ((17, 18, 24, 47) + (99,) * 4 + (18, 21, 26, 66) + (99,) * 4 + (24, 26, 56) + (99,) * 5
               + (47, 66) + (99,) * 6 + (99,) * 32)

def jpeg_qtables(quality):
    """IJG (libjpeg) luma + chroma tables for `quality` 1..100, 128 bytes, natural order."""
    q = max(1, min(100, int(quality)))
    scale = 5000 // q if q < 50 else 200 - 2 * q
    return bytes(max(1, min(255, (v * scale + 50) // 100)) for v in _IJG_LUMA + _IJG_CHROMA)

def set_jpeg_quality(dev, quality):
    """Write the IJG tables for `quality` over the ScanToUSB table pair (RAM, 32 word writes)."""
    t = jpeg_qtables(quality)
    for i in range(0, 128, 4):
        st = ram_write32(dev, JPEG_QSLOT + i, struct.unpack(">I", t[i:i + 4])[0])
        if st != 0:
            raise IOError("writing the JPEG quality tables failed (CSW %d)" % st)

def scan_options(dev, raw=False, sampling=0, wide=False):
    """Arm 0xC3/0x55 for the next ScanToUSB-path scan: raw 8-bit RGB output instead of JPEG,
    a JPEG sampling mode (0 = stock, mode 4 = 4:2:0, 3 = 4:4:4, 1 would be greyscale), and/or
    the full sensor width (see wide_window). All off disarms."""
    cdb = bytearray(16)
    cdb[0] = 0xC3; cdb[1] = SCAN_OPTS
    cdb[2] = (SOPT_RAW if raw else 0) | (SOPT_WIDE if wide else 0); cdb[3] = sampling & 0xff
    cbw = b'USBC' + struct.pack('<I', 0x534f5054) + struct.pack('<I', 0) + bytes([0x00, 0, 16]) + bytes(cdb)
    dev.write(EP_OUT, cbw, timeout=5000)
    csw = bytes(dev.read(EP_IN, 13, timeout=5000))
    if csw[:4] != b'USBS':
        raise ProtocolError("bad CSW signature %r" % csw[:4])
    return csw[12]

# Full sensor width (0x55 flags bit1). Every ScanToUSB dpi reads the CIS at 300 dpi, where this
# sensor (CIS type 1) has 3638 pixels, and the stock window is 3504 of them, hardware pixels 66..3569
# (start term 0x84 halved). Armed, the driver allows all 3638 and drops the start term, so a
# window centred on the sensor gains ~5.4 mm on each side of the stock image. Centred also means
# the image's place in the stock frame does not depend on the sensor's readout direction.
# Pixels outside the stock window are shading-corrected from the same full-width white
# calibration, but nobody has tuned them: expect darker / noisier edge columns.
WIDE_SENSOR_PX = 3638
STOCK_HW_START = 66
# Y margins of a wide scan: the mat starts that many lines less fed in (the scan feeds relative
# to wherever the mat is) and the request asks for twice as many extra lines. A scan feeds 6
# Y-counter units per 300-dpi line (1800/dpi per line at any dpi).
WIDE_MARGIN_300 = 64
ADDR_SCAN_START_Y = 0x88013c48  # c48: where scans start and the mat parks (Y units). After a
                                # scan the firmware sets Y = c48 + 4 x steps and returns to c48
STOCK_SCAN_START_Y = -212       # FUN_88dbc410: -3.00 mm
EDGE_MARGIN_UNITS = 72          # ~1 mm: a scan start keeps the mat's leading edge this far past
                                # where the load found it at the mat sensor (c44), the furthest
                                # back the load itself takes the mat. Behind that, the edge would
                                # leave the feed rollers

def scan_back_max(dev):
    """Y units a scan may start behind the stock start for the mat loaded now (live c44)."""
    edge = _s32(_UsbReader(dev, OP_RAM).u32(ADDR_MATEDGE_Y))
    return max(0, STOCK_SCAN_START_Y - edge - EDGE_MARGIN_UNITS)

def wide_margin_lines(dpi):
    """(lines, Y units) above and below a wide scan: about the X margin (64 px at 300 dpi),
    rounded so the mat move is whole motor steps (a multiple of 4 units)."""
    per = 1800 // dpi
    want = WIDE_MARGIN_300 * dpi / 300.0
    n = min((m for m in range(max(1, int(want) - 4), int(want) + 6) if m * per % 4 == 0),
            key=lambda m: (abs(m - want), m))
    return n, n * per

def wide_window(dpi, y0=0, y1=29880):
    """Geometry of a wide scan of the mat window y0..y1 (0.01 mm) at `dpi`, as a dict:
    rect   the area-7 rectangle (0.01 mm): the widest 16-px multiple the sensor has, centred on
           it, and the stock lines plus 2 x margin
    width, lines   what the builder asks for
    col0, row0     the image's first pixel in the stock full-mat frame, pixels at `dpi`
                   (negative: left of / above the stock image)
    back   Y units the mat must start less fed in (scan_start_y_shift).
    Emulation-verified at 300 dpi: 3632 px from hardware pixel 2, col0 = -64, row0 = -64 lines.
    Below 300 dpi the driver's scaler rounding is unverified (col0 within a pixel or two)."""
    k = dpi / 25.4
    width = int(WIDE_SENSOR_PX * dpi / 300.0) // 16 * 16
    in_w = width * 300.0 / dpi                         # input (300 dpi) pixels
    hw_start = int(WIDE_SENSOR_PX - 2 - in_w) // 2     # centred: 2 at 300 dpi
    off_in = WIDE_SENSOR_PX - in_w - hw_start          # driver: start = valid - width - offset
    x0 = int(round(off_in * 2540.0 / 300))
    x1 = x0 + int(round(width * 2540.0 / dpi))
    n, back = wide_margin_lines(dpi)
    lines = int((y1 - y0) * 0.01 * k + 0.5) + 2 * n
    ny1 = y0 + int(round(lines / k * 100))
    while int((ny1 - y0) * 0.01 * k + 0.5) > lines:    # the builder's rounding
        ny1 -= 1
    while int((ny1 - y0) * 0.01 * k + 0.5) < lines:
        ny1 += 1
    stock = {300: 35, 200: 23, 150: 17, 100: 11}[dpi]  # lines skipped above row 0
    skip = max(stock, int((y0 + 300) * 0.01 * k + 0.5))   # the builder (area 7: +1 below 300)
    return {"rect": (x0, x1, y0, ny1), "width": width, "lines": lines,
            "col0": (hw_start - STOCK_HW_START) * dpi / 300.0, "row0": skip - stock - n,
            "back": back}

def _s32(v):
    return v - (1 << 32) if v & 0x80000000 else v

def _wait_mat_idle(dev, timeout):
    """Wait until the mat mover is idle or positioned (state 0/2). Returns the Y counter (raw)."""
    rd = _UsbReader(dev, OP_RAM)
    deadline = time.time() + timeout
    while rd.u32(ADDR_MAT_STATE) in (1, 3, 4):
        if time.time() >= deadline:
            raise IOError("The mat is still moving after %.0f s." % timeout)
        time.sleep(0.05)
    return rd.u32(ADDR_MAT_Y)

def _mat_move_to(dev, y, timeout):
    """Mat to Y counter `y` (AXMOVE relative, the head stays) and wait. True if it got there."""
    cur = _wait_mat_idle(dev, timeout)
    if cur == Y_UNKNOWN:
        return False
    if _s32(cur) != y:
        motion_retry(dev, AXMOVE, timeout=timeout, Y=y - _s32(cur), flags=1)
        cur = _wait_mat_idle(dev, timeout)
    return _s32(cur) == y

def scan_start_y_shift(dev, back, timeout=15.0):
    """Make the next scan start `back` Y units earlier: put any leftover shift right
    (scan_start_y_restore), lower c48 by `back`, then move the mat there. The scan feeds relative
    to where the mat is, and the firmware's re-base after the scan (Y = c48 + 4 x steps) and its
    return to c48 then stay right. c48 goes first: if anything fails after it, c48 is non-stock
    and scan_start_y_restore (mat to the stock start, then c48 back) repairs it. Ready only."""
    if not scan_start_y_restore(dev, timeout):
        raise IOError("The mat's scan start could not be put back (the machine is busy).")
    room = scan_back_max(dev)
    if back > room:
        raise IOError("The scan area starts %.1f mm higher than this mat can be backed out "
                      "(its leading edge would leave the feed rollers). Move the area down, or "
                      "load the mat with a deeper feed depth." % ((back - room) * 25.4 / 1800))
    target = STOCK_SCAN_START_Y - back
    st = ram_write32(dev, ADDR_SCAN_START_Y, target)
    if st != 0:
        raise IOError("writing the scan start failed (CSW %d)" % st)
    if not _mat_move_to(dev, target, timeout):
        raise IOError("The mat could not be moved to the scan start (is a mat loaded?).")

def scan_start_y_restore(dev, timeout=15.0):
    """Undo scan_start_y_shift once the MovState task is back in Ready: feed the mat to the stock
    scan start, then put c48 back (that order, so c48 always matches where the mat parks).
    Returns True when c48 is stock (or already was). False when it cannot be done yet (not in
    Ready, or the move did not finish): c48 then stays shifted, which is still consistent, and
    the next scan_start_y_shift / restore finishes it."""
    rd = _UsbReader(dev, OP_RAM)
    c48 = _s32(rd.u32(ADDR_SCAN_START_Y))
    if c48 == STOCK_SCAN_START_Y:
        return True
    if get_movstate(dev) != MS_READY:
        return False
    if rd.u32(ADDR_MAT_Y) != Y_UNKNOWN and not _mat_move_to(dev, STOCK_SCAN_START_Y, timeout):
        return False
    return ram_write32(dev, ADDR_SCAN_START_Y, STOCK_SCAN_START_Y) == 0

def scan_start_custom(dev, dpi, window, raw=False, quality=STOCK_JPEG_QUALITY, sampling=0,
                      wide=False, start_back=0):
    """Post a ScanToUSB-path scan of `window` (x0, x1, y0, y1 in 0.01 mm) at `dpi`: JPEG at
    `quality` / `sampling`, or raw RGB planes (raw=True). wide=True lifts the 3504-px (300 dpi)
    limit to the whole sensor, and start_back > 0 starts the scan that many Y units earlier
    (scan_start_y_shift). wide_window() gives both. Returns (CSW status, restore token).
    Always pass the token to scan_custom_restore() afterwards (success or not)."""
    if dpi not in SCAN_JPEG_DPI:
        raise ValueError("scan dpi must be one of %s" % sorted(SCAN_JPEG_DPI))
    rd = _UsbReader(dev, OP_RAM)
    saved = (rd.u32(ADDR_SCAN_AREA), rd.u32(ADDR_SCAN_RES),
             tuple(rd.u32(ADDR_SCAN_RECT + 4 * i) for i in range(4)))
    token = {"saved": saved, "tables": False, "opts": False, "y_shift": False}
    writes = [(ADDR_SCAN_AREA, 7), (ADDR_SCAN_RES, SCAN_JPEG_DPI[dpi])]
    writes += [(ADDR_SCAN_RECT + 4 * i, int(v)) for i, v in enumerate(window)]
    for addr, val in writes:
        st = ram_write32(dev, addr, val)
        if st != 0:
            raise IOError("writing the scan window failed (CSW %d)" % st)
    if not raw and quality != STOCK_JPEG_QUALITY:
        token["tables"] = True
        set_jpeg_quality(dev, quality)
    if raw or sampling or wide:
        require_caps(dev, "scan_opts", *(("scan_wide",) if wide else ()))
        token["opts"] = True
        st = scan_options(dev, raw=raw, sampling=sampling, wide=wide)
        if st != 0:
            raise CommandRejected("the machine refused the scan options (CSW %d): its vendor "
                                  "firmware may be older than this app" % st)
    if start_back:
        token["y_shift"] = True
        scan_start_y_shift(dev, start_back)
    return mov_event(dev, EV_SCAN, 0, 0), token

def scan_custom_restore(dev, token):
    """Undo scan_start_custom: panel selection, stock JPEG tables, scan options disarmed, and the
    scan start put back if the machine is back in Ready (else the next scan does it).
    Returns False if the scan start is still shifted."""
    done = scan_start_y_restore(dev) if token.get("y_shift") else True
    if token.get("opts"):
        scan_options(dev, False, 0)
    if token.get("tables"):
        set_jpeg_quality(dev, STOCK_JPEG_QUALITY)
    scan_jpeg_restore(dev, token["saved"])
    return done

def scan_jpeg_restore(dev, saved):
    """Put the ScanToUSB screen's area/resolution/custom-rectangle selection back (skips
    junk values)."""
    area, res = saved[:2]
    if area < 8 and res < 4:
        ram_write32(dev, ADDR_SCAN_AREA, area)
        ram_write32(dev, ADDR_SCAN_RES, res)
    if len(saved) > 2:
        for i, v in enumerate(saved[2]):
            ram_write32(dev, ADDR_SCAN_RECT + 4 * i, v)

def run_scan(out="scan.jpg", mode=None, submode=0, mat=0, dev=None, progress_cb=None,
             timeout=90.0, prepare=True, load=True, dpi=300, wide=False):
    """Full workflow: reach Ready with a mat, post the scan, follow the MovState
    task through 0xb/0xc/0xd back to Ready, fetch the image, free the buffer.

    mode None/0 = JPEG at `dpi` (fast), mode 1 = raw planar RGB (~37 MB, slow),
    other modes are passed to 0xC3/0x50 as-is. wide = JPEG of the whole sensor width
    (wide_window), 12x12 length.
    """
    own = dev is None
    if own:
        dev = _dev()
    log = lambda s: print(s, file=sys.stderr)
    saved = token = None
    try:
        if prepare:
            scan_prepare(dev, load=load, log=log)
        ms = get_movstate(dev)
        if ms != MS_READY:
            raise IOError("MovState 0x%02x (%s): a scan is only accepted in Ready (2)"
                          % (ms, movstate_name(ms)))
        if not scan_start_y_restore(dev):
            raise IOError("the scan start left by an earlier wide scan could not be put back")
        if wide:
            if mat:
                motion(dev, 0x10, X=mat)
            g = wide_window(dpi)
            log("  wide: rect %s, %d x %d px, first pixel at (%+.0f, %+d) of the stock frame"
                % (g["rect"], g["width"], g["lines"], g["col0"], g["row0"]))
            st, token = scan_start_custom(dev, dpi, g["rect"], wide=True, start_back=g["back"])
        elif not mode:
            st, saved = scan_start_jpeg(dev, dpi=dpi, mat=mat)
        else:
            st = scan_start(dev, mode=mode, submode=submode, mat=mat)
        if st != 0:
            raise IOError("scan_start failed with CSW status %d" % st)
        return _follow_scan(dev, out, progress_cb, timeout, log)
    finally:
        try:
            if saved is not None:
                scan_jpeg_restore(dev, saved)
            if token is not None:
                scan_custom_restore(dev, token)
        except Exception as e:
            log("  [!] could not restore the panel's scan selection: %s" % e)
        if own:
            close_dev(dev)

def _follow_scan(dev, out, progress_cb, timeout, log):
    t0 = time.time()
    started = False
    last_state = -1
    while time.time() - t0 < timeout:
        info = get_scan_status(dev)
        ms = info["movstate"]
        if ms != last_state:
            last_state = ms
            log("  scan state: %s (0x%02x) ptr=0x%08x len=%d handle=0x%08x" % (
                info["state_name"], ms, info["ptr"], info["len"], info["handle"]))
        if ms in MS_SCAN_BUSY:
            started = True
            prog = {0x0b: 0.05, 0x0d: 0.92}.get(ms, min(0.90, 0.10 + (time.time() - t0) / 15.0 * 0.80))
            if progress_cb:
                progress_cb(prog, info["state_name"])
        elif ms in MS_SCAN_FAIL:
            scan_stop(dev)        # event 12: return the mat, back to Ready
            raise IOError("scan failed in MovState 0x%02x (%s), abort sent"
                          % (ms, info["state_name"]))
        elif ms == MS_READY and started:
            if not (info["ptr"] and info["len"]):
                raise IOError("scan finished without image data")
            if progress_cb:
                progress_cb(1.0, "downloading image...")
            return scan_fetch(dev, info["ptr"], info["len"], out=out)
        elif ms == MS_READY and time.time() - t0 > 5.0:
            raise IOError("scan request not taken by the MovState task")
        elif ms != MS_READY:
            raise IOError("unexpected MovState 0x%02x (%s) during scan"
                          % (ms, info["state_name"]))
        time.sleep(0.15)
    raise TimeoutError("scan timed out")


def dump_region(opcode, addr, size, out=None, dev=None, progress_cb=None):
    """Read `size` bytes at `addr` (0xC0 RAM / 0xC1 flash). progress_cb(done, size) is called
    every 64 chunks and at the end."""
    if dev is None:
        with device() as dev:
            return dump_region(opcode, addr, size, out, dev, progress_cb)
    buf = bytearray()
    tag = 0x4D454D00
    done = 0
    t0 = time.time()
    while done < size:
        n = min(CHUNK, size - done)
        tag = (tag + 1) & 0xffffffff
        data = _read_chunk(dev, tag, opcode, addr + done, n)
        if len(data) != n:
            print("  short read %d/%d @ %08x" % (len(data), n, addr + done), file=sys.stderr)
            n = len(data)
        buf += data
        done += n
        if done % (CHUNK * 64) == 0 or done == size:
            if progress_cb:
                progress_cb(done, size)
            rate = done / max(1e-3, time.time() - t0) / 1024
            print("\r  %08x  %d/%d bytes (%.0f KB/s)" % (addr + done, done, size, rate),
                  end='', file=sys.stderr, flush=True)
    print("", file=sys.stderr)
    if out:
        open(out, "wb").write(buf)
        print("wrote %d bytes -> %s" % (len(buf), out))
    return bytes(buf)

FLASH_BASE, FLASH_SIZE = 0x84000000, 0x1000000      # 16 MB, memory-mapped

def dump_flash(out, size=FLASH_SIZE, base=FLASH_BASE, restart=False, dev=None, retries=2,
               progress_cb=None):
    """Stream `size` bytes of flash at `base` to `out`, 0xC1 read by read.

    The file is written chunk by chunk, so a failure keeps everything read so far, and a
    second call RESUMES from the file's length (rounded down to a whole chunk) unless
    `restart`. A chunk that fails (stall, timeout, bad status) gets a bulk-only reset and
    `retries` more tries before the error is raised. Returns the bytes in the file."""
    import os
    if dev is None:
        with device() as dev:
            return dump_flash(out, size, base, restart, dev, retries, progress_cb)
    have = 0 if restart or not os.path.exists(out) else min(os.path.getsize(out), size)
    have -= have % CHUNK
    with open(out, "r+b" if have else "wb") as f:
        f.truncate(have)
        f.seek(have)
        if have:
            print("  resuming at +%#x (%08x)" % (have, base + have), file=sys.stderr)
        tag, t0, start = 0x464C5300, time.time(), have
        while have < size:
            n = min(CHUNK, size - have)
            for attempt in range(retries + 1):
                tag = (tag + 1) & 0xffffffff
                try:
                    data = _read_chunk(dev, tag, OP_FLASH, base + have, n)
                    if len(data) != n:
                        raise ProtocolError("short read %d/%d" % (len(data), n))
                    break
                except (IOError,) + _USB_ERRORS as e:          # includes this module's own errors
                    if attempt == retries:
                        f.flush()
                        raise IOError("flash read failed at %08x after %d tries (%s). %#x bytes "
                                      "are in %s, run the same command again to resume"
                                      % (base + have, attempt + 1, friendly_error(e), have, out)) from e
                    reset_recovery(dev)
            f.write(data)
            have += len(data)
            if have % (CHUNK * 64) == 0 or have == size:
                f.flush()
                if progress_cb:
                    progress_cb(have, size)
                print("\r  %08x  %d/%d bytes (%.0f KB/s)" % (base + have, have, size,
                      (have - start) / max(1e-3, time.time() - t0) / 1024),
                      end='', file=sys.stderr, flush=True)
    print("", file=sys.stderr)
    print("wrote %d bytes -> %s" % (have, out))
    return have

def hexdump(data, base):
    for i in range(0, len(data), 16):
        row = data[i:i+16]
        hexs = ' '.join("%02x" % b for b in row)
        asc = ''.join(chr(b) if 32 <= b < 127 else '.' for b in row)
        print("%08x  %-47s  %s" % (base + i, hexs, asc))

# --- scan-buffer helpers (FUN_88d4c768 / FUN_88d4ce38 replica) ---
# Allocator 0x88d4c768 returns a 32-bit handle (not a pointer).
# Dereference 0x88d4ce38(handle) -> data pointer, 0 on free/invalid.
#   s1 = handle & 0x3ff
#   s2 = handle >> 4 (handles are positive, so sra == srl here)
#   s0 = s2 >> 11  (== handle >> 15, heap-table index)
#   heap_desc = u32(HEAP_TABLE + s0*4)
#   chunk_base = u32(heap_desc + (s2>>4 & 0x7c) + 0x24)
#   data = u32(chunk_base + s1*4 + 0x38)
#   0x66726548 = free, 0x726c7348 = released -> treat as invalid (return 0).
HEAP_TABLE = 0x881CB8E8
MAGIC_FREE = 0x66726548
MAGIC_RLS = 0x726c7348
SCAN_SLOTS = [
    (0x8801413C, "ScanDstBuff"),
    (0x88013028 + 0x24, "ScanWork0"),
    (0x88013028 + 0x28, "ScanWork1"),
    (0x88013028 + 0x2C, "ScanWork2"),
    (0x88013028 + 0x30, "ScanWork3"),
]
DUMPSCAN_DEFAULT_CAP = 0x2200000  # 34 MB: largest ScanDstBuff ask seen (0x02165220)
# Live-heap sanity window. MAT notes say RAM is 0x88000000..~0x8b100000.
# allow up to 0x8c000000 for heap high, but anything else (0x40xxxxxx,
# 0x43xxxxxx, ...) is a torn read or stale handle, not image data.
RAM_LO, RAM_HI = 0x88000000, 0x8c000000
MAX_HEAP_SPAN = 0x4000000  # 64 MB: any [base,limit) bigger than this is garbage

def _is_ram_ptr(x):
    return x is not None and RAM_LO <= (x & 0xffffffff) < RAM_HI

class _UsbReader(object):
    """Single-connection small-read helper (keeps CBW tag incrementing)."""
    def __init__(self, dev, opcode=OP_RAM):
        self.dev = dev
        self.opcode = opcode
        self.tag = 0x5343414e
    def read(self, addr, size):
        buf = bytearray()
        while len(buf) < size:
            n = min(CHUNK, size - len(buf))
            self.tag = (self.tag + 1) & 0xffffffff
            buf += _read_chunk(self.dev, self.tag, self.opcode, addr + len(buf), n)
        return bytes(buf)
    def u32(self, addr):
        return struct.unpack(">I", self.read(addr, 4))[0]

def _deref_chain(rd, handle, heap_table=HEAP_TABLE):
    """Raw FUN_88d4ce38 walk with no RAM-window filtering.

    Returns (ptr, trace). trace is a dict of every hop + a reject reason
    (or None when the firmware itself would accept it). Used for --force:
    even a torn chain yields *some* address to dump.
    """
    handle &= 0xffffffff
    t = {"handle": handle, "reject": "empty handle" if handle == 0 else None}
    if handle == 0:
        return 0, t
    s1 = handle & 0x3ff
    s2 = (handle & 0xffffffff) >> 4
    s0 = s2 >> 11
    t.update(s1=s1, s2=s2, s0=s0)
    if s0 >= 4:
        t["reject"] = "heap id %d >= 4" % s0
        return 0, t
    try:
        heap_desc = rd.u32(heap_table + s0 * 4)
    except Exception as e:
        t["reject"] = "heap-table read fail %s" % e
        return 0, t
    t["heap_desc"] = heap_desc
    try:
        chunk_base = rd.u32((heap_desc + ((s2 >> 4) & 0x7c) + 0x24) & 0xffffffff)
    except Exception as e:
        t["reject"] = "chunk-table read fail %s" % e
        return 0, t
    t["chunk_base"] = chunk_base
    try:
        data = rd.u32((chunk_base + s1 * 4 + 0x38) & 0xffffffff)
    except Exception as e:
        t["reject"] = "data-ptr read fail %s" % e
        return 0, t
    t["data"] = data
    if data in (MAGIC_FREE, MAGIC_RLS, 0):
        t["reject"] = "free/released/null"
        return 0, t
    if not _is_ram_ptr(heap_desc):
        t["reject"] = "heap_desc %08x outside RAM window" % (heap_desc & 0xffffffff)
    elif not _is_ram_ptr(chunk_base):
        t["reject"] = "chunk_base %08x outside RAM window" % (chunk_base & 0xffffffff)
    elif not _is_ram_ptr(data):
        t["reject"] = "data %08x outside RAM window (torn/stale?)" % (data & 0xffffffff)
    return (0 if t["reject"] else data), t

def scan_deref(rd, handle, heap_table=HEAP_TABLE):
    """Python replica of FUN_88d4ce38. Returns data pointer or 0.

    Every intermediate pointer is sanity-checked against the RAM window:
    a torn read mid-scan used to return e.g. 0x4359ad14 as [ok]. Now it
    returns 0 (invalid) instead. Use _deref_chain + --force to dump anyway.
    """
    ptr, _t = _deref_chain(rd, handle, heap_table)
    return ptr

def _stable_u32(rd, addr, retries=3):
    """Read a live word twice and retry while it keeps changing (the allocator is modifying it)."""
    first = rd.u32(addr)
    for _ in range(retries):
        second = rd.u32(addr)
        if second == first:
            return second, True
        first = second
    return first, False

def scan_info(rd, heap_table=HEAP_TABLE, retries=3):
    """Read heap table + scan slots. Returns (heaps, slots).

    heaps entries: (hid, base, limit, note). base/limit None, or note
    'torn/unstable' / 'garbage base>=limit' when the live read is suspect.
    slots entries: (name, addr, handle, ptr, status).
    """
    heaps = []
    for hid in range(4):
        try:
            desc, desc_ok = _stable_u32(rd, heap_table + hid * 4, retries)
        except Exception as e:
            heaps.append((hid, None, None, "read-err %s" % e))
            continue
        if desc == 0:
            heaps.append((hid, 0, 0, "unused"))
            continue
        if not _is_ram_ptr(desc):
            heaps.append((hid, None, None, "bad desc %08x (torn? wrong --heap-table?)" % (desc & 0xffffffff)))
            continue
        try:
            base, base_ok = _stable_u32(rd, desc, retries)
            limit, limit_ok = _stable_u32(rd, desc + 4, retries)
        except Exception as e:
            heaps.append((hid, desc, None, "desc-read-err %s" % e))
            continue
        if not (_is_ram_ptr(base) and _is_ram_ptr(limit)):
            heaps.append((hid, base, limit, "desc@%08x garbage ptr (torn?)" % desc))
        elif base >= limit or (limit - base) > MAX_HEAP_SPAN:
            heaps.append((hid, base, limit, "desc@%08x base>=limit/span-huge (torn?)" % desc))
        else:
            flag = "" if (desc_ok and base_ok and limit_ok) else "unstable! "
            heaps.append((hid, base, limit, "%sdesc@%08x" % (flag, desc)))
    slots = []
    for addr, name in SCAN_SLOTS:
        try:
            handle, h_ok = _stable_u32(rd, addr, retries)
        except Exception as e:
            slots.append((name, addr, None, None, "read-err %s" % e))
            continue
        ptr = scan_deref(rd, handle, heap_table) if handle else 0
        if not handle:
            slots.append((name, addr, handle, 0, "empty"))
        elif ptr:
            slots.append((name, addr, handle, ptr, "ok" if h_ok else "ok? (handle unstable)"))
        else:
            slots.append((name, addr, handle, 0, "invalid/free/stale" if h_ok else "invalid? (handle unstable)"))
    return heaps, slots

def dump_region_tolerant(dev, opcode, addr, size, out=None, retries=1):
    """Like dump_region but keeps a partial file if the buffer is freed mid-dump.

    The scan task frees ScanDstBuff as soon as the scan finishes, so a 34 MB
    dump started mid-scan will almost always hit a CSW error partway. Instead
    of aborting with nothing, stop at first unreadable chunk, write what was read.
    Returns bytes (possibly short).
    """
    buf = bytearray()
    tag = 0x4D454D00
    done = 0
    t0 = time.time()
    while done < size:
        n = min(CHUNK, size - done)
        tag = (tag + 1) & 0xffffffff
        try:
            data = _read_chunk(dev, tag, opcode, addr + done, n)
        except Exception as e:
            print("\n  [!] chunk @%08x failed (%s): buffer likely freed mid-dump." % (addr + done, e), file=sys.stderr)
            try:
                reset_recovery(dev)
            except Exception:
                pass
            if retries > 0:
                try:
                    tag = (tag + 1) & 0xffffffff
                    data = _read_chunk(dev, tag, opcode, addr + done, n)
                except Exception as e2:
                    print("  [!] retry failed (%s). Keeping %d/%d bytes." % (e2, done, size), file=sys.stderr)
                    break
            else:
                print("  [!] keeping %d/%d bytes." % (done, size), file=sys.stderr)
                break
        if len(data) != n:
            print("\n  short read %d/%d @ %08x" % (len(data), n, addr + done), file=sys.stderr)
            buf += data
            break
        buf += data
        done += n
        if done % (CHUNK * 64) == 0 or done == size:
            rate = done / max(1e-3, time.time() - t0) / 1024
            print("\r  %08x  %d/%d bytes (%.0f KB/s)" % (addr + done, done, size, rate),
                  end='', file=sys.stderr, flush=True)
    print("", file=sys.stderr)
    if out:
        open(out, "wb").write(buf)
        extra = " (PARTIAL: freed mid-dump?)" if len(buf) < size else ""
        print("wrote %d/%d bytes -> %s%s" % (len(buf), size, out, extra))
    return bytes(buf)

def dump_scan(out=None, size=None, slot=None, info_only=False, force=False,
              addr=None, heap_table=HEAP_TABLE, raw=False, retries=3):
    dev = _dev()
    rd = _UsbReader(dev, OP_RAM)
    if raw:
        # No deref: snapshot the live words themselves so a torn heap cannot
        # produce a fake 0x4359ad14 pointer. Useful when slots read empty/
        # garbage because the scan already freed the buffer.
        chunks = [(0x88014130, 0x20, "slots_88014130"), (0x88013028, 0x60, "slots_88013028"),
                  (heap_table, 0x20, "heap_table")]
        blob = bytearray()
        print("raw snapshot (no deref):")
        for base, n, tag_ in chunks:
            try:
                d = rd.read(base, n)
                print("  %s @%08x: %s" % (tag_, base, d.hex()))
                blob += struct.pack(">II", base, n) + d
            except Exception as e:
                print("  %s @%08x: READ FAIL %s" % (tag_, base, e))
        if out and not info_only:
            open(out, "wb").write(blob)
            print("wrote %d bytes metadata -> %s" % (len(blob), out))
        return 0
    heaps, slots = scan_info(rd, heap_table, retries)
    print("heap table @%08x:" % heap_table)
    for hid, base, limit, note in heaps:
        if base and limit and _is_ram_ptr(base) and _is_ram_ptr(limit):
            print("  heap%d: base=%08x limit=%08x (%s)" % (hid, base, limit, note))
        else:
            print("  heap%d: base=%s limit=%s (%s)" % (hid,
                  "%08x" % base if isinstance(base, int) else base,
                  "%08x" % limit if isinstance(limit, int) else limit, note))
            if "torn" in str(note) or "unstable" in str(note) or "garbage" in str(note) or "bad desc" in str(note):
                print("       ^ torn read: scan task is mutating the heap mid-read. Retry, or pause the feed.")
    print("scan slots:")
    for name, saddr, handle, ptr, st in slots:
        if handle is None:
            print("  %-12s @%08x: %s" % (name, saddr, st))
        else:
            print("  %-12s @%08x: handle=%08x ptr=%08x [%s]" % (name, saddr, handle, ptr or 0, st))
    if info_only:
        return 0
    if addr is not None:
        ptr, name = addr & 0xffffffff, "manual@%08x" % (addr & 0xffffffff)
        print("using manual --addr, skipping handle deref.")
    else:
        # pick slot
        cand = [s for s in slots if s[3]]
        if slot:
            cand = [s for s in slots if s[0].lower() == slot.lower() and s[3]]
            if not cand:
                print("no valid buffer for slot %r (empty or deref failed)" % slot)
                print("hint: buffer is freed right after the scan. Use --info in a loop during the scan,")
                print("  or dump the file copy path instead. --raw snapshots handles without deref.")
                return 1
            name, saddr, handle, ptr, st = cand[0]
        else:
            # prefer ScanDstBuff, else first valid
            prim = [s for s in cand if s[0] == "ScanDstBuff"]
            picked = (prim + cand)[0] if cand else (None, None, None, None, None)
            if picked[3] is None:
                print("no scan buffer allocated (all handles empty/invalid). Run a scan first, then retry.")
                print("note: the firmware frees ScanDstBuff immediately after the scan, so it must be caught mid-scan.")
                return 1
            name, saddr, handle, ptr, st = picked
    if size is None:
        # heap remaining capped
        size = DUMPSCAN_DEFAULT_CAP
        for hid, base, limit, note in heaps:
            if isinstance(base, int) and isinstance(limit, int) and base <= ptr < limit:
                size = min(size, limit - ptr)
                break
    print("dumping %s: ptr=%08x size=%08x (%d bytes) -> %s" % (name, ptr, size, size, out or "(no file)"))
    if not _is_ram_ptr(ptr) and not force:
        print("ABORT: pointer outside RAM window %08x..%08x (stale handle / torn read). Got %08x." % (RAM_LO, RAM_HI, ptr & 0xffffffff))
        print("  This is exactly your 0x4359ad14 case: the deref chain read garbage mid-scan.")
        print("  Re-run --info during the scan until slots show [ok] with ptr in %08x..%08x." % (RAM_LO, RAM_HI))
        print("  Overrides: --force (dump anyway), --addr <ptr> (manual), --raw (snapshot handles).")
        print("  Example: python pc-driver/memdump.py ram 0x%08x 0x%x probe.bin" % (ptr & 0xffffffff, min(size, 0x10000)))
        return 1
    if not _is_ram_ptr(ptr) and force:
        print("WARNING: --force: dumping outside RAM window. Short/CSW errors will truncate.")
    data = dump_region_tolerant(dev, OP_RAM, ptr, size, out)
    if len(data) < size:
        print("PARTIAL: got %d/%d bytes. The buffer was freed or pointer went stale mid-dump." % (len(data), size))
        print("  Try a smaller --size (e.g. --size 0x10000) caught earlier in the scan, or --raw.")
    return 0 if data else 1

def main():
    if len(sys.argv) < 2:
        print(__doc__); return 1
    cmd = sys.argv[1]
    if cmd == "ping":
        st = _no_data_cmd(_dev(), 0x50494e47, 0xC2)
        print("PING: CSW status = %d  %s" % (st, "OK: vendor dispatch works (no data path)" if st==0 else "(command failed)"))
        return 0
    if cmd in ("g0", "g1", "move"):
        # g0/g1 X Y [feed] [rel]   -- coordinated XY move (steps). g0=rapid, g1=cut.
        X = int(sys.argv[2]); Y = int(sys.argv[3])
        feed = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] not in ("rel","abs") else 5
        flags = 1 if "rel" in sys.argv[2:] else 0
        sub = G00 if cmd == "g0" else G01
        st = motion(_dev(), sub, X=X, Y=Y, feed=feed, flags=flags)
        print("%s X=%d Y=%d feed=%d %s: CSW=%d %s" % (cmd.upper(), X, Y, feed,
              "rel" if flags else "abs", st, "OK" if st==0 else "(failed)"))
        return 0
    if cmd in ("m3", "m5"):
        # m3 [pressure -9..9 | raw <v>]: lower to the blade-table depth (or Z_base + 4v)
        dev = _dev()
        a = sys.argv[2:]
        raw = bool(a) and a[0] == "raw"
        val = int(a[1 if raw else 0]) if len(a) > (1 if raw else 0) else 0
        if cmd == "m3":
            motion_retry(dev, M03, Z=val, flags=M3_RAW if raw else 0, timeout=10)
        else:
            motion_retry(dev, M05, timeout=10)
        t0 = time.time()
        st = locus_state(dev)
        while st["z_state"] in (1, 4) and time.time() - t0 < 5:
            time.sleep(0.02); st = locus_state(dev)
        print("%s %s: done in %.2fs" % (cmd.upper(), ("raw v=%d" % val) if raw else
              ("pressure %d" % val) if cmd == "m3" else "", time.time() - t0))
        print_locus_state(st, "  ")
        return 0
    if cmd == "feed":
        st = motion(_dev(), 0x05)
        print("FEED (feeder, no mat needed): CSW=%d %s" % (st, "OK" if st==0 else "(failed)"))
        return 0
    if cmd == "setmat":
        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 5   # 5 = Scan 12x12 (scannable)
        # CDB[1]=0x10 (set mat kind), CDB[2]=idx
        cdb = bytes([0xC3, 0x10, idx & 0xff]) + bytes(13)
        cbw = b'USBC' + struct.pack('<I', 0x5345544d) + struct.pack('<I', 0) + bytes([0x00,0,16]) + cdb
        dev = _dev(); dev.write(EP_OUT, cbw, timeout=3000)
        csw = bytes(dev.read(EP_IN, 13, timeout=5000)); st = csw[12] if len(csw)>=13 else -1
        names = {0:"None",1:"LowTack12x12",2:"Std12x12",3:"LowTack24x12",4:"Std24x12",5:"Scan12x12",8:"Scan24x12"}
        print("SET MAT KIND = %d (%s): CSW=%d %s" % (idx, names.get(idx,"?"), st, "OK" if st==0 else "(failed)"))
        return 0
    if cmd == "ww":
        # ww <addr_hex> <val_hex> -- 0xC3 sub-op 0x11 guarded 32-bit RAM write.
        # Firmware only writes word-aligned 0x88000000..0x8BFFFFFF. Anything
        # else is ignored but reported as success (CSW=0, no write). E.g. mat/home gates:
        #   ww 0x88092d28 2, ww 0x88013c14 1, ww 0x88013c18 1
        if len(sys.argv) < 4:
            print("usage: python memdump.py ww <addr_hex> <val_hex>"); return 1
        addr = int(sys.argv[2], 16); val = int(sys.argv[3], 16)
        cdb = bytearray(16)
        cdb[0] = 0xC3; cdb[1] = 0x11
        cdb[2:6] = struct.pack(">I", addr & 0xffffffff)
        cdb[6:10] = struct.pack(">I", val & 0xffffffff)
        cbw = b'USBC' + struct.pack('<I', 0x57573131) + struct.pack('<I', 0) + bytes([0x00, 0, 16]) + bytes(cdb)
        dev = _dev(); dev.write(EP_OUT, cbw, timeout=3000)
        csw = bytes(dev.read(EP_IN, 13, timeout=5000)); st = csw[12] if len(csw) >= 13 else -1
        print("WRITE MEM [0x%08x] = 0x%08x: CSW=%d %s" % (addr & 0xffffffff, val & 0xffffffff, st, "OK" if st == 0 else "(failed)"))
        return 0
    if cmd == "tool":
        # tool [status] | tool off [std|deep] [orig|general] | tool auto | tool kind <n>
        dev = _dev()
        a = sys.argv[2:]
        sub = a[0] if a else "status"
        if sub == "off":
            blade = 4 if "deep" in a else 3
            pen = 1 if "orig" in a else 2
            # defaults first, then the flag, so polling never runs with blade/pen kind 0
            sts = [ram_write32(dev, ADDR_TOOL_BLADE, blade), ram_write32(dev, ADDR_TOOL_PEN, pen),
                   ram_write32(dev, ADDR_TOOL_SNSOFF, 1)]
            print("TOOL OVERRIDE ON: cut->%s, draw->%s, emboss->Emboss, foil->glue/transfer: CSW=%s"
                  % (TOOL_KINDS[blade], TOOL_KINDS[pen], sts))
        elif sub == "auto":
            st = ram_write32(dev, ADDR_TOOL_SNSOFF, 0)
            print("TOOL OVERRIDE OFF (holder switches re-read on next poll): CSW=%d" % st)
        elif sub == "kind":
            n = int(a[1])
            sts = [ram_write32(dev, ADDR_TOOL_SNSOFF, 1), ram_write32(dev, ADDR_TOOL_KIND, n)]
            print("TOOL KIND = %d (%s), sensor-off set: CSW=%s" % (n, TOOL_KINDS.get(n, "?"), sts))
            print("  note: the job-start check re-derives the kind from the mode, so use 'tool off' for jobs.")
        elif sub != "status":
            print("usage: tool [status] | tool off [std|deep] [orig|general] | tool auto | tool kind <n>")
            return 1
        tool_status(dev)
        return 0
    if cmd == "lstate":
        print_locus_state(locus_state(_dev()))
        return 0
    if cmd == "lstop":
        st = locus_stop(_dev())
        print("LSTOP: CSW=%d %s" % (st, "OK" if st == 0 else "(failed)"))
        return 0
    if cmd == "lmove":
        # lmove <X> <Y> [speed] [abs]   -- EXPERIMENTAL coordinated locus move (steps, default relative)
        X = int(sys.argv[2]); Y = int(sys.argv[3])
        rest = sys.argv[4:]
        speed = int(rest[0]) if rest and rest[0] not in ("rel", "abs") else 5
        rel = "abs" not in rest
        print("LMOVE X=%d Y=%d speed=%d %s" % (X, Y, speed, "rel" if rel else "abs"))
        ok, st0, st1 = locus_move(_dev(), X, Y, speed=speed, rel=rel)
        dx, dy = st1["curX"] - st0["curX"], (None if st0["curY"] == -2**31 else st1["curY"] - st0["curY"])
        print("result: %s  dX=%d dY=%s" % ("finished" if ok else "STOPPED", dx, dy))
        return 0 if ok else 2
    if cmd == "motion":
        # motion <subop_hex> [X Y Z feed flags mode]
        a = sys.argv[2:]
        sub = int(a[0], 16)
        vals = [int(x) for x in a[1:]]
        while len(vals) < 6: vals.append(0)
        st = motion(_dev(), sub, X=vals[0], Y=vals[1], Z=vals[2], feed=vals[3], flags=vals[4], mode=vals[5])
        print("MOTION 0x%02x %s: CSW=%d %s" % (sub, vals, st, "OK" if st==0 else "(failed)"))
        return 0
    if cmd == "inq":
        d = inquiry(_dev())
        print("INQUIRY:", d[:8].hex(), "...")
        print("  version byte[2] = 0x%02x  %s" % (
            d[2], "<-- TELLTALE: patched firmware is LIVE" if d[2]==5 else
                  "(stock value 2 - patched firmware NOT active)" if d[2]==2 else "(unexpected)"))
        print("  vendor=%r product=%r" % (d[8:16], d[16:32]))
        return 0
    if cmd == "probe":
        try:
            d = dump_region(OP_FLASH, 0x84060000, 16)
        except Exception as e:
            print("probe FAILED: %s" % e)
            print("  -> the device stalled the vendor command (opcode 0xC0).")
            print("  -> run 'python pc-driver/memdump.py inq' to check if the patch is live")
            print("     (version byte 5 = live, 2 = not active).")
            try: reset_recovery(_dev())
            except Exception: pass
            return 1
        hexdump(d, 0x84060000)
        print("expect boot vector starting 3c 1c 88 01 ...")
    elif cmd == "read":
        addr = int(sys.argv[2], 16); length = int(sys.argv[3], 16)
        op = OP_FLASH if addr < 0x88000000 else OP_RAM
        hexdump(dump_region(op, addr, length), addr)
    elif cmd == "flash":
        args = [a for a in sys.argv[2:] if a != "--restart"]
        out = args[0] if args else "flash_dump.bin"
        try:
            dump_flash(out, restart="--restart" in sys.argv[2:])
        except KeyboardInterrupt:
            print("\ninterrupted: run the same command again to resume", file=sys.stderr)
            return 1
        except IOError as e:
            print("\n" + friendly_error(e), file=sys.stderr)
            return 1
    elif cmd == "ram":
        addr = int(sys.argv[2], 16); size = int(sys.argv[3], 16)
        out = sys.argv[4] if len(sys.argv) > 4 else "ram_dump.bin"
        dump_region(OP_RAM, addr, size, out)
    elif cmd == "dump":
        op = int(sys.argv[2], 16); addr = int(sys.argv[3], 16)
        size = int(sys.argv[4], 16); out = sys.argv[5]
        dump_region(op, addr, size, out)
    elif cmd == "dumpscan":
        # dumpscan <out.bin> [--size <hex>] [--slot <name>] [--info] [--force]
        #          [--addr <hex>] [--heap-table <hex>] [--raw] [--stable-retries <n>]
        out = None; size = None; slot = None; info_only = False; force = False
        addr = None; heap_table = HEAP_TABLE; raw = False; retries = 3
        args = sys.argv[2:]
        if args and not args[0].startswith("--"):
            out = args.pop(0)
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--size" and i + 1 < len(args):
                size = int(args[i + 1], 16); i += 2
            elif a == "--slot" and i + 1 < len(args):
                slot = args[i + 1]; i += 2
            elif a == "--addr" and i + 1 < len(args):
                addr = int(args[i + 1], 16); i += 2
            elif a == "--heap-table" and i + 1 < len(args):
                heap_table = int(args[i + 1], 16); i += 2
            elif a == "--stable-retries" and i + 1 < len(args):
                retries = int(args[i + 1]); i += 2
            elif a == "--info":
                info_only = True; i += 1
            elif a == "--force":
                force = True; i += 1
            elif a == "--raw":
                raw = True; i += 1
            else:
                print("unknown dumpscan arg: %r" % a); return 1
        if out is None and not info_only:
            print("usage: python memdump.py dumpscan <out.bin> [--size <hex>] [--slot <name>] [--info] [--force] [--addr <hex>] [--heap-table <hex>] [--raw]")
            return 1
        return dump_scan(out=out, size=size, slot=slot, info_only=info_only,
                         force=force, addr=addr, heap_table=heap_table, raw=raw, retries=retries)
    elif cmd == "scan":
        out = "scan.jpg"
        mode = None
        dpi = 300
        mat = 0
        load = True
        wide = False
        args = sys.argv[2:]
        if args and not args[0].startswith("--"):
            out = args.pop(0)
        i = 0
        while i < len(args):
            if args[i] == "--mode" and i + 1 < len(args):
                mode = int(args[i + 1]); i += 2
            elif args[i] == "--raw":
                mode = 1; i += 1
            elif args[i] == "--dpi" and i + 1 < len(args):
                dpi = int(args[i + 1]); i += 2
            elif args[i] == "--mat" and i + 1 < len(args):
                mat = int(args[i + 1]); i += 2
            elif args[i] == "--no-load":
                load = False; i += 1
            elif args[i] == "--wide":
                wide = True; i += 1
            else:
                print("unknown scan arg: %r" % args[i]); return 1
        if wide and mode:
            print("--wide is JPEG only (no --raw / --mode)"); return 1
        what = ("JPEG %d dpi" % dpi) if not mode else ("mode %d" % mode)
        print("starting scan (%s%s, mat override=%d) -> %s ..."
              % (what, ", full sensor width" if wide else "", mat, out))
        try:
            run_scan(out=out, mode=mode, mat=mat, load=load, dpi=dpi, wide=wide)
            print("scan completed successfully.")
        except Exception as e:
            print("scan failed: %s" % e, file=sys.stderr)
            return 1
    elif cmd == "movstate":
        dev = _dev()
        try:
            ms, kind = get_movstate(dev), get_matkind(dev)
            print("MovState 0x%02x  %s" % (ms, movstate_name(ms)))
            print("mat kind %d  %s" % (kind, MAT_KINDS.get(kind, "?")))
        finally:
            close_dev(dev)
    elif cmd == "movevent":
        if len(sys.argv) < 3:
            print("usage: python memdump.py movevent <event> [p0] [p1]"); return 1
        ev, p = int(sys.argv[2], 0), [int(x, 0) for x in sys.argv[3:5]]
        dev = _dev()
        try:
            ms = get_movstate(dev)
            st = mov_event(dev, ev, *p)
            print("event %d posted in MovState 0x%02x (%s): CSW=%d" % (ev, ms, movstate_name(ms), st))
        finally:
            close_dev(dev)
    elif cmd in ("caps", "matdiag"):
        dev = _dev()
        try:
            if cmd == "caps":
                for k, v in firmware_caps(dev).items():
                    print("%-12s %s" % (k, "yes" if v else "NO (older vendor firmware)"))
            else:
                sn = mat_snapshot(dev)
                print(describe_snapshot(sn))
                print("edge Y %d, park Y %d (units), load record %08x" % (sn["edge_y"], sn["park_y"], sn["load_rec"]))
        finally:
            close_dev(dev)
    elif cmd == "matloadcustom":
        if len(sys.argv) < 3:
            print("usage: python memdump.py matloadcustom <kind 1..8> [top_margin_mm]"); return 1
        kind = int(sys.argv[2])
        margin = float(sys.argv[3]) if len(sys.argv) > 3 else STD_TOP_MARGIN_MM
        dev = _dev()
        try:
            got = mat_load_custom(dev, kind, margin, log=print)
            print("mat kind %d  %s" % (got, MAT_KINDS.get(got, "?")))
        finally:
            close_dev(dev)
    elif cmd in ("scaninit", "matload", "matunload"):
        dev = _dev()
        log = lambda s: print(s)
        try:
            if cmd == "scaninit":
                scan_prepare(dev, load=False, log=log)
            elif cmd == "matload":
                ms = get_movstate(dev)
                if ms == MS_POWERON:
                    scan_prepare(dev, load=False, log=log)
                mov_event(dev, EV_LOAD)
                wait_movstate(dev, MS_READY, 90.0, leave_first=MS_READY, log=log)
                kind = get_matkind(dev)
                print("mat kind %d  %s" % (kind, MAT_KINDS.get(kind, "?")))
            else:
                mov_event(dev, EV_UNLOAD)
                wait_movstate(dev, MS_READY, 60.0, leave_first=MS_READY, log=log)
        finally:
            close_dev(dev)
    elif cmd == "scanstatus":
        dev = _dev()
        try:
            info = get_scan_status(dev)
            print("Scan status:")
            print("  MovState:    0x%02x (%s)" % (info["movstate"], info["state_name"]))
            print("  Mode/Submode: %d / %d" % (info["mode"], info["submode"]))
            print("  Handle:      0x%08x" % info["handle"])
            print("  FCMQ queue:  0x%08x" % info["fcmq"])
            print("  Image ptr:   0x%08x" % info["ptr"])
            print("  Image len:   %d bytes (0x%x)" % (info["len"], info["len"]))
            print("  Offsets:     R=%d G=%d B=%d" % (info["roff"], info["goff"], info["boff"]))
        finally:
            close_dev(dev)
    elif cmd == "scanfree":
        dev = _dev()
        try:
            st = scan_free(dev)
            print("SCAN_FREE: CSW=%d %s" % (st, "OK" if st == 0 else "FAIL"))
        finally:
            close_dev(dev)
    elif cmd == "scanstop":
        dev = _dev()
        try:
            st = scan_stop(dev)
            print("SCAN_STOP: CSW=%d %s" % (st, "OK" if st == 0 else "FAIL"))
        finally:
            close_dev(dev)
    elif cmd == "settings":
        import cm110_settings
        dev = _dev()
        try:
            s = cm110_settings.get_all_settings(dev)
            print("CM110 settings:")
            for k, v in s.items():
                if k != "raw_block":
                    print("  %-22s: %s" % (k, v))
        finally:
            close_dev(dev)
    elif cmd == "getsetting":
        if len(args) < 1:
            print("usage: memdump.py getsetting <key>"); return 1
        import cm110_settings
        dev = _dev()
        try:
            k = cm110_settings.resolve_key(args[0])
            val = cm110_settings.get_setting(dev, k)
            print("Setting %s (0x%02x): %d (0x%x)" % (args[0], k, val, val))
        finally:
            close_dev(dev)
    elif cmd == "setsetting":
        if len(args) < 2:
            print("usage: memdump.py setsetting <key> <val> [--no-eeprom]"); return 1
        import cm110_settings
        commit = "--no-eeprom" not in args
        dev = _dev()
        try:
            k = cm110_settings.resolve_key(args[0])
            val = int(args[1], 0)
            st = cm110_settings.set_setting(dev, k, val, commit_eeprom=commit)
            print("SET %s = %d (%s): CSW=%d %s" % (args[0], val, "EEPROM+RAM" if commit else "RAM-only", st, "OK" if st == 0 else "FAIL"))
        finally:
            close_dev(dev)
    else:
        print(__doc__); return 1
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except DeviceNotFound as e:
        sys.exit(str(e))
