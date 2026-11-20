"""Rasters & volumes page: GeoTIFF layers (orthomosaics, DEMs) and cut / fill volumes.

* GeoTIFF layers: import an orthomosaic or a DEM (shown in the plan view), send it to ROLAVI Viewer
  (an orthomosaic goes as JPG + .aux.xml, much smaller than the GeoTIFF), or save that zip.
* Volume: two DEMs (before -> after), or a DEM and a base elevation, in the whole common area or
  inside polygons (Shapefile, DXF, KML, KMZ, or drawn on the plan view). Cut in red, fill in green.
* Result: the volumes, the cut / fill layer in the plan view, and a CSV report.

The page works on the window's map layers (``host.map_layers``) and runs the long jobs with the
window's task runner, as the other pages do.
"""

from __future__ import annotations

from pathlib import Path

from ..core import volumes
from .layers import MAP_DEM, MAP_ORTHO, MAP_POLYGON, MAP_VOLUME
from .qt import QtWidgets
from .widgets import Segmented, card, field_label

MODE_DEMS = 0
MODE_LEVEL = 1


def _swatch(rgb: tuple[int, int, int], text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(f"  {text}  ")
    label.setStyleSheet(f"background: rgb({rgb[0]}, {rgb[1]}, {rgb[2]}); color: white; border-radius: 4px; "
                        "padding: 2px 6px; font-weight: 600;")
    return label


class VolumesPanel(QtWidgets.QWidget):
    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.result_key: int | None = None
        self._filling = False
        self._build()
        self._connect()
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        raster_card, raster_layout = card("GeoTIFF layers")
        self.import_raster_btn = QtWidgets.QPushButton("Import GeoTIFF…")
        self.import_raster_btn.setToolTip("An orthomosaic (RGB) or a DEM (one band of elevations), as GeoTIFF")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.import_raster_btn)
        row.addStretch(1)
        raster_layout.addLayout(row)
        raster_layout.addWidget(field_label("Layer"))
        self.raster_combo = QtWidgets.QComboBox()
        self.raster_combo.setAccessibleName("GeoTIFF layer")
        raster_layout.addWidget(self.raster_combo)
        self.raster_label = self.host._label("Import an orthomosaic or a DEM (GeoTIFF).", "Muted")
        raster_layout.addWidget(self.raster_label)
        actions = QtWidgets.QHBoxLayout()
        self.send_raster_btn = QtWidgets.QPushButton("Send to ROLAVI Viewer")
        self.send_raster_btn.setToolTip("An orthomosaic goes as JPG + .aux.xml (smaller); a DEM as GeoTIFF")
        self.save_jpg_btn = QtWidgets.QPushButton("Save JPG + .aux.xml…")
        self.save_jpg_btn.setToolTip("The orthomosaic as ROLAVI Viewer stores it: a .zip with the JPG and its "
                                     "georeference (.aux.xml)")
        actions.addWidget(self.send_raster_btn)
        actions.addWidget(self.save_jpg_btn)
        actions.addStretch(1)
        raster_layout.addLayout(actions)
        layout.addWidget(raster_card)

        volume_card, volume_layout = card("Volume")
        self.mode = Segmented()
        self.mode.addItem("Two DEMs", MODE_DEMS, "Before and after: cut where the ground went down, fill where "
                                                 "it went up")
        self.mode.addItem("DEM and elevation", MODE_LEVEL, "A DEM and a base / design elevation: cut above it, "
                                                           "fill below it")
        volume_layout.addWidget(self.mode)
        self.mode_stack = QtWidgets.QStackedWidget()
        dems = QtWidgets.QWidget()
        dems_layout = QtWidgets.QGridLayout(dems)
        dems_layout.setContentsMargins(0, 0, 0, 0)
        dems_layout.setHorizontalSpacing(8)
        dems_layout.addWidget(field_label("Before (reference)"), 0, 0)
        dems_layout.addWidget(field_label("After"), 0, 1)
        self.before_combo = QtWidgets.QComboBox()
        self.before_combo.setAccessibleName("DEM before")
        self.after_combo = QtWidgets.QComboBox()
        self.after_combo.setAccessibleName("DEM after")
        dems_layout.addWidget(self.before_combo, 1, 0)
        dems_layout.addWidget(self.after_combo, 1, 1)
        self.swap_btn = QtWidgets.QPushButton("⇄ Swap")
        self.swap_btn.setObjectName("Ghost")
        dems_layout.addWidget(self.swap_btn, 2, 1)
        level = QtWidgets.QWidget()
        level_layout = QtWidgets.QGridLayout(level)
        level_layout.setContentsMargins(0, 0, 0, 0)
        level_layout.setHorizontalSpacing(8)
        level_layout.addWidget(field_label("DEM"), 0, 0)
        level_layout.addWidget(field_label("Base elevation (m)"), 0, 1)
        self.dem_combo = QtWidgets.QComboBox()
        self.dem_combo.setAccessibleName("DEM")
        self.elevation_spin = QtWidgets.QDoubleSpinBox()
        self.elevation_spin.setRange(-1000.0, 9000.0)
        self.elevation_spin.setDecimals(3)
        self.elevation_spin.setSingleStep(0.1)
        self.elevation_spin.setAccessibleName("Base elevation")
        level_layout.addWidget(self.dem_combo, 1, 0)
        level_layout.addWidget(self.elevation_spin, 1, 1)
        self.mean_btn = QtWidgets.QPushButton("Mean of the DEM in the area")
        self.mean_btn.setObjectName("Ghost")
        self.mean_btn.setToolTip("Put the mean elevation of the DEM inside the area as the base elevation")
        level_layout.addWidget(self.mean_btn, 2, 1)
        self.mode_stack.addWidget(dems)
        self.mode_stack.addWidget(level)
        volume_layout.addWidget(self.mode_stack)

        volume_layout.addWidget(field_label("Area"))
        self.area_combo = QtWidgets.QComboBox()
        self.area_combo.setAccessibleName("Area")
        volume_layout.addWidget(self.area_combo)
        area_row = QtWidgets.QHBoxLayout()
        self.import_polygon_btn = QtWidgets.QPushButton("Import polygon…")
        self.import_polygon_btn.setToolTip("Shapefile (.shp or .zip), DXF, KML or KMZ")
        self.draw_btn = QtWidgets.QPushButton("Draw on the plan view")
        self.draw_btn.setCheckable(True)
        self.draw_btn.setToolTip("Click the vertices on the plan view; double click, right click or Enter closes it")
        area_row.addWidget(self.import_polygon_btn)
        area_row.addWidget(self.draw_btn)
        area_row.addStretch(1)
        volume_layout.addLayout(area_row)

        threshold_row = QtWidgets.QHBoxLayout()
        threshold_row.addWidget(field_label("Ignore changes smaller than (m)"))
        self.threshold_spin = QtWidgets.QDoubleSpinBox()
        self.threshold_spin.setRange(0.0, 100.0)
        self.threshold_spin.setDecimals(3)
        self.threshold_spin.setSingleStep(0.01)
        self.threshold_spin.setAccessibleName("Threshold")
        threshold_row.addWidget(self.threshold_spin)
        threshold_row.addStretch(1)
        volume_layout.addLayout(threshold_row)
        compute_row = QtWidgets.QHBoxLayout()
        self.volume_hint = self.host._label("", "Muted")
        compute_row.addWidget(self.volume_hint, 1)
        self.compute_btn = QtWidgets.QPushButton("Compute volumes")
        self.compute_btn.setObjectName("Primary")
        compute_row.addWidget(self.compute_btn)
        volume_layout.addLayout(compute_row)
        layout.addWidget(volume_card)

        result_card, result_layout = card("Result")
        legend = QtWidgets.QHBoxLayout()
        legend.addWidget(_swatch(volumes.CUT_RGB, "Cut"))
        legend.addWidget(_swatch(volumes.FILL_RGB, "Fill"))
        legend.addStretch(1)
        result_layout.addLayout(legend)
        self.result_label = self.host._label("No volume computed yet.", "Muted")
        result_layout.addWidget(self.result_label)
        report_row = QtWidgets.QHBoxLayout()
        report_row.addStretch(1)
        self.report_btn = QtWidgets.QPushButton("Save report (CSV)…")
        report_row.addWidget(self.report_btn)
        result_layout.addLayout(report_row)
        layout.addWidget(result_card)

    def _connect(self) -> None:
        host = self.host
        self.import_raster_btn.clicked.connect(lambda *_: host.open_raster())
        self.raster_combo.currentIndexChanged.connect(self._raster_changed)
        self.send_raster_btn.clicked.connect(self.send_raster)
        self.save_jpg_btn.clicked.connect(self.save_jpg)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.swap_btn.clicked.connect(self.swap)
        self.mean_btn.clicked.connect(self.use_mean)
        self.area_combo.currentIndexChanged.connect(self._area_changed)
        self.before_combo.currentIndexChanged.connect(self._inputs_changed)
        self.after_combo.currentIndexChanged.connect(self._inputs_changed)
        self.dem_combo.currentIndexChanged.connect(self._inputs_changed)
        self.import_polygon_btn.clicked.connect(lambda *_: host.open_polygon())
        self.draw_btn.toggled.connect(self._draw_toggled)
        self.compute_btn.clicked.connect(self.compute)
        self.report_btn.clicked.connect(self.save_report)

    # ------------------------------------------------------------------ state
    def _layers(self, *kinds) -> list:
        return [layer for layer in self.host.map_layers if layer.kind in kinds]

    def _layer(self, combo: QtWidgets.QComboBox):
        key = combo.currentData()
        return next((layer for layer in self.host.map_layers if layer.key == key), None)

    @staticmethod
    def _fill(combo: QtWidgets.QComboBox, items: list[tuple[str, object]], keep, fallback: int = 0) -> None:
        combo.clear()
        for text, data in items:
            combo.addItem(text, data)
        index = combo.findData(keep) if keep is not None else -1
        if index < 0 and combo.count():
            index = min(fallback, combo.count() - 1)
        if index >= 0:
            combo.setCurrentIndex(index)

    def refresh(self) -> None:
        """Lists and buttons from the map layers of the window."""
        self._filling = True
        try:
            rasters = self._layers(MAP_ORTHO, MAP_DEM)
            dems = self._layers(MAP_DEM)
            polygons = self._layers(MAP_POLYGON)
            self._fill(self.raster_combo, [(f"{l.name}  ·  {'DEM' if l.kind == MAP_DEM else 'Orthomosaic'}", l.key)
                                           for l in rasters], self.raster_combo.currentData())
            dem_items = [(l.name, l.key) for l in dems]
            self._fill(self.before_combo, dem_items, self.before_combo.currentData(), 0)
            self._fill(self.after_combo, dem_items, self.after_combo.currentData(), 1)
            self._fill(self.dem_combo, dem_items, self.dem_combo.currentData(), 0)
            self._fill(self.area_combo, [("Whole area (all the cells in common)", None)]
                       + [(f"Polygon: {l.name}  ·  {l.data.describe()}", l.key) for l in polygons],
                       self.area_combo.currentData())
        finally:
            self._filling = False
        self._raster_changed()
        self._update_state()

    def select_raster(self, key: int) -> None:
        index = self.raster_combo.findData(key)
        if index >= 0:
            self.raster_combo.setCurrentIndex(index)

    def select_polygon(self, key: int) -> None:
        index = self.area_combo.findData(key)
        if index >= 0:
            self.area_combo.setCurrentIndex(index)

    def used_polygon(self) -> int | None:
        return self.area_combo.currentData()

    def _raster_changed(self, *_args) -> None:
        layer = self._layer(self.raster_combo)
        if layer is None:
            self.host._set_label(self.raster_label, "Import an orthomosaic or a DEM (GeoTIFF).", "Muted")
        else:
            info = layer.data if layer.kind == MAP_ORTHO else layer.data.info
            text = info.describe()
            if layer.kind == MAP_DEM:
                text += "\n" + layer.data.describe()
            if info.crs_source:
                text += f"\nCRS from: {info.crs_source}"
            self.host._set_label(self.raster_label, text, "Info")
        self._update_state()

    def _mode_changed(self, index: int) -> None:
        self.mode_stack.setCurrentIndex(int(index))
        self._update_state()

    def _area_changed(self, *_args) -> None:
        if not self._filling:
            self.host._refresh_map_view()
        self._update_state()

    def _inputs_changed(self, *_args) -> None:
        if not self._filling:
            self._update_state()

    def _blocker(self) -> str:
        dems = self._layers(MAP_DEM)
        if self.mode.currentIndex() == MODE_DEMS:
            if len(dems) < 2:
                return "Import two DEMs (GeoTIFF): before and after."
            if self.before_combo.currentData() == self.after_combo.currentData():
                return "Choose two different DEMs."
        elif not dems:
            return "Import a DEM (GeoTIFF)."
        if self.host.backend is None:
            return self.host.backend_error or "No coordinate engine."
        return ""

    def _update_state(self) -> None:
        busy = self.host.task is not None
        layer = self._layer(self.raster_combo)
        self.send_raster_btn.setEnabled(not busy and layer is not None)
        self.save_jpg_btn.setEnabled(not busy and layer is not None and layer.kind == MAP_ORTHO)
        blocker = self._blocker()
        self.compute_btn.setEnabled(not busy and not blocker)
        self.mean_btn.setEnabled(not busy and self._layer(self.dem_combo) is not None)
        self.swap_btn.setEnabled(not busy and self.after_combo.count() > 1)
        for widget in (self.import_raster_btn, self.import_polygon_btn, self.draw_btn, self.mode,
                       self.before_combo, self.after_combo, self.dem_combo, self.area_combo,
                       self.elevation_spin, self.threshold_spin):
            widget.setEnabled(not busy)
        result = self._result()
        self.report_btn.setEnabled(result is not None)
        if blocker:
            self.host._set_label(self.volume_hint, blocker, "Muted")
        elif self.mode.currentIndex() == MODE_DEMS:
            self.host._set_label(self.volume_hint, "dz = after − before: cut where the ground went down, fill "
                                 "where it went up.", "Muted")
        else:
            self.host._set_label(self.volume_hint, "dz = base elevation − DEM: cut above the base, fill below it. "
                                 "Use the same height system as the DEM.", "Muted")

    def _result(self):
        layer = next((l for l in self.host.map_layers if l.key == self.result_key and l.kind == MAP_VOLUME), None)
        return layer.data if layer is not None else None

    # ------------------------------------------------------------------ actions
    def swap(self, *_args) -> None:
        before, after = self.before_combo.currentIndex(), self.after_combo.currentIndex()
        self.before_combo.setCurrentIndex(after)
        self.after_combo.setCurrentIndex(before)

    def _draw_toggled(self, on: bool) -> None:
        if self._filling:
            return
        if on:
            self.host.start_drawing()
        else:
            self.host.plan.cancel_drawing()

    def drawing_changed(self, on: bool) -> None:
        """The plan view started / stopped drawing (the button follows it)."""
        self._filling = True
        try:
            self.draw_btn.setChecked(bool(on))
        finally:
            self._filling = False

    def _area(self):
        layer = self._layer(self.area_combo)
        return layer.data if layer is not None else None

    def use_mean(self, *_args) -> None:
        layer = self._layer(self.dem_combo)
        if layer is None:
            return
        try:
            mean, cells = volumes.mean_elevation(layer.data, self._area(), self.host.backend)
        except (volumes.VolumeError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, self.host.windowTitle(), str(exc))
            return
        self.elevation_spin.setValue(round(mean, 3))
        area = self.area_combo.currentText().split("  ·  ")[0]
        self.host.log(f"   Mean elevation of {layer.name} in {area.lower()}: {mean:,.3f} m ({cells:,} cells)")

    def compute(self, *_args) -> None:
        blocker = self._blocker()
        if blocker or self.host.task is not None:
            return
        backend = self.host.backend
        polygons = self._area()
        threshold = float(self.threshold_spin.value())
        if self.mode.currentIndex() == MODE_DEMS:
            before, after = self._layer(self.before_combo).data, self._layer(self.after_combo).data
            elevation = None
            title = f"Volumes: {before.name} → {after.name}"
        else:
            before, after = self._layer(self.dem_combo).data, None
            elevation = float(self.elevation_spin.value())
            title = f"Volumes: {before.name} vs elevation {elevation:,.3f} m"

        def job(progress, _log):
            return volumes.compare(before, after, elevation, polygons, threshold, backend, progress)

        self.host._start_task(title, job, self._computed)

    def _computed(self, result: volumes.VolumeResult) -> None:
        self.result_key = self.host.add_volume_layer(result, replace=self.result_key)
        lines = result.lines()
        self.host._set_label(self.result_label, "\n".join(lines), "Info")
        self.host.log(f"✅ {result.title()}" + (f" · in {result.polygon}" if result.polygon else ""))
        for line in lines:
            self.host.log(f"   {line}")
        for note in result.notes:
            self.host.log(f"   ⚠ {note}")
        self._update_state()

    def save_report(self, *_args) -> None:
        result = self._result()
        if result is None:
            return
        start = str(Path(self.host.settings.value("last_dir", str(Path.home()), type=str) or Path.home())
                    / "volumes.csv")
        path, _f = QtWidgets.QFileDialog.getSaveFileName(self, "Save the volume report", start, "CSV (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            Path(path).write_text(result.csv_text(), encoding="utf-8")
        except OSError as exc:
            QtWidgets.QMessageBox.critical(self, self.host.windowTitle(), f"Could not save the report: {exc}")
            return
        self.host.log(f"💾 Volume report: {path}")

    def send_raster(self, *_args) -> None:
        layer = self._layer(self.raster_combo)
        if layer is None:
            return
        info = layer.data if layer.kind == MAP_ORTHO else layer.data.info
        self.host.go_step(self.host.step_viewer)
        self.host.viewer_panel.send_raster(str(info.path), info)

    def save_jpg(self, *_args) -> None:
        layer = self._layer(self.raster_combo)
        if layer is None or layer.kind != MAP_ORTHO:
            return
        info = layer.data
        start = str(info.path.with_suffix(".zip"))
        path, _f = QtWidgets.QFileDialog.getSaveFileName(self, "Save the orthomosaic as JPG + .aux.xml", start,
                                                         "Zip (*.zip)")
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        self.host.convert_ortho(info, path)
