"""Raster image / PDF loading and raster -> cut-outline tracing.  No Qt in here.

Coordinate convention (important)
---------------------------------
A pixel ``(col i, row j)`` of an image covers the square ``[i, i+1] x [j, j+1]`` in
"pixel space", and its *centre* is at ``(i + 0.5, j + 0.5)``.  Pixel space has its origin at
the top-left corner of the image, X to the right, Y *down*.  ``mm = px * 25.4 / dpi``.

``outline_loops`` returns loops in that image-local mm frame.  The marching-squares grid
puts its vertices on pixel (cell) centres and places contour points at the 0.5 iso-level,
i.e. at the midpoint between a foreground and a background centre, which is exactly the
boundary between the two cells.  Therefore:

* a foreground region's straight edges lie exactly on pixel boundaries (a fully opaque
  100x100 image at 254 dpi -> rectangle (0,0)-(10,10) mm with margin 0).
* convex corners are chamfered by half a cell (marching squares), which is harmless for
  bounds and is the reason margin > 0 produces octagon-like corners.

Orientation: every loop is wound so the foreground lies on the right-hand side when
walking along it on a Y-down screen.  With the standard shoelace formula
``A = 0.5 * sum(x_i*y_{i+1} - x_{i+1}*y_i)`` outer loops therefore have ``A > 0`` and
holes ``A < 0``.
"""
from __future__ import annotations

import os
from collections import OrderedDict

import numpy as np

_PDF_MSG = "PDF support needs: pip install pypdfium2"

# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


def _clean_dpi(info_dpi):
    """info["dpi"] (tuple or scalar) -> float or None (None if < 30 or unusable)."""
    if info_dpi is None:
        return None
    try:
        v = info_dpi[0] if isinstance(info_dpi, (tuple, list)) else info_dpi
        v = float(v)
    except (TypeError, ValueError, IndexError):
        return None
    if not np.isfinite(v) or v < 30:
        return None
    return v


def load_rgba(path):
    """Load JPEG/PNG/BMP/... -> (uint8 HxWx4 array, dpi or None).  EXIF orientation applied."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        dpi = _clean_dpi(im.info.get("dpi"))
        try:
            im2 = ImageOps.exif_transpose(im)
        except Exception:
            im2 = im
        if im2.mode in ("I;16", "I;16L", "I;16B", "I"):
            arr = np.asarray(im2).astype(np.float64)
            hi = arr.max() if arr.size else 1.0
            scale = 255.0 / (hi if hi > 255 else 255.0)
            im2 = Image.fromarray(np.clip(arr * scale, 0, 255).astype(np.uint8), "L")
        rgba = np.array(im2.convert("RGBA"), dtype=np.uint8)
    return np.ascontiguousarray(rgba), dpi


def pdf_available():
    try:
        import pypdfium2  # noqa: F401
        return True
    except Exception:
        return False


def _pdfium():
    try:
        import pypdfium2 as pdfium
    except Exception:
        raise RuntimeError(_PDF_MSG)
    return pdfium


def pdf_page_count(path):
    pdfium = _pdfium()
    pdf = pdfium.PdfDocument(path)
    try:
        return len(pdf)
    finally:
        pdf.close()


def render_pdf(path, page, dpi):
    """Render a PDF page on a white background -> uint8 HxWx4 (alpha 255)."""
    pdfium = _pdfium()
    pdf = pdfium.PdfDocument(path)
    try:
        n = len(pdf)
        if not 0 <= page < n:
            raise IndexError(f"PDF page {page} out of range (0..{n - 1})")
        pg = pdf[page]
        try:
            bmp = pg.render(scale=float(dpi) / 72.0, fill_color=(255, 255, 255, 255))
            pil = bmp.to_pil()
            rgba = np.array(pil.convert("RGBA"), dtype=np.uint8)
        finally:
            pg.close()
    finally:
        pdf.close()
    rgba[..., 3] = 255
    return np.ascontiguousarray(rgba)


_CACHE_MAX = 6
_cache: "OrderedDict[tuple, np.ndarray]" = OrderedDict()


def get_rgba(kind, path, page=0, dpi=None):
    """Cached pixel array for an image/pdf object (returns the HxWx4 array only).

    kind "pdf": rendered at ``dpi`` (default 150).  kind "image": ``page``/``dpi`` ignored.
    Do not mutate the returned array (it is shared with the cache).
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if kind == "pdf":
        dpi = 150.0 if dpi is None else float(dpi)
        key = ("pdf", os.path.abspath(path), mtime, int(page), dpi)
    else:
        key = ("image", os.path.abspath(path), mtime)
    hit = _cache.get(key)
    if hit is not None:
        _cache.move_to_end(key)
        return hit
    if kind == "pdf":
        arr = render_pdf(path, int(page), dpi)
    else:
        arr = load_rgba(path)[0]
    _cache[key] = arr
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return arr


# --------------------------------------------------------------------------------------
# Mask construction
# --------------------------------------------------------------------------------------


def _parse_color(c):
    if isinstance(c, (tuple, list)):
        return tuple(int(v) for v in c[:3])
    s = str(c).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _foreground_mask(rgba, key_mode, key_color, key_tol):
    """Full-resolution bool mask: alpha >= 128 and not matching the colour key."""
    mask = rgba[..., 3] >= 128
    if key_mode in (None, "none"):
        return mask
    if key_mode == "auto":
        h, w = rgba.shape[:2]
        corners = [tuple(int(v) for v in rgba[y, x, :3])
                   for y in (0, h - 1) for x in (0, w - 1)]
        key = max(set(corners), key=corners.count)
    else:
        key = _parse_color(key_color)
    tol = int(key_tol)
    match = np.ones(mask.shape, dtype=bool)
    for ch in range(3):
        plane = rgba[..., ch]
        k = np.uint8(key[ch])
        d = np.maximum(plane, k) - np.minimum(plane, k)   # |plane - k| in uint8
        match &= d <= tol
    mask &= ~match
    return mask


def _downsample_any(mask, f):
    """Reduce a bool mask by integer factor f (cell is True if any source pixel is)."""
    if f <= 1:
        return mask
    h, w = mask.shape
    hp, wp = -(-h // f) * f, -(-w // f) * f
    if (hp, wp) != (h, w):
        m = np.zeros((hp, wp), dtype=bool)
        m[:h, :w] = mask
    else:
        m = mask
    m = m.reshape(hp // f, f, wp // f, f)
    return m.any(axis=3).any(axis=1)


# --------------------------------------------------------------------------------------
# Morphology (octagonal dilation / erosion)
# --------------------------------------------------------------------------------------


def _neighbours(m, include_diag):
    p = np.pad(m, 1, mode="constant", constant_values=False)
    nb = [p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]]
    if include_diag:
        nb += [p[:-2, :-2], p[:-2, 2:], p[2:, :-2], p[2:, 2:]]
    return nb


def _shift_or(m, include_diag):
    out = m.copy()
    for a in _neighbours(m, include_diag):
        out |= a
    return out


def _shift_and(m, include_diag):
    """Erosion step. Cells outside the array count as background."""
    out = m.copy()
    for a in _neighbours(m, include_diag):
        out &= a
    return out


def _morph(m, n, grow):
    """n alternating 4-/8-neighbour steps (4 first) => octagonal structuring element."""
    step = _shift_or if grow else _shift_and
    for i in range(n):
        m = step(m, i % 2 == 1)
    return m


# --------------------------------------------------------------------------------------
# Marching squares
# --------------------------------------------------------------------------------------
# Square corners: tl(0,0) tr(1,0) br(1,1) bl(0,1), edge midpoints T R B L.
# case index = tl*8 + tr*4 + br*2 + bl.   Edges: 0=T 1=R 2=B 3=L.
_CORNER_XY = {"tl": (0.0, 0.0), "tr": (1.0, 0.0), "br": (1.0, 1.0), "bl": (0.0, 1.0)}
_EDGE_XY = {0: (0.5, 0.0), 1: (1.0, 0.5), 2: (0.5, 1.0), 3: (0.0, 0.5)}
_EDGE_CORNERS = {0: {"tl", "tr"}, 1: {"tr", "br"}, 2: {"bl", "br"}, 3: {"tl", "bl"}}
# (edge_a, edge_b) cutting off the shared corner / crossing the square
_CASE_EDGES = {
    0: [], 15: [],
    1: [(3, 2)], 2: [(2, 1)], 3: [(3, 1)],
    4: [(0, 1)], 6: [(0, 2)], 7: [(3, 0)],
    8: [(3, 0)], 9: [(0, 2)], 11: [(0, 1)],
    12: [(3, 1)], 13: [(2, 1)], 14: [(3, 2)],
    # saddles: foreground diagonal corners stay connected (deterministic, 8-connected fg)
    5: [(3, 0), (1, 2)],      # fg tr + bl : cut off tl and br
    10: [(0, 1), (3, 2)],     # fg tl + br : cut off tr and bl
}


def _build_case_table():
    """Per case: up to two directed (from_edge, to_edge) with foreground on the right."""
    corners = [("tl", 8), ("tr", 4), ("br", 2), ("bl", 1)]
    table = np.full((16, 2, 2), -1, dtype=np.int64)
    for case, segs in _CASE_EDGES.items():
        flags = {n: bool(case & b) for n, b in corners}
        for si, (ea, eb) in enumerate(segs):
            ax, ay = _EDGE_XY[ea]
            bx, by = _EDGE_XY[eb]

            def cross(cn):
                cx, cy = _CORNER_XY[cn]
                return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)

            # reference corner: shared corner of two adjacent edges, else any corner on
            # the positive-cross side
            shared = _EDGE_CORNERS[ea] & _EDGE_CORNERS[eb]
            if shared:
                ref = next(iter(shared))
            else:
                ref = next(cn for cn in _CORNER_XY if cross(cn) > 0)
            fg = flags[ref]
            right = cross(ref) > 0
            # fg must be on the right (cross>0 means the corner is on the right in Y-down)
            if fg == right:
                table[case, si] = (ea, eb)
            else:
                table[case, si] = (eb, ea)
    return table


_CASE_TABLE = _build_case_table()


def _march(g):
    """Marching squares on bool grid g (vertices at cell centres, border must be False).

    Returns (start_ids, end_ids, W) of directed segments between edge-midpoint ids.
    """
    H, W = g.shape
    tl = g[:-1, :-1]
    tr = g[:-1, 1:]
    br = g[1:, 1:]
    bl = g[1:, :-1]
    case = (tl.astype(np.uint8) << 3) | (tr.astype(np.uint8) << 2) | \
           (br.astype(np.uint8) << 1) | bl.astype(np.uint8)
    rr, cc = np.nonzero((case != 0) & (case != 15))
    if rr.size == 0:
        return np.empty(0, np.int64), np.empty(0, np.int64), W
    cs = case[rr, cc].astype(np.int64)
    base = rr.astype(np.int64) * W + cc
    # edge-id offsets relative to base for T,R,B,L, ids = 2*(r*W+c) (+1 for vertical)
    # T: h(r,c)=2*base, B: h(r+1,c)=2*(base+W), L: v(r,c)=2*base+1, R: v(r,c+1)=2*(base+1)+1
    off = np.array([0, 2 * 1 + 1, 2 * W, 1], dtype=np.int64)
    starts, ends = [], []
    for si in range(2):
        ea = _CASE_TABLE[cs, si, 0]
        eb = _CASE_TABLE[cs, si, 1]
        ok = ea >= 0
        if not ok.any():
            continue
        b2 = 2 * base[ok]
        starts.append(b2 + off[ea[ok]])
        ends.append(b2 + off[eb[ok]])
    return np.concatenate(starts), np.concatenate(ends), W


def _stitch(start, end):
    """Chain directed segments into closed loops of edge ids (dict based, O(n))."""
    nxt = dict(zip(start.tolist(), end.tolist()))
    loops = []
    while nxt:
        s, e = nxt.popitem()
        loop = [s]
        cur = e
        while cur != s:
            loop.append(cur)
            cur = nxt.pop(cur, None)
            if cur is None:        # should not happen, so the open chain is dropped
                loop = None
                break
        if loop is not None and len(loop) >= 3:
            loops.append(loop)
    return loops


def _ids_to_xy(ids, W):
    ids = np.asarray(ids, dtype=np.int64)
    vert = (ids & 1).astype(bool)
    cell = ids >> 1
    r = cell // W
    c = cell % W
    x = np.where(vert, c + 0.5, c + 1.0)
    y = np.where(vert, r + 1.0, r + 0.5)
    return np.stack([x, y], axis=1)


# --------------------------------------------------------------------------------------
# Simplification
# --------------------------------------------------------------------------------------


def _rdp_open(pts, tol):
    """Ramer-Douglas-Peucker on an open polyline (Nx2 ndarray) -> kept indices."""
    n = len(pts)
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        seg = pts[i + 1:j]
        a = pts[i]
        d = pts[j] - a
        L = np.hypot(d[0], d[1])
        if L < 1e-12:
            dist = np.hypot(seg[:, 0] - a[0], seg[:, 1] - a[1])
        else:
            dist = np.abs(d[0] * (seg[:, 1] - a[1]) - d[1] * (seg[:, 0] - a[0])) / L
        k = int(np.argmax(dist))
        if dist[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    return np.nonzero(keep)[0]


def _simplify_closed(pts, tol):
    n = len(pts)
    if n <= 3 or tol <= 0:
        return pts
    # split at the point farthest from point 0 => two stable halves
    d0 = np.hypot(pts[:, 0] - pts[0, 0], pts[:, 1] - pts[0, 1])
    m = int(np.argmax(d0))
    if m == 0:
        return pts
    a = pts[:m + 1]
    b = np.vstack([pts[m:], pts[:1]])
    ia = _rdp_open(a, tol)
    ib = _rdp_open(b, tol)
    out = np.vstack([a[ia[:-1]], b[ib[:-1]]])
    return out if len(out) >= 3 else pts


def _collapse_chamfers(xy, cell):
    """Replace isolated marching-squares corner chamfers by the true right-angle corner.

    A chamfer is a (h, h) diagonal (h = half a cell) sitting between two perpendicular
    axis-aligned runs of one full cell each.  Keeps rectangles exactly rectangular.
    """
    n = len(xy)
    if n < 8:
        return xy
    h = 0.5 * cell
    eps = 1e-6 * cell
    d = np.roll(xy, -1, axis=0) - xy
    ax, ay = np.abs(d[:, 0]), np.abs(d[:, 1])
    cham = (np.abs(ax - h) < eps) & (np.abs(ay - h) < eps)
    horiz = (ay < eps) & (np.abs(ax - cell) < eps)
    vert = (ax < eps) & (np.abs(ay - cell) < eps)
    axis = horiz | vert
    prev_ok = np.roll(axis, 1)
    next_ok = np.roll(axis, -1)
    perp = (np.roll(horiz, 1) & np.roll(vert, -1)) | (np.roll(vert, 1) & np.roll(horiz, -1))
    sel = cham & prev_ok & next_ok & perp
    if not sel.any():
        return xy
    idx = np.nonzero(sel)[0]
    a = xy[idx]
    b = xy[(idx + 1) % n]
    prev_h = horiz[(idx - 1) % n]
    corner = np.where(prev_h[:, None],
                      np.stack([b[:, 0], a[:, 1]], axis=1),      # came along x, leave along y
                      np.stack([a[:, 0], b[:, 1]], axis=1))
    out = xy.copy()
    out[idx] = corner
    keep = np.ones(n, dtype=bool)
    keep[(idx + 1) % n] = False
    return out[keep]


def _signed_area(p):
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))


# --------------------------------------------------------------------------------------
# Public: outline
# --------------------------------------------------------------------------------------


def outline_loops(rgba, dpi, margin_mm=0.0, key_mode="none", key_color="#FFFFFF", key_tol=32,
                  include_holes=False, simplify_mm=0.05, max_grid=1200):
    """Trace the foreground of an RGBA image into closed loops (list of [(x_mm, y_mm), ...]).

    See module docstring for the coordinate convention.  Outer loops have positive
    shoelace area, holes negative.  Loops are not repeated at the end (implicitly closed).
    """
    if rgba.ndim != 3 or rgba.shape[2] < 4:
        raise ValueError("rgba must be HxWx4")
    dpi = float(dpi) if dpi else 96.0
    H, W = rgba.shape[:2]
    px_mm = 25.4 / dpi
    margin_mm = float(margin_mm or 0.0)

    mask = _foreground_mask(rgba, key_mode, key_color, key_tol)
    if not mask.any():
        return []
    if mask.all() and margin_mm == 0.0:
        w, h = W * px_mm, H * px_mm
        return [[(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]]

    f = max(1, -(-max(H, W) // int(max(1, max_grid))))
    cell_mm = f * px_mm
    g = _downsample_any(mask, f)

    n_it = int(round(abs(margin_mm) / cell_mm))
    pad = n_it + 2
    g = np.pad(g, pad, mode="constant", constant_values=False)
    if n_it and margin_mm > 0:
        g = _morph(g, n_it, True)
    elif n_it and margin_mm < 0:
        g = _morph(g, n_it, False)
    if not g.any():
        return []

    start, end, gw = _march(g)
    if start.size == 0:
        return []
    id_loops = _stitch(start, end)

    clamp = max(margin_mm, 0.0) + 1e-9
    xmax, ymax = W * px_mm + clamp, H * px_mm + clamp
    min_area = (2.0 * cell_mm) ** 2
    out = []
    for ids in id_loops:
        xy = (_ids_to_xy(ids, gw) - pad) * cell_mm
        xy = _collapse_chamfers(xy, cell_mm)
        xy[:, 0] = np.clip(xy[:, 0], -clamp, xmax)
        xy[:, 1] = np.clip(xy[:, 1], -clamp, ymax)
        area = _signed_area(xy)
        if abs(area) < min_area:
            continue
        if area < 0 and not include_holes:
            continue
        if simplify_mm and simplify_mm > 0:
            xy = _simplify_closed(xy, float(simplify_mm))
            if len(xy) < 3:
                continue
        out.append([(float(x), float(y)) for x, y in xy])
    return out
