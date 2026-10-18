"""cm110_settings.py: PC client library and CLI tool for CM110 device settings (Opcode 0xC5).

Supported Commands:
  - GET ALL (flag=0x00, key=0x00): Retrieves full 64-byte settings block in one transfer.
  - GET INDIVIDUAL (flag=0x00, key>0): Retrieves a specific setting value (4-byte integer).
  - SET (flag=0x01, key>0, value): Updates setting in RAM and commits to EEPROM.
  - SET RAM-only (flag=0x02, key>0, value): Updates setting in RAM only without EEPROM write.

Usage CLI:
  python pc-driver/cm110_settings.py get
  python pc-driver/cm110_settings.py get <key_or_name>
  python pc-driver/cm110_settings.py set <key_or_name> <value> [--no-commit]
"""
import sys
import os
import struct
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from tools.settings_handler import (
        SETTINGS_DEFS,
        EEPROM_TO_LOGICAL_KEY,
        LANGUAGE_NAMES,
        parse_settings_block
    )
except ImportError:
    # Fallback if imported standalone
    pass

OP_SETTINGS = 0xC5
VID, PID = 0x04F9, 0x2107
EP_OUT, EP_IN = 0x01, 0x82

KEY_NAME_MAP = {
    "cut_speed": 0x01,
    "speed": 0x01,
    "cut_pressure": 0x02,
    "pressure": 0x02,
    "draw_speed": 0x03,
    "draw_pressure": 0x04,
    "auto_off_hours": 0x05,
    "auto_shutdown": 0x05,
    "auto_off": 0x06,
    "unit": 0x07,
    "layout_mode": 0x08,
    "language": 0x09,
    "lang": 0x09,
    "level_0x13": 0x0A,
    "sound": 0x0B,
    "beep": 0x0B,
    "buzzer": 0x0B,
    "auto_off_base_sec": 0x0C,
}

class SettingsRejected(IOError):
    """The machine answered 0xC5 with a failed status (firmware without the settings handler,
    or a value it refused)."""

def resolve_key(key_input) -> int:
    """Resolve key from int, hex string, or name."""
    if isinstance(key_input, int):
        return key_input
    s = str(key_input).strip().lower()
    if s in KEY_NAME_MAP:
        return KEY_NAME_MAP[s]
    if s.startswith("0x"):
        return int(s, 16)
    if s.isdigit():
        return int(s)
    raise ValueError(f"Unknown setting key name: {key_input}")

def _open_dev():
    import scsipt
    try:
        dev = scsipt.open_machine(VID, PID)
    except scsipt.OpenFailed as f:
        raise SystemExit(f"CM110 USB device {VID:04X}:{PID:04X} not reachable: {f}"
                         + (" (run as administrator)" if f.elevation else "."))
    try:
        dev.ctrl_transfer(0x21, 0xFF, 0, 0, 0, timeout=1000)
    except Exception:
        pass
    for ep in (EP_OUT, EP_IN):
        try:
            dev.clear_halt(ep)
        except Exception:
            pass
    return dev

def _close_dev(dev):
    if dev is not None:
        if getattr(dev, "passthrough", False):
            dev.close()
            return
        import usb.util
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass

def read_settings_block(dev) -> bytes:
    """The raw 64-byte settings block (RAM 0x881CB720) over USB via opcode 0xC5."""
    # CDB: [0xC5, flag=0x00, key=0x00, 0x00, ...] (16 bytes)
    cdb = bytearray(16)
    cdb[0] = OP_SETTINGS
    cdb[1] = 0x00 # GET
    cdb[2] = 0x00 # ALL
    length = 64
    tag = 0x53455447 # 'SETG'

    cbw = (b'USBC' + struct.pack('<I', tag) + struct.pack('<I', length)
           + bytes([0x80, 0, 16]) + bytes(cdb))
    dev.write(EP_OUT, cbw, timeout=3000)
    data = bytes(dev.read(EP_IN, length, timeout=5000))
    csw = bytes(dev.read(EP_IN, 13, timeout=3000))
    if csw[:4] != b'USBS' or csw[12] != 0:
        raise SettingsRejected(f"settings read (0xC5) failed with CSW status {csw[12] if len(csw) >= 13 else 'None'}")
    if len(data) < 64:
        raise SettingsRejected(f"settings read (0xC5) returned {len(data)} of 64 bytes")
    return data

def get_all_settings(dev) -> dict:
    """Retrieve the entire 64-byte settings block over USB via opcode 0xC5, parsed."""
    from tools.settings_handler import parse_settings_block
    return parse_settings_block(read_settings_block(dev))

def get_setting(dev, key) -> int:
    """Retrieve an individual setting over USB via opcode 0xC5."""
    key_id = resolve_key(key)
    cdb = bytearray(16)
    cdb[0] = OP_SETTINGS
    cdb[1] = 0x00 # GET
    cdb[2] = key_id & 0xFF
    length = 4
    tag = 0x53455453 # 'SETS'

    cbw = (b'USBC' + struct.pack('<I', tag) + struct.pack('<I', length)
           + bytes([0x80, 0, 16]) + bytes(cdb))
    dev.write(EP_OUT, cbw, timeout=3000)
    data = bytes(dev.read(EP_IN, length, timeout=5000))
    csw = bytes(dev.read(EP_IN, 13, timeout=3000))
    if csw[:4] != b'USBS' or csw[12] != 0:
        raise SettingsRejected(f"setting read (0xC5) failed with CSW status {csw[12] if len(csw) >= 13 else 'None'}")
    return struct.unpack('>I', data)[0]

def set_setting(dev, key, value, commit_eeprom: bool = True) -> int:
    """Set an individual setting over USB via opcode 0xC5.
    
    Args:
        dev: USB device handle
        key: Setting key (name or ID)
        value: Integer value to write
        commit_eeprom: If True (default), updates RAM and commits to EEPROM.
                       If False, updates RAM only.
    Returns:
        CSW status (0 = success)
    """
    key_id = resolve_key(key)
    val_int = int(value) & 0xFFFFFFFF
    flag = 0x01 if commit_eeprom else 0x02

    cdb = bytearray(16)
    cdb[0] = OP_SETTINGS
    cdb[1] = flag
    cdb[2] = key_id & 0xFF
    cdb[4:8] = struct.pack('>I', val_int)
    tag = 0x53455457 # 'SETW'

    # No data phase (command fits in CDB)
    cbw = (b'USBC' + struct.pack('<I', tag) + struct.pack('<I', 0)
           + bytes([0x00, 0, 16]) + bytes(cdb))
    dev.write(EP_OUT, cbw, timeout=3000)
    csw = bytes(dev.read(EP_IN, 13, timeout=5000))
    if csw[:4] != b'USBS':
        raise IOError(f"Invalid CSW response: {csw}")
    return csw[12]

def main():
    parser = argparse.ArgumentParser(description="CM110 Device Settings Manager (Opcode 0xC5)")
    subparsers = parser.add_subparsers(dest="cmd")

    get_parser = subparsers.add_parser("get", help="Get settings (all or individual)")
    get_parser.add_argument("key", nargs="?", default=None, help="Setting name or key (omitted = get all)")

    set_parser = subparsers.add_parser("set", help="Set a setting value")
    set_parser.add_argument("key", help="Setting name or key")
    set_parser.add_argument("value", help="Value to set")
    set_parser.add_argument("--no-commit", action="store_true", help="RAM-only update (skip EEPROM commit)")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return 1

    dev = _open_dev()
    try:
        if args.cmd == "get":
            if args.key is None:
                s = get_all_settings(dev)
                print("==================================================")
                print("             CM110 Device Settings                ")
                print("==================================================")
                print(f" Cut Speed:         {s['cut_speed']} (1-5)")
                print(f" Cut Pressure:      {s['cut_pressure']} (rel: {s['cut_pressure_relative']:+d})")
                print(f" Draw Speed:        {s['draw_speed']} (1-5)")
                print(f" Draw Pressure:     {s['draw_pressure']} (rel: {s['draw_pressure_relative']:+d})")
                print(f" Auto Power-Off:    {'ON' if s['auto_off'] else 'OFF'}, after {s['auto_off_hours']} h")
                print(f" Key Beep:          {'ON' if s['sound'] else 'OFF'}")
                print(f" Unit:              {s['unit'].upper()}")
                print(f" Layout Mode:       {s['layout_mode']} (EEPROM 0x11, meaning not decoded)")
                print(f" Language:          {s['language_name']} (ID {s['language_id']})")
                print(f" Level 0x13:        {s['level_0x13']} (meaning not decoded)")
                print(f" Calibration X:     [{s['calibration']['x_min']}, {s['calibration']['x_max']}]")
                print(f" Calibration Y:     [{s['calibration']['y_min']}, {s['calibration']['y_max']}]")
                print("==================================================")
            else:
                key_id = resolve_key(args.key)
                val = get_setting(dev, key_id)
                print(f"{args.key} (key 0x{key_id:02X}): {val} (0x{val:X})")

        elif args.cmd == "set":
            key_id = resolve_key(args.key)
            commit = not args.no_commit
            st = set_setting(dev, key_id, args.value, commit_eeprom=commit)
            mode_str = "RAM + EEPROM" if commit else "RAM only"
            print(f"SET {args.key} (0x{key_id:02X}) = {args.value} [{mode_str}]: {'SUCCESS' if st == 0 else f'FAILED ({st})'}")
    finally:
        _close_dev(dev)
    return 0

if __name__ == "__main__":
    sys.exit(main())
