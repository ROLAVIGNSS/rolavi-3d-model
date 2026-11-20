"""The CRS of a file that does not say it: a DXF always, a shapefile without .prj, a GeoTIFF without
CRS. The user chooses the datum (and so the ellipsoid) and the UTM zone; "Other CRS…" opens the full
search for anything else (a local projection, MAGNA-SIRGAS Origen-Nacional, ...).
"""

from __future__ import annotations

from ..core import crs as crs_mod
from ..core.crs import CrsBackend, CrsError
from .app_icon import app_settings
from .crs_dialog import CrsDialog
from .qt import QtCore, QtWidgets, exec_
from .theme import STYLE
from .widgets import field_label


def utm_parts(code: str) -> tuple[str, int, bool] | None:
    """(datum, zone, south) of a UTM code of the datums offered, e.g. EPSG:24878 -> ('PSAD56', 18, True)."""
    number = crs_mod.epsg_number(code)
    if number is None:
        return None
    if 32601 <= number <= 32660:
        return "WGS84", number - 32600, False
    if 32701 <= number <= 32760:
        return "WGS84", number - 32700, True
    if 24877 <= number <= 24882:
        return "PSAD56", number - 24860, True
    if 24817 <= number <= 24821:
        return "PSAD56", number - 24800, False
    if 31977 <= number <= 31985:
        return "SIRGAS2000", number - 31960, True
    if 31965 <= number <= 31976:
        return "SIRGAS2000", number - 31954, False
    peru = {5839: 17, 5387: 18, 5389: 19}
    if number in peru:
        return "PERU96", peru[number], True
    return None


class DatumDialog(QtWidgets.QDialog):
    """Datum (ellipsoid) + UTM zone + hemisphere -> ``chosen`` (an EPSG code)."""

    def __init__(self, backend: CrsBackend | None, file_name: str, notes: list[str] | None = None,
                 default: str = "", recent: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.recent = list(recent or [])
        self.chosen = ""
        self._other = ""
        self.setWindowTitle("Coordinate system of the file")
        self.resize(560, 360)
        root = QtWidgets.QWidget(self)
        root.setObjectName("RolaviRoot")
        root.setStyleSheet(STYLE)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        title = QtWidgets.QLabel(f"In which coordinates is {file_name}?")
        title.setObjectName("CardTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        hint = QtWidgets.QLabel("The file does not store its coordinate system. Choose the datum (it sets the "
                                "ellipsoid) and the UTM zone the drawing was made in.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        for note in notes or []:
            if "does not store its CRS" in note or "choose its CRS" in note:
                continue
            label = QtWidgets.QLabel("• " + note)
            label.setObjectName("Muted")
            label.setWordWrap(True)
            layout.addWidget(label)
        layout.addWidget(field_label("Datum (ellipsoid)"))
        self.datum_combo = QtWidgets.QComboBox()
        for key, label in crs_mod.DATUMS:
            self.datum_combo.addItem(label, key)
        layout.addWidget(self.datum_combo)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(field_label("UTM zone"))
        self.zone_spin = QtWidgets.QSpinBox()
        self.zone_spin.setRange(1, 60)
        self.zone_spin.setValue(18)
        self.zone_spin.setAccessibleName("UTM zone")
        row.addWidget(self.zone_spin)
        self.hemi_combo = QtWidgets.QComboBox()
        self.hemi_combo.addItem("South", True)
        self.hemi_combo.addItem("North", False)
        self.hemi_combo.setAccessibleName("Hemisphere")
        row.addWidget(self.hemi_combo)
        row.addStretch(1)
        layout.addLayout(row)
        self.result_label = QtWidgets.QLabel("")
        self.result_label.setObjectName("Info")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        layout.addStretch(1)
        buttons = QtWidgets.QHBoxLayout()
        self.other_btn = QtWidgets.QPushButton("Other CRS…")
        self.other_btn.setToolTip("Search any coordinate system (name, EPSG code or area)")
        buttons.addWidget(self.other_btn)
        buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel")
        self.ok_btn = QtWidgets.QPushButton("Use this CRS")
        self.ok_btn.setObjectName("Primary")
        buttons.addWidget(cancel)
        buttons.addWidget(self.ok_btn)
        layout.addLayout(buttons)

        parts = utm_parts(default) if default else None
        if parts is not None:
            index = self.datum_combo.findData(parts[0])
            if index >= 0:
                self.datum_combo.setCurrentIndex(index)
            self.zone_spin.setValue(parts[1])
            self.hemi_combo.setCurrentIndex(0 if parts[2] else 1)
        self.datum_combo.currentIndexChanged.connect(self._changed)
        self.zone_spin.valueChanged.connect(self._changed)
        self.hemi_combo.currentIndexChanged.connect(self._changed)
        self.other_btn.clicked.connect(self._search)
        cancel.clicked.connect(self.reject)
        self.ok_btn.clicked.connect(self._accept)
        self._changed()

    def code(self) -> str:
        """The EPSG code of the choice ('' when the datum has no such zone)."""
        if self._other:
            return self._other
        try:
            return crs_mod.datum_utm_code(self.datum_combo.currentData(), self.zone_spin.value(),
                                          bool(self.hemi_combo.currentData()))
        except CrsError:
            return ""

    def _changed(self, *_args) -> None:
        self._other = ""
        try:
            code = crs_mod.datum_utm_code(self.datum_combo.currentData(), self.zone_spin.value(),
                                          bool(self.hemi_combo.currentData()))
        except CrsError as exc:
            self.result_label.setText(str(exc))
            self.ok_btn.setEnabled(False)
            return
        self._show(code)

    def _show(self, code: str) -> None:
        name = ""
        if self.backend is not None:
            try:
                name = self.backend.info(code).name
            except CrsError as exc:
                self.result_label.setText(str(exc))
                self.ok_btn.setEnabled(False)
                return
        self.result_label.setText(f"{code}" + (f" · {name}" if name else ""))
        self.ok_btn.setEnabled(True)

    def _search(self, *_args) -> None:
        dialog = CrsDialog(self.backend, "", self.recent, self)
        if exec_(dialog) and dialog.chosen:
            self._other = dialog.chosen
            self._show(dialog.chosen)

    def _accept(self, *_args) -> None:
        self.chosen = self.code()
        if self.chosen:
            self.accept()


def ask_crs(parent, backend: CrsBackend | None, file_name: str, notes: list[str] | None, default: str,
            recent: list[str] | None = None) -> str:
    """Show the dialog; the EPSG code chosen, or '' when cancelled."""
    settings = app_settings()
    last = str(settings.value("file_crs", "", type=str) or "")
    dialog = DatumDialog(backend, file_name, notes, last or default, recent, parent)
    if not exec_(dialog) or not dialog.chosen:
        return ""
    settings.setValue("file_crs", dialog.chosen)
    return dialog.chosen
