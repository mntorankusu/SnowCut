"""Mini thickness->pressure curve plot (Qt only, no matplotlib)."""
from PySide6 import QtWidgets, QtGui, QtCore
from .icons import icon_button


class CurvePlot(QtWidgets.QWidget):
    def __init__(self, get_curve, parent=None):
        super().__init__(parent)
        self.get_curve = get_curve
        self.setMinimumHeight(140)

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#1e1e1e"))
        curve = sorted(self.get_curve() or [], key=lambda q: q[0])
        if len(curve) < 1:
            p.setPen(QtGui.QColor("#888"))
            p.drawText(self.rect(), QtCore.Qt.AlignCenter, "No points")
            return
        ts = [t for t, _ in curve]
        tmin, tmax = min(ts + [0.0]), max(ts + [2.0])
        if tmax == tmin:
            tmax = tmin + 1
        W, H = self.width() - 16, self.height() - 16
        def X(t):
            return 8 + (t - tmin) / (tmax - tmin) * W
        def Y(pr):
            return 8 + (9 - pr) / 18 * H  # -9 bottom .. 9 top
        # grid at integer pressures
        p.setPen(QtGui.QColor("#333"))
        for pr in range(-9, 10):
            y = Y(pr)
            p.drawLine(8, int(y), 8 + W, int(y))
        p.setPen(QtGui.QPen(QtGui.QColor("#4fc3f7"), 2))
        path = QtGui.QPainterPath()
        for i, (t, pr) in enumerate(curve):
            x, y = X(t), Y(pr)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        p.drawPath(path)
        p.setPen(QtGui.QPen(QtGui.QColor("#ffd54f"), 2))
        br = QtGui.QBrush(QtGui.QColor("#ffd54f"))
        for t, pr in curve:
            p.setBrush(br)
            p.drawEllipse(QtCore.QPointF(X(t), Y(pr)), 4, 4)
        p.setBrush(QtCore.Qt.NoBrush)


class CurveEditor(QtWidgets.QWidget):
    changed = QtCore.Signal()

    def __init__(self, get_curve, set_curve, parent=None):
        super().__init__(parent)
        self.get_curve = get_curve
        self.set_curve = set_curve
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = CurvePlot(get_curve)
        lay.addWidget(self.plot)
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Thickness (mm)", "Pressure"])
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setToolTip("Line thickness and the cutting pressure (\u22129 to 9) used for it")
        lay.addWidget(self.table)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.add_btn = icon_button("plus", "Add point", px=32, scale=0.55)
        self.del_btn = icon_button("minus", "Remove selected point", px=32, scale=0.55)
        row.addWidget(self.add_btn)
        row.addWidget(self.del_btn)
        lay.addLayout(row)
        self.add_btn.clicked.connect(self._add)
        self.del_btn.clicked.connect(self._del)
        self.table.cellChanged.connect(self._cell)
        self._loading = False
        self.refresh()

    def refresh(self):
        self._loading = True
        curve = sorted(self.get_curve() or [], key=lambda q: q[0])
        self.table.setRowCount(len(curve))
        for i, (t, pr) in enumerate(curve):
            self.table.setItem(i, 0, QtWidgets.QTableWidgetItem("%.3g" % t))
            self.table.setItem(i, 1, QtWidgets.QTableWidgetItem("%d" % int(pr)))
        rows = max(2, min(5, len(curve)))
        hh = self.table.horizontalHeader().sizeHint().height() or 24
        rh = self.table.rowHeight(0) if len(curve) > 0 else 24
        self.table.setFixedHeight(int(2 * self.table.frameWidth() + hh + rows * rh + 4))
        self.plot.update()
        self._loading = False

    def _add(self):
        c = list(self.get_curve() or [(0.0, -9), (2.0, 9)])
        c.append((c[-1][0] + 0.5 if c else 1.0, 0))
        self.set_curve(sorted(c))
        self.refresh()
        self.changed.emit()

    def _del(self):
        r = self.table.currentRow()
        c = list(self.get_curve() or [])
        if 0 <= r < len(c) and len(c) > 1:
            c.pop(r)
            self.set_curve(sorted(c))
            self.refresh()
            self.changed.emit()

    def _cell(self, r, col):
        if self._loading:
            return
        c = list(self.get_curve() or [])
        if not (0 <= r < len(c)):
            return
        try:
            v = float(self.table.item(r, col).text())
        except (ValueError, AttributeError):
            self.refresh()
            return
        t, pr = c[r]
        if col == 0:
            t = v
        else:
            pr = max(-9, min(9, int(round(v))))
        c[r] = (t, pr)
        self.set_curve(sorted(c))
        self.plot.update()
        self.changed.emit()
