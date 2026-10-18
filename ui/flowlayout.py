"""Adaptive flow layout for icon-button grids (Tools, Tool Options, Device).

FlowLayout arranges square icon buttons compactly without forcing extra
rows/columns: minimum is a single button, sizeHint a single row (horizontal
mode) or single column (vertical mode), and setGeometry reflows into the
available space.
- horizontal mode (left/right docks, floating): fill width first, wrap down.
- vertical mode (top/bottom docks): fill height first, wrap into columns.

FlowDivider items separate button groups. When everything fits on one line
they are short lines between neighbours. Once the grid wraps, each divider
starts a new row (column) and is drawn as a rule across the full width
(height), so groups stay visually separate at any dock size.

pack_groups (horizontal mode only, Tool Options pages): when wrapping, whole
groups share a row while they fit and a row break only comes between groups
that do not, so wide option bars wrap into a few full rows instead of one
row per group.
"""
from PySide6 import QtWidgets, QtGui, QtCore

DIV = "div"   # divider marker in item lists passed to add_flow_items
DIV_T = 2     # divider thickness
DIV_COLOR = QtGui.QColor("#1b1b1b")


class FlowDivider(QtWidgets.QWidget):
    """Group separator. FlowLayout decides its length and direction."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)

    def sizeHint(self):
        return QtCore.QSize(DIV_T, DIV_T)

    def minimumSizeHint(self):
        return QtCore.QSize(0, 0)

    def paintEvent(self, ev):
        r = self.rect()
        if r.isEmpty():
            return
        p = QtGui.QPainter(self)
        # inset along the long axis so rules do not touch the card edges
        if r.width() >= r.height():
            r = r.adjusted(2, 0, -2, 0)
        else:
            r = r.adjusted(0, 4, 0, -4)
        p.fillRect(r, DIV_COLOR)
        p.end()


def add_flow_items(flow, items):
    """Add widgets to a FlowLayout. DIV markers become FlowDividers."""
    for it in items:
        flow.addWidget(FlowDivider() if it == DIV else it)


class FlowLayout(QtWidgets.QLayout):
    def __init__(self, parent=None, spacing=4, vertical=False):
        super().__init__(parent)
        self._items = []
        self._vertical = bool(vertical)
        self.pack_groups = False
        self.setSpacing(spacing)
        self.setContentsMargins(4, 4, 4, 4)

    def set_vertical(self, v):
        v = bool(v)
        if v != self._vertical:
            self._vertical = v
            self.invalidate()

    def is_vertical(self):
        return self._vertical

    # -- QLayout plumbing --
    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        try:
            return self._items[i]
        except IndexError:
            return None

    def takeAt(self, i):
        try:
            return self._items.pop(i)
        except IndexError:
            return None

    def expandingDirections(self):
        return QtCore.Qt.Orientations()

    def hasHeightForWidth(self):
        return not self._vertical

    def heightForWidth(self, w):
        m = self.contentsMargins()
        return self._do_layout(QtCore.QRect(0, 0, w, 0), True) + m.top() + m.bottom()

    def minimumSize(self):
        # One button, not one row: the dock must shrink down to a single
        # column (vertical dock) or row (horizontal dock). Reflow at the real
        # geometry is handled by setGeometry, and sizeHint stays single-line so
        # the default span is still compact-but-roomy.
        m = self.contentsMargins()
        items = [it for it in self._visible_items() if not self._is_div(it)]
        if not items:
            return QtCore.QSize(m.left() + m.right(), m.top() + m.bottom())
        one_w = max(it.minimumSize().width() for it in items)
        one_h = max(it.minimumSize().height() for it in items)
        return QtCore.QSize(one_w + m.left() + m.right(), one_h + m.top() + m.bottom())

    def sizeHint(self):
        return self._compact_size()

    @staticmethod
    def _is_div(it):
        return isinstance(it.widget(), FlowDivider)

    @staticmethod
    def _hint(it):
        """The widget's size hint bounded by its min/max (setFixedWidth etc. win)."""
        w = it.widget()
        if w is None:
            return it.sizeHint()
        return w.sizeHint().expandedTo(w.minimumSize()).boundedTo(w.maximumSize())

    def _visible_items(self):
        out = []
        for it in self._items:
            w = it.widget()
            if w is not None and w.isHidden():
                continue
            out.append(it)
        return out

    def _runs(self):
        """Visible items with dividers that would sit at an end, or next to
        another divider, dropped. Returns (laid_out, dropped_dividers)."""
        items = self._visible_items()
        keep, dropped, pending = [], [], None
        for it in items:
            if self._is_div(it):
                if pending is not None or not keep:
                    dropped.append(it)
                else:
                    pending = it
                continue
            if pending is not None:
                keep.append(pending)
                pending = None
            keep.append(it)
        if pending is not None:
            dropped.append(pending)
        return keep, dropped

    def _line(self, items):
        """(length along the flow, thickness across it) as a single line."""
        sp = self.spacing()
        main = lambda s: s.height() if self._vertical else s.width()
        cross = lambda s: s.width() if self._vertical else s.height()
        length = sum(DIV_T if self._is_div(it) else main(self._hint(it)) for it in items)
        length += sp * max(0, len(items) - 1)
        thick = max([cross(self._hint(it)) for it in items if not self._is_div(it)] or [0])
        return length, thick

    def _compact_size(self):
        m = self.contentsMargins()
        items, _dropped = self._runs()
        if not items:
            return QtCore.QSize(m.left() + m.right(), m.top() + m.bottom())
        length, thick = self._line(items)
        w, h = (thick, length) if self._vertical else (length, thick)
        return QtCore.QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def _layout_packed(self, items, m0, c0, m_len, sp, main, cross, put):
        """pack_groups wrapping (see the module doc). Returns the extent across the flow."""
        groups, divs = [[]], []
        for it in items:          # _runs() already dropped end and doubled dividers
            if self._is_div(it):
                divs.append(it)
                groups.append([])
            else:
                groups[-1].append(it)

        def width(g):
            return sum(main(self._hint(i)) for i in g) + sp * (len(g) - 1)
        lines, w = [[0]], width(groups[0])
        for k in range(1, len(groups)):
            need = 2 * sp + DIV_T + width(groups[k])
            if w + need <= m_len:
                lines[-1].append(k)
                w += need
            else:
                lines.append([k])
                w = width(groups[k])
        cp = c0
        for li, line in enumerate(lines):
            if li:                # the divider at a row break: a rule across the card
                put(divs[line[0] - 1], m0, cp, m_len, DIV_T)
                cp += DIV_T + sp
            thick = max(cross(self._hint(i)) for k in line for i in groups[k])
            mp = m0
            for j, k in enumerate(line):
                if j:
                    put(divs[k - 1], mp, cp, DIV_T, thick)
                    mp += DIV_T + sp
                for it in groups[k]:
                    s = self._hint(it)
                    if mp + main(s) > m0 + m_len and mp > m0:   # a group wider than the card
                        mp = m0
                        cp += thick + sp
                    put(it, mp, cp + (thick - cross(s)) // 2, main(s), cross(s))
                    mp += main(s) + sp
            cp += thick + sp
        return cp - sp - c0

    def _do_layout(self, rect, test):
        """Lay out (or, with test, just measure) inside rect. Works in
        (main, cross) coordinates: main runs along the flow direction.
        Returns the extent used across the flow (excluding margins)."""
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        vert = self._vertical
        sp = self.spacing()
        items, dropped = self._runs()
        m0, c0 = (area.y(), area.x()) if vert else (area.x(), area.y())
        m_len = area.height() if vert else area.width()

        def main(s):
            return s.height() if vert else s.width()

        def cross(s):
            return s.width() if vert else s.height()

        def put(it, mp, cp, ml, cl):
            if not test:
                it.setGeometry(QtCore.QRect(cp, mp, cl, ml) if vert else QtCore.QRect(mp, cp, ml, cl))

        if not test:
            for it in dropped:
                it.setGeometry(QtCore.QRect())
        if not items:
            return 0
        length, thick = self._line(items)
        if length <= m_len:
            # everything on one line: dividers are short lines between groups
            mp = m0
            for it in items:
                if self._is_div(it):
                    put(it, mp, c0, DIV_T, thick)
                    mp += DIV_T + sp
                else:
                    s = self._hint(it)
                    put(it, mp, c0 + (thick - cross(s)) // 2, main(s), cross(s))
                    mp += main(s) + sp
            return thick
        if self.pack_groups and not vert:
            return self._layout_packed(items, m0, c0, m_len, sp, main, cross, put)
        # wrapping: each divider starts a new line and spans the full line,
        # and the button lines are centred so spare width is split evenly
        mp, cp, line, total = m0, c0, 0, 0
        placed, used = [], 0     # (item, main pos, cross pos, main len, cross len)
        for it in items:
            if self._is_div(it):
                if line:
                    cp += line + sp
                put(it, m0, cp, m_len, DIV_T)
                cp += DIV_T + sp
                mp, line = m0, 0
                total = cp - c0
                continue
            s = self._hint(it)
            if mp + main(s) > m0 + m_len and line > 0:
                mp = m0
                cp += line + sp
                line = 0
            placed.append((it, mp, cp, main(s), cross(s)))
            used = max(used, mp + main(s) - m0)
            mp += main(s) + sp
            line = max(line, cross(s))
            total = max(total, cp + line - c0)
        shift = max(0, (m_len - used) // 2)
        for it, ip, ic, ml, cl in placed:
            put(it, ip + shift, ic, ml, cl)
        return total
