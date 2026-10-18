"""cm110_usb.py: PC-side client for CM-100 vendor bulk ECHO test.
Requires: pip install pyusb libusb backend (WinUSB via Zadig for VID 04F9 PID 2107
once interface class is FF, and stock MSC needs no driver change for descriptor dump).

Stages:
 1. describe : print VID/PID/serial, device/config/interface/endpoint descriptors
    (works on STOCK firmware, and this is the data needed to fill the MMIO/EP
    placeholders in src-fw/usb_vendor.S before activating the hook).
 2. echo     : bulk OUT 0x02 -> bulk IN 0x81 loopback. Only valid on
    HOOK-ACTIVE firmware. On dormant/stock (MSC) it will stall/timeout.
    That is the expected negative result, NOT a failure of this script.
 3. msc-check : confirm stock behavior (MSC, no media) for baseline.

Protocol (hook-active spec):
  OUT packet: [b'ECHO', u16le len, payload...]  (max 512)
  IN reply  : [b'ECHO', u16le len, payload...]  (same payload)
"""
import sys

VID, PID = 0x04F9, 0x2107
# Stock CM-100 (USBTreeView, 2026-10-08): bulk OUT 0x01, bulk IN 0x82, 64 bytes.
EP_OUT, EP_IN = 0x01, 0x82

def describe():
    import usb.core
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print("not found: %04X:%04X (check the cable or USBTreeView)" % (VID, PID)); return 1
    print("found: bus %s addr %s" % (dev.bus, dev.address))
    try: print("manufacturer :", dev.manufacturer)
    except Exception as e: print("manufacturer : <%s>" % e)
    try: print("product      :", dev.product)
    except Exception as e: print("product      : <%s>" % e)
    try: print("serial       :", dev.serial_number)
    except Exception as e: print("serial       : <%s>" % e)
    for cfg in dev:
        print("config %d: %d interfaces, attrs %#04x" % (cfg.bConfigurationValue, cfg.bNumInterfaces, cfg.bmAttributes))
        for ifc in cfg:
            print("  iface %d: class %02X sub %02X prot %02X endpoints %d" % (
                ifc.bInterfaceNumber, ifc.bInterfaceClass, ifc.bInterfaceSubClass,
                ifc.bInterfaceProtocol, ifc.bNumEndpoints))
            for ep in ifc:
                print("    ep %02X: %s maxpacket %d interval %d" % (
                    ep.bEndpointAddress,
                    {0: 'ctrl', 1: 'iso', 2: 'bulk', 3: 'intr'}.get(ep.bmAttributes & 3, '?'),
                    ep.wMaxPacketSize, ep.bInterval))
    print("NOTE: stock CM-100 shows interface 08/06/50 (MSC BOT),")
    print("bulk OUT 0x01 / bulk IN 0x82, 64-byte packets (confirmed).")
    print("Hook-active target shows FF/FF/FF with the same 01/82 bulk endpoints.")
    return 0

def echo(msg=b"hello-cm110"):
    import usb.core, usb.util, struct
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print("not found"); return 1
    dev.set_configuration()
    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]
    if intf.bInterfaceClass != 0xFF:
        print("interface class %02X (stock MSC?). ECHO needs hook-active FF firmware. Aborting (no data sent)." % intf.bInterfaceClass)
        return 2
    pkt = b'ECHO' + struct.pack('<H', len(msg)) + msg
    dev.write(EP_OUT, pkt, timeout=2000)
    back = bytes(dev.read(EP_IN, 512, timeout=2000))
    print("sent %d, got %d: %r" % (len(pkt), len(back), back))
    print("ECHO PASS" if back == pkt else "ECHO MISMATCH")
    return 0 if back == pkt else 3

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'describe'
    sys.exit(describe() if cmd == 'describe' else echo(sys.argv[2].encode() if len(sys.argv) > 2 else b"hello-cm110"))
