"""Table of GCPs: use, name, model coordinates, known E / N / H (or Lon / Lat / h), residuals."""

from __future__ import annotations

import math

from ..core.gcp_io import Gcp
from .qt import QtCore, QtGui, QtWidgets, Signal

COLUMNS = ["Use", "Name", "Model X", "Model Y", "Model Z", "E", "N", "H", "dE", "dN", "dH", "d3D"]
COL_USE, COL_NAME = 0, 1
COL_OBJ = (2, 3, 4)
COL_KNOWN = (5, 6, 7)
COL_RES = (8, 9, 10, 11)

_OBJ_TIP = "Coordinates of the point in the model file (pick it on the model or type it)"
_RES_TIP = "Residual: known minus model, after the adjustment (or the check)"
TOOLTIPS = {
    "Use": "Use this GCP in the adjustment",
    "Model X": _OBJ_TIP, "Model Y": _OBJ_TIP, "Model Z": _OBJ_TIP,
    "E": "Known Easting in the project CRS",
    "N": "Known Northing in the project CRS",
    "H": "Known height (same height system as the model)",
    "Lon": "Known longitude, decimal degrees (east positive, west negative)",
    "Lat": "Known latitude, decimal degrees (north positive, south negative)",
    "h": "Known height (same height system as the model)",
    "dE": _RES_TIP, "dN": _RES_TIP, "dH": _RES_TIP, "d3D": _RES_TIP,
}


def _parse(text: str) -> float | None:
    value = str(text or "").strip().replace(" ", "")
    if not value:
        return None
    if value.count(",") == 1 and "." not in value:
        value = value.replace(",", ".")
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


class GcpTable(QtWidgets.QTableWidget):
    edited = Signal()

    def __init__(self, parent=None):
        super().__init__(0, len(COLUMNS), parent)
        self._filling = False
        self._known_decimals = 3
        self.setHorizontalHeaderLabels(COLUMNS)
        for column, name in enumerate(COLUMNS):
            if name in TOOLTIPS:
                self.horizontalHeaderItem(column).setToolTip(TOOLTIPS[name])
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.setMinimumHeight(170)
        self.itemChanged.connect(self._item_changed)

    # ------------------------------------------------------------------ rows
    def set_geographic(self, on: bool) -> None:
        """Known coordinates as Lon / Lat (degrees, 9 decimals) / h, or E / N / H (metres)."""
        names = ("Lon", "Lat", "h") if on else ("E", "N", "H")
        decimals = 9 if on else 3
        if decimals == self._known_decimals and self.horizontalHeaderItem(COL_KNOWN[0]) is not None \
                and self.horizontalHeaderItem(COL_KNOWN[0]).text() == names[0]:
            return
        self._known_decimals = decimals
        labels = list(COLUMNS)
        for column, name in zip(COL_KNOWN, names):
            labels[column] = name
        self.setHorizontalHeaderLabels(labels)
        for column, name in enumerate(labels):
            if name in TOOLTIPS and self.horizontalHeaderItem(column) is not None:
                self.horizontalHeaderItem(column).setToolTip(TOOLTIPS[name])
        self.set_gcps(self.gcps())         # the values again, with the decimals of the new units

    def _number_item(self, value: float | None, editable: bool = True, decimals: int = 3) -> QtWidgets.QTableWidgetItem:
        item = QtWidgets.QTableWidgetItem("" if value is None else f"{value:.{decimals}f}")
        item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        if not editable:
            item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            item.setForeground(QtGui.QBrush(QtGui.QColor(90, 105, 120)))
        return item

    def _write_row(self, row: int, gcp: Gcp) -> None:
        use = QtWidgets.QTableWidgetItem("")
        use.setFlags((use.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable) & ~QtCore.Qt.ItemFlag.ItemIsEditable)
        use.setCheckState(QtCore.Qt.CheckState.Checked if gcp.enabled else QtCore.Qt.CheckState.Unchecked)
        self.setItem(row, COL_USE, use)
        self.setItem(row, COL_NAME, QtWidgets.QTableWidgetItem(gcp.name))
        obj = gcp.obj or (None, None, None)
        known = gcp.known or (None, None, None)
        for column, value in zip(COL_OBJ, obj):
            self.setItem(row, column, self._number_item(value))
        for column, value in zip(COL_KNOWN, known):
            self.setItem(row, column, self._number_item(value, decimals=self._known_decimals))
        self._write_residual(row, gcp.residual)

    def _write_residual(self, row: int, residual) -> None:
        values = [None, None, None, None]
        if residual is not None:
            d3 = math.sqrt(sum(float(v) ** 2 for v in residual))
            values = [float(residual[0]), float(residual[1]), float(residual[2]), d3]
        for column, value in zip(COL_RES, values):
            self.setItem(row, column, self._number_item(value, editable=False))

    def set_gcps(self, gcps: list[Gcp]) -> None:
        self._filling = True
        try:
            self.setRowCount(0)
            for gcp in gcps:
                row = self.rowCount()
                self.insertRow(row)
                self._write_row(row, gcp)
        finally:
            self._filling = False
        self.edited.emit()

    def add_gcp(self, gcp: Gcp) -> int:
        self._filling = True
        try:
            row = self.rowCount()
            self.insertRow(row)
            self._write_row(row, gcp)
        finally:
            self._filling = False
        self.selectRow(row)
        self.edited.emit()
        return row

    def remove_selected(self) -> None:
        row = self.selected_row()
        if row >= 0:
            self.removeRow(row)
            self.edited.emit()

    def selected_row(self) -> int:
        rows = self.selectionModel().selectedRows() if self.selectionModel() is not None else []
        return rows[0].row() if rows else -1

    def next_name(self) -> str:
        names = {g.name for g in self.gcps()}
        number = self.rowCount() + 1
        while f"GCP{number}" in names:
            number += 1
        return f"GCP{number}"

    def set_obj(self, row: int, xyz) -> None:
        self._filling = True
        try:
            for column, value in zip(COL_OBJ, xyz):
                self.setItem(row, column, self._number_item(float(value)))
            self._write_residual(row, None)
        finally:
            self._filling = False
        self.edited.emit()

    def set_residuals(self, residuals: dict[int, tuple[float, float, float] | None]) -> None:
        """``{row: (dE, dN, dH)}``; the rows not given are cleared."""
        self._filling = True
        try:
            for row in range(self.rowCount()):
                self._write_residual(row, residuals.get(row))
        finally:
            self._filling = False

    # ------------------------------------------------------------------ reading
    def _cell(self, row: int, column: int) -> str:
        item = self.item(row, column)
        return item.text() if item is not None else ""

    def gcp_at(self, row: int) -> Gcp:
        use = self.item(row, COL_USE)
        enabled = use is None or use.checkState() == QtCore.Qt.CheckState.Checked
        obj = [_parse(self._cell(row, c)) for c in COL_OBJ]
        known = [_parse(self._cell(row, c)) for c in COL_KNOWN]
        residual = [_parse(self._cell(row, c)) for c in COL_RES[:3]]
        return Gcp(
            name=self._cell(row, COL_NAME).strip() or f"GCP{row + 1}",
            enabled=enabled,
            obj=tuple(obj) if all(v is not None for v in obj) else None,
            known=tuple(known) if all(v is not None for v in known) else None,
            residual=tuple(residual) if all(v is not None for v in residual) else None,
        )

    def gcps(self) -> list[Gcp]:
        return [self.gcp_at(row) for row in range(self.rowCount())]

    def _item_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        if self._filling:
            return
        if item.column() in COL_OBJ + COL_KNOWN:
            bad = bool(item.text().strip()) and _parse(item.text()) is None
            self._filling = True  # the colour change below must not come back here
            try:
                item.setForeground(QtGui.QBrush(QtGui.QColor(220, 40, 40) if bad else QtGui.QColor(23, 37, 49)))
                item.setToolTip("Not a number" if bad else "")
            finally:
                self._filling = False
        self.edited.emit()
