"""Type tool for the canvas (mixin).

- Click on empty canvas: a new text object at that point (the first line's baseline) and a
  blinking caret. Type to fill it. Click on a text object: edit it, the caret at the click.
- Shift+Enter: new line. Enter / Esc / another tool / clicking elsewhere: finish (an empty text
  is removed). Arrows and Home / End move the caret, and Backspace / Delete delete.
- Selecting characters: drag across the text, Shift+click, Shift + caret keys, or Ctrl+A.
  Typing, Backspace / Delete and paste replace the selection. Ctrl+C / Ctrl+X / Ctrl+V use
  the clipboard.
- Double-clicking a text object with the mouse or transform tool switches to this tool and edits.

The text object (doc kind "text") is rebuilt by ui.text_ops after every change, and typing bursts
coalesce into one undo step. The options panel drives set_text_style(), which edits the text being
typed (or the selected text objects) and the style of new text.
"""
from dataclasses import replace

from PySide6 import QtCore, QtGui

from core.doc import Xf

from . import text_ops

TYPE_TOOL = "type"
TYPE_HINT = "Click to type, or click text to edit it. Shift+Enter adds a line, Enter finishes."
CARET_MS = 530
PICK_PAD_PX = 4.0
CARET_Z = 16
SELECT_RGBA = (10, 108, 182, 90)    # the accent blue, translucent: letters stay readable
NEWLINE_W = 0.25                    # a selected line break shows this wide (x ascent)


class TextMixin:
    def _init_text(self):
        self.text_style = None        # TextStyle of new text (made on first use: fonts need the app)
        self._text_edit = None        # {"svg_id", "caret", "anchor"} while typing. anchor =
                                      # other end of the character selection, None = none
        self._caret_on = True
        self._caret_items = []
        self._caret_timer = QtCore.QTimer(self)
        self._caret_timer.setInterval(CARET_MS)
        self._caret_timer.timeout.connect(self._blink)

    # -- style --
    def default_text_style(self):
        if self.text_style is None:
            self.text_style = text_ops.new_style()
        return self.text_style

    def _style_targets(self):
        sid = self.editing_text_id()
        if sid is not None:
            return [sid]
        d = self.doc
        return sorted(d.text_svg_ids(d.svg_ids_for_selection()))

    def current_text_style(self):
        """What the options panel shows: the text being edited, else the first selected text
        object, else the style of new text."""
        for sid in self._style_targets():
            st = self.doc.svgs[sid].text_style
            if st is not None:
                return st
        return self.default_text_style()

    def set_text_style(self, **changes):
        """Change TextStyle fields of the text being edited (else the selected text objects) and
        of new text."""
        self.text_style = replace(self.current_text_style(), **changes)
        targets = self._style_targets()
        for sid in targets:
            s = self.doc.svgs[sid]
            s.text_style = replace(s.text_style or self.text_style, **changes)
            text_ops.regenerate(self.doc, sid)
        if targets:
            self.history_coalesce_key = ("text_style", tuple(targets), tuple(sorted(changes)))
            self._after_edit()
        self.options_changed.emit()

    def current_text_thickness(self):
        """Thickness shown on the Type page: the edited / selected text's, else the value shared
        with the drawing tools."""
        return self.current_text_style().thickness_mm if self._style_targets() else self.draw_thickness_mm

    def set_text_thickness(self, mm):
        """Line thickness of the edited / selected text, of new text, and of new drawn shapes
        (one value shared with the drawing tools' Thickness)."""
        self.set_draw_thickness(mm)
        self.set_text_style(thickness_mm=self.draw_thickness_mm)

    def current_text_color(self):
        for sid in self._style_targets():
            return self.doc.svgs[sid].outline_color
        return self.draw_color

    def set_text_color(self, color):
        """Color (color group) of the text being edited / selected text objects, and of new
        text and shapes."""
        self.set_draw_color(color)
        targets = self._style_targets()
        for sid in targets:
            s = self.doc.svgs[sid]
            s.outline_color = self.draw_color
            for pid in s.path_ids:
                if pid in self.doc.paths:
                    self.doc.set_path_color(pid, self.draw_color)
        if targets:
            self._after_edit()
        self.options_changed.emit()

    # -- state --
    def editing_text_id(self):
        st = self._text_edit
        if st is None:
            return None
        s = self.doc.svgs.get(st["svg_id"])
        if s is None or s.kind != "text":
            self._text_stop()
            return None
        return st["svg_id"]

    def _text_local(self, s, x, y):
        return Xf.of(s).to_local(x, y)

    def _text_at(self, x, y):
        """The text object under scene point (x, y) (its text box, a few px of slack), topmost
        first. The one being edited wins."""
        pad = PICK_PAD_PX / max(1e-6, self._px_per_mm())
        ids = [sid for sid, s in self.doc.svgs.items() if s.kind == "text" and s.visible]
        cur = self.editing_text_id()
        if cur in ids:
            ids.remove(cur)
            ids.append(cur)
        for sid in reversed(ids):
            s = self.doc.svgs[sid]
            x0, y0, x1, y1 = text_ops.layout(s).bbox()
            lx, ly = self._text_local(s, x, y)      # the text box in the object's own frame
            p = pad / (s.scale or 1.0)
            if x0 - p <= lx <= x1 + p and y0 - p <= ly <= y1 + p:
                return sid
        return None

    def _text_hit_index(self, sid, x, y):
        s = self.doc.svgs[sid]
        return text_ops.hit(text_ops.layout(s), *self._text_local(s, x, y))

    # -- lifecycle --
    def text_selection(self):
        """(start, end) character indexes of the selection while typing, or None."""
        st = self._text_edit
        if st is None or st.get("anchor") is None or st["anchor"] == st["caret"]:
            return None
        return min(st["anchor"], st["caret"]), max(st["anchor"], st["caret"])

    def _set_caret(self, idx, extend=False):
        """Move the caret. extend = the selection grows / shrinks with it (Shift, drags)."""
        st = self._text_edit
        if extend:
            if st.get("anchor") is None:
                st["anchor"] = st["caret"]
        else:
            st["anchor"] = None
        st["caret"] = idx
        self._caret_on = True
        self._update_caret()

    def select_all_text(self):
        sid = self.editing_text_id()
        if sid is not None:
            self._text_edit["anchor"] = 0
            self._set_caret(len(self.doc.svgs[sid].text), extend=True)

    def _text_begin(self, sid, caret, new=False):
        self._text_edit = {"svg_id": sid, "caret": caret, "anchor": None}
        sel = self.doc.selection
        sel.clear()
        sel.svg_ids.add(sid)
        self._caret_on = True
        self._caret_timer.start()
        self.options_changed.emit()
        if new:
            self.refresh()          # nothing to record until something is typed
        else:
            self._after_edit()

    def _text_stop(self):
        self._text_edit = None
        self._caret_timer.stop()
        self._clear_caret()

    def _text_finish(self, select=True):
        """End typing. An empty text object is removed, otherwise it stays selected."""
        st = self._text_edit
        if st is None:
            return
        self._text_stop()
        s = self.doc.svgs.get(st["svg_id"])
        if s is not None and s.kind == "text" and not s.text.strip():
            self.doc.delete_svg(s.id)
        elif s is not None and select:
            self.doc.selection.clear()
            self.doc.selection.svg_ids.add(s.id)
        self.options_changed.emit()
        self._after_edit()

    def _text_on_tool_change(self, new):
        if new != TYPE_TOOL:
            self._text_finish()

    def edit_text_at(self, x, y):
        """Start editing the text object under scene point (x, y). False if there is none."""
        sid = self._text_at(x, y)
        if sid is None:
            return False
        if self.editing_text_id() not in (None, sid):
            self._text_finish(select=False)
        self._text_begin(sid, self._text_hit_index(sid, x, y))
        return True

    # -- mouse / keys --
    def _press_type(self, ev, sp):
        self._drag_kind = None
        x, y = sp.x(), sp.y()
        sid = self._text_at(x, y)
        if sid is not None and sid == self.editing_text_id():
            shift = bool(ev.modifiers() & QtCore.Qt.ShiftModifier)
            self._set_caret(self._text_hit_index(sid, x, y), extend=shift)
            self._drag_kind = "text_select"
            return
        if self.edit_text_at(x, y):
            self._drag_kind = "text_select"     # dragging on from here selects
            return
        self._text_finish(select=False)
        style = replace(self.default_text_style(), thickness_mm=self.draw_thickness_mm)
        sid = self.doc.add_text(x, y, style, self.draw_color)
        self._text_begin(sid, 0, new=True)

    def _text_drag_tick(self, ev, sp):
        sid = self.editing_text_id()
        if sid is not None:
            idx = self._text_hit_index(sid, sp.x(), sp.y())
            if idx != self._text_edit["caret"]:
                self._set_caret(idx, extend=True)

    def _text_drag_end(self, ev, sp):
        self._drag_kind = None

    def _text_changed(self, s, text, caret):
        s.text = text
        self._text_edit["caret"] = caret
        self._text_edit["anchor"] = None
        text_ops.regenerate(self.doc, s.id)
        self._caret_on = True
        self.history_coalesce_key = ("type", s.id)   # a typing burst is one undo step
        self._after_edit()

    def _text_key(self, ev):
        """Keys while typing. True = consumed (every key except Ctrl / Alt shortcuts)."""
        sid = self.editing_text_id()
        if sid is None:
            return False
        s = self.doc.svgs[sid]
        key, mods = ev.key(), ev.modifiers()
        Qt = QtCore.Qt
        text, c = s.text, max(0, min(self._text_edit["caret"], len(s.text)))
        sel = self.text_selection()
        a, b = sel if sel else (c, c)

        def put(new, lo=a, hi=b):     # replace text[lo:hi] (the selection by default)
            self._text_changed(s, text[:lo] + new + text[hi:], lo + len(new))
        if mods & Qt.ControlModifier and not mods & Qt.AltModifier:     # (Ctrl+Alt = AltGr)
            board = QtGui.QGuiApplication.clipboard()
            if key == Qt.Key_A:
                self.select_all_text()
            elif key in (Qt.Key_C, Qt.Key_X) and sel:
                board.setText(text[a:b])
                if key == Qt.Key_X:
                    put("")
            elif key == Qt.Key_V:
                pasted = "".join(ch for ch in (board.text() or "").replace("\r\n", "\n")
                                 .replace("\r", "\n").replace("\t", " ")
                                 if ch == "\n" or ch.isprintable())
                if pasted or sel:
                    put(pasted)
            else:
                return False     # Ctrl+Z and the other shortcuts
            return True
        if mods & Qt.AltModifier and not (ev.text() and ev.text().isprintable()):
            return False         # Alt shortcuts (AltGr characters, Ctrl+Alt, still type)
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if mods & Qt.ShiftModifier:
                put("\n")
            else:
                self._text_finish()
            return True
        if key == Qt.Key_Escape:
            self._text_finish()
            return True
        if key == Qt.Key_Backspace:
            if sel:
                put("")
            elif c > 0:
                put("", c - 1, c)
            return True
        if key == Qt.Key_Delete:
            if sel:
                put("")
            elif c < len(text):
                put("", c, c + 1)
            return True
        lay = text_ops.layout(s)
        k, col = text_ops.line_col(lay, c)
        moves = {Qt.Key_Left: lambda: max(0, c - 1),
                 Qt.Key_Right: lambda: min(len(text), c + 1),
                 Qt.Key_Up: lambda: text_ops.vertical(lay, c, -1),
                 Qt.Key_Down: lambda: text_ops.vertical(lay, c, 1),
                 Qt.Key_Home: lambda: lay.lines[k].start,
                 Qt.Key_End: lambda: lay.lines[k].start + len(lay.lines[k].text)}
        if key in moves:
            extend = bool(mods & Qt.ShiftModifier)
            if sel and not extend and key in (Qt.Key_Left, Qt.Key_Right):
                self._set_caret(a if key == Qt.Key_Left else b)   # collapse to that end
            else:
                self._set_caret(moves[key](), extend=extend)
            return True
        typed = ev.text()
        if typed and typed.isprintable():
            put(typed)
        return True

    # -- caret --
    def _clear_caret(self):
        for it in self._caret_items:
            try:
                self.scene.removeItem(it)
            except RuntimeError:
                pass
        self._caret_items = []

    def _blink(self):
        self._caret_on = not self._caret_on
        for it in self._caret_items:
            if it.data(2) == "caret":
                it.setVisible(self._caret_on and self.text_selection() is None)

    def _update_caret(self):
        self._clear_caret()
        self._text_items()
        self.viewport().update()

    def _text_items(self):
        """(Re)create the caret (hidden while characters are selected) and the selection
        highlight of the text being typed. Also called by _rebuild."""
        self._caret_items = []      # the scene clear in _rebuild already deleted them
        sid = self.editing_text_id()
        if sid is None:
            return
        s = self.doc.svgs[sid]
        lay = text_ops.layout(s)
        f = Xf.of(s).to_abs
        sel = self.text_selection()
        if sel is not None:
            brush = QtGui.QBrush(QtGui.QColor(*SELECT_RGBA))
            for x0, x1, base in text_ops.selection_spans(lay, *sel, newline_w=NEWLINE_W * lay.ascent):
                top, bot = base - lay.ascent, base + lay.descent
                k = lay.shear
                poly = QtGui.QPolygonF([QtCore.QPointF(*f(x + dx, y)) for x, dx, y in (
                    (x0, k * lay.ascent, top), (x1, k * lay.ascent, top),
                    (x1, -k * lay.descent, bot), (x0, -k * lay.descent, bot))])
                it = self.scene.addPolygon(poly, QtGui.QPen(QtCore.Qt.NoPen), brush)
                it.setZValue(CARET_Z - 1)
                it.setAcceptedMouseButtons(QtCore.Qt.NoButton)
                it.setData(2, "text_select")
                self._caret_items.append(it)
            return
        (x0, y0), (x1, y1) = text_ops.caret_line(lay, self._text_edit["caret"])
        pen = self._cosmetic_pen("#111111", 1.5)
        it = self.scene.addLine(*f(x0, y0), *f(x1, y1), pen)
        it.setZValue(CARET_Z)
        it.setAcceptedMouseButtons(QtCore.Qt.NoButton)
        it.setData(2, "caret")
        it.setVisible(self._caret_on)
        self._caret_items.append(it)
