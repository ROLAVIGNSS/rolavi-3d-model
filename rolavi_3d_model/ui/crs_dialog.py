"""Coordinate reference system search (as the CRS selector of QGIS).

Type a name, an EPSG code or an area ("UTM 18S", "Peru96", "WGS 84", "4326", "Lima") and pick
a projected CRS (metres) or a geographic one (degrees). With an empty search it lists the CRS
used last and the common ones. Without pyproj's database a code can still be typed.
"""

from __future__ import annotations

from ..core import crs as crs_mod
from ..core.crs import CrsBackend, CrsEntry, CrsError
from .qt import QtCore, QtWidgets
from .theme import STYLE
from .widgets import Segmented

KIND_FILTERS = (("All", ("projected", "geographic")), ("Projected (metres)", ("projected",)),
                ("Geographic (degrees)", ("geographic",)))
KIND_LABELS = {"projected": "Projected", "geographic": "Geographic"}


class CrsDialog(QtWidgets.QDialog):
    def __init__(self, backend: CrsBackend | None, text: str = "", recent: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.recent = list(recent or [])
        self.chosen = ""
        self._entries: list[CrsEntry] = []
        self.setWindowTitle("Coordinate reference system")
        self.resize(820, 560)
        root = QtWidgets.QWidget(self)
        root.setObjectName("RolaviRoot")
        root.setStyleSheet(STYLE)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        title = QtWidgets.QLabel("Search the coordinate reference system")
        title.setObjectName("CardTitle")
        layout.addWidget(title)
        self.search = QtWidgets.QLineEdit(text)
        self.search.setPlaceholderText("Name, EPSG code or area, e.g. UTM 18S, Peru96, WGS 84, 4326, Lima")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.kind = Segmented()
        for label, kinds in KIND_FILTERS:
            self.kind.addItem(label, kinds)
        layout.addWidget(self.kind)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Code", "Name", "Type", "Area of use"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.note = QtWidgets.QLabel("")
        self.note.setObjectName("FieldLabel")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setObjectName("Ghost")
        self.use_btn = QtWidgets.QPushButton("Use this CRS")
        self.use_btn.setObjectName("Primary")
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.use_btn)
        layout.addLayout(buttons)

        self.search.textChanged.connect(self.refresh)
        self.kind.currentIndexChanged.connect(self.refresh)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellDoubleClicked.connect(lambda *_: self._use())
        self.use_btn.clicked.connect(self._use)
        self.cancel_btn.clicked.connect(self.reject)
        self.refresh()
        self.search.setFocus()

    # ------------------------------------------------------------------ results
    def _kinds(self) -> tuple:
        return self.kind.currentData() or ("projected", "geographic")

    def _known(self, codes: list[str]) -> list[CrsEntry]:
        """Entries of given codes, with the name the CRS library gives them."""
        common = dict(crs_mod.COMMON_CRS)
        out, seen = [], set()
        for code in codes:
            code = crs_mod.normalize(code)
            if not code or code in seen:
                continue
            seen.add(code)
            name, kind = common.get(code, code), "projected"
            if self.backend is not None:
                try:
                    info = self.backend.info(code)
                    name, kind = info.name, "projected" if info.projected else "geographic"
                except CrsError:
                    continue
            out.append(CrsEntry(code, name, kind, "used before" if code in self.recent else ""))
        return out

    def refresh(self, *_args) -> None:
        text = self.search.text().strip()
        kinds = self._kinds()
        if not text:
            entries = self._known(self.recent + [code for code, _name in crs_mod.COMMON_CRS])
            entries = [e for e in entries if e.kind in kinds]
            note = "Used before and common CRS. Type to search every CRS of the EPSG database."
        else:
            entries = self.backend.search(text, kinds) if self.backend is not None else []
            if not entries and not crs_mod.is_query(text):
                entries = [e for e in self._known([text]) if e.kind in kinds]   # a code typed by hand
            if entries:
                note = f"{len(entries)} found" + (" (the first 300)" if len(entries) >= 300 else "") + \
                       ". Projected = metres; geographic = latitude / longitude in degrees."
            elif self.backend is not None and not self.backend.search("4326"):
                note = "This Python has no CRS database to search: type the EPSG code (e.g. 32718)."
            else:
                note = "Nothing found: try fewer words, e.g. 'UTM 18S' or 'Peru96'."
        self._fill(entries)
        self.note.setText(note)

    def _fill(self, entries: list[CrsEntry]) -> None:
        self._entries = entries
        self.table.setRowCount(0)
        for row, entry in enumerate(entries):
            self.table.insertRow(row)
            for column, text in enumerate((entry.code, entry.name, KIND_LABELS.get(entry.kind, entry.kind),
                                           entry.area)):
                item = QtWidgets.QTableWidgetItem(text)
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, column, item)
        if entries:
            self.table.selectRow(0)
        self._selection_changed()

    def _selected(self) -> CrsEntry | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() is not None else []
        row = rows[0].row() if rows else -1
        return self._entries[row] if 0 <= row < len(self._entries) else None

    def _selection_changed(self) -> None:
        self.use_btn.setEnabled(self._selected() is not None)

    def _use(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        self.chosen = entry.code
        self.accept()
