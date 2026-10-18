"""Image / PDF object helpers (Phase B3).  No widgets in here: the main window and the
properties panel call these, and the tests drive them directly.

Frames: an image/pdf object's raster occupies (0,0)-(w_mm,h_mm) in the object's local frame
(``w_mm = px_w / dpi * 25.4``). ``raster.outline_loops`` returns loops in that same frame, so
they go straight into ``doc.set_outline_paths``.
"""
import hashlib
import os
import re

from core import raster, userdata
from core.doc import ControlPoint, VectorPath, Xf, image_size_mm, new_id

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp")
PDF_EXTS = (".pdf",)
SVG_EXTS = (".svg",)
DEFAULT_IMAGE_DPI = 300.0
DEFAULT_PDF_DPI = 150.0
MIN_DPI, MAX_DPI = 30.0, 1200.0
RESAMPLE_DPI = 150.0           # PDF images that must be resampled get at least this
IMPORT_FILTER = ("All supported (*.svg *.png *.jpg *.jpeg *.bmp *.pdf);;SVG (*.svg);;"
                 "Images (*.png *.jpg *.jpeg *.bmp);;PDF (*.pdf)")
PDF_IMAGE_DIR = "PDF Images"   # Documents/SnowCut/PDF Images: images broken out of PDFs
KEY_MODES = ("none", "auto", "custom")
KEY_MODE_LABELS = ("None", "Auto (corner)", "Custom")


def file_kind(path):
    """'svg' | 'image' | 'pdf' | None, by extension (case-insensitive)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in SVG_EXTS:
        return "svg"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in PDF_EXTS:
        return "pdf"
    return None


def file_dpi(path):
    """DPI stored in an image file's header, or None (reads the header only)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return raster._clean_dpi(im.info.get("dpi"))
    except Exception:
        return None


def is_raster(svg):
    return svg is not None and svg.kind in ("image", "pdf")


def source_available(svg):
    return bool(svg.source_path) and os.path.isfile(svg.source_path)


def get_pixels(svg):
    """The (shared, read-only) RGBA array of an image/pdf object at its current settings."""
    if svg.kind == "pdf":
        return raster.get_rgba("pdf", svg.source_path, svg.pdf_page, svg.dpi)
    return raster.get_rgba("image", svg.source_path)


def add_image_object(doc, path, kind, page=0, offset=(10.0, 10.0)):
    """Load `path` and add an image (dpi = file dpi or 300) / pdf (render dpi 150) object.
    Raises on unreadable files / missing PDF support.  Returns the new svg id."""
    path = os.path.abspath(path)
    if kind == "pdf":
        dpi = DEFAULT_PDF_DPI
        rgba = raster.get_rgba("pdf", path, page, dpi)
    else:
        dpi = file_dpi(path) or DEFAULT_IMAGE_DPI
        rgba = raster.get_rgba("image", path)
    h, w = rgba.shape[:2]
    return doc.add_image(kind, path, w, h, dpi, page=page, offset=offset)


def regenerate_outline(doc, svg_id):
    """Rebuild (or clear) the generated outline paths of one image/pdf object from its settings.
    Returns None on success or an error string (the existing paths are then left untouched)."""
    s = doc.svgs.get(svg_id)
    if not is_raster(s):
        return None
    if not s.outline_enabled:
        doc.set_outline_paths(svg_id, [])
        return None
    if not source_available(s):
        return "source file missing: %s" % s.source_path
    try:
        rgba = get_pixels(s)
        loops = raster.outline_loops(
            rgba, s.dpi, margin_mm=s.outline_margin_mm, key_mode=s.color_key_mode,
            key_color=s.color_key, key_tol=s.color_key_tol, include_holes=s.outline_holes)
    except Exception as e:
        return "outline failed for %s: %s" % (s.label, e)
    doc.set_outline_paths(svg_id, loops)
    return None


def regenerate_all(doc):
    """Regenerate every enabled outline (project load).  Returns a list of warning strings. Objects
    whose source file is missing keep their stored paths."""
    warns = []
    for sid, s in list(doc.svgs.items()):
        if is_raster(s) and s.outline_enabled:
            err = regenerate_outline(doc, sid)
            if err:
                warns.append(err)
    return warns


def retarget_pdf(svg):
    """Re-render a pdf object's pixel size after a dpi/page change (keeps the file)."""
    rgba = raster.get_rgba("pdf", svg.source_path, svg.pdf_page, svg.dpi)
    svg.px_h, svg.px_w = int(rgba.shape[0]), int(rgba.shape[1])


def set_pdf_page_label(svg):
    base = re.sub(r" p\d+$", "", svg.label)
    svg.label = "%s p%d" % (base, svg.pdf_page + 1)


def apply_dpi(svg, dpi):
    """Change an object's dpi.  Image: the pixel grid stays, the physical size changes.  PDF: this
    is the render resolution: the object is re-rendered and the physical size stays (page size in mm).  The top-left
    offset never moves.  Raises if the pdf cannot be re-rendered (svg is then left unchanged)."""
    dpi = max(MIN_DPI, min(MAX_DPI, float(dpi)))
    old = svg.dpi
    svg.dpi = dpi
    if svg.kind == "pdf":
        try:
            retarget_pdf(svg)
        except Exception:
            svg.dpi = old
            raise


def apply_pdf_page(svg, page):
    old = svg.pdf_page
    svg.pdf_page = int(page)
    try:
        retarget_pdf(svg)
    except Exception:
        svg.pdf_page = old
        raise
    set_pdf_page_label(svg)


def _save_pdf_image(rgba, w_mm, h_mm, folder, stem, page, n):
    """Write a broken-out PDF image as PNG and return (path, px_w, px_h, dpi).  Pixels that are
    square and within the DPI control's range are kept as they are. Otherwise (the PDF stretched
    the image, or it is very coarse / fine) it is resampled to square pixels at >= RESAMPLE_DPI,
    so rounding to whole pixels moves its edges by < 0.1 mm.  Identical pixels reuse one file."""
    import numpy as np
    from PIL import Image
    from core.pdf_vector import MAX_IMAGE_PX
    h, w = rgba.shape[:2]
    dpi_x, dpi_y = w / (w_mm / 25.4), h / (h_mm / 25.4)
    dpi = max(dpi_x, dpi_y)
    im = Image.fromarray(np.ascontiguousarray(rgba), "RGBA")
    if abs(dpi_x - dpi_y) <= 0.005 * dpi and MIN_DPI <= dpi <= MAX_DPI:
        dpi = dpi_x
    else:
        dpi = min(MAX_DPI, max(RESAMPLE_DPI, dpi), MAX_IMAGE_PX / (max(w_mm, h_mm) / 25.4))
        size = (max(1, round(w_mm / 25.4 * dpi)), max(1, round(h_mm / 25.4 * dpi)))
        # enlarging: whole source pixels, as pdfium draws images without /Interpolate
        flt = Image.NEAREST if size[0] * size[1] > w * h else Image.LANCZOS
        im = im.convert("RGBa").resize(size, flt).convert("RGBA")
        dpi = size[0] / (w_mm / 25.4)
    if im.getextrema()[3][0] == 255:
        im = im.convert("RGB")
    digest = hashlib.sha1(im.tobytes() + repr(im.size).encode()).hexdigest()[:8]
    path = os.path.join(folder, "%s p%d image %d %s.png" % (stem, page + 1, n, digest))
    if not os.path.isfile(path):
        im.save(path, dpi=(dpi, dpi))
    return path, im.size[0], im.size[1], dpi


def break_apart_pdf(doc, svg_id, image_dir=None):
    """Replace a pdf object with the separate elements of its page: shapes, lines and lines of
    text become drawing objects of plain editable paths, and images become image objects (PNG files
    in Documents/SnowCut/PDF Images, or image_dir).  Everything keeps its place and size on the
    mat, and the new objects take the pdf object's place in the object order.
    Returns (new svg ids, elements left out).  Raises when the page cannot be read, and the
    document is then unchanged, as it is when the page has no elements."""
    from core import pdf_vector
    s = doc.svgs.get(svg_id)
    if s is None or s.kind != "pdf":
        return [], 0
    if not source_available(s):
        raise FileNotFoundError("source file missing: %s" % s.source_path)
    pe = pdf_vector.extract_page(s.source_path, s.pdf_page)
    if not pe.elements:
        return [], pe.skipped
    w_img, h_img = image_size_mm(s)
    kx, ky = w_img / pe.width_mm, h_img / pe.height_mm     # page units -> the object's local mm
    fl = (kx * ky) ** 0.5 * s.scale
    xf = Xf.of(s)

    def to_abs(x, y):
        return xf.to_abs(x * kx, y * ky)
    folder = image_dir or userdata.user_dir(PDF_IMAGE_DIR)
    stem = os.path.splitext(os.path.basename(s.source_path))[0] or "pdf"
    new_ids, n_img = [], 0
    for el in pe.elements:
        if el.kind == "image":
            n_img += 1
            w_mm = (el.rect[2] - el.rect[0]) * kx * s.scale
            h_mm = (el.rect[3] - el.rect[1]) * ky * s.scale
            if w_mm <= 0 or h_mm <= 0:
                continue
            path, w, h, dpi = _save_pdf_image(el.rgba, w_mm, h_mm, folder, stem, s.pdf_page, n_img)
            sid = doc.add_image("image", path, w, h, dpi, offset=to_abs(el.rect[0], el.rect[1]))
            doc.svgs[sid].rotation, doc.svgs[sid].flip = s.rotation, s.flip   # turned with the page
            doc.svgs[sid].label = ("%s image %d" % (stem, n_img)) if el.label == "Image" else el.label
            new_ids.append(sid)
            continue
        paths = []
        for anchors, closed, color in el.paths:
            pts = [ControlPoint(*to_abs(a[0], a[1]), el.thickness_mm * fl,
                                to_abs(*a[2]) if a[2] is not None else None,
                                to_abs(*a[3]) if a[3] is not None else None) for a in anchors]
            vp = VectorPath(id=new_id("path"), svg_id="", color=color, points=pts, closed=closed)
            if el.dash is not None:
                vp.dash_enabled, vp.dash_on_mm, vp.dash_off_mm = True, el.dash[0] * fl, el.dash[1] * fl
            paths.append(vp)
        label = el.label if el.kind == "text" else doc._next_label(el.label)
        sid = doc.add_svg(label, paths)
        doc.svgs[sid].kind = "drawing"
        b = doc.svg_bbox(sid)            # the object's origin at its top-left corner
        if b is not None:
            for p in paths:
                p.points = doc._reframe(p.points, Xf(), Xf(ox=b[0], oy=b[1]))
            doc.set_svg_pos(sid, b[0], b[1])
        new_ids.append(sid)
    if not new_ids:
        return [], pe.skipped
    order = list(doc.svgs)
    pos = order.index(svg_id)
    doc.delete_svg(svg_id)
    fresh = set(new_ids)
    rest = [k for k in order if k != svg_id and k not in fresh]
    ordered = rest[:pos] + new_ids + rest[pos:]
    objs = dict(doc.svgs)
    doc.svgs.clear()
    doc.svgs.update((k, objs[k]) for k in ordered)
    return new_ids, pe.skipped


def size_text(svg):
    w, h = image_size_mm(svg)
    return "%.1f x %.1f mm" % (w * svg.scale, h * svg.scale)


def title_for(svgs):
    """Properties-card title for a list of SvgInstance objects."""
    kinds = {("svg" if s.kind in ("svg", "drawing") else s.kind) for s in svgs}
    if not kinds or kinds == {"svg"}:
        return "SVG Properties"
    if kinds == {"image"}:
        return "Image Properties"
    if kinds == {"pdf"}:
        return "PDF Properties"
    if kinds == {"text"}:
        return "Text Properties"
    return "Object Properties"
