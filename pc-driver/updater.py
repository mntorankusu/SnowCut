"""updater.py: host side of vendor opcode 0xC8 UPDATER (boot into the Updater).

Arms EEPROM 0x30, the upgrade-pending flag. While it is 1 the bootloader starts
the Updater instead of the main firmware, the same way an interrupted update
resumes. Arming reboots nothing: power-cycle, or run `reboot`. Requires firmware
built with tools/updater_handler.py (*_vendor.upf), and `reboot` needs handler v2.

`reboot` makes the main firmware stop feeding its hardware watchdog, so the
machine resets about a second after the command completes (the same exit the
firmware's own crash handler takes). Close any open files first.

The Updater clears the flag only when it finishes an update, so once the
machine is in the Updater, the way back is to run an update. Keep a good .upf
ready (stock or *_vendor.upf). Before power-cycling, `disarm` undoes `arm`.

  python updater.py status
  python updater.py arm --yes
  python updater.py disarm
  python updater.py reboot --yes

  CDB[0] = 0xC8
  CDB[1] = 0x00 STATUS (data-in 8 B) | 0x01 ARM | 0x02 DISARM | 0x03 REBOOT
  CDB[2:4] = b"UP" for ARM / DISARM, b"RB" for REBOOT
"""
import struct

OP_UPDATER = 0xC8
SUB_STATUS, SUB_ARM, SUB_DISARM, SUB_REBOOT = 0x00, 0x01, 0x02, 0x03
MAGIC = b"UP"
REBOOT_MAGIC = b"RB"
REBOOT_MIN_VERSION = 2
STATUS_LEN = 8
ARMED, DISARMED = 0x01, 0xFF
VID, PID = 0x04F9, 0x2107
EP_OUT, EP_IN = 0x01, 0x82


def updater_cdb(sub):
    if sub not in (SUB_STATUS, SUB_ARM, SUB_DISARM, SUB_REBOOT):
        raise ValueError("unknown updater sub-op %#x" % sub)
    cdb = bytearray(16)
    cdb[0] = OP_UPDATER
    cdb[1] = sub
    if sub == SUB_REBOOT:
        cdb[2:4] = REBOOT_MAGIC
    elif sub != SUB_STATUS:
        cdb[2:4] = MAGIC
    return bytes(cdb)


def decode_status(raw):
    """8-byte STATUS reply -> dict. Bytes 0..5 = EEPROM 0x30..0x35."""
    raw = bytes(raw)
    if len(raw) != STATUS_LEN or raw[7] != OP_UPDATER:
        raise IOError("bad UPDATER status reply %s" % raw.hex())
    pending = raw[0]
    return dict(
        pending=pending,
        armed=pending == ARMED,
        state={ARMED: "ARMED (next power-on starts the Updater)",
               DISARMED: "idle (normal boot)"}.get(pending, "unknown value %#04x" % pending),
        last_error=raw[1],
        line_mode=raw[2],           # 0x32: must stay 0xFF (1 = factory line upgrade)
        done_flag=raw[3],
        upf_id=raw[4] | raw[5] << 8,
        version=raw[6],
        raw=raw,
    )


def _cbw(tag, cdb, length, data_in):
    return (b"USBC" + struct.pack("<II", tag, length)
            + bytes([0x80 if data_in else 0x00, 0, 16]) + cdb)


def _csw(dev, tag):
    csw = bytes(dev.read(EP_IN, 13, timeout=5000))
    if len(csw) < 13 or csw[:4] != b"USBS":
        raise IOError("bad CSW %r" % csw)
    if struct.unpack("<I", csw[4:8])[0] != tag:
        raise IOError("CSW tag mismatch")
    return csw[12]


def request_sense(dev):
    tag = 0x53454E53
    cdb = bytes([0x03, 0, 0, 0, 18, 0]) + bytes(10)
    dev.write(EP_OUT, _cbw(tag, cdb, 18, True), timeout=3000)
    data = bytes(dev.read(EP_IN, 18, timeout=3000))
    _csw(dev, tag)
    return data[2] & 0x0F, data[12], data[13]


def status(dev):
    tag = 0x55504453
    dev.write(EP_OUT, _cbw(tag, updater_cdb(SUB_STATUS), STATUS_LEN, True), timeout=3000)
    data = bytes(dev.read(EP_IN, STATUS_LEN, timeout=5000))
    st = _csw(dev, tag)
    if st != 0:
        raise IOError("STATUS failed: CSW %d (old firmware without 0xC8?)" % st)
    return decode_status(data)


def set_pending(dev, armed):
    """ARM (True) or DISARM (False). Returns (csw_status, sense or None)."""
    tag = 0x55504441
    dev.write(EP_OUT, _cbw(tag, updater_cdb(SUB_ARM if armed else SUB_DISARM), 0, False),
              timeout=3000)
    st = _csw(dev, tag)
    return st, (request_sense(dev) if st else None)


def reboot(dev):
    """Ask the main firmware to let its watchdog reset the machine.
    Returns (csw_status, sense or None). Sense 05/2C/00 = watchdog not running
    (power-on key mode 3), 05/24/00 = firmware without REBOOT (handler v1).
    The device drops off the bus about a second after a GOOD status."""
    tag = 0x55505242
    dev.write(EP_OUT, _cbw(tag, updater_cdb(SUB_REBOOT), 0, False), timeout=3000)
    st = _csw(dev, tag)
    return st, (request_sense(dev) if st else None)


def wait_gone(timeout=15.0, poll=0.25):
    """True once VID:PID has left the bus (within timeout)."""
    import time
    import scsipt
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not scsipt.machine_present(VID, PID):
            return True
        time.sleep(poll)
    return False


def wait_back(timeout=60.0, poll=0.5):
    """Wait for VID:PID to re-enumerate. True once it has, None if it never did."""
    import time
    import scsipt
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if scsipt.machine_present(VID, PID):
            return True
        time.sleep(poll)
    return None


def _open():
    import scsipt
    try:
        return scsipt.open_machine(VID, PID)
    except scsipt.OpenFailed as f:
        raise SystemExit("device %04X:%04X not reachable: %s%s" % (
            VID, PID, f, " (run as administrator)" if f.elevation else ""))


def _print_status(s):
    print("EEPROM 0x30 pending : %#04x  %s" % (s["pending"], s["state"]))
    print("EEPROM 0x31 error   : %#04x" % s["last_error"])
    print("EEPROM 0x32 line    : %#04x%s" % (s["line_mode"], "" if s["line_mode"] == 0xFF
                                             else "  <-- WARNING: line-upgrade mode set"))
    print("EEPROM 0x33 done    : %#04x" % s["done_flag"])
    print("EEPROM 0x34 upf id  : %#06x" % s["upf_id"])
    print("handler version     : %d" % s["version"])


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("status", "arm", "disarm", "reboot"):
        print(__doc__)
        return 1
    if argv[0] == "reboot" and "--yes" not in argv:
        print("reboot resets the machine about a second from now, like a crash would:")
        print("anything still being written is lost. If EEPROM 0x30 is armed it comes")
        print("back up in the Updater. Re-run as:  python updater.py reboot --yes")
        return 1
    if argv[0] == "arm" and "--yes" not in argv:
        print("arm sets EEPROM 0x30 = 1: the NEXT power-on starts the Updater, and the")
        print("machine stays in the Updater until an update completes. Have a good .upf")
        print("ready. Re-run as:  python updater.py arm --yes")
        return 1
    dev = _open()
    if argv[0] == "status":
        _print_status(status(dev))
        return 0
    if argv[0] == "reboot":
        s = status(dev)
        if s["version"] < REBOOT_MIN_VERSION:
            print("firmware handler v%d has no REBOOT (needs v%d)"
                  % (s["version"], REBOOT_MIN_VERSION))
            return 3
        st, sense = reboot(dev)
        if st:
            why = {(5, 0x2C, 0): " (watchdog not running)"}.get(tuple(sense), "")
            print("reboot refused: CSW %d, sense %02x/%02x/%02x%s" % (st, *sense, why))
            return 3
        print("Reboot accepted (%s). Waiting for the device to drop off..."
              % ("next boot: UPDATER" if s["armed"] else "next boot: main firmware"))
        if not wait_gone():
            print("device still present after 15 s: the watchdog did not fire")
            return 4
        print("Gone. Waiting for it to come back...")
        if wait_back() is None:
            print("not back within 60 s")
            return 5
        print("Back on the bus.")
        return 0
    armed = argv[0] == "arm"
    st, sense = set_pending(dev, armed)
    if st:
        print("%s failed: CSW %d, sense %02x/%02x/%02x" % (argv[0], st, *sense))
        return 3
    s = status(dev)
    _print_status(s)
    if s["armed"] != armed:
        print("read-back does not match!")
        return 4
    if armed:
        print("\nArmed. Turn the machine off and on to start the Updater.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
