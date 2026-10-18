"""busy.py: host side of the on-screen "busy" pop-up (vendor opcode 0xC6).

While a busy request is active, the machine shows a modal pop-up (the same box
it uses for its own "Saving..."), and its touch UI is blocked, so nobody can
start a USB drive operation in the middle of a transfer. Requires firmware
built with tools/busy_handler.py.

  CDB[0] = 0xC6
  CDB[1] = 0x00 clear | 0x01..0xDB firmware message id | 0xFF custom text
  CDB[2..15] = custom text (<= 14 bytes UTF-8, NUL-padded) when CDB[1] = 0xFF

Firmware message IDs are translated by the machine into its UI language,
and custom text is shown verbatim. The pop-up expires by itself 15 s after the
LAST vendor command of any kind (host crash safety), so use Busy(...) as a
context manager: it keeps the pop-up alive with a background ping and always
clears it on exit.
"""
import struct
import threading

OP_BUSY, OP_PING = 0xC6, 0xC2
CLEAR, CUSTOM = 0x00, 0xFF
CUSTOM_MAX = 14
MSG_MAX = 0xDB

# Firmware message IDs (English text, and tools/test_busy.py checks them against
# the firmware's own table). The machine shows them in its UI language.
MESSAGES = dict(
    scanning=0x1d,
    moving=0x1f,
    loading=0x23,
    unloading=0x24,
    cutting=0x26,
    drawing=0x27,
    saving=0x32,
    retrieving=0x33,
    deleting=0x34,
    please_wait=0x3b,       # "Please wait a while."
    processing=0x52,
    operating=0xb5,
)

KEEPALIVE_S = 3.0           # well inside the firmware's 15 s expiry


def busy_cdb(msg):
    """msg: None/0 = clear, int = firmware message id, name from MESSAGES, or
    str/bytes = custom text (<= 14 UTF-8 bytes, printable)."""
    cdb = bytearray(16)
    cdb[0] = OP_BUSY
    if msg is None or msg == 0:
        cdb[1] = CLEAR
    elif isinstance(msg, int):
        if not 1 <= msg <= MSG_MAX:
            raise ValueError("message id %#x out of range 0x01..%#x" % (msg, MSG_MAX))
        cdb[1] = msg
    elif isinstance(msg, str) and msg in MESSAGES:
        cdb[1] = MESSAGES[msg]
    else:
        text = msg.encode("utf-8") if isinstance(msg, str) else bytes(msg)
        if not text:
            raise ValueError("custom busy text is empty")
        if len(text) > CUSTOM_MAX:
            raise ValueError("custom busy text is %d bytes UTF-8, but the maximum is %d"
                             % (len(text), CUSTOM_MAX))
        # The renderer treats 0x10/0x11 as dictionary-word tokens and stops at
        # NUL, so only printable text plus newline is allowed.
        bad = [b for b in text if b < 0x20 and b != 0x0A or b == 0x7F]
        if bad:
            raise ValueError("custom busy text has control bytes %r" % bytes(bad))
        cdb[1] = CUSTOM
        cdb[2:2 + len(text)] = text
    return bytes(cdb)


def _cbw(tag, cdb):
    return (b"USBC" + struct.pack("<II", tag, 0) + bytes([0x00, 0, 16]) + cdb)


def send_raw(dev, cdb, tag=0x42555359, ep_out=0x01, ep_in=0x82):
    """Send a no-data vendor CDB on a raw pyusb handle. Returns CSW status
    (0 = ok, 1 = rejected e.g. bad id / old firmware)."""
    dev.write(ep_out, _cbw(tag, cdb), timeout=3000)
    csw = bytes(dev.read(ep_in, 13, timeout=5000))
    if len(csw) < 13 or csw[:4] != b"USBS":
        raise IOError("bad CSW for busy command")
    return csw[12]


def set_busy(dev, msg):
    """One-shot (no keep-alive). Prefer Busy(...) for anything longer than a
    few seconds."""
    return send_raw(dev, busy_cdb(msg))


def ping_raw(dev):
    """Refresh the pop-up heartbeat (any vendor command does, and PING is the cheapest)."""
    return send_raw(dev, bytes([OP_PING]) + bytes(15), tag=0x50494E47)


def try_set_busy(dev, msg, warn=None):
    """set_busy that never raises (the pop-up is cosmetic, and older firmware
    rejects 0xC6 with CSW status 1). Returns True if the machine accepted it."""
    try:
        return set_busy(dev, msg) == 0
    except Exception as e:
        if warn:
            warn("busy %r failed: %s" % (msg, e))
        return False


class Busy(object):
    """with Busy(send, "saving"): ...   -- send(cdb) issues one no-data CDB.

    Shows the pop-up on entry, pings every KEEPALIVE_S so it outlives quiet
    stretches (waiting on a slow motor, host-side processing), clears on exit
    even if the body raised. Nested/reentrant use just changes the message.
    Failures to show/clear are reported to `warn` and never mask the body's
    own result: the pop-up is cosmetic.

    keepalive=None: no background thread. Use this when `send` is NOT
    thread-safe (a raw pyusb handle driven from one thread, e.g. pc-app): the
    caller's own vendor commands refresh the heartbeat, and it must call
    ping_raw() itself during quiet waits longer than a few seconds.
    """

    def __init__(self, send, msg, warn=None, keepalive=KEEPALIVE_S):
        self._send = send
        self._cdb = busy_cdb(msg)       # validate before touching the device
        self._warn = warn or (lambda m: None)
        self._period = keepalive
        self._stop = threading.Event()
        self._thr = None
        self._shown = False

    def _try(self, cdb, what):
        try:
            self._send(cdb)
            return True
        except Exception as e:
            self._warn("busy %s failed: %s" % (what, e))
            return False

    def _keepalive(self):
        ping = bytes([OP_PING]) + bytes(15)
        while not self._stop.wait(self._period):
            self._try(ping, "keep-alive")

    def __enter__(self):
        self._shown = self._try(self._cdb, "show")
        if self._shown and self._period is not None:
            self._thr = threading.Thread(target=self._keepalive, daemon=True)
            self._thr.start()
        return self

    def update(self, msg):
        """Switch the pop-up text while staying busy."""
        self._try(busy_cdb(msg), "update")

    def __exit__(self, *exc):
        self._stop.set()
        if self._thr is not None:
            self._thr.join(timeout=5)
        if self._shown:
            self._try(busy_cdb(None), "clear")
        return False
