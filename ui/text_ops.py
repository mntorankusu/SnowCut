"""Text objects: font styles, layout, glyph outlines and caret geometry (Qt fonts).

A text object (SvgInstance kind "text") keeps its text and TextStyle. Its cut paths are generated
from the glyph outlines (VectorPath.generated) and rebuilt by regenerate() after every edit.
Local frame: mm, origin on the first line's baseline at the alignment point (left end, centre
or right end of each line), and lines go down by the font's line spacing.

Only real font styles are used. The bold / italic choices pick existing styles of the family
(QFont.setStyleName), never Qt's synthesized ones. TextStyle.faux_* do the synthetic versions
explicitly: faux bold grows the outline (a stroke of the outline united with it), faux italic
shears each line about its own baseline.
"""
from dataclasses import dataclass, field

from PySide6 import QtGui, QtCore

from core.doc import TextStyle
from core import shapes as geom

REF_PX = 1000           # glyphs are laid out at this pixel size, then scaled to mm
FLAT_TOL_MM = 0.01      # faux bold outlines come back flattened: simplified to this tolerance
PREFERRED_FAMILIES = ("Arial", "Segoe UI", "Helvetica", "DejaVu Sans")
DB = QtGui.QFontDatabase
_ITALIC_WORDS = ("italic", "oblique")


# ---------------------------------------------------------------- font styles
def families():
    return [f for f in DB.families() if DB.isScalable(f) and not DB.isPrivateFamily(f)]


def default_family():
    fams = set(families())
    for f in PREFERRED_FAMILIES:
        if f in fams:
            return f
    return QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.GeneralFont).family()


def _is_italic(family, style):
    return DB.italic(family, style) or any(w in style.lower() for w in _ITALIC_WORDS)


def upright_styles(family):
    """The family's non-italic styles, lightest first (by weight, then name)."""
    st = [s for s in DB.styles(family) if not _is_italic(family, s)]
    return sorted(st, key=lambda s: (DB.weight(family, s), s))


def base_style(family):
    """The style the bold button returns to: "Regular" / "Normal" / "Book", else the one whose
    weight is nearest 400."""
    up = upright_styles(family)
    if not up:
        return "Regular"
    for name in ("Regular", "Normal", "Book", "Roman"):
        if name in up:
            return name
    return min(up, key=lambda s: (abs(DB.weight(family, s) - 400), s))


def _words(style):
    return {w for w in style.lower().replace("-", " ").split()
            if w not in _ITALIC_WORDS and w not in ("regular", "normal", "book", "roman")}


def italic_for(family, style):
    """The real italic counterpart of an upright style, or None. Same weight and same other
    words (e.g. "SemiBold Condensed" -> "SemiBold Condensed Italic")."""
    want_w, want = DB.weight(family, style), _words(style)
    best = None
    for s in DB.styles(family):
        if not _is_italic(family, s) or DB.weight(family, s) != want_w:
            continue
        if _words(s) == want:
            return s
        best = best or s
    return best if not want else None


def resolve(style):
    """(family, style name actually used) for a TextStyle, falling back to the family's base
    style when the stored one does not exist (e.g. another computer)."""
    fam = style.font_family or default_family()
    up = upright_styles(fam)
    st = style.font_style if style.font_style in up else base_style(fam)
    if style.font_italic:
        st = italic_for(fam, st) or st
    return fam, st


def qfont(style):
    fam, st = resolve(style)
    f = QtGui.QFont(fam)
    f.setStyleName(st)
    f.setPixelSize(REF_PX)
    f.setHintingPreference(QtGui.QFont.PreferNoHinting)
    f.setStyleStrategy(QtGui.QFont.StyleStrategy(QtGui.QFont.ForceOutline | QtGui.QFont.NoFontMerging))
    return f


def new_style(family=None):
    fam = family or default_family()
    return TextStyle(font_family=fam, font_style=base_style(fam))


# ---------------------------------------------------------------- layout
@dataclass
class Line:
    text: str
    start: int            # index of the line's first character in the whole text
    baseline: float       # local mm
    x0: float             # left end, local mm
    carets: list = field(default_factory=list)   # caret x (unsheared) before each char + end


@dataclass
class Layout:
    lines: list
    ascent: float
    descent: float
    line_h: float
    shear: float          # x shift per mm above the baseline (faux italic)
    scale: float          # mm per font pixel

    def bbox(self):
        """(x0, y0, x1, y1) local mm of the text box, shear included."""
        top = self.lines[0].baseline - self.ascent
        bot = self.lines[-1].baseline + self.descent
        xs = []
        for ln in self.lines:
            for c in (ln.carets[0], ln.carets[-1]):
                xs += [c + self.shear * self.ascent, c - self.shear * self.descent]
        return min(xs), top, max(xs), bot


def layout(s):
    st = s.text_style or new_style()
    f = qfont(st)
    fm = QtGui.QFontMetricsF(f)
    sc = st.size_mm / REF_PX
    line_h = fm.lineSpacing() * sc
    lines, start = [], 0
    for i, text in enumerate(s.text.split("\n")):
        w = fm.horizontalAdvance(text) * sc
        x0 = {"left": 0.0, "center": -w / 2, "right": -w}.get(st.align, 0.0)
        carets = [x0 + fm.horizontalAdvance(text[:j]) * sc for j in range(len(text) + 1)]
        lines.append(Line(text, start, i * line_h, x0, carets))
        start += len(text) + 1
    shear = st.faux_italic if st.faux_italic_on else 0.0
    return Layout(lines, fm.ascent() * sc, fm.descent() * sc, line_h, shear, sc)


def glyph_path(s, lay=None):
    """QPainterPath of the glyph outlines, local mm (faux bold and italic applied)."""
    st = s.text_style or new_style()
    lay = lay or layout(s)
    f = qfont(st)
    sc = lay.scale
    out = QtGui.QPainterPath()
    for ln in lay.lines:
        if not ln.text.strip():
            continue
        p = QtGui.QPainterPath()
        p.addText(QtCore.QPointF(ln.x0 / sc, ln.baseline / sc), f, ln.text)
        if st.faux_bold_on and st.faux_bold > 0:
            stroker = QtGui.QPainterPathStroker()
            stroker.setWidth(2 * st.faux_bold * st.size_mm / sc)
            stroker.setJoinStyle(QtCore.Qt.MiterJoin)
            stroker.setMiterLimit(2.0)
            p = p.united(stroker.createStroke(p)).simplified()
        tr = QtGui.QTransform()
        tr.scale(sc, sc)
        p = tr.map(p)
        if lay.shear:
            sh = QtGui.QTransform(1, 0, -lay.shear, 1, lay.shear * ln.baseline, 0)   # x += k*(base - y)
            p = sh.map(p)
        out.addPath(p)
    return out


def path_shapes(qp, eps=1e-6, flat_tol=FLAT_TOL_MM):
    """[(anchors, closed)] from a QPainterPath: anchors (x, y, hin, hout), every subpath closed
    (glyph contours always are). Contours of straight lines only (Qt's boolean operations
    flatten curves) are thinned with RDP to flat_tol."""
    shapes, cur = [], None
    E = QtGui.QPainterPath.ElementType
    i, n = 0, qp.elementCount()
    while i < n:
        e = qp.elementAt(i)
        if e.type == E.MoveToElement:
            if cur:
                shapes.append(cur)
            cur = [[e.x, e.y, None, None]]
            i += 1
        elif e.type == E.LineToElement:
            cur.append([e.x, e.y, None, None])
            i += 1
        else:   # CurveToElement + 2 CurveToDataElements
            c1, c2, end = e, qp.elementAt(i + 1), qp.elementAt(i + 2)
            cur[-1][3] = (c1.x, c1.y)
            cur.append([end.x, end.y, (c2.x, c2.y), None])
            i += 3
    if cur:
        shapes.append(cur)
    out = []
    for pts in shapes:
        clean = []
        for a in pts:   # drop repeated points (a zero-length line), merging their handles
            if clean and abs(a[0] - clean[-1][0]) < eps and abs(a[1] - clean[-1][1]) < eps:
                clean[-1][3] = a[3]
                if a[2] is not None and clean[-1][2] is None and len(clean) == 1:
                    clean[-1][2] = a[2]
                continue
            clean.append(a)
        if len(clean) >= 2 and abs(clean[0][0] - clean[-1][0]) < eps and abs(clean[0][1] - clean[-1][1]) < eps:
            last = clean.pop()     # the closing point: its incoming handle belongs to the start
            clean[0][2] = last[2]
        if len(clean) > 8 and all(a[2] is None and a[3] is None for a in clean):
            ring = [(a[0], a[1]) for a in clean]
            thin = geom.rdp(ring + [ring[0]], flat_tol)[:-1]
            clean = [[x, y, None, None] for x, y in thin]
        if len(clean) >= 2:
            out.append(([tuple(a) for a in clean], True))
    return out


def regenerate(doc, svg_id):
    """Rebuild a text object's generated paths from its text and style, and keep the label in step."""
    s = doc.svgs.get(svg_id)
    if s is None or s.kind != "text":
        return
    lay = layout(s)
    old = [doc.paths[p] for p in s.path_ids if p in doc.paths and doc.paths[p].generated]
    doc.set_generated_paths(svg_id, path_shapes(glyph_path(s, lay)),
                            (s.text_style or new_style()).thickness_mm)
    if old:     # Line Properties' dash settings outlive the rebuild
        for pid in s.path_ids:
            p = doc.paths[pid]
            p.dash_enabled, p.dash_on_mm, p.dash_off_mm = old[0].dash_enabled, old[0].dash_on_mm, old[0].dash_off_mm
    first = s.text.strip().split("\n")[0].strip() if s.text.strip() else ""
    s.label = (first[:24] + "…" if len(first) > 24 else first) or "Text"


# ---------------------------------------------------------------- caret
def line_col(lay, idx):
    for k, ln in enumerate(lay.lines):
        if idx <= ln.start + len(ln.text):
            return k, max(0, idx - ln.start)
    last = lay.lines[-1]
    return len(lay.lines) - 1, len(last.text)


def caret_line(lay, idx):
    """((x, y) top, (x, y) bottom) of the caret before character idx, local mm."""
    k, c = line_col(lay, idx)
    ln = lay.lines[k]
    x = ln.carets[c]
    return ((x + lay.shear * lay.ascent, ln.baseline - lay.ascent),
            (x - lay.shear * lay.descent, ln.baseline + lay.descent))


def selection_spans(lay, start, end, newline_w=0.0):
    """[(x0, x1, baseline)] per line of the characters start..end (unsheared local mm). A
    selected line break adds newline_w at the end of its line."""
    out = []
    for i, ln in enumerate(lay.lines):
        lo, hi = ln.start, ln.start + len(ln.text)
        a, b = max(start, lo), min(end, hi)
        brk = i < len(lay.lines) - 1 and start <= hi < end      # the "\n" after this line
        if a < b or (brk and a <= b):
            x0 = ln.carets[a - lo]
            x1 = ln.carets[b - lo] + (newline_w if brk else 0.0)
            out.append((x0, x1, ln.baseline))
    return out


def hit(lay, x, y):
    """Caret index nearest the local point (x, y)."""
    k = min(range(len(lay.lines)),
            key=lambda i: abs(y - (lay.lines[i].baseline - (lay.ascent - lay.descent) / 2)))
    ln = lay.lines[k]
    ux = x - lay.shear * (ln.baseline - y)       # undo the shear at this height
    c = min(range(len(ln.carets)), key=lambda j: abs(ln.carets[j] - ux))
    return ln.start + c


def vertical(lay, idx, step):
    """Caret index one line up (step -1) / down (+1), keeping the caret's x (idx at the ends)."""
    k, c = line_col(lay, idx)
    j = k + step
    if j < 0:
        return 0
    if j >= len(lay.lines):
        return lay.lines[-1].start + len(lay.lines[-1].text)
    x = lay.lines[k].carets[c]
    ln = lay.lines[j]
    return ln.start + min(range(len(ln.carets)), key=lambda m: abs(ln.carets[m] - x))
