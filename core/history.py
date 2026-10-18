"""Undo / redo history: whole-document snapshots, detected at one sync point (no Qt here).

A state is (doc_json, selection): doc_json is json.dumps(doc.to_dict()) (to_dict hands out live
list references, so only a string is a safe immutable snapshot) and selection is an immutable
copy of the four selection sets.  The History keeps a *baseline* = the state the document was
in after the last commit. commit() compares the document with it, so the caller never has to
say what changed (or remember to call anything per kind of edit).

    h = History()
    h.reset(doc)             # after creating / loading a document
    ...edit the document...
    h.commit(doc)            # at the app's single refresh point: pushes an undo step if changed
    h.undo(doc)              # restore in place
    h.rebase(doc)            # once the UI refresh has finished

The step count is dynamic: `max_levels` can be changed at any time (a settings dialog) and
trims the stacks immediately.
"""
from __future__ import annotations
import json
import time

DEFAULT_UNDO_LEVELS = 64
MIN_UNDO_LEVELS = 1
MAX_UNDO_LEVELS = 10000
COALESCE_WINDOW_S = 1.0


def clamp_levels(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = DEFAULT_UNDO_LEVELS
    return max(MIN_UNDO_LEVELS, min(MAX_UNDO_LEVELS, n))


def doc_json(doc):
    return json.dumps(doc.to_dict(), sort_keys=True, default=str)


class History:
    def __init__(self, max_levels=DEFAULT_UNDO_LEVELS, clock=time.monotonic):
        self._max = clamp_levels(max_levels)
        self._clock = clock
        self._undo = []            # states before the baseline, oldest first
        self._redo = []            # states after the baseline, next redo last
        self._base = None          # (json, selection) of the document as last committed
        self._last_key = None      # coalescing key of the last pushing/merging commit
        self._last_time = 0.0

    # -- levels --
    @property
    def max_levels(self):
        return self._max

    @max_levels.setter
    def max_levels(self, n):
        self._max = clamp_levels(n)
        self._trim()

    def set_max_levels(self, n):
        self.max_levels = n
        return self._max

    def _trim(self):
        if len(self._undo) > self._max:
            del self._undo[:len(self._undo) - self._max]    # drop the oldest
        if len(self._redo) > self._max:
            del self._redo[:len(self._redo) - self._max]    # drop the furthest redo

    # -- state --
    @staticmethod
    def _state(doc):
        return (doc_json(doc), doc.selection_state())

    def _forget_coalescing(self):
        self._last_key = None
        self._last_time = 0.0

    def reset(self, doc):
        """Forget everything. The document as it is now is the starting point."""
        self._undo.clear()
        self._redo.clear()
        self._base = self._state(doc)
        self._forget_coalescing()

    def rebase(self, doc):
        """The baseline becomes the document as it is now, without an undo step (right after a
        restore, so whatever the refresh normalised is not mistaken for a new edit)."""
        self._base = self._state(doc)
        self._forget_coalescing()

    def commit(self, doc, coalesce_key=None, window_s=COALESCE_WINDOW_S):
        """Compare the document with the baseline.  Unchanged: only the stored selection is
        refreshed (selection alone is not an undo step) and False is returned.  Changed: the old
        baseline becomes an undo step (unless this commit merges into the previous one, see
        below), redo is cleared and True is returned.

        coalesce_key: a commit whose key equals the previous commit's key, less than window_s
        after it, replaces the baseline without pushing, so a held / repeated action (arrow
        nudges) becomes a single step."""
        if self._base is None:
            self.reset(doc)
            return False
        js, sel = self._state(doc)
        if js == self._base[0]:
            self._base = (js, sel)
            return False
        now = self._clock()
        merge = (coalesce_key is not None and coalesce_key == self._last_key
                 and now - self._last_time < window_s)
        pushed = False
        if not merge:
            self._undo.append(self._base)
            pushed = True
        self._redo.clear()
        self._base = (js, sel)
        self._last_key = coalesce_key
        self._last_time = now
        self._trim()
        return pushed

    # -- undo / redo --
    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    def undo_count(self):
        return len(self._undo)

    def redo_count(self):
        return len(self._redo)

    def _restore(self, doc, state):
        js, sel = state
        doc.restore_dict(json.loads(js))     # fresh objects every time: no sharing with the stack
        doc.set_selection_state(sel)

    def undo(self, doc):
        if not self._undo:
            return False
        target = self._undo.pop()
        self._redo.append(self._base)
        self._trim()
        self._base = target
        self._forget_coalescing()
        self._restore(doc, target)
        return True

    def redo(self, doc):
        if not self._redo:
            return False
        target = self._redo.pop()
        self._undo.append(self._base)
        self._trim()
        self._base = target
        self._forget_coalescing()
        self._restore(doc, target)
        return True
