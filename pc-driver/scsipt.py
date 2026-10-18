"""scsipt.py: access the CM110 through Windows' own mass-storage driver, no libusb needed.

The machine is a standard USB mass-storage (bulk-only) device and every command sent to it is a
SCSI CDB in a hand-built CBW. libusb (pyusb) carries those as raw bulk transfers but needs the
libusb-win32 / WinUSB driver bound to the device, which replaces usbstor. This module sends the
same CDBs with IOCTL_SCSI_PASS_THROUGH_DIRECT on the disk usbstor already created, so nothing
has to be installed. The price: Windows only lets administrators open a disk for pass-through.

`ScsiDevice` behaves like the pyusb device the driver modules already use (write(EP_OUT, ..),
read(EP_IN, ..), ctrl_transfer, clear_halt) and emulates just enough bulk-only transport on top
of pass-through for them: a CBW write is held back, the command runs when its data phase is
reached (OUT payload written / first IN read / CSW read), and the data and a synthetic CSW are
handed back by the following reads.

  * A failed IN command with no data looks like a stalled IN pipe (the read raises, as the
    clients expect), and its CSW then reports status 1.
  * usbstor fetches REQUEST SENSE itself after a failure, which would eat the firmware's sense
    before the client's own 0x03 command arrives. The sense usbstor returned is kept and
    served to that next 0x03 instead of asking the device again.
  * Bus-level recovery (bulk-only reset / clear halt) is handled by usbstor. Here it only resets
    this emulation's state.

A normal (non-elevated) process cannot do this itself, and an elevated *app* cannot own a drive
letter Explorer can see. So open_machine() hands the disk to a small elevated helper process
(bottom of this file) when the user has allowed it (allow_broker / start_broker).

API: find_paths(vid, pid), present(vid, pid), open_device(vid, pid) -> ScsiDevice, is_admin(),
open_machine(vid, pid) (libusb -> direct pass-through -> helper), start_broker(), stop_broker(),
broker_alive(), allow_broker().
Errors are IOErrors: PassthroughError, PassthroughTimeout ("timeout" in its text),
PassthroughStall, DeviceMissing, AccessDenied (not elevated).
"""
import ctypes
import struct
import sys
import uuid

EP_OUT, EP_IN = 0x01, 0x82

IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x4D014
IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
GUID_DEVINTERFACE_DISK = "53f56307-b6bf-11d0-94f2-00a0c91efb8b"
GUID_DEVINTERFACE_CDROM = "53f56308-b6bf-11d0-94f2-00a0c91efb8b"

ERROR_ACCESS_DENIED = 5
ERROR_SEM_TIMEOUT = 121


class PassthroughError(IOError):
    """The Windows storage stack refused or failed a pass-through request."""


class PassthroughTimeout(PassthroughError):
    """A command did not finish in time."""


class PassthroughStall(PassthroughError):
    """Emulated stalled IN pipe: the command failed before any data."""


class DeviceMissing(PassthroughError):
    """No CM110 disk is visible to Windows."""


class AccessDenied(PassthroughError):
    """Windows needs administrator rights for SCSI pass-through."""


def _win():
    return sys.platform == "win32"


def is_admin():
    if not _win():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# -- Win32 plumbing (built lazily so importing this module is safe anywhere) --
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text):
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


class _IfData(ctypes.Structure):          # SP_DEVICE_INTERFACE_DATA
    _fields_ = [("cbSize", ctypes.c_uint32), ("Guid", _GUID), ("Flags", ctypes.c_uint32),
                ("Reserved", ctypes.c_size_t)]


class _DevInfo(ctypes.Structure):         # SP_DEVINFO_DATA
    _fields_ = [("cbSize", ctypes.c_uint32), ("Guid", _GUID), ("DevInst", ctypes.c_uint32),
                ("Reserved", ctypes.c_size_t)]


class _SPTD(ctypes.Structure):            # SCSI_PASS_THROUGH_DIRECT
    _fields_ = [("Length", ctypes.c_uint16), ("ScsiStatus", ctypes.c_ubyte),
                ("PathId", ctypes.c_ubyte), ("TargetId", ctypes.c_ubyte),
                ("Lun", ctypes.c_ubyte), ("CdbLength", ctypes.c_ubyte),
                ("SenseInfoLength", ctypes.c_ubyte), ("DataIn", ctypes.c_ubyte),
                ("DataTransferLength", ctypes.c_uint32), ("TimeOutValue", ctypes.c_uint32),
                ("DataBuffer", ctypes.c_void_p), ("SenseInfoOffset", ctypes.c_uint32),
                ("Cdb", ctypes.c_ubyte * 16)]


class _SPTDBuf(ctypes.Structure):         # ..._WITH_BUFFERS shape: header + sense area
    _fields_ = [("sptd", _SPTD), ("Filler", ctypes.c_uint32), ("Sense", ctypes.c_ubyte * 32)]


class _Query(ctypes.Structure):           # STORAGE_PROPERTY_QUERY
    _fields_ = [("PropertyId", ctypes.c_uint32), ("QueryType", ctypes.c_uint32),
                ("Extra", ctypes.c_ubyte * 1)]


class _AdapterDesc(ctypes.Structure):     # STORAGE_ADAPTER_DESCRIPTOR (leading fields)
    _fields_ = [("Version", ctypes.c_uint32), ("Size", ctypes.c_uint32),
                ("MaximumTransferLength", ctypes.c_uint32),
                ("MaximumPhysicalPages", ctypes.c_uint32), ("AlignmentMask", ctypes.c_uint32)]


_api = {}


def _load():
    if _api:
        return _api
    if not _win():
        raise DeviceMissing("SCSI pass-through is only available on Windows")
    from ctypes import wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sa = ctypes.WinDLL("setupapi", use_last_error=True)
    cm = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    vp = ctypes.c_void_p
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, vp, wt.DWORD, wt.DWORD, vp]
    k32.CreateFileW.restype = vp
    k32.CloseHandle.argtypes = [vp]
    k32.CloseHandle.restype = wt.BOOL
    k32.DeviceIoControl.argtypes = [vp, wt.DWORD, vp, wt.DWORD, vp, wt.DWORD,
                                    ctypes.POINTER(wt.DWORD), vp]
    k32.DeviceIoControl.restype = wt.BOOL
    sa.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wt.LPCWSTR, vp, wt.DWORD]
    sa.SetupDiGetClassDevsW.restype = vp
    sa.SetupDiEnumDeviceInterfaces.argtypes = [vp, vp, ctypes.POINTER(_GUID), wt.DWORD,
                                               ctypes.POINTER(_IfData)]
    sa.SetupDiEnumDeviceInterfaces.restype = wt.BOOL
    sa.SetupDiGetDeviceInterfaceDetailW.argtypes = [vp, ctypes.POINTER(_IfData), vp, wt.DWORD,
                                                    ctypes.POINTER(wt.DWORD),
                                                    ctypes.POINTER(_DevInfo)]
    sa.SetupDiGetDeviceInterfaceDetailW.restype = wt.BOOL
    sa.SetupDiDestroyDeviceInfoList.argtypes = [vp]
    sa.SetupDiDestroyDeviceInfoList.restype = wt.BOOL
    cm.CM_Get_Parent.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD, wt.ULONG]
    cm.CM_Get_Parent.restype = wt.DWORD
    cm.CM_Get_Device_IDW.argtypes = [wt.DWORD, wt.LPWSTR, wt.ULONG, wt.ULONG]
    cm.CM_Get_Device_IDW.restype = wt.DWORD
    _api.update(k32=k32, sa=sa, cm=cm, wt=wt, invalid=ctypes.c_void_p(-1).value)
    return _api


def _ancestor_ids(cm, wt, devinst, depth=6):
    """Device instance IDs of `devinst` and its parents (upper-cased)."""
    out = []
    cur = wt.DWORD(devinst)
    for _ in range(depth):
        buf = ctypes.create_unicode_buffer(512)
        if cm.CM_Get_Device_IDW(cur.value, buf, 512, 0) == 0:
            out.append(buf.value.upper())
        parent = wt.DWORD()
        if cm.CM_Get_Parent(ctypes.byref(parent), cur.value, 0) != 0:
            break
        cur = parent
    return out


def find_paths(vid, pid):
    """Device-interface paths of the disks (or CD-ROMs) whose USB ancestor is vid:pid."""
    if not _win():
        return []
    a = _load()
    sa, cm, wt = a["sa"], a["cm"], a["wt"]
    tag = "VID_%04X&PID_%04X" % (vid, pid)
    paths = []
    for g in (GUID_DEVINTERFACE_DISK, GUID_DEVINTERFACE_CDROM):
        guid = _guid(g)
        h = sa.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x02 | 0x10)  # PRESENT|IFACE
        if h in (None, a["invalid"]):
            continue
        try:
            i = 0
            while True:
                ifd = _IfData()
                ifd.cbSize = ctypes.sizeof(_IfData)
                if not sa.SetupDiEnumDeviceInterfaces(h, None, ctypes.byref(guid), i,
                                                      ctypes.byref(ifd)):
                    break
                i += 1
                need = wt.DWORD()
                sa.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(ifd), None, 0,
                                                    ctypes.byref(need), None)
                if not need.value:
                    continue
                buf = ctypes.create_string_buffer(need.value)
                struct.pack_into("<I", buf, 0, 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6)
                info = _DevInfo()
                info.cbSize = ctypes.sizeof(_DevInfo)
                if not sa.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(ifd), buf,
                                                           need.value, None,
                                                           ctypes.byref(info)):
                    continue
                path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                if any(tag in s for s in _ancestor_ids(cm, wt, info.DevInst)):
                    paths.append(path)
        finally:
            sa.SetupDiDestroyDeviceInfoList(h)
    return paths


def present(vid, pid):
    """True when Windows has a mass-storage disk for vid:pid (no elevation needed)."""
    try:
        return bool(find_paths(vid, pid))
    except Exception:
        return False


def _winerr(code):
    try:
        return str(ctypes.WinError(code)).split("] ", 1)[-1].strip()
    except Exception:
        return "Windows error %d" % code


class ScsiDevice:
    """pyusb-device look-alike over pass-through. See the module docstring."""
    passthrough = True

    def __init__(self, handle, path, max_transfer=0x10000, align_mask=0xFFF):
        self._h = handle
        self.path = path
        self._max = max_transfer
        self._align = max(16, align_mask + 1)
        self._reset_state()
        self._sense = b""            # last sense usbstor fetched after a failure

    # -- emulated bulk-only state --
    def _reset_state(self):
        self._cbw = None             # (tag, dlen, data_in, cdb) waiting to run
        self._out = bytearray()
        self._ran = False
        self._data_phase = False     # IN data phase not yet consumed by a read
        self._in = bytearray()
        self._csw = b""
        self._stall = False

    def close(self):
        h, self._h = self._h, None
        if h is not None:
            _load()["k32"].CloseHandle(h)

    def ctrl_transfer(self, bm, req, value=0, index=0, data=0, timeout=None):
        if (bm, req) == (0x21, 0xFF):                 # Bulk-Only Mass Storage Reset
            self._reset_state()
            return 0
        raise PassthroughError("control transfer %02x/%02x is not available without libusb"
                               % (bm, req))

    def clear_halt(self, ep):
        if ep == EP_IN:
            self._stall = False

    def write(self, ep, data, timeout=None):
        data = bytes(data)
        if ep != EP_OUT:
            raise PassthroughError("write to endpoint %#x" % ep)
        c = self._cbw
        if c is not None and not self._ran and not c[2] and c[1] > 0:
            self._out += data                         # OUT payload for the pending command
            if len(self._out) >= c[1]:
                self._run(timeout)
            return len(data)
        if self._cbw is not None and not self._ran:
            self._flush()
        # Not always 31 bytes: cm110_fs sends its CDB unpadded (a 9-byte memread -> 24 bytes),
        # which libusb and the firmware accept, so the length only has to cover the CDB.
        if len(data) < 16 or data[:4] != b"USBC":
            raise PassthroughError("expected a CBW, got %d bytes" % len(data))
        tag, dlen, flags, _lun, n = struct.unpack("<IIBBB", data[4:15])
        if not 1 <= n <= 16 or len(data) < 15 + n:
            raise PassthroughError("bad CBW: CDB length %d in a %d-byte block" % (n, len(data)))
        self._reset_state()
        self._cbw = (tag, dlen, bool(flags & 0x80), bytes(data[15:15 + n]))
        self._data_phase = bool(flags & 0x80) and dlen > 0
        return len(data)

    def read(self, ep, n, timeout=None):
        if ep != EP_IN:
            raise PassthroughError("read from endpoint %#x" % ep)
        if self._cbw is None and not self._csw and not self._in:
            raise PassthroughTimeout("timeout: no command is waiting for a reply")
        if not self._ran:
            self._run(timeout)
        if self._stall:
            self._stall = False
            raise PassthroughStall("IN pipe stalled (command failed)")
        if self._in:
            out = bytes(self._in[:n])
            del self._in[:n]
            self._data_phase = False
            return out
        if self._data_phase and not (n == 13 and self._dlen != 13):
            self._data_phase = False
            return b""                                 # zero-length data phase
        self._data_phase = False
        if self._csw:
            out, self._csw = self._csw[:n], b""
            return out
        raise PassthroughTimeout("timeout: no reply from the machine")

    # -- running a command --
    def _flush(self):
        try:
            self._run(3000)
        except Exception:
            pass

    @property
    def _dlen(self):
        return self._cbw[1] if self._cbw else 0

    def _run(self, timeout_ms):
        tag, dlen, data_in, cdb = self._cbw
        out = bytes(self._out[:dlen]) if not data_in else b""
        self._ran = True
        try:
            if cdb[0] == 0x03 and self._sense:         # serve the sense usbstor already read
                got, status, sense = self._sense[:dlen].ljust(min(dlen, 18), b"\0"), 0, b""
            else:
                got, status, sense = self._sptd(cdb, data_in, dlen, out, timeout_ms or 5000)
        except Exception:
            self._cbw, self._ran = None, False
            self._data_phase = False
            raise
        sent = len(got) if data_in else len(out)
        if status:
            self._sense = sense
        self._in = bytearray(got if data_in else b"")
        self._stall = bool(data_in and dlen and status and not got)
        self._csw = struct.pack("<4sIIB", b"USBS", tag, max(0, dlen - sent), 1 if status else 0)

    def _sptd(self, cdb, data_in, length, payload, timeout_ms):
        if length > self._max:
            raise PassthroughError(
                "a %d-byte transfer is more than Windows' USB storage driver allows (%d)"
                % (length, self._max))
        a = _load()
        raw = (ctypes.c_ubyte * (length + self._align))()
        base = (ctypes.addressof(raw) + self._align - 1) // self._align * self._align
        if payload:
            ctypes.memmove(base, payload, len(payload))
        s = _SPTDBuf()
        p = s.sptd
        p.Length = ctypes.sizeof(_SPTD)
        p.CdbLength = len(cdb)
        p.SenseInfoLength = ctypes.sizeof(s.Sense)
        p.DataIn = (1 if data_in else 0) if length else 2
        p.DataTransferLength = length
        p.TimeOutValue = max(1, -(-int(timeout_ms) // 1000))
        p.DataBuffer = base if length else None
        p.SenseInfoOffset = _SPTDBuf.Sense.offset
        p.Cdb[:len(cdb)] = list(cdb)
        got = a["wt"].DWORD()
        if self._h is None:
            raise PassthroughError("the machine's disk handle is closed")
        ok = a["k32"].DeviceIoControl(self._h, IOCTL_SCSI_PASS_THROUGH_DIRECT,
                                      ctypes.byref(s), ctypes.sizeof(s),
                                      ctypes.byref(s), ctypes.sizeof(s), ctypes.byref(got), None)
        if not ok:
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED:
                raise AccessDenied("Windows needs administrator rights to send SCSI commands "
                                   "to the machine")
            if err == ERROR_SEM_TIMEOUT:
                raise PassthroughTimeout("timeout: the machine did not answer")
            raise PassthroughError(_winerr(err))
        n = min(p.DataTransferLength, length) if data_in else 0
        data = ctypes.string_at(base, n) if n else b""
        sense = bytes(s.Sense[:p.SenseInfoLength]) if p.ScsiStatus else b""
        return data, p.ScsiStatus, sense


class OpenFailed(IOError):
    """open_machine() could not reach the machine by either route.

    elevation: Windows' storage stack shows the machine but needs administrator rights.
    open_error: the exception from libusb when it found the machine but could not open it.
    no_backend: pyusb or its libusb library is missing."""

    def __init__(self, msg, elevation=False, open_error=None, no_backend=False):
        super().__init__(msg)
        self.elevation, self.open_error, self.no_backend = elevation, open_error, no_backend


def open_machine(vid, pid):
    """The machine as a pyusb-style device: libusb when it is bound to the machine (no
    elevation), else SCSI pass-through on the disk Windows made for it (needs admin).
    Raises OpenFailed. The caller owns the handle (memdump.close_dev)."""
    usb = None
    no_backend = open_error = None
    try:
        import usb.core
        import usb.util
    except ImportError:
        usb, no_backend = None, True
    dev = None
    if usb is not None:
        try:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
        except usb.core.NoBackendError:
            no_backend = True
        except Exception as e:
            open_error = e
        if dev is not None:
            try:
                try:                         # Linux: usb-storage usually owns the interface
                    if dev.is_kernel_driver_active(0):
                        dev.detach_kernel_driver(0)
                except Exception:
                    pass
                dev.set_configuration()
                return dev
            except Exception as e:           # also NotImplementedError: no libusb-capable driver
                open_error = e
                try:
                    usb.util.dispose_resources(dev)
                except Exception:
                    pass
    def failed(e, elevation=False):
        # a pass-through error other than "no such disk" / "not elevated" is itself the
        # reason the machine could not be opened
        reason = open_error or (None if elevation or isinstance(e, DeviceMissing) else e)
        return OpenFailed(str(e), elevation=elevation, open_error=reason,
                          no_backend=bool(no_backend))

    try:
        return open_device(vid, pid)
    except AccessDenied as denied:
        # Not elevated: the elevated helper (started with the user's consent) can do it
        try:
            dev = _open_via_broker(vid, pid)
            if dev is not None:
                return dev
        except AccessDenied:
            pass                                   # UAC declined
        except PassthroughError as e:
            raise failed(e) from None
        raise failed(denied, elevation=True) from None
    except PassthroughError as e:
        raise failed(e) from None


def machine_present(vid, pid):
    """True while vid:pid is on the bus, whichever driver owns it (for wait-for-reboot loops)."""
    try:
        import usb.core
        if usb.core.find(idVendor=vid, idProduct=pid) is not None:
            return True
    except Exception:
        pass
    return present(vid, pid)


def _adapter_limits(handle):
    a = _load()
    q = _Query()
    q.PropertyId, q.QueryType = 1, 0                   # StorageAdapterProperty, standard query
    d = _AdapterDesc()
    got = a["wt"].DWORD()
    if a["k32"].DeviceIoControl(handle, IOCTL_STORAGE_QUERY_PROPERTY, ctypes.byref(q),
                                ctypes.sizeof(q), ctypes.byref(d), ctypes.sizeof(d),
                                ctypes.byref(got), None) and d.MaximumTransferLength:
        return d.MaximumTransferLength, d.AlignmentMask
    return 0x10000, 0xFFF


def open_device(vid, pid):
    """Open the machine's disk for pass-through. Raises DeviceMissing / AccessDenied."""
    paths = find_paths(vid, pid)
    if not paths:
        raise DeviceMissing("no Windows mass-storage disk for %04X:%04X" % (vid, pid))
    a = _load()
    last = None
    for path in paths:
        h = a["k32"].CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)   # R|W, share, OPEN
        if h in (None, a["invalid"]):
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED:
                raise AccessDenied("Windows needs administrator rights to send SCSI commands "
                                   "to the machine")
            last = PassthroughError("could not open the machine's disk (%s)" % _winerr(err))
            continue
        dev = ScsiDevice(h, path, *_adapter_limits(h))
        try:                                            # elevation check: INQUIRY needs R|W too
            dev._sptd(bytes([0x12, 0, 0, 0, 36, 0]), True, 36, b"", 3000)
        except AccessDenied:
            dev.close()
            raise
        except Exception:
            pass                                        # busy / odd: the real command will say
        return dev
    raise last or DeviceMissing("no usable disk for %04X:%04X" % (vid, pid))


# -- elevated helper ("broker") ---------------------------------------------------------------
# Windows lets only administrators open the disk for pass-through, but an elevated *app* cannot
# own a drive letter a normal Explorer can see (UAC keeps two sets of drive letters). So the app
# stays a normal process and one small elevated helper does the pass-through for it: it opens
# the machine's disk and relays write / read / reset over a loopback socket. The app asks for
# it once (one UAC prompt). RemoteScsiDevice is a drop-in for ScsiDevice that communicates with it.
#
# The helper connects OUT to a port the app listens on. Both sides prove they know a random
# token (HMAC challenge, the token itself never crosses the socket) before any command is
# accepted. The helper only ever opens the CM110's own disk.
OP_OPEN, OP_WRITE, OP_READ, OP_CTRL, OP_CLEAR, OP_CLOSE = 1, 2, 3, 4, 5, 6
_ERRORS = {1: PassthroughError, 2: PassthroughTimeout, 3: PassthroughStall,
           4: DeviceMissing, 5: AccessDenied}
_MAX_FRAME = 1 << 24
_link = None                    # the live connection to the helper, if any
_broker_allowed = False         # the user has agreed to the helper (open_machine may start it)


def _send(sock, payload):
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf += chunk
    return bytes(buf)


def _recv(sock):
    (n,) = struct.unpack(">I", _recv_exact(sock, 4))
    if n > _MAX_FRAME:
        raise EOFError("oversized frame")
    return _recv_exact(sock, n)


def _mac(token, tag, nonce):
    import hashlib
    import hmac
    return hmac.new(token, tag + nonce, hashlib.sha256).digest()


def _error_code(e):
    for code, cls in ((2, PassthroughTimeout), (3, PassthroughStall), (4, DeviceMissing),
                      (5, AccessDenied)):
        if isinstance(e, cls):
            return code
    return 1


def _serve(sock, opener=None):
    """Helper side: answer requests until the app disconnects. `opener(vid, pid)` -> device."""
    opener = opener or open_device
    devs, nxt = {}, 1
    try:
        while True:
            try:
                req = _recv(sock)
            except (EOFError, OSError):
                return
            try:
                op, h, a, b, c = struct.unpack(">BIIII", req[:17])
                data, tmo, out, res = req[17:], (c or None), b"", 0
                if op == OP_OPEN:
                    devs[nxt] = opener(a, b)
                    res, nxt = nxt, nxt + 1
                else:
                    d = devs.get(h)
                    if d is None:
                        raise PassthroughError("unknown device handle %d" % h)
                    if op == OP_WRITE:
                        d.write(a, data, tmo)
                    elif op == OP_READ:
                        out = bytes(d.read(a, b, tmo))
                    elif op == OP_CTRL:
                        d.ctrl_transfer(a, b, 0, 0, 0, tmo)
                    elif op == OP_CLEAR:
                        d.clear_halt(a)
                    elif op == OP_CLOSE:
                        devs.pop(h).close()
                    else:
                        raise PassthroughError("bad request %d" % op)
                resp = struct.pack(">BI", 0, res) + out
            except Exception as e:
                resp = struct.pack(">BI", _error_code(e), 0) + str(e).encode("utf-8", "replace")
            try:
                _send(sock, resp)
            except OSError:
                return
    finally:
        for d in devs.values():
            try:
                d.close()
            except Exception:
                pass


def broker_main(port, token_hex):
    """Entry point of the elevated helper process. -> exit code."""
    import os
    import socket
    import tempfile
    log = os.path.join(tempfile.gettempdir(), "snowcut-helper.log")
    try:
        if not is_admin():
            raise PermissionError("the helper must run elevated")
        token = bytes.fromhex(token_hex)
        sock = socket.create_connection(("127.0.0.1", int(port)), timeout=10)
        sock.settimeout(10)
        nonce = os.urandom(16)
        _send(sock, nonce)
        if _recv(sock) != _mac(token, b"c", nonce):
            return 4                                  # not the app that started this helper
        _send(sock, _mac(token, b"h", nonce))
        sock.settimeout(None)
        _serve(sock)
        return 0
    except Exception as e:
        try:
            with open(log, "a") as f:
                f.write("helper: %s: %s\n" % (type(e).__name__, e))
        except OSError:
            pass
        return 3


class _BrokerLink:
    """App side of the connection: one request at a time."""

    def __init__(self, sock):
        import threading
        self.sock, self.alive, self.lock = sock, True, threading.Lock()

    def call(self, op, h=0, a=0, b=0, c=0, data=b""):
        with self.lock:
            if not self.alive:
                raise PassthroughError("the administrator helper is not running")
            try:
                _send(self.sock, struct.pack(">BIIII", op, h, a, b, c) + bytes(data))
                resp = _recv(self.sock)
            except (OSError, EOFError):
                self.close()
                raise PassthroughError("lost the administrator helper") from None
        status, res = struct.unpack(">BI", resp[:5])
        if status:
            raise _ERRORS.get(status, PassthroughError)(resp[5:].decode("utf-8", "replace"))
        return res, resp[5:]

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


def _ms(timeout):
    return 0 if timeout is None else max(1, int(timeout))


class RemoteScsiDevice:
    """ScsiDevice look-alike whose disk handle lives in the elevated helper."""
    passthrough = True

    def __init__(self, link, handle):
        self._link, self._h = link, handle

    def write(self, ep, data, timeout=None):
        self._link.call(OP_WRITE, self._h, ep, 0, _ms(timeout), bytes(data))
        return len(data)

    def read(self, ep, n, timeout=None):
        return self._link.call(OP_READ, self._h, ep, n, _ms(timeout))[1]

    def ctrl_transfer(self, bm, req, value=0, index=0, data=0, timeout=None):
        self._link.call(OP_CTRL, self._h, bm, req, _ms(timeout))
        return 0

    def clear_halt(self, ep):
        self._link.call(OP_CLEAR, self._h, ep)

    def close(self):
        h, self._h = self._h, None
        if h is not None:
            try:
                self._link.call(OP_CLOSE, h)
            except Exception:
                pass


def broker_alive():
    return _link is not None and _link.alive


def allow_broker(allowed=True):
    """Let open_machine() start the elevated helper when it needs one (the user agreed)."""
    global _broker_allowed
    _broker_allowed = bool(allowed)


def _broker_command(port, token_hex):
    import os
    import subprocess
    if getattr(sys, "frozen", False):                 # the packaged app re-enters via main.py
        return sys.executable, subprocess.list2cmdline(["--scsi-broker", str(port), token_hex])
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw                                     # no console window
    return exe, subprocess.list2cmdline([os.path.abspath(__file__), "--broker", str(port),
                                         token_hex])


def _shell_runas(exe, params):
    """Start `exe params` elevated (UAC prompt). True once it was started."""
    import os
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params,
                                                 os.path.dirname(exe) or None, 0)
    except Exception:
        return False
    return rc > 32


def start_broker(timeout=60.0):
    """Start the elevated helper (shows the UAC prompt) and connect to it. Blocking.
    Raises AccessDenied when the prompt is declined, PassthroughError otherwise."""
    global _link, _broker_allowed
    import os
    import socket
    import time
    if broker_alive():
        return _link
    if not _win():
        raise PassthroughError("the administrator helper is only available on Windows")
    ls = socket.socket()
    try:
        ls.bind(("127.0.0.1", 0))
        ls.listen(4)
        token = os.urandom(16)
        exe, params = _broker_command(ls.getsockname()[1], token.hex())
        if not _shell_runas(exe, params):
            _broker_allowed = False                   # declined: do not keep asking
            raise AccessDenied("administrator permission was not given")
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise PassthroughError("the administrator helper did not start")
            ls.settimeout(left)
            try:
                conn, _ = ls.accept()
            except socket.timeout:
                continue
            try:
                conn.settimeout(10)
                nonce = _recv(conn)
                _send(conn, _mac(token, b"c", nonce))
                ok = _recv(conn) == _mac(token, b"h", nonce)
            except Exception:
                ok = False
            if ok:
                conn.settimeout(None)
                _link = _BrokerLink(conn)
                return _link
            conn.close()                              # someone else knocked: keep waiting
    finally:
        ls.close()


def stop_broker():
    """Disconnect: the helper closes its handles and exits."""
    global _link
    link, _link = _link, None
    if link is not None:
        link.close()


def _open_via_broker(vid, pid):
    """The machine through the helper, starting it first when the user has allowed that.
    None when there is no helper and none may be started."""
    link = _link if broker_alive() else (start_broker() if _broker_allowed else None)
    if link is None:
        return None
    handle, _ = link.call(OP_OPEN, 0, vid, pid)
    return RemoteScsiDevice(link, handle)


import atexit
atexit.register(stop_broker)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--broker":
        sys.exit(broker_main(sys.argv[2], sys.argv[3]))
    print(__doc__)
