"""buttons.py: read the machine's front-panel buttons over USB.

Requires firmware built with tools/busy_handler.py (button hook + opcode 0xC7).

The firmware records every debounced press/release of the 5 front-panel
switches into a block in the cave's reserved RAM. It samples them at the same scan the
machine's own UI uses, so every press the machine would register is recorded. The block holds
  current pressed mask, per-key press counters, and a 16-entry event ring
  (tick, sequence number, key, pressed/released).
The host reads that block with the ordinary 0xC0 memory read and remembers the
last sequence number it consumed. Reads are non-destructive, so a slow or
retried poll never loses a press. If more than 16 events happen between polls,
the oldest events drop out of the ring but the per-key counters stay exact.

Keys (index = bit): 0 POWER, 3 START (Start/Stop, "play/pause") are known from
the firmware. 1, 2 and 4 are the remaining switches. Run
`python buttons.py watch` and press them to label them.

Withholding (0xC7): keys in the mask read as "not pressed" to the machine's
own UI while the host keeps communicating with it (any vendor command within 15 s),
but they are still recorded for the host. Power can never be withheld. While the
busy pop-up (busy.py) is up, all keys except Power are already kept from the
UI, and input made during the pop-up is discarded rather than replayed.
"""
import struct
import time

OP_MEMREAD, OP_BUTTONS = 0xC0, 0xC7
STATE_BASE = 0x89499628
BLOCK_OFF, BLOCK_LEN = 0x240, 0xB0          # canary .. end of ring
BLOCK_ADDR = STATE_BASE + BLOCK_OFF
BTN_MAGIC = 0xB7B70001
RING_N = 16
TICK_HZ = 500                               # firmware tick (2 ms)

POWER, START = 0, 3
NAMES = {0: "power", 1: "key1", 2: "key2", 3: "start", 4: "key4"}
KEY_COUNT = 5


def _cbw(tag, cdb, n, data_in):
    return (b"USBC" + struct.pack("<II", tag, n)
            + bytes([0x80 if data_in else 0x00, 0, len(cdb)]) + cdb
            + bytes(16 - len(cdb)))


def read_block_raw(dev, ep_out=0x01, ep_in=0x82):
    """0xC0 read of the button block on a raw pyusb handle."""
    a, n = BLOCK_ADDR, BLOCK_LEN
    cdb = bytes([OP_MEMREAD, 0, a >> 24 & 0xFF, a >> 16 & 0xFF, a >> 8 & 0xFF,
                 a & 0xFF, 0, n >> 8, n & 0xFF])
    tag = 0x42544E52
    dev.write(ep_out, _cbw(tag, cdb, n, True), timeout=3000)
    data = bytes(dev.read(ep_in, n, timeout=3000))
    csw = bytes(dev.read(ep_in, 13, timeout=3000))
    if len(data) != n or csw[:4] != b"USBS" or csw[12] != 0:
        raise IOError("button block read failed")
    return data


def withhold_cdb(mask):
    return bytes([OP_BUTTONS, 0x00, mask & 0x1E]) + bytes(13)


class State(object):
    __slots__ = ("mask", "seq", "tick", "counts", "ring")

    def pressed(self, key):
        return bool(self.mask >> key & 1)


def parse(block):
    u = lambda o: struct.unpack_from(">I", block, o)[0]
    if u(0) != BTN_MAGIC:
        raise IOError("button block not initialized (canary %08x): firmware "
                      "without the button hook?" % u(0))
    s = State()
    s.mask, s.seq, s.tick = u(4), u(8), u(12)
    s.counts = [u(0x10 + 4 * k) for k in range(KEY_COUNT)]
    s.ring = [struct.unpack_from(">IHBB", block, 0x30 + 8 * i) for i in range(RING_N)]
    return s


class Buttons(object):
    """Poll the front panel. `read` returns the raw block, e.g.
    lambda: read_block_raw(dev), or a C4Client's memread.

        b = Buttons(lambda: read_block_raw(dev))
        ...
        for key, pressed, tick in b.poll(): ...
        if b.pressed_since(START): ...
    """

    def __init__(self, read):
        self._read = read
        st = parse(self._read())
        self._seq = st.seq                  # only events after construction
        self._counts = list(st.counts)
        self.state = st
        self.lost = 0                       # events that fell out of the ring

    def poll(self):
        """New events since the last poll: [(key, pressed, tick), ...] oldest
        first. Also refreshes .state."""
        st = parse(self._read())
        self.state = st
        new = (st.seq - self._seq) & 0xFFFFFFFF
        if new > 0x7FFFFFFF:               # counter went backwards: re-init
            self._seq, self._counts = st.seq, list(st.counts)
            return []
        first = st.seq - min(new, RING_N)
        self.lost += new - min(new, RING_N)
        out = []
        for s in range(first, st.seq):
            tick, seq16, key, pressed = st.ring[s % RING_N]
            if seq16 != s & 0xFFFF:         # overwritten during the read: skip
                self.lost += 1
                continue
            out.append((key, bool(pressed), tick))
        self._seq = st.seq
        return out

    def pressed_since(self, key):
        """Number of presses of `key` since the last call for that key (exact,
        independent of the ring). Updates .state."""
        st = parse(self._read())
        self.state = st
        n = (st.counts[key] - self._counts[key]) & 0xFFFFFFFF
        self._counts[key] = st.counts[key]
        return n

    def wait_press(self, key, timeout=None, interval=0.1, cancel=None):
        """Block until `key` is pressed (True) or timeout / cancel() (False).
        Each poll is a vendor command, so it also keeps a busy pop-up alive."""
        self.pressed_since(key)
        t_end = None if timeout is None else time.time() + timeout
        while t_end is None or time.time() < t_end:
            if cancel is not None and cancel():
                return False
            time.sleep(interval)
            if self.pressed_since(key):
                return True
        return False


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("watch", "withhold"):
        print("usage: buttons.py watch | withhold <mask>")
        print("  watch: print presses/releases live (Ctrl+C to stop)")
        print("  withhold MASK: hide keys (bit k = key k) from the machine UI")
        return 1
    import scsipt
    try:
        dev = scsipt.open_machine(0x04F9, 0x2107)
    except scsipt.OpenFailed as f:
        print("device not reachable: %s%s" % (f, " (run as administrator)" if f.elevation else ""))
        return 2
    if argv[0] == "withhold":
        import busy
        st = busy.send_raw(dev, withhold_cdb(int(argv[1], 0)))
        print("withhold %s -> CSW %d" % (argv[1], st))
        return 0 if st == 0 else 3
    b = Buttons(lambda: read_block_raw(dev))
    print("pressed now: %s   (press buttons, Ctrl+C to stop)"
          % [NAMES[k] for k in range(KEY_COUNT) if b.state.pressed(k)])
    try:
        while True:
            for key, pressed, tick in b.poll():
                print("%9.3f s  %-6s %s" % (tick / float(TICK_HZ), NAMES[key],
                                            "pressed" if pressed else "released"))
            if b.lost:
                print("  (%d events lost: poll faster)" % b.lost)
                b.lost = 0
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("counts:", dict((NAMES[k], c) for k, c in enumerate(b.state.counts)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
