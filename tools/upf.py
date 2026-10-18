"""CM110 .upf container: 0x100-byte header + payload (= mem image @0x84060000).
Header[0:4] = little-endian sum(file[4:]) checksum.  Boot loader descriptor
table at payload 0x708 lists LZSS segments {dst,src,clen} (big-endian)."""
import struct
from tools import lzss  # when run as module (fallback below)
MEMBASE=0x84060000
HDR=0x100
DESC_TABLES=[0x708, 0x140708]   # updater (seg0) + main-fw (seg4) LZSS tables
DESC_SPAN=0x84          # 0x708..0x78c
# Updater bank 1 (0x84100000) decompresses to the SAME RAM addresses as bank 0,
# so it is never searched by default: pass table=TABLE_UPD1 to patch it.
TABLE_UPD0, TABLE_UPD1, TABLE_MAIN = 0x708, 0xA0708, 0x140708

class Upf:
    def __init__(self, path):
        raw=open(path,"rb").read()
        self.header=bytearray(raw[:HDR])
        self.payload=bytearray(raw[HDR:])
    # ---- descriptor table ----
    def descriptors(self, table=None):
        out=[]
        for base in (DESC_TABLES if table is None else [table]):
            for off in range(base, base+DESC_SPAN, 12):
                dst,src,clen=struct.unpack(">III", self.payload[off:off+12])
                out.append((off,dst,src,clen))
        return out
    def _seg_for_mem(self, mem, table=None):
        """return (desc_off, dst, src, clen, dlen) of the segment whose
           decompressed image contains `mem` (only in `table` if given)."""
        for off,dst,src,clen in self.descriptors(table):
            if clen==0: continue
            poff=src-MEMBASE
            dec=lzss.decode(self.payload[poff:poff+clen])
            if dst<=mem<dst+len(dec):
                return off,dst,src,clen,len(dec),dec
        raise KeyError("no segment for %08x"%mem)
    # ---- patching ----
    def get_mem(self, mem, n, table=None):
        off,dst,src,clen,dlen,dec=self._seg_for_mem(mem, table)
        o=mem-dst; return bytes(dec[o:o+n])
    def patch_mem(self, mem, newbytes, table=None):
        off,dst,src,clen,dlen,dec=self._seg_for_mem(mem, table)
        o=mem-dst
        dec=bytearray(dec); dec[o:o+len(newbytes)]=newbytes
        comp=lzss.encode(bytes(dec))
        if len(comp)>clen:
            comp=lzss.encode_lazy(bytes(dec))   # better ratio when greedy overflows
        poff=src-MEMBASE
        if len(comp)<=clen:
            self.payload[poff:poff+len(comp)]=comp
            # update descriptor clen
            struct.pack_into(">I", self.payload, off+8, len(comp))
            return ("in-place", clen, len(comp))
        else:
            raise RuntimeError("recompressed %#x > original clen %#x at seg %08x, relocation needed"%(len(comp),clen,dst))
    def patch_mem_batch(self, patches, table=None):
        """Apply multiple (mem, newbytes) patches, recompressing each affected segment once."""
        segs = {}
        for mem, newbytes in patches:
            off, dst, src, clen, dlen, dec = self._seg_for_mem(mem, table)
            if off not in segs:
                segs[off] = [dst, src, clen, bytearray(dec)]
            o = mem - dst
            segs[off][3][o:o+len(newbytes)] = newbytes
        results = []
        for off, (dst, src, clen, dec) in segs.items():
            comp = lzss.encode(bytes(dec))
            if len(comp) > clen:
                comp = lzss.encode_lazy(bytes(dec))
            poff = src - MEMBASE
            if len(comp) <= clen:
                self.payload[poff:poff+len(comp)] = comp
                struct.pack_into(">I", self.payload, off+8, len(comp))
                results.append(("in-place", clen, len(comp)))
            else:
                raise RuntimeError("recompressed %#x > original clen %#x at seg %08x, relocation needed"%(len(comp),clen,dst))
        return results
    def patch_file(self, file_off, newbytes):
        """raw byte patch at .upf file offset (header or uncompressed payload)."""
        if file_off<HDR:
            self.header[file_off:file_off+len(newbytes)]=newbytes
        else:
            p=file_off-HDR; self.payload[p:p+len(newbytes)]=newbytes
    # ---- save ----
    def bytes_with_checksum(self):
        buf=bytearray(self.header)+self.payload
        s=sum(buf[4:]) & 0xFFFFFFFF
        struct.pack_into("<I", buf, 0, s)
        return bytes(buf)
    def save(self, path):
        data=self.bytes_with_checksum(); open(path,"wb").write(data)
        return len(data), struct.unpack("<I",data[:4])[0]

if __name__=="__main__":
    import sys
    # self-test: load stock, verify checksum, round-trip repack, verify segments
    u=Upf("SNC0001V1819W.upf")
    orig=open("SNC0001V1819W.upf","rb").read()
    # verify stored checksum matches recompute
    calc=sum(orig[4:])&0xFFFFFFFF
    print("stock checksum stored=%08x recompute=%08x match=%s"%(
        struct.unpack("<I",orig[:4])[0], calc, struct.pack("<I",calc)==orig[:4]))
    print("descriptors:")
    for off,dst,src,clen in u.descriptors():
        print("  @payload+%#05x dst=%08x src=%08x clen=%#x"%(off,dst,src,clen))
    # resave unmodified -> must be byte-identical
    out=u.bytes_with_checksum()
    print("resave byte-identical to stock:", out==orig)
