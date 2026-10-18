"""apply_patch.py: build the patched CM110 firmware from YOUR stock .upf plus a
patch made by tools.make_patch.  Nothing from the vendor firmware is shipped.

    python -m tools.apply_patch SNC0001V1819W.upf -o SNC0001V1819W_vendor.upf
    python -m tools.apply_patch SNC0001V1819W.upf -o out.upf --splash-dir replacement

The stock file is identified by SHA-256, and every hunk also checks the SHA-256 of the
bytes it replaces, so a different firmware version fails instead of being corrupted.
"""
import argparse, hashlib, json, os, sys
from tools import lzss
from tools.upf import Upf, MEMBASE

DEFAULT_PATCH = os.path.join("patches", "cm110_v1819w.sncpatch.json")


class PatchError(RuntimeError):
    pass


def _sha(b):
    return hashlib.sha256(bytes(b)).hexdigest()


def find_target(patch, stock_bytes):
    h = _sha(stock_bytes)
    for t in patch["targets"]:
        if t["stock_sha256"] == h:
            return t
    raise PatchError("stock firmware not recognised (sha256 %s). Known: %s"
                     % (h, ", ".join("%s=%s..." % (t["name"], t["stock_sha256"][:12]) for t in patch["targets"])))


def _verify_orig(u, hunks):
    segs = {}
    def seg(table, mem):
        if table not in segs:
            segs[table] = []
            for _o, dst, src, clen in u.descriptors(table):
                if clen:
                    segs[table].append((dst, lzss.decode(u.payload[src - MEMBASE:src - MEMBASE + clen])))
        for dst, dec in segs[table]:
            if dst <= mem < dst + len(dec):
                return dst, dec
        raise PatchError("no segment for %#x in table %#x" % (mem, table))
    for h in hunks:
        n = len(h["new"]) // 2
        if h["kind"] == "mem":
            dst, dec = seg(h["table"], h["addr"])
            cur = dec[h["addr"] - dst:h["addr"] - dst + n]
            where = "%#x" % h["addr"]
        else:
            cur = u.payload[h["file_offset"] - 0x100:h["file_offset"] - 0x100 + n]
            where = "file+%#x" % h["file_offset"]
        if _sha(cur) != h["orig_sha256"]:
            raise PatchError("original bytes at %s do not match the patch (wrong firmware?)" % where)


def apply_to_upf(u, target):
    hunks = target["hunks"]
    _verify_orig(u, hunks)
    by_table = {}
    for h in hunks:
        if h["kind"] == "mem":
            by_table.setdefault(h["table"], []).append((h["addr"], bytes.fromhex(h["new"])))
    for table, patches in by_table.items():
        u.patch_mem_batch(patches, table=table)
    for h in hunks:
        if h["kind"] == "raw":
            u.patch_file(h["file_offset"], bytes.fromhex(h["new"]))


def apply_to_bytes(stock_bytes, target):
    """Pure in-memory apply (no splash). Returns the patched file bytes."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "stock.upf")
        open(p, "wb").write(stock_bytes)
        u = Upf(p)
    apply_to_upf(u, target)
    return u.bytes_with_checksum()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stock", help="your original firmware .upf")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--patch", default=DEFAULT_PATCH)
    ap.add_argument("--splash-dir", help="folder of replacement PNGs (boot logo / splash / screensaver)")
    a = ap.parse_args(argv)
    patch = json.load(open(a.patch))
    if patch.get("format") != 1:
        raise PatchError("unsupported patch format")
    stock = open(a.stock, "rb").read()
    target = find_target(patch, stock)
    u = Upf(a.stock)
    apply_to_upf(u, target)
    data = u.bytes_with_checksum()
    if _sha(data) != target["output_sha256"]:
        raise PatchError("patched image hash mismatch (LZSS encoder differs from the one the patch was made with)")
    print("patched %s: %d hunks applied, output sha256 verified" % (target["name"], len(target["hunks"])))
    if a.splash_dir:
        from tools.splash_handler import apply_splash_replacements
        info = apply_splash_replacements(u, a.splash_dir)
        print("splash: %s" % ("replaced %d image(s)" % info["count"] if info else "no images found in " + a.splash_dir))
    n, ck = u.save(a.out)
    print("wrote %s (%d bytes, checksum %08x)" % (a.out, n, ck))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PatchError as e:
        print("error:", e); sys.exit(1)
