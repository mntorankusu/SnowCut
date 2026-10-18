"""PDF page -> its separate elements (Break Apart of a PDF object).  No Qt in here.

``extract_page(path, page)`` walks a page's content with pdfium (pypdfium2) and returns every
element in the page's display frame: mm, origin at the top-left of the rendered page, y down --
the same frame ``raster.render_pdf`` draws, so the elements land exactly on the PDF object's raster.

- path objects -> one VectorElement each (all its subpaths, real cubic beziers)
- text -> one VectorElement per line of text (glyph outlines from the embedded fonts)
- image objects -> ImageElement (RGBA as drawn on the page: matrix, flips and masks applied).

Text whose glyph outlines pdfium cannot give (e.g. Type3 fonts) comes out as an image of that
text object instead.  Not extracted: shading (gradient) objects and invisible text (OCR layers).
Clipping is ignored: every element comes out whole.
"""
from __future__ import annotations
import ctypes
from dataclasses import dataclass, field

from .doc import DEFAULT_THICKNESS
from .svg_import import _Builder, mat_mul, xform_pt

MM_PER_PT = 25.4 / 72.0
_CAL = 64          # FPDF_PageToDevice works in ints: calibrate at 1/64 pt
_LINE, _BEZIER, _MOVE = 0, 1, 2
_INVISIBLE_TEXT = (3, 7)      # render modes "invisible" and "clip only"
_STROKE_TEXT = (1, 5)
MAX_IMAGE_PX = 8000           # longest side of an extracted image
MIN_RESAMPLE_DPI = 150.0      # rotated / skewed images are resampled at least this fine
TEXT_RASTER_DPI = 300.0       # text that has no outlines comes out as an image at this dpi


@dataclass
class VectorElement:
    kind: str                       # "shape" | "text"
    label: str
    # [(anchors, closed, color)], anchors [[x, y, hin, hout]] in page mm (svg_import layout)
    paths: list = field(default_factory=list)
    thickness_mm: float = DEFAULT_THICKNESS
    dash: object = None             # (on_mm, off_mm) or None
    order: int = 0                  # paint order on the page


@dataclass
class ImageElement:
    label: str
    rgba: object                    # uint8 HxWx4, display orientation
    rect: tuple                     # (x0, y0, x1, y1) page mm
    order: int = 0
    kind: str = "image"


@dataclass
class PageElements:
    width_mm: float
    height_mm: float
    elements: list                  # VectorElement | ImageElement, in paint order
    skipped: int = 0                # shading objects etc. that were left out


def _addr(h):
    return ctypes.cast(h, ctypes.c_void_p).value


def _matrix(R, obj):
    m = R.FS_MATRIX()
    if not R.FPDFPageObj_GetMatrix(obj, m):
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    return (m.a, m.b, m.c, m.d, m.e, m.f)


def _display_matrix(R, page, w_pt, h_pt):
    """Affine page space -> display pt (rotation and crop box applied, y down), derived from
    FPDF_PageToDevice so it matches pdfium's own rendering exactly."""
    sx, sy = int(round(w_pt * _CAL)), int(round(h_pt * _CAL))

    def dev(x, y):
        dx, dy = ctypes.c_int(), ctypes.c_int()
        R.FPDF_PageToDevice(page, 0, 0, sx, sy, 0, x, y, dx, dy)
        return dx.value / _CAL, dy.value / _CAL
    o, ex, ey = dev(0.0, 0.0), dev(1000.0, 0.0), dev(0.0, 1000.0)
    return ((ex[0] - o[0]) / 1000.0, (ex[1] - o[1]) / 1000.0,
            (ey[0] - o[0]) / 1000.0, (ey[1] - o[1]) / 1000.0, o[0], o[1])


def _linear_scale(M):
    return abs(M[0] * M[3] - M[1] * M[2]) ** 0.5


def _color(R, obj, stroke):
    r, g, b, a = (ctypes.c_uint() for _ in range(4))
    fn = R.FPDFPageObj_GetStrokeColor if stroke else R.FPDFPageObj_GetFillColor
    if not fn(obj, r, g, b, a):
        return "#000000", 255
    return "#%02X%02X%02X" % (r.value & 255, g.value & 255, b.value & 255), a.value


def _seg_point(R, seg):
    x, y = ctypes.c_float(), ctypes.c_float()
    R.FPDFPathSegment_GetPoint(seg, x, y)
    return x.value, y.value


def _subpaths(R, count, get_seg, T):
    """pdfium path segments -> [(anchors, closed)] with every point mapped through T."""
    b = _Builder()
    i = 0
    while i < count:
        seg = get_seg(i)
        kind = R.FPDFPathSegment_GetType(seg)
        p = xform_pt(T, *_seg_point(R, seg))
        last = seg
        if kind == _MOVE:
            b.move_to(*p)
        elif kind == _BEZIER and i + 2 < count:
            c2 = xform_pt(T, *_seg_point(R, get_seg(i + 1)))
            last = get_seg(i + 2)
            p3 = xform_pt(T, *_seg_point(R, last))
            b.cubic_to(p, c2, p3)
            i += 2
        else:
            b.line_to(*p)
        if R.FPDFPathSegment_GetClose(last):
            b.close()
        i += 1
    return [(a, c) for a, c in b.finish() if len(a) >= 2]


def _bitmap_rgba(R, bmp):
    """FPDF_BITMAP -> owned uint8 HxWx4 RGBA array."""
    import numpy as np
    w, h = R.FPDFBitmap_GetWidth(bmp), R.FPDFBitmap_GetHeight(bmp)
    stride, fmt = R.FPDFBitmap_GetStride(bmp), R.FPDFBitmap_GetFormat(bmp)
    buf = R.FPDFBitmap_GetBuffer(bmp)
    raw = np.ctypeslib.as_array(ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)), shape=(h * stride,))
    rows = raw.reshape(h, stride)
    ch = {R.FPDFBitmap_Gray: 1, R.FPDFBitmap_BGR: 3, R.FPDFBitmap_BGRx: 4, R.FPDFBitmap_BGRA: 4}.get(fmt, 4)
    px = rows[:, :w * ch].reshape(h, w, ch)
    out = np.empty((h, w, 4), np.uint8)
    if ch == 1:
        out[..., 0] = out[..., 1] = out[..., 2] = px[..., 0]
    else:
        out[..., 0], out[..., 1], out[..., 2] = px[..., 2], px[..., 1], px[..., 0]
    out[..., 3] = px[..., 3] if fmt == R.FPDFBitmap_BGRA else 255
    return out


def _place(rgba, F, max_px=MAX_IMAGE_PX):
    """Put a bitmap where F maps it: F takes the unit square (x right, y up, the bitmap's top row
    at y = 1) to display mm.  Returns (rgba in display orientation, (x0, y0, x1, y1) mm).
    Axis-aligned placements only flip / transpose (lossless). Rotated or skewed ones are
    resampled into their bounding box with transparent corners."""
    import numpy as np
    h, w = rgba.shape[:2]
    a, b, c, d, e, f = F
    corners = [xform_pt(F, x, y) for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
    xs, ys = zip(*corners)
    rect = (min(xs), min(ys), max(xs), max(ys))
    u, v = (a, b), (-c, -d)             # display direction of the columns / rows (whole width)
    lu, lv = (a * a + b * b) ** 0.5, (c * c + d * d) ** 0.5
    if lu <= 0 or lv <= 0:
        return None
    eps = 1e-6
    if abs(u[1]) <= eps * lu and abs(v[0]) <= eps * lv:
        out = rgba[:, ::-1] if u[0] < 0 else rgba
        out = out[::-1] if v[1] < 0 else out
        return np.ascontiguousarray(out), rect
    if abs(u[0]) <= eps * lu and abs(v[1]) <= eps * lv:
        out = rgba.transpose(1, 0, 2)
        out = out[::-1] if u[1] < 0 else out
        out = out[:, ::-1] if v[0] < 0 else out
        return np.ascontiguousarray(out), rect
    from PIL import Image
    det = a * d - b * c
    if abs(det) < 1e-12:
        return None
    native = max(w / lu, h / lv)
    s = max(native, MIN_RESAMPLE_DPI / 25.4)     # output px per mm: no coarser than the source
    bw, bh = rect[2] - rect[0], rect[3] - rect[1]
    s = min(s, max_px / max(bw, bh, 1e-9))
    W, H = max(1, int(round(bw * s))), max(1, int(round(bh * s)))
    dx, dy = rect[0] - e, rect[1] - f
    # output pixel (X, Y) -> display mm -> unit square (F^-1) -> source pixel
    coeffs = (w * d / (det * s), -w * c / (det * s), w * (d * dx - c * dy) / det,
              h * b / (det * s), -h * a / (det * s), h - h * (a * dy - b * dx) / det)
    im = Image.fromarray(np.ascontiguousarray(rgba), "RGBA").convert("RGBa")
    # enlarging: whole source pixels, as pdfium draws images without /Interpolate
    flt = Image.NEAREST if s > native * 1.01 else Image.BICUBIC
    im = im.transform((W, H), Image.AFFINE, coeffs, resample=flt).convert("RGBA")
    return np.asarray(im).copy(), rect


def _image_pixels(R, pdf, page, obj, max_px=MAX_IMAGE_PX):
    """An image object's pixels at its own resolution (decoded, masks applied), RGBA, top row
    first, or None if pdfium cannot decode it.  The object's matrix is swapped for a pixel-sized
    one while rendering (pdfium renders at matrix size) and restored after."""
    pw, ph = ctypes.c_uint(), ctypes.c_uint()
    if not R.FPDFImageObj_GetImagePixelSize(obj, pw, ph) or not pw.value or not ph.value:
        return None
    k = min(1.0, max_px / max(pw.value, ph.value))
    w, h = max(1, int(pw.value * k)), max(1, int(ph.value * k))
    old = R.FS_MATRIX()
    R.FPDFPageObj_GetMatrix(obj, old)
    R.FPDFPageObj_SetMatrix(obj, R.FS_MATRIX(float(w), 0.0, 0.0, float(h), 0.0, 0.0))
    try:
        bmp = R.FPDFImageObj_GetRenderedBitmap(pdf, page, obj)
    finally:
        R.FPDFPageObj_SetMatrix(obj, old)
    if not bmp:
        bmp = R.FPDFImageObj_GetBitmap(obj)      # undecorated pixels (no mask)
    if not bmp:
        return None
    try:
        return _bitmap_rgba(R, bmp)
    finally:
        R.FPDFBitmap_Destroy(bmp)


def _text_raster(R, pdf, page, obj, M, T_disp):
    """(rgba, rect) of a text object drawn as an image (glyphs without outlines), or None."""
    bmp = R.FPDFTextObj_GetRenderedBitmap(pdf, page, obj, TEXT_RASTER_DPI / 72.0)
    if not bmp:
        return None
    try:
        rgba = _bitmap_rgba(R, bmp)
    finally:
        R.FPDFBitmap_Destroy(bmp)
    l, b, r, t = (ctypes.c_float() for _ in range(4))
    if not R.FPDFPageObj_GetBounds(obj, l, b, r, t) or rgba.size == 0:
        return None
    F = mat_mul(T_disp, mat_mul(M, (r.value - l.value, 0.0, 0.0, t.value - b.value, l.value, b.value)))
    return _place(rgba, F)


def _glyph_subpaths(R, font, code, size, L, origin, T_disp):
    """Outline of one glyph: em units * size -> char matrix (linear L) at origin (page space)
    -> display mm (T_disp).  None when pdfium has no outline for it."""
    gp = R.FPDFFont_GetGlyphPath(font, code, size)
    if not gp:
        return None
    n = R.FPDFGlyphPath_CountGlyphSegments(gp)
    if n <= 0:
        return None
    G = mat_mul(T_disp, (L[0] * size, L[1] * size, L[2] * size, L[3] * size, origin[0], origin[1]))
    return _subpaths(R, n, lambda k: R.FPDFGlyphPath_GetGlyphPathSegment(gp, k), G)


def _text_size(R, obj):
    sz = ctypes.c_float()
    return sz.value if R.FPDFTextObj_GetFontSize(obj, sz) else 0.0


def extract_page(path, page_index=0):
    """All elements of one page (see the module docstring).  Raises RuntimeError without
    pypdfium2, IndexError for a bad page."""
    from .raster import _pdfium
    pdfium = _pdfium()
    import pypdfium2.raw as R
    pdf = pdfium.PdfDocument(path)
    try:
        n = len(pdf)
        if not 0 <= page_index < n:
            raise IndexError(f"PDF page {page_index} out of range (0..{n - 1})")
        page = pdf[page_index]
        try:
            return _extract(R, pdf, page)
        finally:
            page.close()
    finally:
        pdf.close()


def _extract(R, pdf, page):
    w_pt, h_pt = page.get_size()
    D = _display_matrix(R, page, w_pt, h_pt)
    K = (MM_PER_PT, 0.0, 0.0, MM_PER_PT, 0.0, 0.0)
    T_disp = mat_mul(K, D)
    elements, skipped = [], 0
    text_objs = {}      # text object address -> (paint order, container matrix)
    seq = [0]

    def walk(count, get, M):
        nonlocal skipped
        for i in range(count):
            obj = get(i)
            order = seq[0]
            seq[0] += 1
            kind = R.FPDFPageObj_GetType(obj)
            Mo = mat_mul(M, _matrix(R, obj))
            if kind == R.FPDF_PAGEOBJ_FORM:
                walk(R.FPDFFormObj_CountObjects(obj), lambda j, o=obj: R.FPDFFormObj_GetObject(o, j), Mo)
            elif kind == R.FPDF_PAGEOBJ_PATH:
                el = _path_element(R, obj, mat_mul(T_disp, Mo), order)
                if el is not None:
                    elements.append(el)
            elif kind == R.FPDF_PAGEOBJ_IMAGE:
                px = _image_pixels(R, pdf, page, obj)
                got = _place(px, mat_mul(T_disp, Mo)) if px is not None else None
                if got is None:
                    skipped += 1
                else:
                    elements.append(ImageElement("Image", got[0], got[1], order))
            elif kind == R.FPDF_PAGEOBJ_TEXT:
                text_objs[_addr(obj)] = (order, M)
            else:
                skipped += 1
    walk(R.FPDFPage_CountObjects(page), lambda j: R.FPDFPage_GetObject(page, j), (1.0, 0.0, 0.0, 1.0, 0.0, 0.0))
    elements += _text_elements(R, pdf, page, text_objs, T_disp)
    elements.sort(key=lambda e: e.order)
    return PageElements(w_pt * MM_PER_PT, h_pt * MM_PER_PT, elements, skipped)


def _path_element(R, obj, T, order):
    fill, stroke = ctypes.c_int(), ctypes.c_int()
    if not R.FPDFPath_GetDrawMode(obj, fill, stroke):
        return None
    filled, stroked = fill.value != 0, bool(stroke.value)
    if not (filled or stroked):
        return None             # neither painted (e.g. a bare clip outline)
    color, alpha = _color(R, obj, stroked)
    if alpha == 0 and not (stroked and filled and _color(R, obj, False)[1]):
        return None             # fully transparent
    subs = _subpaths(R, R.FPDFPath_CountSegments(obj), lambda k: R.FPDFPath_GetPathSegment(obj, k), T)
    if not subs:
        return None
    sc = _linear_scale(T)       # page units -> mm
    thick, dash = DEFAULT_THICKNESS, None
    if stroked:
        w = ctypes.c_float()
        if R.FPDFPageObj_GetStrokeWidth(obj, w) and w.value > 0:
            thick = w.value * sc
        nd = R.FPDFPageObj_GetDashCount(obj)
        if nd > 0:
            arr = (ctypes.c_float * nd)()
            if R.FPDFPageObj_GetDashArray(obj, arr, nd):
                on = arr[0] * sc
                off = (arr[1] if nd > 1 else arr[0]) * sc
                if on > 0 and off > 0:
                    dash = (on, off)
    label = "Shape" if all(c for _a, c in subs) else "Line"
    return VectorElement("shape", label, [(a, c, color) for a, c in subs], thick, dash, order)


def _run_label(glyphs):
    """A line's text in reading order: its characters sorted along the baseline, with a space
    wherever two stand further apart than the line's usual letter gap (+0.15 em).  Space
    characters themselves are not trusted: pdfium's text order, inserted spaces and the spaces
    of letter-spaced text are all unreliable, the glyph positions are not."""
    gl = sorted(g for g in glyphs if not g[2].isspace())
    gaps = sorted(b[0] - a[1] for a, b in zip(gl, gl[1:]))
    usual = gaps[len(gaps) // 2] if gaps else 0.0
    out = []
    for k, (start, _end, ch, em) in enumerate(gl):
        if k and start - gl[k - 1][1] > usual + 0.15 * em:
            out.append(" ")
        out.append(ch)
    return "".join(out)[:40].strip() or "Text"


def _text_elements(R, pdf, page, text_objs, T_disp):
    """One VectorElement per line of text, glyph outlines placed from the text page's per-char
    origins and matrices. Text objects without outlines come back as images."""
    tp = R.FPDFText_LoadPage(page)
    if not tp:
        return []
    out = []
    try:
        # run: {"chars": [(obj addr, paths)], "glyphs": [(start, end, ch, em)], "raster": {addr: obj}}
        runs, run = [], None
        failed = {}               # text objects with a glyph pdfium has no outline for
        prev = None               # (origin, up unit vector, em, end along the baseline) of the last char
        for i in range(R.FPDFText_CountChars(tp)):
            code = R.FPDFText_GetUnicode(tp, i)
            obj = R.FPDFText_GetTextObject(tp, i)
            if code in (0x0A, 0x0D, 0xFFFE, 0x02):
                run, prev = None, None          # line break (real or inserted by pdfium)
                continue
            if not obj or R.FPDFTextObj_GetTextRenderMode(obj) in _INVISIBLE_TEXT:
                continue                        # inserted word gaps are re-derived from positions
            x, y = ctypes.c_double(), ctypes.c_double()
            R.FPDFText_GetCharOrigin(tp, i, x, y)
            m = R.FS_MATRIX()
            R.FPDFText_GetMatrix(tp, i, m)
            L = (m.a, m.b, m.c, m.d)
            size = _text_size(R, obj)
            font = R.FPDFTextObj_GetFont(obj)
            ul = (L[2] ** 2 + L[3] ** 2) ** 0.5 or 1.0
            lx = (L[0] ** 2 + L[1] ** 2) ** 0.5 or 1.0
            up = (L[2] / ul, L[3] / ul)
            em = max(size * ul, 1e-6)
            origin = (x.value, y.value)
            start = (origin[0] * L[0] + origin[1] * L[1]) / lx     # along the baseline
            adv = ctypes.c_float()
            if not (font and R.FPDFFont_GetGlyphWidth(font, code, size, adv)):
                adv.value = 0.5 * size
            end = start + adv.value * lx
            if prev is not None and run is not None:
                (px, py), pup, pem, pend = prev
                rise = (origin[0] - px) * pup[0] + (origin[1] - py) * pup[1]
                gap = start - pend
                if abs(rise) > 0.5 * pem or not -2.0 * pem <= gap <= 2.0 * pem:
                    run = None                   # a new line (or column) pdfium did not mark
            prev = (origin, up, em, end)
            if run is None:
                run = {"chars": [], "glyphs": [], "raster": {}}
                runs.append(run)
            run["glyphs"].append((start, end, chr(code), em))
            if chr(code).isspace():
                continue
            subs = _glyph_subpaths(R, font, code, size, L, origin, T_disp) if font else None
            if subs is None:
                if _addr(obj) not in failed:
                    failed[_addr(obj)] = obj
                    run["raster"][_addr(obj)] = obj     # the whole object becomes an image
                continue
            mode = R.FPDFTextObj_GetTextRenderMode(obj)
            color = _color(R, obj, mode in _STROKE_TEXT)[0]
            run["chars"].append((_addr(obj), [(a, c, color) for a, c in subs]))
        ident = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        for run in runs:
            label = _run_label(run["glyphs"])
            orders = [text_objs.get(a, (0, None))[0] for a, _s in run["chars"]]
            orders += [text_objs.get(a, (0, None))[0] for a in run["raster"]]
            order = min(orders) if orders else 0
            paths = [p for a, subs in run["chars"] if a not in failed for p in subs]
            if paths:
                out.append(VectorElement("text", label, paths, DEFAULT_THICKNESS, None, order))
            for a, obj in run["raster"].items():
                o_order, M = text_objs.get(a, (order, ident))
                got = _text_raster(R, pdf, page, obj, M, T_disp)
                if got is not None:
                    out.append(ImageElement(label, got[0], got[1], o_order))
    finally:
        R.FPDFText_ClosePage(tp)
    return out
