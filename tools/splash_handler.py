"""tools/splash_handler.py: Replace boot logo, splash screen, and screensaver
slides in ScanNCut UPF firmware images.

The target images live in Group 0xC9 of the raw-deflate block in flash segment seg5
(flash address 0x8495328A, UPF payload offset 0x3A0000 + 0x35328A = 0x6F328A):
  Group 0xC9 entry 0: Vendor / Boot Logo (432x240)
  Group 0xC9 entry 1: Boot Splash Screen (432x240)
  Group 0xC9 entries 2-6: Screensaver slides 1 through 5 (432x240)

All images in the firmware UI use 8-bit indexed palette (depth=8, ctype=3).
This module automatically validates image dimensions (432x240) and quantizes
RGB/RGBA replacement images to 8-bit indexed palette if necessary.
"""
import io
import os
import re
import struct
import zlib
from typing import Dict, Optional, Tuple
from PIL import Image

# UPF and Flash Layout Constants
SEG5_PAYLOAD_OFFSET = 0x3A0000      # seg5 payload base in UPF
SEG5_FLASH_BASE     = 0x84600000      # seg5 base in flash
BLOCK_FLASH_ADDR    = 0x8495328A      # flash address of the compressed block header
BLOCK_SEG5_OFFSET   = BLOCK_FLASH_ADDR - SEG5_FLASH_BASE  # 0x35328A
BLOCK_PAYLOAD_OFFSET = SEG5_PAYLOAD_OFFSET + BLOCK_SEG5_OFFSET # 0x6F328A
SEG5_SIZE           = 0x500000        # 5 MB
MAX_BLOCK_SPAN      = SEG5_SIZE - BLOCK_SEG5_OFFSET  # 0x1ACD76 (1,756,534 bytes)

# Group 0xC9 screen resolution
EXPECTED_DIMS_0xC9 = (432, 240)

# Friendly aliases mapping to (group, index)
ALIASES = {
    # Entry 0: Boot / Vendor Logo
    "00_vendor_logo": (0xC9, 0),
    "vendor_logo":    (0xC9, 0),
    "boot_logo":      (0xC9, 0),
    "logo":           (0xC9, 0),
    # Entry 1: Splash Screen
    "01_splash":      (0xC9, 1),
    "splash":         (0xC9, 1),
    "boot_splash":    (0xC9, 1),
    "splash_screen":  (0xC9, 1),
    # Entries 2-6: Screensaver slides 1-5
    "02_screensaver_frame1": (0xC9, 2),
    "screensaver_1":         (0xC9, 2),
    "screensaver_frame1":    (0xC9, 2),
    "slide1":                (0xC9, 2),
    "slide_1":               (0xC9, 2),
    "03_screensaver_frame2": (0xC9, 3),
    "screensaver_2":         (0xC9, 3),
    "screensaver_frame2":    (0xC9, 3),
    "slide2":                (0xC9, 3),
    "slide_2":               (0xC9, 3),
    "04_screensaver_frame3": (0xC9, 4),
    "screensaver_3":         (0xC9, 4),
    "screensaver_frame3":    (0xC9, 4),
    "slide3":                (0xC9, 4),
    "slide_3":               (0xC9, 4),
    "05_screensaver_frame4": (0xC9, 5),
    "screensaver_4":         (0xC9, 5),
    "screensaver_frame4":    (0xC9, 5),
    "slide4":                (0xC9, 5),
    "slide_4":               (0xC9, 5),
    "06_screensaver_frame5": (0xC9, 6),
    "screensaver_5":         (0xC9, 6),
    "screensaver_frame5":    (0xC9, 6),
    "slide5":                (0xC9, 6),
    "slide_5":               (0xC9, 6),
}


def parse_target_key(filename: str) -> Optional[Tuple[int, int]]:
    """Determine (group, index) from a filename, supporting canonical names and aliases."""
    base, ext = os.path.splitext(os.path.basename(filename))
    if ext.lower() != ".png":
        return None

    # Check canonical pattern: gGGG_IIII (e.g. g0c9_0000_432x240.png or g0c9_0000.png)
    m = re.match(r"^g([0-9a-fA-F]+)_(\d+)", base)
    if m:
        grp = int(m.group(1), 16)
        idx = int(m.group(2))
        return (grp, idx)

    # Check friendly aliases (case-insensitive)
    lower_base = base.lower()
    for alias, target in ALIASES.items():
        if lower_base == alias or lower_base.startswith(alias + "_"):
            return target

    return None


def prepare_png_bytes(file_path: str, target: Tuple[int, int]) -> bytes:
    """Validate image dimensions and ensure PNG format is 8-bit indexed palette."""
    grp, idx = target
    with open(file_path, "rb") as f:
        raw = f.read()
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"'{file_path}' is not a valid PNG file")

    with Image.open(file_path) as im:
        w, h = im.size
        if grp == 0xC9 and (w, h) != EXPECTED_DIMS_0xC9:
            raise ValueError(
                f"Image '{file_path}' has dimensions {w}x{h}, expected {EXPECTED_DIMS_0xC9[0]}x{EXPECTED_DIMS_0xC9[1]} "
                f"for Group 0xC9 (boot/splash/screensaver)."
            )

        # Check existing PNG format: depth=8, color_type=3 (palette)
        # PNG IHDR chunk is at offset 12..29 (signature=8, chunk_len=4, 'IHDR'=4, data=13)
        depth = raw[24]
        ctype = raw[25]
        if depth == 8 and ctype == 3 and b"PLTE" in raw:
            return raw

        # Convert / quantize to 8-bit indexed mode P with palette
        if im.mode == "RGBA":
            # Quantize preserving transparency if present
            quantized = im.quantize(colors=256)
        elif im.mode == "RGB":
            quantized = im.quantize(colors=256)
        else:
            quantized = im.convert("RGBA").quantize(colors=256)

        out = io.BytesIO()
        quantized.save(out, format="PNG", optimize=True)
        return out.getvalue()


def scan_replacement_dir(directory: str) -> Dict[Tuple[int, int], Tuple[str, bytes]]:
    """Scan directory for replacement images and return mapping of (group, index) -> (filename, png_bytes)."""
    if not os.path.isdir(directory):
        return {}

    replacements = {}
    for entry in sorted(os.listdir(directory)):
        full_path = os.path.join(directory, entry)
        if not os.path.isfile(full_path):
            continue
        target = parse_target_key(entry)
        if target is None:
            continue

        png_bytes = prepare_png_bytes(full_path, target)
        replacements[target] = (entry, png_bytes)

    return replacements


def extract_uncompressed_block(payload: bytearray) -> Tuple[bytes, int, int]:
    """Extract and decompress the flash block from seg5 in the UPF payload."""
    hdr_size, usize, csize = struct.unpack_from(">III", payload, BLOCK_PAYLOAD_OFFSET)
    stream = payload[BLOCK_PAYLOAD_OFFSET + hdr_size : BLOCK_PAYLOAD_OFFSET + hdr_size + csize]
    data = zlib.decompressobj(-15).decompress(stream, usize)
    if len(data) != usize:
        raise RuntimeError(f"Deflate stream inflated to {len(data)} bytes, expected {usize}")
    return bytes(data), usize, csize


def repack_uncompressed_block(data: bytes, replacements: Dict[Tuple[int, int], bytes]) -> bytes:
    """Replace target images in the uncompressed block and rebuild group tables and descriptors."""
    count = struct.unpack_from("<H", data, 0x70)[0]
    pos = 0x72 + 8 * count

    groups = []
    for k in range(count):
        grp, n, size = struct.unpack_from("<HHI", data, 0x72 + 8 * k)
        groups.append({
            "grp": grp,
            "n": n,
            "pos": pos,
            "size": size,
            "data": data[pos : pos + size],
        })
        pos += size

    # Update groups that have replacements
    new_groups = []
    for g in groups:
        grp = g["grp"]
        n = g["n"]
        gdata = g["data"]
        group_replacements = {idx: png for (g_id, idx), png in replacements.items() if g_id == grp}

        if not group_replacements:
            new_groups.append(g)
            continue

        offs = [struct.unpack_from("<I", gdata, 4 * i)[0] for i in range(n)]
        records = []
        for i in range(n):
            rec = offs[i]
            png_sz = struct.unpack_from("<I", gdata, rec)[0]
            gap = gdata[rec + 4 : rec + 12]
            png = gdata[rec + 12 : rec + 12 + png_sz]

            if i in group_replacements:
                png = group_replacements[i]
                png_sz = len(png)

            pad_len = (4 - ((12 + png_sz) % 4)) % 4
            pad = b"\x00" * pad_len
            rec_bytes = struct.pack("<I", png_sz) + gap + png + pad
            records.append(rec_bytes)

        # Recompute offset table
        cur_off = 4 * n
        new_offs = []
        for r in records:
            new_offs.append(cur_off)
            cur_off += len(r)

        new_table = b"".join(struct.pack("<I", o) for o in new_offs)
        new_gdata = new_table + b"".join(records)
        new_groups.append({
            "grp": grp,
            "n": n,
            "size": len(new_gdata),
            "data": new_gdata,
        })

    # Assemble new uncompressed block
    out_hdr = bytearray(data[:0x72])
    out_desc = bytearray()
    for g in new_groups:
        out_desc += struct.pack("<HHI", g["grp"], g["n"], g["size"])
    out_body = b"".join(g["data"] for g in new_groups)

    return bytes(out_hdr + out_desc + out_body)


def apply_splash_replacements(u, replacement_dir: str = "replacement") -> Optional[dict]:
    """Inspect replacement_dir and patch the raw-deflate block in seg5 of the Upf object.
    
    Returns a dictionary of summary statistics if replacements were applied, or None if none found.
    """
    found = scan_replacement_dir(replacement_dir)
    if not found:
        return None

    uncomp, old_usize, old_csize = extract_uncompressed_block(u.payload)
    replacements_data = {target: png_bytes for target, (fn, png_bytes) in found.items()}

    new_uncomp = repack_uncompressed_block(uncomp, replacements_data)

    # Recompress with raw DEFLATE (level 9, windowBits -15)
    co = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-15)
    new_stream = co.compress(new_uncomp) + co.flush()

    new_usize = len(new_uncomp)
    new_csize = len(new_stream)
    new_hdr = struct.pack(">III", 0xC, new_usize, new_csize)
    new_blob = new_hdr + new_stream

    if len(new_blob) > MAX_BLOCK_SPAN:
        raise RuntimeError(
            f"Recompressed block size {len(new_blob)} exceeds available seg5 space {MAX_BLOCK_SPAN}"
        )

    # Write new block into seg5 in UPF payload
    orig_total_span = 0xC + old_csize
    u.payload[BLOCK_PAYLOAD_OFFSET : BLOCK_PAYLOAD_OFFSET + len(new_blob)] = new_blob

    # Pad leftover space with erased flash (0xFF)
    if len(new_blob) < orig_total_span:
        pad_len = orig_total_span - len(new_blob)
        u.payload[BLOCK_PAYLOAD_OFFSET + len(new_blob) : BLOCK_PAYLOAD_OFFSET + orig_total_span] = b"\xFF" * pad_len

    return {
        "count": len(found),
        "items": found,
        "old_usize": old_usize,
        "new_usize": new_usize,
        "old_csize": old_csize,
        "new_csize": new_csize,
    }


if __name__ == "__main__":
    import argparse
    from tools.upf import Upf

    parser = argparse.ArgumentParser(description="ScanNCut PNG replacement tool")
    parser.add_argument("--in", dest="in_file", help="Input UPF firmware file")
    parser.add_argument("--out", dest="out_file", help="Output UPF firmware file")
    parser.add_argument("--dir", dest="rep_dir", default="replacement", help="Folder containing replacement PNGs")
    parser.add_argument("--inspect", action="store_true", help="Inspect and dump stats of input UPF block")
    args = parser.parse_args()

    if args.inspect:
        if not args.in_file:
            print("Error: --in <file.upf> required for --inspect")
            exit(1)
        u = Upf(args.in_file)
        _, u_sz, c_sz = extract_uncompressed_block(u.payload)
        print(f"{args.in_file}: uncompressed={u_sz} bytes, compressed={c_sz} bytes")
    elif args.in_file and args.out_file:
        u = Upf(args.in_file)
        res = apply_splash_replacements(u, args.rep_dir)
        if res:
            print(f"Applied {res['count']} replacement(s):")
            for (grp, idx), (fn, data) in res["items"].items():
                print(f"  Group 0x{grp:03X} Index {idx}: '{fn}' ({len(data)} bytes)")
            print(f"Compressed size: {res['old_csize']} -> {res['new_csize']} bytes")
        else:
            print(f"No replacements found in '{args.rep_dir}'. Outputting unmodified image.")
        u.save(args.out_file)
        print(f"Saved: {args.out_file}")
    else:
        parser.print_help()
