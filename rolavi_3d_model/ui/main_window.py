"""Main window of ROLAVI 3D Model.

Title bar (logo, project, ROLAVI Viewer account), workflow rail (left), one page per
step (centre), top view + log (right), status bar (progress, Cancel, coordinates):

1. Model    - import the model: a textured OBJ, or a PLY (mesh, point cloud or 3D Gaussian
              splats, e.g. from ROLAVIScan, placed with its georeference); say how its
              coordinates are (georeferenced / local, up axis, offset), its CRS and heights.
2. GCPs     - optional: known points, picked on the model or typed; Check fit
              (how far the model is from them) or Solve (adjust the model).
3. View     - the model in the project CRS; a LAS preview to check it in QGIS.
4. Export   - the SLPK in WGS84 (IntegratedMesh I3S 1.7 for a mesh, PointCloud I3S 2.0 for a
              point cloud or the centres of the splats), as ROLAVI Viewer reads.
5. Rasters  - optional: GeoTIFF orthomosaics and DEMs in the plan view; cut / fill volumes between
              two DEMs or a DEM and a base elevation, inside polygons (``volumes_panel``).
6. Viewer   - sign in with the ROLAVI Viewer account and send the SLPK, a DEM, an orthomosaic
              (as JPG + .aux.xml) or a vector layer to its space (``viewer_panel``).

Standalone in VS Code now (``run_dev.py``); the QGIS plugin will open this same
window with a QGIS coordinate backend and add the layers to the project.
"""

from __future__ import annotations

import json
import math
import sys
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np

from .. import __version__
from ..core import crs as crs_mod
from ..core import gcp_io, georef, las_preview, pipeline, raster_io, scan_io, tiles, vector_io, volumes
from ..core.crs import CrsBackend, CrsError, CrsInfo
from ..core.gcp_io import Gcp
from ..core.geoid import GeoidError, GtxGeoid
from ..core.georef import CrsFrame, GcpSolution, GeorefError, ModelFrame, Similarity
from ..core.scan_io import KIND_MESH, KIND_SPLATS, ScanFrame
from .basemap import BasemapLayer
from .app_icon import app_icon, app_settings, set_windows_app_id
from .crs_dialog import CrsDialog
from .datum_dialog import ask_crs
from .layers import MAP_DEM, MAP_ORTHO, MAP_POLYGON, MAP_VOLUME, Layer, LayersPanel, MapLayer
from .gcp_table import GcpTable
from .plan_view import PlanView
from .qt import QtCore, QtGui, QtWidgets, exec_
from . import view3d as view3d_mod
from .theme import STATUS_STYLE, STYLE
from .viewer_panel import ViewerPanel
from .volumes_panel import VolumesPanel
from .widgets import RAIL_WIDTH, Segmented, StepItem, card, field_label, logo_label
from .worker import TaskThread

APP_TITLE = "ROLAVI 3D Model"
APP_PRODUCT = "3D Model"
STEP_TITLES = ["Model", "GCP adjustment", "Review in CRS", "Export SLPK", "Rasters & volumes", "ROLAVI Viewer"]
STEP_RASTERS = 5
STEP_VIEWER = 6
COORDS_GEOREF, COORDS_LOCAL = 0, 1
PREVIEW_POINTS = 400_000
GEOREF_COORDINATE = 20_000.0   # |coordinate| above this: the OBJ is most likely in a projected CRS
RASTER_FILTER = "GeoTIFF: orthomosaic or DEM (*.tif *.tiff)"
POLYGON_FILTER = ("Polygons (*.shp *.zip *.dxf *.kml *.kmz);;Shapefile (*.shp *.zip);;DXF (*.dxf);;"
                  "KML / KMZ (*.kml *.kmz)")
MAP_ORDER = {MAP_ORTHO: 0, MAP_DEM: 0, MAP_VOLUME: 1}     # the last raster on top; cut / fill over them
MODEL_FILTER = ("3D models (*.obj *.ply);;OBJ mesh (*.obj);;PLY: mesh, point cloud or 3DGS splats (*.ply);;"
                "All files (*)")
TEXTURE_CHOICES = [(1024, "1024 px (light)"), (2048, "2048 px (default)"), (4096, "4096 px (sharper, heavier)")]



def _adjustment_key(adjustment: Similarity | None):
    """A GCP adjustment as plain numbers (to tell when a layer must be drawn again)."""
    if adjustment is None:
        return None
    return [float(adjustment.scale), *np.asarray(adjustment.rotation, dtype=float).ravel().tolist(),
            *np.asarray(adjustment.translation, dtype=float).ravel().tolist()]

def _number(text: str) -> float | None:
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


def _dot_icon(color: str) -> QtGui.QIcon:
    """A small filled circle (connected / not connected)."""
    pixmap = QtGui.QPixmap(20, 20)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QColor(color))
    painter.drawEllipse(4, 4, 12, 12)
    painter.end()
    return QtGui.QIcon(pixmap)


def _duration(seconds: float) -> str:
    seconds = int(max(0, round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, backend: CrsBackend | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(app_icon())        # also inside QGIS, where run() is not used
        self.resize(1440, 900)
        self.settings = app_settings()
        self.backend = backend
        self.backend_error = ""
        if self.backend is None:
            try:
                self.backend = crs_mod.default_backend()
            except CrsError as exc:
                self.backend_error = str(exc)
        self.model: pipeline.LoadedModel | None = None
        self.world: np.ndarray | None = None          # preview points in the displayed frame
        self.solution: GcpSolution | None = None
        self.solution_key: str | None = None          # frame + CRS the solution belongs to
        self.solution_stale = False
        self.crs_info: CrsInfo | None = None           # the CRS the window works in (projected, metres)
        self.user_crs: CrsInfo | None = None           # the CRS chosen (may be geographic: degrees)
        self._zone_code = ""                           # UTM zone worked in for a geographic CRS
        self.task: TaskThread | None = None
        self._threads: list[TaskThread] = []
        self._task_started = 0.0
        self._updating = False
        self._shown_frame_key = ""
        self._exported_file = ""
        self.geoid: GtxGeoid | None = None
        self.geoid_error = ""
        self.layers: list[Layer] = []    # every model imported; the active one is self.model
        self.active: Layer | None = None
        self._layer_seq = 0
        self.map_layers: list[MapLayer] = []   # orthomosaics, DEMs, polygons, cut / fill
        self.selected_map: int | None = None   # map layer selected in the list (the active model stays)
        self.step_viewer = STEP_VIEWER
        self._pending_reset = True       # the 3D view is fitted when the layers being built are ready
        self.view3d: Any = None                               # view3d.View3D, made when 3D is opened
        self._ui_ready = False
        self._build_ui()
        self._connect()
        self._restore_settings()
        self._update_crs_info()
        self._refresh_all(keep_view=False)
        self.log(f"{APP_TITLE} ready.")
        if self.backend_error:
            self.log(f"❌ {self.backend_error}")
        else:
            self.log(f"   Coordinate engine: {self.backend.name}")

    # ================================================================== UI
    @staticmethod
    def _label(text: str = "", kind: str = "Muted") -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setObjectName(kind)
        label.setWordWrap(True)
        label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @staticmethod
    def _set_label(label: QtWidgets.QLabel, text: str, kind: str = "Muted") -> None:
        label.setText(text)
        if label.objectName() != kind:
            label.setObjectName(kind)
            label.style().unpolish(label)
            label.style().polish(label)

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        root.setObjectName("RolaviRoot")
        root.setStyleSheet(STYLE)
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_title_bar())

        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_rail())

        self.pages = QtWidgets.QStackedWidget()
        self.pages.setObjectName("Pages")
        self.pages.setMinimumWidth(460)
        for spec in self._page_specs():
            self.pages.addWidget(self._make_page(*spec))

        right = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        right.setChildrenCollapsible(False)
        right.setHandleWidth(1)
        right.addWidget(self._build_plan_frame())
        right.addWidget(self._build_log_frame())
        right.setStretchFactor(0, 4)
        right.setStretchFactor(1, 1)
        right.setSizes([640, 170])

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)
        splitter.addWidget(self.pages)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([480, 700])
        body.addWidget(splitter, 1)
        outer.addLayout(body, 1)
        self.setCentralWidget(root)
        self._build_status_bar()
        self._ui_ready = True

    # ------------------------------------------------------------------ title bar
    def _build_title_bar(self) -> QtWidgets.QFrame:
        bar = QtWidgets.QFrame()
        bar.setObjectName("TitleBar")
        bar.setFixedHeight(56)
        layout = QtWidgets.QHBoxLayout(bar)
        layout.setContentsMargins(20, 0, 14, 0)
        layout.setSpacing(14)
        layout.addWidget(logo_label(20))
        sep = QtWidgets.QFrame()
        sep.setObjectName("TitleSep")
        sep.setFixedSize(1, 24)
        layout.addWidget(sep)
        product = QtWidgets.QLabel(APP_PRODUCT)
        product.setObjectName("Product")
        layout.addWidget(product)
        pill = QtWidgets.QLabel(f"v{__version__}")
        pill.setObjectName("VersionPill")
        layout.addWidget(pill)
        layout.addStretch(1)
        self.project_label = QtWidgets.QLabel("")
        self.project_label.setObjectName("TitleMuted")
        layout.addWidget(self.project_label)
        self.account_btn = QtWidgets.QPushButton("Sign in to ROLAVI Viewer")
        self.account_btn.setObjectName("AccountChip")
        self.account_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.account_btn.setToolTip("ROLAVI Viewer account")
        self.account_btn.clicked.connect(lambda: self.go_step(STEP_VIEWER))
        layout.addWidget(self.account_btn)
        return bar

    # ------------------------------------------------------------------ workflow rail
    def _build_rail(self) -> QtWidgets.QFrame:
        rail = QtWidgets.QFrame()
        rail.setObjectName("StepRail")
        rail.setFixedWidth(RAIL_WIDTH)
        layout = QtWidgets.QVBoxLayout(rail)
        layout.setContentsMargins(12, 18, 12, 12)
        layout.setSpacing(4)
        header = QtWidgets.QLabel("WORKFLOW")
        header.setObjectName("RailHeader")
        layout.addWidget(header)
        self.step_items: list[StepItem] = []
        for number, title in enumerate(STEP_TITLES, start=1):
            item = StepItem(number, title)
            item.clicked.connect(self.go_step)
            layout.addWidget(item)
            self.step_items.append(item)
        self.layers_panel = LayersPanel()
        layout.addWidget(self.layers_panel)
        layout.addStretch(1)
        self.rail_foot = QtWidgets.QLabel("")
        self.rail_foot.setObjectName("RailFoot")
        self.rail_foot.setWordWrap(True)
        layout.addWidget(self.rail_foot)
        return rail

    def go_step(self, number: int) -> None:
        number = max(1, min(len(STEP_TITLES), int(number)))
        self.pages.setCurrentIndex(number - 1)
        self._update_steps()

    def _step_states(self) -> list[tuple[bool, str]]:
        """(done, status line) of each step, from the state of the window."""
        if self.model is None:
            model = (False, "Import a model")
        elif self.crs_info is None:
            model = (False, "Choose the CRS of the model")
        else:
            model = (True, self.model.opened.name)
        if not self.use_gcp.isChecked():
            gcp = (False, "Optional · not used")
        elif self.solution is not None and not self.solution_stale:
            gcp = (True, f"{len(self.solution.names)} GCPs · RMSE {self.solution.rmse_3d:.3f} m")
        else:
            gcp = (False, "Press Solve")
        ready = self.model is not None and self.crs_info is not None
        review = (ready and self._georeferenced(), "Plan view · LAS preview" if ready else "Waiting for the model")
        out = self.out_edit.text().strip()
        if self._exported_file and out == self._exported_file and Path(out).is_file():
            export = (True, Path(out).name)
        else:
            export = (False, "Ready to export" if self._export_blocker() is None else "Not exported yet")
        result = self.volumes_panel._result()
        if result is not None:
            rasters = (True, f"Cut {result.stats['cut']:,.0f} m³ · fill {result.stats['fill']:,.0f} m³")
        else:
            dems = sum(1 for l in self.map_layers if l.kind == MAP_DEM)
            orthos = sum(1 for l in self.map_layers if l.kind == MAP_ORTHO)
            rasters = (False, f"{dems} DEM · {orthos} ortho" if dems or orthos else "Optional · GeoTIFF, volumes")
        client = self.viewer_panel.client
        user = ((client.session or {}).get("user") or {}).get("username") or self.viewer_panel.user_edit.text().strip()
        viewer = (True, f"Signed in as {user}") if client.token else (False, "Not signed in")
        return [model, gcp, review, export, rasters, viewer]

    def _update_steps(self) -> None:
        """Workflow rail, account chip and title bar from the state of the window."""
        if not self._ui_ready:
            return
        current = self.pages.currentIndex() + 1
        for item, (done, sub) in zip(self.step_items, self._step_states()):
            item.set_state("active" if item.number == current else ("done" if done else "todo"), sub)
        client = self.viewer_panel.client
        if client.token:
            user = ((client.session or {}).get("user") or {}).get("username") or self.viewer_panel.user_edit.text().strip()
            self.account_btn.setText(f"{user or 'Signed in'}  ·  {urlparse(client.base_url).netloc}")
        else:
            self.account_btn.setText("Sign in to ROLAVI Viewer")
        self.account_btn.setIcon(_dot_icon("#34C27A" if client.token else "#5F7486"))
        if self.model is not None:
            self.project_label.setText(f"Project: {self.model.opened.stem}")
        else:
            self.project_label.setText(f"Project: {self.map_layers[0].name}" if self.map_layers else "")
        engine = self.backend.name if self.backend is not None else "none"
        self.rail_foot.setText(f"Coordinate engine: {engine}\n{APP_TITLE} {__version__} · I3S 1.7")

    def viewer_state_changed(self) -> None:
        """Called by the ROLAVI Viewer panel when the account or its state changes."""
        self._update_steps()

    # ------------------------------------------------------------------ pages
    def _page_specs(self) -> list[tuple]:
        model_cards = self._build_model_cards()
        gcp_cards = self._build_gcp_cards()
        view_cards = self._build_view_cards()
        export_cards = self._build_export_cards()
        self.volumes_panel = VolumesPanel(self)
        self.viewer_panel = ViewerPanel(self)      # after the export cards: it reads the SLPK path
        return [
            (1, "Model", "Import the model (OBJ, or PLY mesh, point cloud or 3DGS) and say where its coordinates are.",
             model_cards, False),
            (2, "GCP adjustment", "Fit the model to known points. Required for a local model.", gcp_cards, True),
            (3, "Review in CRS", "Check where the model falls in the project CRS before exporting it.",
             view_cards, False),
            (4, "Export SLPK", "Write the Scene Layer Package in WGS84: IntegratedMesh for a mesh, PointCloud for "
             "a point cloud or 3DGS.", export_cards, False),
            (5, "Rasters & volumes", "Orthomosaics and DEMs (GeoTIFF) in the plan view, and cut / fill volumes "
             "between two DEMs or a DEM and a base elevation, inside a polygon.", [self.volumes_panel], True),
            (6, "ROLAVI Viewer", "Send the model, DEMs, orthomosaics and layers to your space.", [self.viewer_panel],
             False),
        ]

    def _make_page(self, number: int, title: str, description: str, cards: list, optional: bool) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        page_layout = QtWidgets.QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("PageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QtWidgets.QWidget()
        inner.setObjectName("PageInner")
        layout = QtWidgets.QVBoxLayout(inner)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(14)
        eyebrow = QtWidgets.QLabel(f"STEP {number} OF {len(STEP_TITLES)}" + ("  ·  OPTIONAL" if optional else ""))
        eyebrow.setObjectName("Eyebrow")
        heading = QtWidgets.QLabel(title)
        heading.setObjectName("PageTitle")
        desc = QtWidgets.QLabel(description)
        desc.setObjectName("PageDesc")
        desc.setWordWrap(True)
        head = QtWidgets.QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(eyebrow)
        head.addWidget(heading)
        head.addWidget(desc)
        layout.addLayout(head)
        for widget in cards:
            layout.addWidget(widget)
        layout.addStretch(1)
        scroll.setWidget(inner)
        page_layout.addWidget(scroll, 1)

        footer = QtWidgets.QFrame()
        footer.setObjectName("PageFooter")
        row = QtWidgets.QHBoxLayout(footer)
        row.setContentsMargins(22, 10, 22, 14)
        row.setSpacing(10)
        if number > 1:
            back = QtWidgets.QPushButton("Back")
            back.clicked.connect(lambda: self.go_step(number - 1))
            row.addWidget(back)
        row.addStretch(1)
        if number < len(STEP_TITLES):
            nxt = QtWidgets.QPushButton(f"Next: {STEP_TITLES[number]}  ›")
            nxt.setObjectName("Primary")
            nxt.clicked.connect(lambda: self.go_step(number + 1))
            row.addWidget(nxt)
        else:
            open_viewer = QtWidgets.QPushButton("Open ROLAVI Viewer  ↗")
            open_viewer.setObjectName("Primary")
            open_viewer.clicked.connect(self.viewer_panel.open_viewer)
            row.addWidget(open_viewer)
        page_layout.addWidget(footer)
        return page

    def _build_model_cards(self) -> list:
        file_card, file_layout = card("Model file")
        row = QtWidgets.QHBoxLayout()
        self.obj_edit = QtWidgets.QLineEdit()
        self.obj_edit.setReadOnly(True)
        self.obj_edit.setPlaceholderText("No model imported")
        self.open_btn = QtWidgets.QPushButton("Import model…")
        self.open_btn.setToolTip("OBJ (with its MTL and textures), or PLY: mesh, point cloud or 3D Gaussian splats")
        self.open_btn.setObjectName("Primary")
        row.addWidget(self.obj_edit, 1)
        row.addWidget(self.open_btn)
        file_layout.addLayout(row)
        facts = QtWidgets.QGridLayout()
        facts.setHorizontalSpacing(8)
        self.fact_labels: dict[str, QtWidgets.QLabel] = {}
        for column, key in enumerate(("Vertices", "Faces", "Textures", "Size")):
            label = QtWidgets.QLabel("")
            label.setObjectName("Fact")
            label.setVisible(False)
            facts.addWidget(label, 0, column)
            facts.setColumnStretch(column, 1)
            self.fact_labels[key] = label
        file_layout.addLayout(facts)
        self.model_label = self._label("Import a textured OBJ (MTL and textures beside it), or a PLY: mesh, "
                                       "point cloud or 3DGS splats.")
        file_layout.addWidget(self.model_label)

        coords_card, coords_layout = card("Coordinates of the model")
        self.scan_box = QtWidgets.QWidget()
        scan_layout = QtWidgets.QVBoxLayout(self.scan_box)
        scan_layout.setContentsMargins(0, 0, 0, 6)
        scan_layout.setSpacing(4)
        self.scan_check = QtWidgets.QCheckBox("Place it with its ROLAVIScan georeference")
        self.scan_check.setChecked(True)
        self.scan_check.setToolTip("Origin latitude / longitude / height and north of the capture: the model is placed "
                                   "exactly in the CRS below. Untick it to set the coordinates by hand.")
        scan_layout.addWidget(self.scan_check)
        self.scan_label = self._label("")
        scan_layout.addWidget(self.scan_label)
        self.scan_box.setVisible(False)
        coords_layout.addWidget(self.scan_box)
        self.coords_combo = Segmented()
        self.coords_combo.addItem("Georeferenced in the CRS", None, "X Easting, Y Northing, Z height in the CRS of the model")
        self.coords_combo.addItem("Local · place with GCPs", None, "Model not georeferenced: place it with GCPs in step 2")
        coords_layout.addWidget(self.coords_combo)
        coords_layout.addWidget(field_label("Up axis"))
        self.up_combo = Segmented()
        self.up_combo.addItem("Z up", None, "The model's height is its Z coordinate")
        self.up_combo.addItem("Y up", None, "The model's height is its Y coordinate")
        coords_layout.addWidget(self.up_combo)
        coords_layout.addWidget(field_label("Offset (shift used when the OBJ was exported)"))
        offset_row = QtWidgets.QHBoxLayout()
        offset_row.setSpacing(8)
        self.offset_edits: list[QtWidgets.QLineEdit] = []
        for axis in "XYZ":
            tag = QtWidgets.QLabel(axis)
            tag.setObjectName("AxisTag")
            edit = QtWidgets.QLineEdit("0")
            edit.setToolTip("Added to every coordinate of the file: the shift used when it was exported (0 if none)")
            edit.setAccessibleName(f"Offset {axis}")
            offset_row.addWidget(tag)
            offset_row.addWidget(edit, 1)
            self.offset_edits.append(edit)
        coords_layout.addLayout(offset_row)

        crs_card, crs_layout = card("Reference system")
        crs_layout.addWidget(field_label("Coordinate reference system"))
        crs_row = QtWidgets.QHBoxLayout()
        self.crs_combo = QtWidgets.QComboBox()
        self.crs_combo.setEditable(True)
        self.crs_combo.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        recent = self._recent_crs()
        for code, name in [(code, code) for code in recent] + crs_mod.COMMON_CRS:
            if self.crs_combo.findData(code) < 0:
                self.crs_combo.addItem(f"{code} - {name}" if name != code else code, code)
        self.crs_combo.lineEdit().setPlaceholderText("Search: UTM 18S, Peru96, WGS 84, 4326…")
        self.crs_combo.setToolTip("Projected (metres) or geographic (latitude / longitude) CRS. Pick one, type its "
                                  "EPSG code, or type a name and press Enter to search.")
        crs_row.addWidget(self.crs_combo, 1)
        self.crs_search_btn = QtWidgets.QPushButton("🔍 Search…")
        self.crs_search_btn.setObjectName("Ghost")
        self.crs_search_btn.setToolTip("Search every CRS by name, EPSG code or area")
        crs_row.addWidget(self.crs_search_btn)
        crs_layout.addLayout(crs_row)
        self.crs_label = self._label("")
        crs_layout.addWidget(self.crs_label)
        crs_layout.addWidget(field_label("Heights"))
        self.height_combo = Segmented()
        for key in (pipeline.HEIGHT_ELLIPSOIDAL, pipeline.HEIGHT_ORTHOMETRIC):
            self.height_combo.addItem(pipeline.HEIGHT_LABELS[key], key, "Height system of the exported SLPK")
        crs_layout.addWidget(self.height_combo)
        self.geoid_box = QtWidgets.QWidget()
        geoid_layout = QtWidgets.QVBoxLayout(self.geoid_box)
        geoid_layout.setContentsMargins(0, 4, 0, 0)
        geoid_layout.setSpacing(6)
        self.geoid_convert = QtWidgets.QCheckBox("Convert the ellipsoidal heights with a geoid model (H = h − N)")
        self.geoid_convert.setChecked(True)
        self.geoid_convert.setToolTip("Untick it if the Z of the model is already orthometric")
        geoid_layout.addWidget(self.geoid_convert)
        geoid_layout.addWidget(field_label("Geoid model (GTX)"))
        geoid_row = QtWidgets.QHBoxLayout()
        self.geoid_edit = QtWidgets.QLineEdit()
        self.geoid_edit.setPlaceholderText("e.g. egm2008.gtx")
        self.geoid_btn = QtWidgets.QPushButton("Browse…")
        geoid_row.addWidget(self.geoid_edit, 1)
        geoid_row.addWidget(self.geoid_btn)
        geoid_layout.addLayout(geoid_row)
        self.geoid_label = self._label("")
        geoid_layout.addWidget(self.geoid_label)
        self.geoid_box.setVisible(False)
        crs_layout.addWidget(self.geoid_box)
        return [file_card, coords_card, crs_card]

    def _build_gcp_cards(self) -> list:
        use_card, use_layout = card()
        self.use_gcp = QtWidgets.QCheckBox("Adjust the model with known GCPs")
        use_layout.addWidget(self.use_gcp)
        use_layout.addWidget(self._label("Least squares: rotation, scale and shift. Required for a local model."))

        points_card, points_layout = card("Control points")
        row1 = QtWidgets.QHBoxLayout()
        row1.setSpacing(6)
        self.import_gcp_btn = QtWidgets.QPushButton("Import CSV…")
        self.import_gcp_btn.setToolTip("Known coordinates of the GCPs (name, E, N, H); merged by name")
        self.order_combo = QtWidgets.QComboBox()
        self.order_combo.addItem("No header: Name, E, N, H", gcp_io.ORDER_PENZ)
        self.order_combo.addItem("No header: Name, N, E, H", gcp_io.ORDER_PNEZ)
        self.order_combo.setToolTip("Column order of a CSV without header line")
        row1.addWidget(self.import_gcp_btn)
        row1.addWidget(self.order_combo, 1)
        points_layout.addLayout(row1)
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(6)
        self.add_gcp_btn = QtWidgets.QPushButton("Add")
        self.remove_gcp_btn = QtWidgets.QPushButton("Remove")
        self.save_gcp_btn = QtWidgets.QPushButton("Save CSV…")
        self.pick_btn = QtWidgets.QPushButton("Pick on model")
        self.pick_btn.setCheckable(True)
        self.pick_btn.setToolTip("Select a GCP row, then click its point on the 3D model: its model coordinates are "
                                 "filled in (snapped to the nearest real vertex / point)")
        row2.addWidget(self.add_gcp_btn)
        row2.addWidget(self.remove_gcp_btn)
        row2.addWidget(self.save_gcp_btn)
        row2.addStretch(1)
        row2.addWidget(self.pick_btn)
        points_layout.addLayout(row2)
        self.gcp_table = GcpTable()
        points_layout.addWidget(self.gcp_table)

        adjust_card, adjust_layout = card("Adjustment")
        self.mode_combo = Segmented()
        for mode, short in ((georef.MODE_VERTICAL, "Keep vertical"), (georef.MODE_3D, "Full 3D"),
                            (georef.MODE_SHIFT, "Shift only")):
            self.mode_combo.addItem(short, mode, georef.MODE_LABELS[mode])
        adjust_layout.addWidget(self.mode_combo)
        row3 = QtWidgets.QHBoxLayout()
        self.lock_scale = QtWidgets.QCheckBox("Lock scale (1.0)")
        self.lock_scale.setToolTip("Keep the model's size (RTK / georeferenced models): only rotation and shift")
        self.check_btn = QtWidgets.QPushButton("Check fit")
        self.check_btn.setToolTip("Residuals of the GCPs on the model as it is (no adjustment)")
        self.solve_btn = QtWidgets.QPushButton("Solve")
        self.solve_btn.setObjectName("Primary")
        self.solve_btn.setToolTip("Adjust the model to the GCPs (least squares)")
        row3.addWidget(self.lock_scale)
        row3.addStretch(1)
        row3.addWidget(self.check_btn)
        row3.addWidget(self.solve_btn)
        adjust_layout.addLayout(row3)
        row4 = QtWidgets.QHBoxLayout()
        self.solve_label = self._label("No adjustment.")
        self.clear_solve_btn = QtWidgets.QPushButton("Clear adjustment")
        self.clear_solve_btn.setObjectName("Ghost")
        row4.addWidget(self.solve_label, 1)
        row4.addWidget(self.clear_solve_btn)
        adjust_layout.addLayout(row4)
        self.gcp_widgets = [self.gcp_table, self.import_gcp_btn, self.order_combo, self.add_gcp_btn,
                            self.remove_gcp_btn, self.save_gcp_btn, self.mode_combo, self.lock_scale]
        return [use_card, points_card, adjust_card]

    def _build_view_cards(self) -> list:
        view_card, view_layout = card("The model in the project CRS")
        self.view_label = self._label("No model yet.", "Info")
        view_layout.addWidget(self.view_label)
        las_card, las_layout = card("Check it in QGIS")
        las_layout.addWidget(self._label("Save the preview points in the project CRS and add them in QGIS "
                                         "(Layer ▸ Add Layer ▸ Add Point Cloud Layer) over your basemap."))
        self.las_btn = QtWidgets.QPushButton("Save preview LAS…")
        self.las_btn.setToolTip("Coloured points of the model in the project CRS, to check the position in QGIS")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.las_btn)
        row.addStretch(1)
        las_layout.addLayout(row)
        return [view_card, las_card]

    def _build_export_cards(self) -> list:
        out_card, out_layout = card("Output")
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        self.layer_edit = QtWidgets.QLineEdit()
        self.layer_edit.setPlaceholderText("Name of the layer in ROLAVI Viewer")
        out_row = QtWidgets.QHBoxLayout()
        self.out_edit = QtWidgets.QLineEdit()
        self.out_btn = QtWidgets.QPushButton("Browse…")
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(self.out_btn)
        form.addRow(field_label("Layer name"), self.layer_edit)
        form.addRow(field_label("SLPK file"), out_row)
        out_layout.addLayout(form)

        quality_card, quality_layout = card("Quality (meshes)")
        self.quality_card = quality_card
        qform = QtWidgets.QFormLayout()
        qform.setHorizontalSpacing(12)
        qform.setVerticalSpacing(10)
        self.texture_combo = QtWidgets.QComboBox()
        for size, text in TEXTURE_CHOICES:
            self.texture_combo.addItem(text, size)
        self.texture_combo.setCurrentIndex(1)
        self.tile_spin = QtWidgets.QSpinBox()
        self.tile_spin.setRange(20_000, 1_000_000)
        self.tile_spin.setSingleStep(10_000)
        self.tile_spin.setValue(200_000)
        self.tile_spin.setGroupSeparatorShown(True)
        self.jpeg_spin = QtWidgets.QSpinBox()
        self.jpeg_spin.setRange(50, 95)
        self.jpeg_spin.setValue(85)
        qform.addRow(field_label("Texture size"), self.texture_combo)
        qform.addRow(field_label("Triangles per tile"), self.tile_spin)
        qform.addRow(field_label("JPEG quality"), self.jpeg_spin)
        quality_layout.addLayout(qform)

        go_card, go_layout = card()
        self.export_btn = QtWidgets.QPushButton("Export SLPK")
        self.export_btn.setObjectName("Primary")
        go_layout.addWidget(self.export_btn)
        self.export_hint = self._label("")
        go_layout.addWidget(self.export_hint)
        return [out_card, quality_card, go_card]

    # ------------------------------------------------------------------ plan view, log, status bar
    def _build_plan_frame(self) -> QtWidgets.QWidget:
        frame = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar = QtWidgets.QFrame()
        bar.setObjectName("PlanBar")
        row = QtWidgets.QHBoxLayout(bar)
        row.setContentsMargins(12, 7, 12, 7)
        row.setSpacing(6)
        self.view2d_btn = QtWidgets.QPushButton("2D")
        self.view3d_btn = QtWidgets.QPushButton("3D")
        group = QtWidgets.QButtonGroup(bar)
        group.setExclusive(True)
        for button, tip in ((self.view2d_btn, "Plan view in the project CRS (pick the GCPs here)"),
                            (self.view3d_btn, "The model in 3D: left button moves, right button rotates, wheel zooms")):
            button.setObjectName("DarkTool")
            button.setCheckable(True)
            button.setToolTip(tip)
            group.addButton(button)
            row.addWidget(button)
        self.view2d_btn.setChecked(True)
        row.addSpacing(8)
        self.plan_caption = QtWidgets.QLabel("Plan view")
        self.plan_caption.setObjectName("PlanCaption")
        row.addWidget(self.plan_caption, 1)
        self.top_btn = QtWidgets.QPushButton("Top")
        self.top_btn.setObjectName("DarkTool")
        self.top_btn.setToolTip("Look at the 3D model from above")
        self.fit_btn = QtWidgets.QPushButton("Fit view")
        self.fit_btn.setObjectName("DarkTool")
        self.trackball_btn = QtWidgets.QPushButton("Trackball")
        self.trackball_btn.setObjectName("DarkTool")
        self.trackball_btn.setCheckable(True)
        self.trackball_btn.setChecked(bool(self.settings.value("trackball", True, type=bool)))
        self.trackball_btn.setToolTip("3D: show the sphere with the red / green / blue circles at the centre of "
                                      "rotation (the right button rotates around it; double-click the model to "
                                      "move the centre there)")
        self.basemap_btn = QtWidgets.QPushButton("🗺 Basemap")
        self.basemap_btn.setObjectName("DarkTool")
        self.basemap_btn.setToolTip("2D: map under the model (satellite, map, hybrid, terrain…). "
                                    "The model must be in the project CRS.")
        self.basemap_menu = QtWidgets.QMenu(self.basemap_btn)
        self.basemap_btn.setMenu(self.basemap_menu)
        row.addWidget(self.basemap_btn)
        row.addWidget(self.trackball_btn)
        row.addWidget(self.top_btn)
        row.addWidget(self.fit_btn)
        layout.addWidget(bar)

        self.view_stack = QtWidgets.QStackedWidget()
        self.plan = PlanView()
        self.basemap_layer = BasemapLayer(self)
        self.plan.set_basemap(self.basemap_layer)
        self._build_basemap_menu()
        self.trackball_btn.setVisible(False)          # 2D first: the trackball button is for the 3D view
        self.view_stack.addWidget(self.plan)
        self.view3d_holder = QtWidgets.QWidget()
        self.view3d_holder.setObjectName("View3DHolder")
        holder = QtWidgets.QVBoxLayout(self.view3d_holder)
        holder.setContentsMargins(0, 0, 0, 0)
        self.view3d_note = QtWidgets.QLabel("")
        self.view3d_note.setObjectName("ViewNote")
        self.view3d_note.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.view3d_note.setWordWrap(True)
        holder.addWidget(self.view3d_note, 1)
        self.view_stack.addWidget(self.view3d_holder)
        layout.addWidget(self.view_stack, 1)
        return frame

    # ------------------------------------------------------------------ base map
    def _basemaps(self) -> list[tiles.Basemap]:
        custom = []
        for entry in self.settings.value("custom_basemaps", [], type=list) or []:
            label, _sep, url = str(entry).partition("|")
            if label and "{x}" in url and "{y}" in url and "{z}" in url:
                custom.append(tiles.custom_basemap(label, url))
        return list(tiles.PRESETS) + custom

    def _build_basemap_menu(self) -> None:
        menu = self.basemap_menu
        menu.clear()
        chosen = str(self.settings.value("basemap", "", type=str) or "")
        self._basemap_actions = []
        for basemap in [None] + self._basemaps():
            action = menu.addAction("No base map" if basemap is None else basemap.label)
            action.setCheckable(True)
            action.setChecked((basemap.key if basemap else "") == chosen)
            action.triggered.connect(lambda _checked=False, b=basemap: self.set_basemap(b))
            self._basemap_actions.append((action, basemap))
        menu.addSeparator()
        add = menu.addAction("Add XYZ tiles…")
        add.triggered.connect(lambda *_: self.add_custom_basemap())
        current = next((b for b in self._basemaps() if b.key == chosen), None)
        self.basemap_layer.set_basemap(current)
        self.basemap_btn.setText(f"🗺 {current.label}" if current else "🗺 Basemap")

    def set_basemap(self, basemap: tiles.Basemap | None) -> None:
        self.settings.setValue("basemap", basemap.key if basemap else "")
        for action, item in self._basemap_actions:
            action.setChecked(item == basemap)
        self.basemap_layer.set_basemap(basemap)
        self.basemap_btn.setText(f"🗺 {basemap.label}" if basemap else "🗺 Basemap")
        if basemap is not None and not self._georeferenced():
            self.statusBar().showMessage("The base map shows when the model is in the project CRS "
                                         "(georeferenced or adjusted with GCPs).", 7000)
        if basemap is not None and self._is_3d():
            self.show_2d()

    def add_custom_basemap(self) -> None:
        label, ok = QtWidgets.QInputDialog.getText(self, APP_TITLE, "Name of the base map:")
        if not ok or not label.strip():
            return
        url, ok = QtWidgets.QInputDialog.getText(
            self, APP_TITLE, "XYZ address with {x} {y} {z}, e.g. https://tile.openstreetmap.org/{z}/{x}/{y}.png")
        url = url.strip()
        if not ok or not all(key in url for key in ("{x}", "{y}", "{z}")):
            if ok:
                QtWidgets.QMessageBox.warning(self, APP_TITLE, "The address needs {x}, {y} and {z}.")
            return
        entries = [str(e) for e in (self.settings.value("custom_basemaps", [], type=list) or [])]
        entries.append(f"{label.strip()}|{url}")
        self.settings.setValue("custom_basemaps", entries)
        basemap = tiles.custom_basemap(label.strip(), url)
        self._build_basemap_menu()
        self.set_basemap(next((b for b in self._basemaps() if b.key == basemap.key), basemap))

    # ------------------------------------------------------------------ 2D / 3D
    def _is_3d(self) -> bool:
        return self.view_stack.currentIndex() == 1

    def show_2d(self) -> None:
        self.view2d_btn.setChecked(True)
        self.view_stack.setCurrentIndex(0)
        self.basemap_btn.setVisible(True)
        self.trackball_btn.setVisible(False)
        self._refresh_state()

    def show_3d(self) -> None:
        self.view3d_btn.setChecked(True)
        self.view_stack.setCurrentIndex(1)
        self.basemap_btn.setVisible(False)
        self.trackball_btn.setVisible(True)
        self._ensure_3d()
        self._refresh_state()

    def _note_3d(self, text: str) -> None:
        self.view3d_note.setText(text)
        self.view3d_note.setVisible(True)
        if self.view3d is not None:
            self.view3d.setVisible(False)

    @property
    def display_mesh(self) -> Any:
        """What the 3D view draws of the active layer (pipeline.DisplayMesh / DisplayCloud)."""
        return self.active.display if self.active is not None else None

    @display_mesh.setter
    def display_mesh(self, value: Any) -> None:
        if self.active is not None:
            self.active.display = value

    def _layers_3d(self) -> list[Layer]:
        """The layers the 3D view can show with the active one: the active first, then the others
        that are in the project CRS too (a local model is shown alone)."""
        if self.active is None:
            return []
        together = [self.active]
        if self._georeferenced():
            together += [layer for layer in self.layers
                         if layer is not self.active and self._georeferenced_for(layer)]
        return together

    def _ensure_3d(self, reset: bool = True) -> None:
        """Build what is missing for the 3D view (one mesh at a time, in the background), then show it."""
        self._pending_reset = self._pending_reset or reset
        if not view3d_mod.AVAILABLE:
            self._note_3d(f"{view3d_mod.INSTALL_HINT}\n\n({view3d_mod.IMPORT_ERROR})")
            return
        if self.model is None:
            self._note_3d("Import a model to see it in 3D.")
            return
        for layer in self._layers_3d():
            if layer.display is not None or (layer is not self.active and not layer.visible):
                continue
            if layer.model.kind != KIND_MESH:
                cloud = pipeline.build_display_cloud(layer.model)
                layer.display = cloud
                self.log(f"   3D view    : {layer.title}: {len(cloud.points_obj):,} points (from {cloud.source_points:,})")
                continue
            if self.task is not None:
                if self.view3d is None or not self.view3d.layer_keys():
                    self._note_3d("Waiting for the current job: press 3D again when it ends.")
                return
            if self.view3d is None or not self.view3d.layer_keys():
                self._note_3d(f"Building the 3D view of {layer.title}…\n"
                              "(the whole mesh is read once; a big model takes a while)")
            path, key = layer.model.path, layer.key

            def job(progress, log, path=path):
                return pipeline.build_display_mesh(path, pipeline.DISPLAY_FACES, progress, log)

            self._start_task(f"Building the 3D view of {layer.title}", job,
                             lambda display, key=key: self._display_mesh_built(key, display))
            return
        reset, self._pending_reset = self._pending_reset, False
        self._push_3d(reset=reset)

    def _display_mesh_built(self, key: int, display: pipeline.DisplayMesh) -> None:
        layer = self._layer(key)
        if layer is None or display.path != layer.model.path:
            return  # the layer was removed meanwhile
        layer.display = display
        self.log(f"   3D view    : {layer.title}: {len(display.faces):,} triangles (from {display.source_faces:,}), "
                 f"colours from {display.colour_source}")
        if self._is_3d():
            self._ensure_3d(reset=False)

    def _push_3d(self, reset: bool) -> None:
        """Every layer the 3D view can show (hidden ones kept hidden); only the changed ones are redrawn."""
        if self.display_mesh is None or not view3d_mod.AVAILABLE:
            return
        if self.view3d is None:
            self.view3d = view3d_mod.View3D(self.view3d_holder)
            self.view3d_holder.layout().addWidget(self.view3d, 1)
            self.view3d.picked.connect(self._picked_3d)
            self.view3d.hovered.connect(self._cursor_3d)
            self.view3d.set_pick_mode(self.pick_btn.isChecked())
            self.view3d.set_trackball(self.trackball_btn.isChecked())
            reset = True
        self.view3d_note.setVisible(False)
        self.view3d.setVisible(True)
        shown = [layer for layer in self._layers_3d() if layer.display is not None]
        if reset:
            self.view3d.clear_model()      # the active layer (pushed first) gives the new origin
            for layer in self.layers:
                layer.pushed = ""
        keys = {layer.key for layer in shown}
        for key in self.view3d.layer_keys():
            if key not in keys:
                self.view3d.remove_layer(key, render=False)
                found = self._layer(key)
                if found is not None:
                    found.pushed = ""
        for layer in shown:
            frame, adjustment = self._frame_for(layer), self._adjustment_for(layer)
            signature = json.dumps({"frame": frame.to_dict(), "adjustment": _adjustment_key(adjustment),
                                    "display": id(layer.display)}, sort_keys=True, default=str)
            if signature == layer.pushed and self.view3d.has_layer(layer.key):
                self.view3d.set_layer_visible(layer.key, layer.visible)
                continue
            display = layer.display
            if isinstance(display, pipeline.DisplayCloud):
                world = pipeline.to_world(display.points_obj, frame, adjustment)
                self.view3d.set_layer_points(layer.key, world, display.colors,
                                             3.0 if display.kind == KIND_SPLATS else 2.0, visible=layer.visible)
            else:
                world = pipeline.to_world(display.vertices_obj, frame, adjustment)
                self.view3d.set_layer_mesh(layer.key, world, display.parts, display.colors, visible=layer.visible)
            layer.pushed = signature
        self.view3d.set_active_layer(self.active.key if self.active is not None else None)
        if reset:
            self.view3d.reset_view()
        self.view3d.set_markers(self._markers_3d())

    def _trackball_toggled(self, on: bool) -> None:
        self.settings.setValue("trackball", bool(on))
        if self.view3d is not None:
            self.view3d.set_trackball(on)

    def _markers_3d(self) -> list[dict]:
        if self.model is None:
            return []
        frame, adjustment, georeferenced = self._frame(), self._adjustment(), self._georeferenced()
        selected = self.gcp_table.selected_row()
        markers = []
        for row, gcp in enumerate(self.gcp_table.gcps()):
            model_xyz = None
            if gcp.obj is not None:
                point = pipeline.to_world(np.array([gcp.obj], dtype=np.float64), frame, adjustment)[0]
                model_xyz = (float(point[0]), float(point[1]), float(point[2]))
            known_xyz = self._known_world(gcp.known) if georeferenced else None
            if model_xyz is None and known_xyz is None:
                continue
            markers.append({"name": gcp.name, "model": model_xyz, "known": known_xyz, "selected": row == selected})
        return markers

    def _pick_toggled(self, on: bool) -> None:
        """GCPs are picked on the 3D model (the 2D view picks too, if it is the one shown)."""
        self.plan.set_pick_mode(on)
        if self.view3d is not None:
            self.view3d.set_pick_mode(on)
        if on:
            if not self._is_3d():
                self.show_3d()
            if self.gcp_table.selected_row() < 0:
                self.statusBar().showMessage("Select a GCP row, or click on the model to add one.", 6000)
            else:
                name = self.gcp_table.gcp_at(self.gcp_table.selected_row()).name
                self.statusBar().showMessage(f"Click {name} on the model (left click; drag to move the view).", 6000)

    def _picked_3d(self, face: int, r: float, s: float) -> None:
        display = self.display_mesh
        if display is None or self.model is None or display.path != self.model.path:
            return
        if self.active is not None and not self.active.visible:
            self.statusBar().showMessage("The active layer is hidden: tick it in LAYERS to pick on it.", 6000)
            return
        if isinstance(display, pipeline.DisplayCloud):
            if 0 <= face < len(display.points_obj):
                self._set_picked_obj(display.points_obj[face])
            return
        if not 0 <= face < len(display.faces):
            return
        self._set_picked_obj(display.snap(display.point_on_face(face, r, s)))

    def fit_view(self) -> None:
        if self._is_3d():
            if self.view3d is not None:
                self.view3d.reset_view()
        else:
            self.plan.fit()

    def top_view(self) -> None:
        if self.view3d is not None:
            self.view3d.top_view()

    def _build_log_frame(self) -> QtWidgets.QFrame:
        frame = QtWidgets.QFrame()
        frame.setObjectName("LogFrame")
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(12, 4, 12, 6)
        layout.setSpacing(4)
        title_row = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Log")
        title.setObjectName("LogTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        layout.addLayout(title_row)
        self.console = QtWidgets.QPlainTextEdit()
        self.console.setObjectName("Console")
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        self.console.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(self.console, 1)
        frame.setMinimumHeight(110)
        return frame

    def _build_status_bar(self) -> None:
        status = self.statusBar()
        status.setStyleSheet(STATUS_STYLE)
        status.setSizeGripEnabled(False)
        self.task_label = self._label("Ready")
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(170)
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.coords_label = QtWidgets.QLabel("")
        self.coords_label.setObjectName("Coords")
        status.addWidget(self.task_label, 1)
        status.addPermanentWidget(self.progress)
        status.addPermanentWidget(self.cancel_btn)
        status.addPermanentWidget(self.coords_label)

    def _connect(self) -> None:
        self.open_btn.clicked.connect(self.open_obj)
        self.layers_panel.add_requested.connect(self._add_requested)
        self.layers_panel.activated.connect(self.activate_layer)
        self.layers_panel.visibility_changed.connect(self.set_layer_visible)
        self.layers_panel.remove_requested.connect(self.remove_layer)
        self.trackball_btn.toggled.connect(self._trackball_toggled)
        self.scan_check.toggled.connect(self._frame_changed)
        self.coords_combo.currentIndexChanged.connect(self._frame_changed)
        self.up_combo.currentIndexChanged.connect(self._frame_changed)
        for edit in self.offset_edits:
            edit.editingFinished.connect(self._frame_changed)
        self.crs_combo.activated.connect(self._crs_changed)
        self.crs_search_btn.clicked.connect(lambda *_: self.search_crs())
        self.crs_combo.lineEdit().editingFinished.connect(self._crs_changed)
        self.height_combo.currentIndexChanged.connect(self._height_changed)
        self.geoid_btn.clicked.connect(self.choose_geoid)
        self.geoid_edit.editingFinished.connect(self._geoid_changed)
        self.geoid_convert.toggled.connect(self._height_changed)
        self.use_gcp.toggled.connect(self._gcp_use_toggled)
        self.gcp_table.edited.connect(self._gcps_edited)
        self.gcp_table.itemSelectionChanged.connect(self._update_markers)
        self.import_gcp_btn.clicked.connect(self.import_gcps)
        self.add_gcp_btn.clicked.connect(self.add_gcp)
        self.remove_gcp_btn.clicked.connect(self.gcp_table.remove_selected)
        self.save_gcp_btn.clicked.connect(self.save_gcps)
        self.pick_btn.toggled.connect(self._pick_toggled)
        self.view2d_btn.clicked.connect(self.show_2d)
        self.view3d_btn.clicked.connect(self.show_3d)
        self.top_btn.clicked.connect(self.top_view)
        self.mode_combo.currentIndexChanged.connect(self._mark_stale)
        self.lock_scale.toggled.connect(self._mark_stale)
        self.check_btn.clicked.connect(self.check_fit)
        self.solve_btn.clicked.connect(self.solve)
        self.clear_solve_btn.clicked.connect(self._clear_adjustment_clicked)
        self.fit_btn.clicked.connect(self.fit_view)
        self.las_btn.clicked.connect(self.save_preview_las)
        self.out_btn.clicked.connect(self.choose_output)
        self.out_edit.textChanged.connect(self._refresh_state)
        self.export_btn.clicked.connect(self.export_slpk)
        self.cancel_btn.clicked.connect(self.cancel_task)
        self.plan.cursor_moved.connect(self._cursor_moved)
        self.plan.picked.connect(self._picked)
        self.plan.polygon_drawn.connect(self._polygon_drawn)
        self.plan.drawing_changed.connect(self.volumes_panel.drawing_changed)

    def _restore_settings(self) -> None:
        geoid_path = str(self.settings.value("geoid", "", type=str) or "")
        if geoid_path:
            self.geoid_edit.setText(geoid_path)
            self._load_geoid(geoid_path)
        crs = str(self.settings.value("crs", "", type=str) or "")
        if crs:
            self._set_crs_text(crs)
        texture = int(self.settings.value("max_texture", 2048, type=int) or 2048)
        index = self.texture_combo.findData(texture)
        if index >= 0:
            self.texture_combo.setCurrentIndex(index)

    # ================================================================== log / state
    def log(self, text: str) -> None:
        self.console.appendPlainText(str(text))
        print(text, flush=True)  # also in the VS Code terminal

    def _scan_active(self) -> bool:
        """The model is placed by its ROLAVIScan georeference (in the CRS of the project)."""
        scan = self.model.scan if self.model is not None else None
        return (scan is not None and scan.placeable and self.scan_check.isChecked()
                and self.crs_info is not None and self.backend is not None)

    def _frame(self) -> Any:
        """ModelFrame (by hand), CrsFrame (file in a geographic CRS) or ScanFrame (ROLAVIScan georeference):
        ``apply``, ``to_dict``, ``describe``."""
        if self._scan_active():
            try:
                return ScanFrame(self.model.scan, self.crs_info.code, self.backend)
            except (CrsError, ValueError) as exc:
                self.log(f"❌ ROLAVIScan georeference: {exc}")
        base = self._manual_frame()
        if self._geographic() and self.crs_info is not None and self.coords_combo.currentIndex() == COORDS_GEOREF:
            return CrsFrame(base, self.user_crs.code, self.crs_info.code, self.backend)
        return base

    def _manual_frame(self) -> ModelFrame:
        offset = []
        for edit in self.offset_edits:
            value = _number(edit.text())
            offset.append(0.0 if value is None else value)
        up = georef.UP_Y if self.up_combo.currentIndex() == 1 else georef.UP_Z
        return ModelFrame(up_axis=up, offset=(offset[0], offset[1], offset[2]))

    def _is_local(self) -> bool:
        if self._scan_active():
            return False
        return self.coords_combo.currentIndex() == COORDS_LOCAL

    # ------------------------------------------------------------------ the other layers
    def _layer(self, key: int) -> Layer | None:
        return next((layer for layer in self.layers if layer.key == key), None)

    def _scan_active_for(self, layer: Layer) -> bool:
        if layer is self.active:
            return self._scan_active()
        scan = layer.model.scan
        return (scan is not None and scan.placeable and bool(layer.state.get("scan", True))
                and self.crs_info is not None and self.backend is not None)

    def _frame_for(self, layer: Layer) -> Any:
        """The frame of any layer: from the widgets for the active one, from its saved settings otherwise."""
        if layer is self.active:
            return self._frame()
        if self._scan_active_for(layer):
            try:
                return ScanFrame(layer.model.scan, self.crs_info.code, self.backend)
            except (CrsError, ValueError):
                pass
        state = layer.state
        offset = [_number(text) or 0.0 for text in state.get("offsets", ["0", "0", "0"])]
        up = georef.UP_Y if state.get("up", 0) == 1 else georef.UP_Z
        base = ModelFrame(up_axis=up, offset=(offset[0], offset[1], offset[2]))
        if self._geographic() and self.crs_info is not None and state.get("coords", COORDS_GEOREF) == COORDS_GEOREF:
            return CrsFrame(base, self.user_crs.code, self.crs_info.code, self.backend)
        return base

    def _adjustment_for(self, layer: Layer) -> Similarity | None:
        if layer is self.active:
            return self._adjustment()
        if layer.state.get("use_gcp") and layer.solution is not None:
            return layer.solution.transform
        return None

    def _georeferenced_for(self, layer: Layer) -> bool:
        if layer is self.active:
            return self._georeferenced()
        local = not self._scan_active_for(layer) and layer.state.get("coords", COORDS_GEOREF) == COORDS_LOCAL
        return (not local) or self._adjustment_for(layer) is not None

    def _layer_preview(self, layer: Layer) -> np.ndarray:
        """Preview points of another layer in the project CRS (kept until its frame changes)."""
        frame, adjustment = self._frame_for(layer), self._adjustment_for(layer)
        signature = json.dumps({"frame": frame.to_dict(), "adjustment": _adjustment_key(adjustment)},
                               sort_keys=True, default=str)
        if layer.preview[0] != signature or layer.preview[1] is None:
            layer.preview = (signature, pipeline.to_world(layer.model.points_obj, frame, adjustment))
        return layer.preview[1]

    def _widget_state(self) -> dict:
        """The settings of the active layer (kept while another layer is active)."""
        return {
            "coords": self.coords_combo.currentIndex(), "up": self.up_combo.currentIndex(),
            "offsets": [edit.text() for edit in self.offset_edits], "scan": self.scan_check.isChecked(),
            "use_gcp": self.use_gcp.isChecked(), "mode": self.mode_combo.currentIndex(),
            "lock": self.lock_scale.isChecked(),
            # the GCP list (names, E / N / H) is shared; the picks on this model and the residuals are its own
            "picks": {gcp.name: gcp.obj for gcp in self.gcp_table.gcps() if gcp.obj is not None},
            "residuals": {gcp.name: gcp.residual for gcp in self.gcp_table.gcps() if gcp.residual is not None},
            "gcp_row": self.gcp_table.selected_row(), "solve_text": self.solve_label.text(),
            "layer_name": self.layer_edit.text(), "out": self.out_edit.text(),
        }

    def _apply_widget_state(self, state: dict) -> None:
        self.coords_combo.setCurrentIndex(state.get("coords", COORDS_GEOREF))
        self.up_combo.setCurrentIndex(state.get("up", 0))
        for edit, text in zip(self.offset_edits, state.get("offsets", ["0", "0", "0"])):
            edit.setText(text)
        self.scan_check.setChecked(bool(state.get("scan", True)))
        self.use_gcp.setChecked(bool(state.get("use_gcp", False)))
        self.mode_combo.setCurrentIndex(state.get("mode", 0))
        self.lock_scale.setChecked(bool(state.get("lock", False)))
        picks, residuals = state.get("picks", {}), state.get("residuals", {})
        gcps = self.gcp_table.gcps()
        for gcp in gcps:
            gcp.obj, gcp.residual = picks.get(gcp.name), residuals.get(gcp.name)
        self.gcp_table.set_gcps(gcps)
        if state.get("gcp_row", -1) >= 0 and state["gcp_row"] < self.gcp_table.rowCount():
            self.gcp_table.selectRow(state["gcp_row"])
        self._set_label(self.solve_label, state.get("solve_text") or "No adjustment.")
        self.layer_edit.setText(state.get("layer_name", ""))
        self.out_edit.setText(state.get("out", ""))

    def _save_active(self) -> None:
        layer = self.active
        if layer is None:
            return
        layer.state = self._widget_state()
        layer.solution, layer.solution_key, layer.solution_stale = self.solution, self.solution_key, self.solution_stale
        layer.exported = self._exported_file

    def activate_layer(self, key: int) -> None:
        """Make another layer the one the workflow works on (a map layer is only selected)."""
        if self._map_layer(key) is not None:
            self._select_map_layer(key)
            return
        layer = self._layer(key)
        if layer is None:
            return
        if layer is self.active:
            if self.selected_map is not None:
                self.selected_map = None
                self._refresh_layers()
            return
        self.selected_map = None
        if self.task is not None:
            self.statusBar().showMessage("Wait for the current job before changing the active layer.", 5000)
            self._refresh_layers()
            return
        if self.pick_btn.isChecked():
            self.pick_btn.setChecked(False)
        self._save_active()
        self._show_layer(layer)
        self.log(f"▶ Active layer: {layer.title}")

    def _show_layer(self, layer: Layer) -> None:
        self.active = layer
        self.model = layer.model
        self.solution = None          # refilling the widgets must not mark its adjustment as changed
        self._exported_file = layer.exported
        self._updating = True
        try:
            self._apply_widget_state(layer.state)
        finally:
            self._updating = False
        self.solution, self.solution_key, self.solution_stale = layer.solution, layer.solution_key, layer.solution_stale
        self._show_model_info(layer.model)
        self._shown_frame_key = ""
        self._refresh_layers()
        self._refresh_all(keep_view=True)
        if self._is_3d():
            self._ensure_3d(reset=False)

    def set_layer_visible(self, key: int, visible: bool) -> None:
        map_layer = self._map_layer(key)
        if map_layer is not None:
            map_layer.visible = bool(visible)
            self._refresh_map_view()
            return
        layer = self._layer(key)
        if layer is None or layer.visible == bool(visible):
            return
        layer.visible = bool(visible)
        if self.view3d is not None and self.view3d.has_layer(key):
            self.view3d.set_layer_visible(key, layer.visible)
        self._refresh_view(keep_view=True)
        if self._is_3d() and layer.visible and layer.display is None:
            self._ensure_3d(reset=False)

    def remove_layer(self, key: int) -> None:
        if self._map_layer(key) is not None:
            self.remove_map_layer(key)
            return
        layer = self._layer(key)
        if layer is None:
            return
        if self.task is not None:
            self.statusBar().showMessage("Wait for the current job before removing a layer.", 5000)
            return
        answer = QtWidgets.QMessageBox.question(
            self, APP_TITLE, f"Remove the layer {layer.title} from the project?\n\n"
            "The file is not touched; its GCP picks and adjustment are lost.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        index = self.layers.index(layer)
        self.layers.remove(layer)
        if self.view3d is not None:
            self.view3d.remove_layer(key)
        self.log(f"🗑 Layer removed: {layer.title}")
        if layer is not self.active:
            self._refresh_layers()
            self._refresh_view(keep_view=True)
            return
        self.active = None
        if self.pick_btn.isChecked():
            self.pick_btn.setChecked(False)
        if self.layers:
            self._show_layer(self.layers[min(index, len(self.layers) - 1)])
            return
        self.model = None
        self.solution, self.solution_key, self.solution_stale = None, None, False
        self._exported_file = ""
        gcps = self.gcp_table.gcps()
        for gcp in gcps:
            gcp.obj, gcp.residual = None, None
        self.gcp_table.set_gcps(gcps)
        self._set_label(self.solve_label, "No adjustment.")
        self._show_model_info(None)
        self._refresh_layers()
        self._refresh_all(keep_view=False)

    def _refresh_layers(self) -> None:
        if self.selected_map is not None and self._map_layer(self.selected_map) is None:
            self.selected_map = None
        self.layers_panel.set_layers(self.layers, self.active.key if self.active is not None else None,
                                     self.map_layers, self.selected_map)

    # ================================================================== map layers
    def _map_layer(self, key) -> MapLayer | None:
        return next((layer for layer in self.map_layers if layer.key == key), None)

    def _select_map_layer(self, key: int) -> None:
        layer = self._map_layer(key)
        if layer is None:
            return
        self.selected_map = key
        if layer.kind in (MAP_ORTHO, MAP_DEM):
            self.volumes_panel.select_raster(key)
        elif layer.kind == MAP_POLYGON:
            self.volumes_panel.select_polygon(key)
        self._refresh_layers()

    def _add_requested(self, kind: str) -> None:
        if kind == "raster":
            self.open_raster()
        elif kind == "polygon":
            self.open_polygon()
        elif kind == "draw":
            self.start_drawing()
        else:
            self.open_obj()

    def _add_map_layer(self, kind: str, name: str, data, preview=None, path: str = "") -> MapLayer:
        self._layer_seq += 1
        layer = MapLayer(self._layer_seq, kind, name, data, preview, path)
        # drawing order: rasters (the last imported on top), the cut / fill over them, polygons over all
        self.map_layers.append(layer)
        self.map_layers.sort(key=lambda l: (MAP_ORDER.get(l.kind, 3), l.key))
        self.selected_map = layer.key
        self.volumes_panel.refresh()
        self._refresh_layers()
        self._update_steps()
        return layer

    def remove_map_layer(self, key: int) -> None:
        layer = self._map_layer(key)
        if layer is None:
            return
        if self.task is not None:
            self.statusBar().showMessage("Wait for the current job before removing a layer.", 5000)
            return
        answer = QtWidgets.QMessageBox.question(
            self, APP_TITLE, f"Remove the layer {layer.title} from the project?\n\nThe file is not touched.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.map_layers.remove(layer)
        if self.selected_map == key:
            self.selected_map = None
        self.log(f"🗑 Layer removed: {layer.title}")
        self.volumes_panel.refresh()
        self._refresh_layers()
        self._refresh_all(keep_view=True)

    def _project_crs_code(self) -> str:
        if self.user_crs is not None:
            return self.user_crs.code
        return self._crs_code() if not crs_mod.is_query(self._crs_code()) else ""

    def _ensure_project_crs(self, code: str, first: bool) -> None:
        """The first layer of an empty project (no model, no other map layer) gives the project its CRS;
        later layers are drawn in the project CRS (converted)."""
        if not code or self.backend is None:
            return
        current = self.user_crs.code if self.user_crs is not None else ""
        if first and crs_mod.normalize(code) != crs_mod.normalize(current):
            self.log(f"   Project CRS set to that of the layer: {code}")
            self._pick_crs(code)
        elif self.user_crs is None:
            self._pick_crs(code)

    def open_raster(self) -> None:
        if self.task is not None:
            return
        start = str(self.settings.value("last_dir", str(Path.home()), type=str) or Path.home())
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(self, "Import a GeoTIFF (orthomosaic or DEM)", start,
                                                              RASTER_FILTER)
        if path:
            self.load_raster(path)

    def load_raster(self, path: str) -> None:
        """Read a GeoTIFF: its tags here (the CRS is asked if the file does not say it), the pixels in a job."""
        if self.task is not None:
            return
        self.settings.setValue("last_dir", str(Path(path).parent))
        try:
            info = raster_io.inspect(path, self.backend)
        except (raster_io.RasterError, OSError) as exc:
            QtWidgets.QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        if not info.crs:
            code = ask_crs(self, self.backend, info.name, ["The GeoTIFF has no coordinate system."],
                           self._project_crs_code(), self._recent_crs())
            if not code:
                return
            info.crs, info.crs_source = code, "chosen by you"
            raster_io._set_geographic(info, self.backend)

        def job(progress, log):
            if info.kind == raster_io.KIND_DEM:
                dem = raster_io.read_dem(info, lambda f, t: progress(0.8 * f, t))
                if dem.factor > 1:
                    log(f"   ⚠ Large DEM: averaged in blocks of {dem.factor}×{dem.factor} cells "
                        f"({dem.grid.cell_size_m():.3f} m) for the volumes.")
                progress(0.85, "Drawing the relief")
                return info, dem, volumes.dem_preview(dem)
            return info, info, raster_io.ortho_preview(info, progress)

        title = "Reading the DEM" if info.kind == raster_io.KIND_DEM else "Reading the orthomosaic"
        self._start_task(f"{title} {info.name}", job, self._raster_loaded)

    def _raster_loaded(self, result) -> None:
        info, data, preview = result
        kind = MAP_DEM if info.kind == raster_io.KIND_DEM else MAP_ORTHO
        first = not self.map_layers and self.model is None
        self._add_map_layer(kind, info.name, data, preview, str(info.path))
        self._ensure_project_crs(info.crs, first)
        self.log(f"✅ {info.describe()}" + (f" (CRS from {info.crs_source})" if info.crs_source else ""))
        if kind == MAP_DEM:
            self.log(f"   {data.describe()}")
        if self._is_3d():
            self.show_2d()
        self._refresh_all(keep_view=self.plan.has_content())
        if first:
            self.plan.fit()

    def open_polygon(self) -> None:
        if self.task is not None:
            return
        start = str(self.settings.value("last_dir", str(Path.home()), type=str) or Path.home())
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(self, "Import polygons", start, POLYGON_FILTER)
        if path:
            self.load_polygon(path)

    def load_polygon(self, path: str) -> None:
        """Polygons of a Shapefile, DXF, KML or KMZ (a DXF always asks its datum and UTM zone)."""
        self.settings.setValue("last_dir", str(Path(path).parent))
        try:
            polygons = vector_io.read_polygons(path, self.backend)
        except (vector_io.VectorError, OSError) as exc:
            QtWidgets.QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        if polygons.needs_crs or not polygons.crs:
            code = ask_crs(self, self.backend, Path(path).name, polygons.notes, self._project_crs_code(),
                           self._recent_crs())
            if not code:
                return
            polygons.crs, polygons.needs_crs = code, False
        first = not self.map_layers and self.model is None
        self._add_map_layer(MAP_POLYGON, polygons.name, polygons, path=path)
        self._ensure_project_crs(polygons.crs, first)
        self.volumes_panel.select_polygon(self.selected_map)
        self.log(f"✅ Polygon {polygons.name}: {polygons.describe(metres=self._metric(polygons.crs))} · {polygons.crs}")
        for note in polygons.notes:
            if "does not store its CRS" not in note and "choose its CRS" not in note:
                self.log(f"   ⚠ {note}")
        if self._is_3d():
            self.show_2d()
        self._refresh_all(keep_view=True)

    def _metric(self, code: str) -> bool:
        try:
            return self.backend is not None and self.backend.info(code).projected
        except CrsError:
            return False

    def start_drawing(self) -> None:
        """Draw a polygon on the plan view (in the project CRS)."""
        if self.crs_info is None:
            QtWidgets.QMessageBox.information(self, APP_TITLE, "Choose the project CRS first (step 1), or import a "
                                              "GeoTIFF: the polygon is drawn in the project CRS.")
            self.volumes_panel.drawing_changed(False)
            return
        if self.model is not None and not self._georeferenced():
            QtWidgets.QMessageBox.information(self, APP_TITLE, "The plan view shows the model in its local "
                                              "coordinates: place it with GCPs before drawing a polygon.")
            self.volumes_panel.drawing_changed(False)
            return
        if self.pick_btn.isChecked():
            self.pick_btn.setChecked(False)
        if self._is_3d():
            self.show_2d()
        self.plan.start_drawing()
        self.statusBar().showMessage("Click the vertices on the plan view; double click, right click or Enter "
                                     "closes the polygon; Esc cancels.", 10000)

    def _polygon_drawn(self, points) -> None:
        if self.crs_info is None:
            return
        count = sum(1 for l in self.map_layers if l.kind == MAP_POLYGON and l.data.source == "Drawn") + 1
        try:
            polygons = vector_io.drawn(points, self.crs_info.code, f"Drawn polygon {count}")
        except vector_io.VectorError as exc:
            QtWidgets.QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self._add_map_layer(MAP_POLYGON, polygons.name, polygons)
        self.volumes_panel.select_polygon(self.selected_map)
        self.log(f"✅ {polygons.name}: {len(polygons.rings[0])} vertices · {polygons.describe()} · {polygons.crs}")
        self._refresh_map_view()

    def add_volume_layer(self, result: volumes.VolumeResult, replace: int | None = None) -> int:
        """The cut / fill of a volume as a layer (the previous result of the page is replaced)."""
        old = self._map_layer(replace) if replace is not None else None
        if old is not None and old.kind == MAP_VOLUME:
            old.name, old.data, old.preview = result.title(), result, result.preview
            old.image, old.placed, old.visible = None, ("", None), True
            self.selected_map = old.key
            self._refresh_layers()
            key = old.key
        else:
            key = self._add_map_layer(MAP_VOLUME, result.title(), result, result.preview).key
        if self._is_3d():
            self.show_2d()
        self._refresh_map_view()
        self._update_steps()
        return key

    def convert_ortho(self, info: raster_io.RasterInfo, path: str) -> None:
        """Save an orthomosaic as ROLAVI Viewer stores it: a .zip with the JPG and its .aux.xml."""
        backend = self.backend
        source = info.path.stat().st_size if info.path.is_file() else 0

        def job(progress, _log):
            return raster_io.write_ortho_zip(info, path, backend, progress=progress)

        def done(result: raster_io.OrthoZip) -> None:
            size = Path(result.path).stat().st_size
            if result.pieces > 1:
                what = f"{result.pieces} JPG pieces + mosaic.json, {result.width:,} × {result.height:,} px in all"
            else:
                reduced = f", reduced 1/{result.factor}" if result.factor > 1 else ""
                what = f"{result.width:,} × {result.height:,} px JPG{reduced}"
            self.log(f"💾 {Path(result.path).name}: {size / 1048576.0:,.1f} MB ({what}) — the GeoTIFF is "
                     f"{source / 1048576.0:,.1f} MB")

        self._start_task(f"Saving {info.name} as JPG + .aux.xml", job, done)

    def _qimage(self, layer: MapLayer):
        if layer.image is None and layer.preview is not None:
            rgba = np.ascontiguousarray(layer.preview.rgba, dtype=np.uint8)
            height, width = rgba.shape[:2]
            layer.image = QtGui.QImage(rgba.tobytes(), width, height, 4 * width,
                                       QtGui.QImage.Format.Format_RGBA8888).copy()
        return layer.image

    def _placed(self, layer: MapLayer, view_crs: str):
        """Where a map layer falls in the view's CRS: the corners of a raster, the rings of polygons."""
        if layer.placed[0] == view_crs and layer.placed[1] is not None:
            return layer.placed[1]
        if layer.kind == MAP_POLYGON:
            placed = layer.data.in_crs(self.backend, view_crs).rings
        else:
            grid = layer.preview.grid
            corners = grid.corners()[[0, 1, 3]]          # top-left, top-right, bottom-left
            x, y = crs_mod.transform(self.backend, grid.crs, view_crs, corners[:, 0], corners[:, 1])
            placed = tuple((float(a), float(b)) for a, b in zip(x, y))
        layer.placed = (view_crs, placed)
        return placed

    def _refresh_map_view(self) -> None:
        """The visible map layers in the plan view (in the project CRS; not over a local model)."""
        if not self._ui_ready:
            return
        overlays, polygons = [], []
        can_place = self.crs_info is not None and self.backend is not None and (
            self.model is None or self._georeferenced())
        if can_place:
            view_crs = self.crs_info.code
            used = self.volumes_panel.used_polygon()
            for layer in self.map_layers:
                if not layer.visible:
                    continue
                try:
                    placed = self._placed(layer, view_crs)
                except (CrsError, vector_io.VectorError, ValueError) as exc:
                    self.statusBar().showMessage(f"{layer.title}: {exc}", 7000)
                    continue
                if layer.kind == MAP_POLYGON:
                    polygons.append({"rings": placed, "used": layer.key == used})
                else:
                    overlays.append({"image": self._qimage(layer), "corners": placed,
                                     "opacity": 0.92 if layer.kind == MAP_VOLUME else 1.0})
        self.plan.set_overlays(overlays)
        self.plan.set_polygons(polygons)

    def _map_value_at(self, x: float, y: float) -> str:
        """The elevation of the DEM and the cut / fill under the cursor (the top visible layers)."""
        if self.crs_info is None or self.backend is None:
            return ""
        view_crs = self.crs_info.code
        parts = []
        for layer in reversed(self.map_layers):
            if not layer.visible or layer.kind not in (MAP_DEM, MAP_VOLUME):
                continue
            grid_crs = layer.data.grid.crs
            try:
                gx, gy = crs_mod.transform(self.backend, view_crs, grid_crs, np.array([x]), np.array([y]))
            except CrsError:
                continue
            if layer.kind == MAP_VOLUME and not any(p.startswith("Δz") for p in parts):
                dz = layer.data.dz_at(float(gx[0]), float(gy[0]))
                if dz is not None:
                    word = "fill" if dz > layer.data.threshold else "cut" if dz < -layer.data.threshold else "no change"
                    parts.append(f"Δz {dz:+,.3f} m ({word})")
            elif layer.kind == MAP_DEM and not any(p.startswith("DEM") for p in parts):
                z = layer.data.elevation_at(float(gx[0]), float(gy[0]))
                if z is not None:
                    parts.append(f"DEM Z {z:,.3f}")
        return "   ".join(parts)

    def _crs_code(self) -> str:
        text = self.crs_combo.currentText().strip()
        index = self.crs_combo.findText(text)
        if index >= 0 and self.crs_combo.itemData(index):
            return str(self.crs_combo.itemData(index))
        return crs_mod.normalize(text)

    def _recent_crs(self) -> list[str]:
        value = self.settings.value("recent_crs", [], type=list) or []
        return [str(code) for code in value if str(code).strip()][:8]

    def _remember_crs(self, code: str) -> None:
        recent = [code] + [c for c in self._recent_crs() if c != code]
        self.settings.setValue("recent_crs", recent[:8])

    def search_crs(self, text: str = "") -> None:
        """The CRS search dialog; the CRS picked becomes the project CRS."""
        dialog = CrsDialog(self.backend, text, self._recent_crs(), self)
        if not exec_(dialog) or not dialog.chosen:
            # back to the CRS in use (a name left in the field would open the search again)
            self._set_crs_text(self.user_crs.code if self.user_crs is not None else "")
            return
        self._pick_crs(dialog.chosen)

    def _pick_crs(self, code: str) -> None:
        if self.crs_combo.findData(code) < 0:
            name = code
            if self.backend is not None:
                try:
                    name = self.backend.info(code).name
                except CrsError:
                    pass
            self.crs_combo.addItem(f"{code} - {name}", code)
        self._set_crs_text(code)
        self._crs_changed()

    def _geographic(self) -> bool:
        """The chosen CRS is geographic: coordinates are shown and typed as longitude / latitude."""
        return self.user_crs is not None and not self.user_crs.projected

    def _working_zone(self) -> str:
        """For a geographic CRS: the WGS 84 / UTM zone of the model (or of the GCPs) the window works in."""
        user = self.user_crs
        lonlat = None
        try:
            model = self.model
            scan = model.scan if model is not None else None
            if scan is not None and scan.placeable and self.scan_check.isChecked():
                lonlat = (scan.georef.lon0, scan.georef.lat0)
            elif model is not None and self.coords_combo.currentIndex() == COORDS_GEOREF:
                centre = self._manual_frame().apply(((model.bbox_min + model.bbox_max) / 2.0).reshape(1, 3))[0]
                lon, lat = self.backend.to_lonlat(user.code, np.array([centre[0]]), np.array([centre[1]]))
                lonlat = (float(lon[0]), float(lat[0]))
            else:
                known = [g.known for g in self.gcp_table.gcps() if g.known is not None]
                if known:
                    mean = np.mean(np.array(known, dtype=np.float64), axis=0)
                    lon, lat = self.backend.to_lonlat(user.code, np.array([mean[0]]), np.array([mean[1]]))
                    lonlat = (float(lon[0]), float(lat[0]))
        except (CrsError, ValueError, TypeError):
            lonlat = None
        if lonlat is None and self.map_layers and self.backend is not None:
            first = self.map_layers[0]
            try:
                if first.kind == MAP_POLYGON:
                    x0, y0, x1, y1 = first.data.bounds()
                    crs = first.data.crs
                else:
                    x0, y0, x1, y1 = first.preview.grid.bounds()
                    crs = first.preview.grid.crs
                lon, lat = self.backend.to_lonlat(crs, np.array([(x0 + x1) / 2.0]), np.array([(y0 + y1) / 2.0]))
                lonlat = (float(lon[0]), float(lat[0]))
            except (CrsError, ValueError, TypeError):
                lonlat = None
        if lonlat is not None and abs(lonlat[0]) <= 180.0 and abs(lonlat[1]) <= 84.0:
            self._zone_code = crs_mod.utm_code(lonlat[0], lonlat[1])
        return self._zone_code

    def _shown_lonlat(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Working coordinates (metres) -> longitude / latitude in the chosen geographic CRS."""
        lon, lat = self.backend.to_lonlat(self.crs_info.code, np.asarray(x, dtype=np.float64),
                                          np.asarray(y, dtype=np.float64))
        if crs_mod.normalize(self.user_crs.code) != crs_mod.WGS84:
            lon, lat = self.backend.from_lonlat(self.user_crs.code, lon, lat)
        return lon, lat

    def _known_world(self, known) -> tuple[float, float, float] | None:
        """Known GCP coordinates typed in the chosen CRS -> the CRS the window works in."""
        if known is None:
            return None
        if not self._geographic() or self.crs_info is None:
            return (float(known[0]), float(known[1]), float(known[2]))
        array = self._known_array([known])[0]
        return (float(array[0]), float(array[1]), float(array[2]))

    def _known_array(self, knowns: list) -> np.ndarray:
        known = np.array(knowns, dtype=np.float64).reshape(-1, 3)
        if self._geographic() and self.crs_info is not None and len(known):
            lon, lat = self.backend.to_lonlat(self.user_crs.code, known[:, 0], known[:, 1])
            x, y = self.backend.from_lonlat(self.crs_info.code, lon, lat)
            known = np.column_stack([x, y, known[:, 2]])
        return known

    def _set_crs_text(self, code: str) -> None:
        code = crs_mod.normalize(code)
        index = self.crs_combo.findData(code)
        if index >= 0:
            self.crs_combo.setCurrentIndex(index)
        else:
            self.crs_combo.setEditText(code)

    def _height_model(self) -> str:
        return str(self.height_combo.currentData() or pipeline.HEIGHT_ELLIPSOIDAL)

    # ------------------------------------------------------------------ geoid
    def _load_geoid(self, path: str) -> None:
        self.geoid, self.geoid_error = None, ""
        if path:
            try:
                self.geoid = GtxGeoid(path)
                self.settings.setValue("geoid", path)
            except GeoidError as exc:
                self.geoid_error = str(exc)

    def choose_geoid(self) -> None:
        start = self.geoid_edit.text().strip() or str(self.settings.value("last_dir", str(Path.home()), type=str))
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(self, "Geoid model", start,
                                                              "Geoid model (*.gtx);;All files (*)")
        if path:
            self.geoid_edit.setText(path)
            self._geoid_changed()

    def _geoid_changed(self, *_args) -> None:
        path = self.geoid_edit.text().strip().strip('"')
        if self.geoid is not None and str(self.geoid.path) == path:
            return
        self._load_geoid(path)
        if self.geoid is not None:
            self.log(f"   Geoid      : {self.geoid.describe()}")
        elif self.geoid_error:
            self.log(f"❌ {self.geoid_error}")
        self._refresh_state()

    def _geoid_wanted(self) -> bool:
        return self._height_model() == pipeline.HEIGHT_ORTHOMETRIC and self.geoid_convert.isChecked()

    def _geoid_at_centre(self) -> float | None:
        """N at the centre of the model (None without model or CRS, NaN outside the geoid)."""
        if self.geoid is None or self.world is None or self.crs_info is None or self.backend is None:
            return None
        if not self._georeferenced():
            return None
        low, high = self.world.min(axis=0), self.world.max(axis=0)
        try:
            lon, lat = self.backend.to_lonlat(self.crs_info.code, np.array([(low[0] + high[0]) / 2.0]),
                                              np.array([(low[1] + high[1]) / 2.0]))
        except CrsError:
            return None
        return float(self.geoid.undulation(lon, lat)[0])

    def _update_geoid_label(self) -> None:
        if not self.geoid_convert.isChecked():
            self._set_label(self.geoid_label, "The Z of the model is already orthometric: nothing is converted.")
        elif self.geoid_error:
            self._set_label(self.geoid_label, self.geoid_error, "Bad")
        elif self.geoid is None:
            self._set_label(self.geoid_label, "Choose the geoid model (GTX) of the project, e.g. EGM2008.")
        else:
            n = self._geoid_at_centre()
            if n is not None and not math.isfinite(n):
                self._set_label(self.geoid_label, f"{self.geoid.describe()}\nThe model falls outside this geoid model.",
                                "Bad")
            elif n is not None:
                self._set_label(self.geoid_label, f"{self.geoid.describe()}\nN at the model centre: {n:.3f} m", "Good")
            else:
                self._set_label(self.geoid_label, self.geoid.describe(), "Good")

    def _adjustment(self) -> Similarity | None:
        if self.use_gcp.isChecked() and self.solution is not None:
            return self.solution.transform
        return None

    def _georeferenced(self) -> bool:
        """True when the displayed coordinates are in the project CRS."""
        return (not self._is_local()) or self._adjustment() is not None

    def _solution_signature(self) -> str:
        return json.dumps({"frame": self._frame().to_dict(), "crs": self.crs_info.code if self.crs_info else "",
                           "user": self.user_crs.code if self.user_crs else ""},
                          sort_keys=True)

    def _drop_solution(self, reason: str = "") -> None:
        if self.solution is None:
            return
        self.solution = None
        self.solution_key = None
        self.solution_stale = False
        self.gcp_table.set_residuals({})
        self._set_label(self.solve_label, "No adjustment.")
        if reason:
            self.log(f"⚠️ {reason}")

    def _update_crs_info(self) -> None:
        """The chosen CRS (``user_crs``) and the one the window works in (``crs_info``: projected, metres)."""
        code = self._crs_code()
        previous_user = self.user_crs.code if self.user_crs is not None else ""
        self.crs_info = None
        self.user_crs = None
        if self.backend is None:
            self._set_label(self.crs_label, self.backend_error, "Bad")
        elif not code:
            self._set_label(self.crs_label, "Search the CRS (🔍) or type its EPSG code, e.g. 32718.", "Bad")
        elif crs_mod.is_query(code):
            self._set_label(self.crs_label, f"'{self.crs_combo.currentText().strip()}' is not a CRS code: press "
                            "🔍 Search to find it by name.", "Bad")
        else:
            try:
                self.user_crs = self.backend.info(code)
            except CrsError as exc:
                self._set_label(self.crs_label, str(exc), "Bad")
        user = self.user_crs
        if user is not None and user.projected:
            try:
                self.crs_info = self.backend.require_projected(user.code)
                self._set_label(self.crs_label, self.crs_info.label, "Good")
            except CrsError as exc:
                self._set_label(self.crs_label, str(exc), "Bad")
        elif user is not None:
            if user.code != previous_user:
                self._zone_code = ""
            zone = self._working_zone()
            if zone:
                try:
                    self.crs_info = self.backend.require_projected(zone)
                    self._set_label(self.crs_label, f"{user.code} - {user.name} (geographic: latitude / longitude in "
                                    f"degrees).\nWorked in {self.crs_info.code} - {self.crs_info.name} (metres) for "
                                    "the GCP adjustment and the views.", "Good")
                except CrsError as exc:
                    self._set_label(self.crs_label, str(exc), "Bad")
            else:
                self._set_label(self.crs_label, f"{user.code} - {user.name} (geographic). Import the model, or type "
                                "the longitude / latitude of the GCPs, to choose the UTM zone it is worked in.", "Info")
        if user is not None:
            self.settings.setValue("crs", user.code)
            self._remember_crs(user.code)
        geographic = self._geographic()
        self.gcp_table.set_geographic(geographic)
        self.order_combo.setItemText(0, "No header: Name, Lon, Lat, h" if geographic else "No header: Name, E, N, H")
        self.order_combo.setItemText(1, "No header: Name, Lat, Lon, h" if geographic else "No header: Name, N, E, H")

    # ================================================================== reactions
    def _crs_changed(self, *_args) -> None:
        if self._updating:
            return
        if crs_mod.is_query(self._crs_code()) and self.backend is not None:
            self.search_crs(self.crs_combo.currentText().strip())   # a name typed: search it
            return
        before = (self.crs_info.code if self.crs_info else "", self.user_crs.code if self.user_crs else "")
        self._update_crs_info()
        after = (self.crs_info.code if self.crs_info else "", self.user_crs.code if self.user_crs else "")
        if before != after:
            self._drop_solution("The CRS changed: the GCP adjustment was cleared, solve it again.")
            self._drop_other_solutions()
            self._refresh_all(keep_view=False)
        else:
            self._refresh_state()

    def _drop_other_solutions(self) -> None:
        """The GCP adjustments of the other layers belong to the previous CRS."""
        names = []
        for layer in self.layers:
            if layer is not self.active and layer.solution is not None:
                layer.solution, layer.solution_key, layer.solution_stale = None, None, False
                layer.state["solve_text"] = "No adjustment."
                names.append(layer.title)
        if names:
            self.log(f"⚠️ The CRS changed: the GCP adjustment of {', '.join(names)} was cleared too.")

    def _frame_key(self) -> str:
        return json.dumps({"frame": self._frame().to_dict(), "local": self._is_local()}, sort_keys=True)

    def _frame_changed(self, *_args) -> None:
        if not self._updating and self._geographic():
            self._update_crs_info()        # the UTM zone worked in follows the model
        if self._updating or self._frame_key() == self._shown_frame_key:
            return  # e.g. leaving an offset field without changing it
        if self._is_local() and not self.use_gcp.isChecked():
            self._updating = True
            self.use_gcp.setChecked(True)
            self._updating = False
        if self.solution is not None and self.solution_key != self._solution_signature():
            self._drop_solution("The model frame changed: the GCP adjustment was cleared, solve it again.")
        self._refresh_all(keep_view=False)

    def _height_changed(self, *_args) -> None:
        if not self._updating:
            self._refresh_state()

    def _gcp_use_toggled(self, *_args) -> None:
        if self._updating:
            return
        if not self.use_gcp.isChecked() and self.pick_btn.isChecked():
            self.pick_btn.setChecked(False)
        self._refresh_all(keep_view=False)

    def _gcps_edited(self) -> None:
        if self._geographic() and self.crs_info is None and not self._updating:
            self._update_crs_info()        # the GCPs may give the UTM zone to work in
            if self.crs_info is not None:
                self._refresh_all(keep_view=False)
                return
        if self.solution is not None:
            self.solution_stale = True
        self._update_markers()
        self._refresh_state()

    def _mark_stale(self, *_args) -> None:
        if self.solution is not None:
            self.solution_stale = True
        self._refresh_state()

    def _clear_adjustment_clicked(self) -> None:
        self._drop_solution()
        self.log("Adjustment cleared: the model is used as it is.")
        self._refresh_all(keep_view=False)

    # ================================================================== refresh
    def _refresh_all(self, keep_view: bool = True) -> None:
        self._refresh_view(keep_view)
        self._refresh_state()

    def _refresh_view(self, keep_view: bool) -> None:
        if self.model is None:
            self.world = None
            self._shown_frame_key = ""
            self.plan.clear()
            if self.view3d is not None:
                self.view3d.clear_model()
            self._refresh_map_view()
            if self.map_layers and self.crs_info is not None:
                code = self.crs_info.code
                self.plan.set_caption("", True)
                self.basemap_layer.set_crs(self.backend, code)
                self.plan_caption.setText(f"Plan view  ·  Project CRS {code} - E / N")
                self._set_label(self.view_label, "No model yet: the plan view shows the GeoTIFF and polygon "
                                "layers.", "Info")
            else:
                self.plan.set_caption("", False)
                self.plan_caption.setText("Plan view")
                self._set_label(self.view_label, "No model yet.", "Info")
            return
        self.world = pipeline.to_world(self.model.points_obj, self._frame(), self._adjustment())
        self._shown_frame_key = self._frame_key()
        georeferenced = self._georeferenced()
        code = self.crs_info.code if self.crs_info else "?"
        points, colours = [], []
        if georeferenced:      # the other visible layers in the project CRS, under the active one
            for layer in self.layers:
                if layer is not self.active and layer.visible and self._georeferenced_for(layer):
                    points.append(self._layer_preview(layer))
                    colours.append(layer.model.colors)
        if self.active is None or self.active.visible:
            points.append(self.world)
            colours.append(self.model.colors)
        if points:
            self.plan.set_points(np.concatenate(points), np.concatenate(colours),
                                 keep_view=keep_view and self.plan.has_points())
        else:
            self.plan.clear()
        if georeferenced and self._geographic():
            caption = (f"{self.user_crs.code} lat / lon - drawn in {code}"
                       + ("  (adjusted with GCPs)" if self._adjustment() is not None
                          else "  (ROLAVIScan georeference)" if self._scan_active() else ""))
        elif georeferenced:
            caption = f"Project CRS {code} - E / N" + ("  (adjusted with GCPs)" if self._adjustment() is not None
                                                      else "  (ROLAVIScan georeference)" if self._scan_active() else "")
        else:
            caption = "Local model coordinates (not georeferenced yet)"
        self.plan.set_caption("", georeferenced)       # the caption goes in the bar above the view
        self._refresh_map_view()
        self.basemap_layer.set_crs(self.backend, self.crs_info.code if georeferenced and self.crs_info else "")
        self.plan_caption.setText(f"Plan view  ·  {caption}")
        low = self.world.min(axis=0)
        high = self.world.max(axis=0)
        lines = [caption]
        if georeferenced and self._geographic():
            try:
                lon, lat = self._shown_lonlat(np.array([low[0], high[0], low[0], high[0]]),
                                              np.array([low[1], low[1], high[1], high[1]]))
                lines.append(f"Lat {lat.min():.8f} .. {lat.max():.8f}   Lon {lon.min():.8f} .. {lon.max():.8f}   "
                             f"h {low[2]:,.3f} .. {high[2]:,.3f}")
            except CrsError as exc:
                lines.append(str(exc))
            lines.append(f"Worked in {self.crs_info.code} (metres): the plan view is drawn in it.")
        elif georeferenced:
            lines.append(f"E {low[0]:,.3f} .. {high[0]:,.3f}   N {low[1]:,.3f} .. {high[1]:,.3f}   "
                         f"H {low[2]:,.3f} .. {high[2]:,.3f}")
            if self.crs_info is not None and self.backend is not None:
                try:
                    lon, lat = self.backend.to_lonlat(self.crs_info.code, np.array([(low[0] + high[0]) / 2.0]),
                                                      np.array([(low[1] + high[1]) / 2.0]))
                    lines.append(f"Centre in WGS84: latitude {lat[0]:.8f}, longitude {lon[0]:.8f}")
                except CrsError as exc:
                    lines.append(f"Centre in WGS84: {exc}")
        else:
            lines.append(f"x {low[0]:,.3f} .. {high[0]:,.3f}   y {low[1]:,.3f} .. {high[1]:,.3f}   "
                         f"z {low[2]:,.3f} .. {high[2]:,.3f}")
        lines.append(f"Size: {high[0] - low[0]:,.2f} x {high[1] - low[1]:,.2f} x {high[2] - low[2]:,.2f}")
        self._set_label(self.view_label, "\n".join(lines), "Info")
        self._update_markers()
        if self._is_3d() and self.display_mesh is not None and self.display_mesh.path == self.model.path:
            self._push_3d(reset=not keep_view)

    def _update_markers(self) -> None:
        if self.model is None:
            return
        frame = self._frame()
        adjustment = self._adjustment()
        georeferenced = self._georeferenced()
        selected = self.gcp_table.selected_row()
        markers = []
        for row, gcp in enumerate(self.gcp_table.gcps()):
            model_xy = None
            if gcp.obj is not None:
                point = pipeline.to_world(np.array([gcp.obj], dtype=np.float64), frame, adjustment)[0]
                model_xy = (float(point[0]), float(point[1]))
            known_world = self._known_world(gcp.known) if georeferenced else None
            known_xy = (known_world[0], known_world[1]) if known_world is not None else None
            if model_xy is None and known_xy is None:
                continue
            markers.append({"name": gcp.name, "model": model_xy, "known": known_xy, "selected": row == selected})
        self.plan.set_markers(markers)
        if self.view3d is not None and self._is_3d() and self.display_mesh is not None:
            self.view3d.set_markers(self._markers_3d())

    def _export_blocker(self) -> str | None:
        if self.backend is None:
            return self.backend_error
        if self.model is None:
            return "Import a model first."
        if self.crs_info is None:
            return "Choose the projected CRS of the model."
        if self._geoid_wanted() and self.geoid is None:
            return "Choose the geoid model (GTX) for the orthometric heights, or untick the conversion (step 1)."
        if self.use_gcp.isChecked():
            if self.solution is None:
                if self._is_local():
                    return "A local model must be placed with GCPs: press Solve first."
                return "Press Solve to adjust the model, or untick the GCP adjustment."
            if self.solution_stale:
                return "The GCPs or the adjustment settings changed since the last Solve: press Solve again."
        if not self.out_edit.text().strip():
            return "Choose the output SLPK file."
        return None

    def _refresh_state(self, *_args) -> None:
        busy = self.task is not None
        has_model = self.model is not None
        local = self._is_local()
        gcp_on = self.use_gcp.isChecked()
        scan_on = self._scan_active()
        for widget in (self.open_btn, self.crs_combo, self.height_combo,
                       self.layer_edit, self.out_edit, self.out_btn, self.texture_combo, self.tile_spin,
                       self.jpeg_spin):
            widget.setEnabled(not busy)
        self.scan_check.setEnabled(not busy and has_model and self.model.scan is not None
                                   and self.model.scan.placeable)
        self.layers_panel.set_enabled(not busy)
        for widget in (self.coords_combo, self.up_combo, *self.offset_edits):
            widget.setEnabled(not busy and not scan_on)       # the georeference places the model
        self.quality_card.setVisible(not has_model or self.model.kind == KIND_MESH)
        self.use_gcp.setEnabled(not busy and not local)
        for widget in self.gcp_widgets:
            widget.setEnabled(not busy and gcp_on)
        self.pick_btn.setEnabled(not busy and gcp_on and has_model)
        if not self.pick_btn.isEnabled() and self.pick_btn.isChecked():
            self.pick_btn.setChecked(False)
        self.check_btn.setEnabled(not busy and gcp_on and has_model and not local and self.crs_info is not None)
        self.solve_btn.setEnabled(not busy and gcp_on and has_model)
        self.clear_solve_btn.setEnabled(not busy and self.solution is not None)
        self.fit_btn.setEnabled(has_model or bool(self.map_layers))
        self.top_btn.setVisible(self._is_3d())
        self.top_btn.setEnabled(self.view3d is not None and self.display_mesh is not None)
        self.las_btn.setEnabled(not busy and has_model and self.crs_info is not None and self._georeferenced())
        orthometric = self._height_model() == pipeline.HEIGHT_ORTHOMETRIC
        self.geoid_box.setVisible(orthometric)
        self.geoid_convert.setEnabled(not busy)
        self.geoid_edit.setEnabled(not busy and self.geoid_convert.isChecked())
        self.geoid_btn.setEnabled(not busy and self.geoid_convert.isChecked())
        if orthometric:
            self._update_geoid_label()
        blocker = self._export_blocker()
        self.export_btn.setEnabled(not busy and blocker is None)
        if blocker:
            self._set_label(self.export_hint, blocker, "Muted")
        else:
            geoid = f", converted with {self.geoid.path.name}" if self._geoid_wanted() and self.geoid else ""
            if self.model.kind == KIND_MESH:
                layer = "IntegratedMesh I3S 1.7"
            elif self.model.kind == KIND_SPLATS:
                layer = "PointCloud I3S 2.0 (the centres of the Gaussians, with their colour)"
            else:
                layer = "PointCloud I3S 2.0"
            self._set_label(self.export_hint, f"Ready: WGS84 (EPSG:4326) {layer}, heights "
                            f"{pipeline.HEIGHT_LABELS[self._height_model()].lower()}{geoid}.", "Good")
        if self.solution is not None and self.solution_stale:
            self._set_label(self.solve_label, "The GCPs or the settings changed since the last Solve: press Solve again.",
                            "Bad")
        self.cancel_btn.setEnabled(busy and not self.task.is_cancel_requested())
        self.volumes_panel._update_state()
        self.viewer_panel.refresh_state(busy)      # also updates the workflow rail (viewer_state_changed)

    def _cursor_moved(self, x: float, y: float) -> None:
        extra = ""
        if self.map_layers and (self.model is None or self._georeferenced()):
            extra = self._map_value_at(x, y)
            extra = f"   {extra}" if extra else ""
        if self._georeferenced() and self._geographic() and self.crs_info is not None:
            try:
                lon, lat = self._shown_lonlat(np.array([x]), np.array([y]))
                self.coords_label.setText(f"Lat {lat[0]:.8f}   Lon {lon[0]:.8f}{extra}   {self.user_crs.code}")
            except CrsError:
                self.coords_label.setText(f"E {x:,.3f}   N {y:,.3f}{extra}   {self.crs_info.code}")
        elif self._georeferenced():
            code = self.crs_info.code if self.crs_info else ""
            self.coords_label.setText(f"E {x:,.3f}   N {y:,.3f}{extra}   {code}")
        else:
            self.coords_label.setText(f"x {x:,.3f}   y {y:,.3f}   (local model)")

    def _cursor_3d(self, x: float, y: float, z: float, on_model: bool) -> None:
        """The point of the 3D model under the cursor, with its height."""
        if not on_model:
            self.coords_label.setText("3D: move over the model")
            return
        if self._georeferenced() and self._geographic() and self.crs_info is not None:
            try:
                lon, lat = self._shown_lonlat(np.array([x]), np.array([y]))
                self.coords_label.setText(f"Lat {lat[0]:.8f}   Lon {lon[0]:.8f}   h {z:,.3f}   {self.user_crs.code}")
                return
            except CrsError:
                pass
        if self._georeferenced():
            code = self.crs_info.code if self.crs_info else ""
            self.coords_label.setText(f"E {x:,.3f}   N {y:,.3f}   Z {z:,.3f}   {code}")
        else:
            self.coords_label.setText(f"x {x:,.3f}   y {y:,.3f}   z {z:,.3f}   (local model)")

    # ================================================================== model
    def open_obj(self) -> None:
        if self.task is not None:
            return
        start = str(self.settings.value("last_dir", str(Path.home()), type=str) or Path.home())
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(self, "Import model", start, MODEL_FILTER)
        if path:
            self.load_obj(path)

    def load_obj(self, path: str) -> None:
        """Read a model: OBJ, or PLY (mesh, point cloud, 3DGS)."""
        if self.task is not None:
            return
        self.settings.setValue("last_dir", str(Path(path).parent))
        backend = self.backend

        def job(progress, log):
            return pipeline.load_any(path, backend, PREVIEW_POINTS, progress, log)

        self._start_task(f"Reading {Path(path).name}", job, self._model_loaded)

    def _model_loaded(self, model: pipeline.LoadedModel) -> None:
        """A new layer, made the active one (the others keep their settings)."""
        previous = self.model.path if self.model is not None else None
        crs_before = self.crs_info.code if self.crs_info else ""
        self._save_active()
        self.selected_map = None
        self._layer_seq += 1
        layer = Layer(key=self._layer_seq, model=model)
        self.layers.append(layer)
        self.active = layer
        self.model = model
        self._exported_file = ""
        if self.pick_btn.isChecked():
            self.pick_btn.setChecked(False)
        self._drop_solution()
        self.solution, self.solution_key, self.solution_stale = None, None, False
        self._set_label(self.solve_label, "No adjustment.")
        if previous is not None and previous != model.path and self.gcp_table.rowCount():
            gcps = self.gcp_table.gcps()
            for gcp in gcps:
                gcp.obj, gcp.residual = None, None
            self.gcp_table.set_gcps(gcps)
            self.log("   The model coordinates of the GCPs belonged to the previous model: pick them again.")

        hints = model.hints
        scan = model.scan
        placed = scan is not None and scan.placeable
        biggest = float(np.max(np.abs(np.concatenate([model.bbox_min, model.bbox_max]))))
        self._updating = True
        try:
            self.scan_check.setChecked(placed)
            if scan is not None:
                # the settings by hand, used when the georeference is not (or cannot be) used
                if hints.offset is None:
                    for edit in self.offset_edits:
                        edit.setText("0")
                if scan.frame == scan_io.FRAME_AR:
                    self.coords_combo.setCurrentIndex(COORDS_LOCAL)   # ARKit world: Y up, local
                    self.up_combo.setCurrentIndex(1)
                elif scan.frame == scan_io.FRAME_ENU and biggest < GEOREF_COORDINATE:
                    self.coords_combo.setCurrentIndex(COORDS_LOCAL)
                    self.up_combo.setCurrentIndex(0)
                elif scan.frame == scan_io.FRAME_NONE:
                    self.up_combo.setCurrentIndex(0)                  # a PLY without georeference: Z up
                    lonlat = (abs(float(model.bbox_min[0])) <= 180.0 and abs(float(model.bbox_max[0])) <= 180.0
                              and abs(float(model.bbox_min[1])) <= 90.0 and abs(float(model.bbox_max[1])) <= 90.0)
                    if self._geographic() and lonlat:
                        self.coords_combo.setCurrentIndex(COORDS_GEOREF)   # longitude / latitude of the CRS
            if placed and (self.user_crs is None or (len(self.layers) == 1 and self.user_crs.projected)):
                self._set_crs_text(f"EPSG:{scan.georef.origin_epsg}")
            if hints.crs:
                self._set_crs_text(hints.crs)
            if hints.offset is not None:
                for edit, value in zip(self.offset_edits, hints.offset):
                    edit.setText(f"{value:.6f}".rstrip("0").rstrip("."))
            elif biggest >= GEOREF_COORDINATE:
                for edit in self.offset_edits:
                    edit.setText("0")
            if hints.height_model:
                index = self.height_combo.findData(hints.height_model)
                if index >= 0:
                    self.height_combo.setCurrentIndex(index)
                if hints.height_model == pipeline.HEIGHT_ORTHOMETRIC:
                    self.geoid_convert.setChecked(False)      # its Z is already orthometric
            if hints.offset is not None or biggest >= GEOREF_COORDINATE:
                self.coords_combo.setCurrentIndex(COORDS_GEOREF)
                self.up_combo.setCurrentIndex(0)
            self.layer_edit.setText(model.opened.stem)
            self.out_edit.setText(str(model.opened.with_suffix(".slpk")))
            # a new layer starts without adjustment (GCPs on for a local model: it needs them)
            self.use_gcp.setChecked(not placed and self.coords_combo.currentIndex() == COORDS_LOCAL)
        finally:
            self._updating = False

        for line in model.summary_lines():
            self.log(f"   {line}")
        if placed:
            self.log(f"   Placement  : ROLAVIScan georeference ({scan.georef.source}), file in "
                     f"{scan_io.FRAME_LABELS.get(scan.frame, scan.frame)}")
            for line in scan.georef.describe():
                self.log(f"                {line}")
            self.log(f"   CRS        : EPSG:{scan.georef.origin_epsg} is the UTM of the capture. Any CRS can be chosen "
                     "(🔍 Search): the model is placed exactly in it.")
        elif scan is not None and scan.georef is not None:
            for line in scan.georef.describe():
                self.log(f"⚠️ {line}")
        if placed or model.kind != KIND_MESH:
            pass
        elif hints.offset is not None:
            self.log(f"   Offset from {', '.join(hints.sources)}: {hints.offset[0]:.3f}, {hints.offset[1]:.3f}, {hints.offset[2]:.3f}")
        elif biggest >= GEOREF_COORDINATE:
            self.log("   The OBJ coordinates look georeferenced (projected CRS): check the CRS of the model.")
        else:
            self.log("ℹ️ The OBJ coordinates are small: if the OBJ was exported with a shift, type that offset; "
                     "if it is a local model, choose 'Local model' and place it with GCPs.")
        if not placed and model.kind != KIND_MESH and biggest < GEOREF_COORDINATE and scan is not None \
                and scan.georef is None:
            self.log("ℹ️ The coordinates are small and the file has no georeference: choose 'Local' and place it "
                     "with GCPs, or type its offset.")
        if model.kind == KIND_MESH and not model.textures and model.vertex_colors:
            self.log("   Colours    : per vertex (no texture image): the SLPK keeps them.")
        elif model.kind == KIND_MESH and not model.textures:
            self.log("⚠️ No texture image and no vertex colours: the SLPK will be grey.")
        self._show_model_info(model)
        self._update_crs_info()
        if crs_before and self.crs_info is not None and self.crs_info.code != crs_before:
            self._drop_other_solutions()
        self.log(f"   Layer      : {layer.title} added ({len(self.layers)} in the project)")
        self._refresh_layers()
        if self._is_3d():
            QtCore.QTimer.singleShot(0, lambda: self._ensure_3d(reset=len(self.layers) == 1))
        self._refresh_all(keep_view=len(self.layers) > 1)

    def _show_model_info(self, model: pipeline.LoadedModel | None) -> None:
        """File, facts and georeference of the active layer on the Model page."""
        if model is None:
            self.obj_edit.setText("")
            for label in self.fact_labels.values():
                label.setVisible(False)
            self.scan_box.setVisible(False)
            self._set_label(self.model_label, "Import a textured OBJ (MTL and textures beside it), or a PLY: mesh, "
                            "point cloud or 3DGS splats.", "Info")
            return
        self.obj_edit.setText(str(model.opened))
        scan = model.scan
        placed = scan is not None and scan.placeable
        self.scan_box.setVisible(scan is not None and scan.georef is not None)
        if scan is not None and scan.georef is not None:
            lines = scan.georef.describe()
            lines.insert(0, f"{scan_io.KIND_LABELS.get(scan.kind, scan.kind)} in "
                            f"{scan_io.FRAME_LABELS.get(scan.frame, scan.frame)} · from {scan.georef.source}")
            self._set_label(self.scan_label, "\n".join(lines), "Good" if placed else "Bad")
        for label, (key, shown) in zip(self.fact_labels.values(), self._facts(model)):
            label.setText(f'<span style="color:#5B6B7A; font-size:8pt">{key}</span><br><b>{shown}</b>')
            label.setVisible(True)
        if model.kind == KIND_SPLATS:
            self._set_label(self.model_label, "3D Gaussian splats: shown and exported as their centres with their "
                            "colour (PointCloud SLPK).", "Good")
        elif model.kind != KIND_MESH:
            self._set_label(self.model_label, "Point cloud: exported as a PointCloud SLPK"
                            + (" with its RGB colours." if model.vertex_colors else " (no colours: grey)."),
                            "Good" if model.vertex_colors else "Info")
        elif model.textures:
            self._set_label(self.model_label, f"MTL and {len(model.textures)} texture(s) found beside the mesh.", "Good")
        elif model.vertex_colors:
            self._set_label(self.model_label, "Colours per vertex (no texture image): the SLPK keeps those colours.",
                            "Good")
        else:
            self._set_label(self.model_label, "No texture and no vertex colours: the SLPK will be grey.", "Bad")

    @staticmethod
    def _facts(model: pipeline.LoadedModel) -> list[tuple[str, str]]:
        size = ("Size", f"{model.size_mb:,.1f} MB")
        if model.kind == KIND_SPLATS:
            cloud = model.cloud
            visible = int((cloud.opacity >= pipeline.SPLAT_MIN_OPACITY).sum()) if cloud is not None \
                and cloud.opacity is not None else model.vertices
            return [("Gaussians", f"{model.vertices:,}"), ("Visible", f"{visible:,}"), ("Type", "3DGS"), size]
        if model.kind != KIND_MESH:
            return [("Points", f"{model.vertices:,}"), ("Colour", "RGB" if model.vertex_colors else "none"),
                    ("Type", "Point cloud"), size]
        return [("Vertices", f"{model.vertices:,}"), ("Faces", f"{model.faces:,}"),
                ("Textures", f"{len(model.textures)}"), size]

    # ================================================================== GCPs
    def _usable_rows(self) -> tuple[list[int], list[Gcp]]:
        rows, gcps = [], []
        for row, gcp in enumerate(self.gcp_table.gcps()):
            if gcp.usable():
                rows.append(row)
                gcps.append(gcp)
        return rows, gcps

    def add_gcp(self) -> None:
        row = self.gcp_table.add_gcp(Gcp(name=self.gcp_table.next_name()))
        if self.model is not None and self.pick_btn.isEnabled():
            self.pick_btn.setChecked(True)
            known = "Lon, Lat, h" if self._geographic() else "E, N, H"
            self.log(f"Click the point of {self.gcp_table.gcp_at(row).name} on the model, then type its {known}.")

    def import_gcps(self) -> None:
        start = str(self.settings.value("last_dir", str(Path.home()), type=str) or Path.home())
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(self, "Import GCPs", start,
                                                              "GCP files (*.csv *.txt);;All files (*)")
        if not path:
            return
        try:
            read = gcp_io.read_gcp_csv(path, str(self.order_combo.currentData()))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, APP_TITLE, f"The GCP file could not be read:\n{exc}")
            return
        if not read:
            QtWidgets.QMessageBox.warning(self, APP_TITLE, "No GCP with coordinates was found in the file.")
            return
        if self._geographic():      # lon / lat: a file with latitude first is turned around
            swapped = [g for g in read if g.known is not None and abs(g.known[0]) <= 90.0 < abs(g.known[1])]
            if swapped and len(swapped) == len([g for g in read if g.known is not None]):
                for g in swapped:
                    g.known = (g.known[1], g.known[0], g.known[2])
                self.log("   The file has latitude before longitude: read as Lat, Lon.")
        current = self.gcp_table.gcps()
        by_name = {gcp.name: gcp for gcp in current}
        added = updated = 0
        for gcp in read:
            if gcp.name in by_name:
                target = by_name[gcp.name]
                target.known = gcp.known or target.known
                target.obj = gcp.obj or target.obj
                target.residual = None
                updated += 1
            else:
                current.append(gcp)
                by_name[gcp.name] = gcp
                added += 1
        self.gcp_table.set_gcps(current)
        if not self.use_gcp.isChecked():
            self.use_gcp.setChecked(True)
        self.log(f"📄 GCPs from {Path(path).name}: {added} added, {updated} updated.")
        missing = [g.name for g in current if g.obj is None]
        if missing:
            self.log(f"   Pick on the model: {', '.join(missing[:12])}{' …' if len(missing) > 12 else ''}")
            first = next(row for row, g in enumerate(current) if g.obj is None)
            self.gcp_table.selectRow(first)

    def save_gcps(self) -> None:
        if not self.gcp_table.rowCount():
            return
        start = str(self.model.opened.with_name(self.model.opened.stem + "_gcps.csv")) if self.model else str(Path.home())
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(self, "Save GCPs", start, "CSV (*.csv)")
        if not path:
            return
        code = self.user_crs.code if self.user_crs else (self.crs_info.code if self.crs_info else "")
        gcp_io.write_gcp_csv(path, self.gcp_table.gcps(), code, geographic=self._geographic())
        self.log(f"💾 GCPs saved: {path}")

    def _picked(self, x: float, y: float) -> None:
        if self.world is None or self.model is None:
            return
        d2 = (self.world[:, 0] - x) ** 2 + (self.world[:, 1] - y) ** 2
        index = int(np.argmin(d2))
        tolerance = self.plan.pick_tolerance()
        if float(d2[index]) > tolerance * tolerance:
            self.statusBar().showMessage("No model point there: click on the model.", 4000)
            return
        self._set_picked_obj(self.model.points_obj[index])

    def _set_picked_obj(self, obj) -> None:
        """OBJ coordinates picked on the model (2D or 3D) -> the selected GCP row (a new one if none)."""
        obj = np.asarray(obj, dtype=np.float64)
        row = self.gcp_table.selected_row()
        if row < 0:
            row = self.gcp_table.add_gcp(Gcp(name=self.gcp_table.next_name()))
        self.gcp_table.set_obj(row, obj)
        name = self.gcp_table.gcp_at(row).name
        self.log(f"📍 {name}: model X {obj[0]:.3f}  Y {obj[1]:.3f}  Z {obj[2]:.3f}")
        for other in range(self.gcp_table.rowCount()):
            if self.gcp_table.gcp_at(other).obj is None:
                self.gcp_table.selectRow(other)
                self.log(f"   Next: click {self.gcp_table.gcp_at(other).name}")
                break
        else:
            self.gcp_table.selectRow(row)

    def _write_residuals(self, rows: list[int], residuals: np.ndarray) -> None:
        self.gcp_table.set_residuals({row: tuple(float(v) for v in res) for row, res in zip(rows, residuals)})
        self._update_markers()

    def check_fit(self) -> None:
        rows, gcps = self._usable_rows()
        if not gcps:
            QtWidgets.QMessageBox.information(self, APP_TITLE, "At least one used GCP with model and known coordinates "
                                              "is needed.")
            return
        frame = self._frame()
        model = frame.apply(np.array([g.obj for g in gcps], dtype=np.float64))
        known = self._known_array([g.known for g in gcps])
        res = known - model
        self._write_residuals(rows, res)
        rmse_h = float(np.sqrt(np.mean(np.sum(res[:, :2] ** 2, axis=1))))
        rmse_v = float(np.sqrt(np.mean(res[:, 2] ** 2)))
        self.log(f"🔎 Check fit of the model as it is ({len(gcps)} GCPs): RMSE horizontal {rmse_h:.4f} m, vertical {rmse_v:.4f} m")
        for gcp, r in zip(gcps, res):
            self.log(f"   {gcp.name:<12} dE {r[0]:+.4f}  dN {r[1]:+.4f}  dH {r[2]:+.4f}")
        if self.solution is None:
            self._set_label(self.solve_label, f"Check fit (no adjustment): RMSE horizontal {rmse_h:.3f} m, "
                            f"vertical {rmse_v:.3f} m. Solve to adjust the model if it is too far.", "Info")

    def solve(self) -> None:
        rows, gcps = self._usable_rows()
        frame = self._frame()
        mode = str(self.mode_combo.currentData())
        try:
            model = frame.apply(np.array([g.obj for g in gcps], dtype=np.float64).reshape(-1, 3))
            known = self._known_array([g.known for g in gcps])
            solution = georef.solve_gcps([g.name for g in gcps], model, known, mode, self.lock_scale.isChecked())
        except GeorefError as exc:
            QtWidgets.QMessageBox.warning(self, APP_TITLE, str(exc))
            return
        self.solution = solution
        self.solution_key = self._solution_signature()
        self.solution_stale = False
        self._write_residuals(rows, solution.residuals)
        self.log("✅ GCP adjustment solved.")
        for line in solution.report_lines:
            self.log(f"   {line}")
        for name, r in zip(solution.names, solution.residuals):
            self.log(f"   {name:<12} dE {r[0]:+.4f}  dN {r[1]:+.4f}  dH {r[2]:+.4f}")
        self._set_label(self.solve_label, f"Adjusted with {len(gcps)} GCPs: RMSE horizontal {solution.rmse_h:.3f} m, "
                        f"vertical {solution.rmse_v:.3f} m, scale {solution.transform.scale:.6f}, "
                        f"yaw {solution.transform.yaw_deg:.4f}°. The view and the export use the adjusted model.", "Good")
        self._refresh_all(keep_view=False)

    # ================================================================== outputs
    def save_preview_las(self) -> None:
        if self.world is None or self.model is None or self.crs_info is None:
            return
        default = str(self.model.opened.with_name(self.model.opened.stem + "_preview.las"))
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(self, "Save preview LAS", default, "LAS point cloud (*.las)")
        if not path:
            return
        try:
            info = las_preview.write_las(path, self.world, self.model.colors, self.crs_info.code)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, APP_TITLE, f"The LAS could not be written:\n{exc}")
            return
        crs_note = f"CRS {self.crs_info.code} written" if info["epsg_written"] else "CRS not written (set it in QGIS)"
        self.log(f"✅ Preview LAS: {path} ({info['points']:,} points, {crs_note})")
        self.log("   QGIS: Layer > Add Layer > Add Point Cloud Layer. It must sit on your survey / basemap.")

    def choose_output(self) -> None:
        start = self.out_edit.text().strip() or (str(self.model.opened.with_suffix(".slpk")) if self.model else str(Path.home()))
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(self, "Save SLPK", start, "Scene Layer Package (*.slpk)")
        if path:
            if not path.lower().endswith(".slpk"):
                path += ".slpk"
            self.out_edit.setText(path)

    def export_slpk(self) -> None:
        blocker = self._export_blocker()
        if blocker:
            QtWidgets.QMessageBox.information(self, APP_TITLE, blocker)
            return
        out = Path(self.out_edit.text().strip())
        if out.suffix.lower() != ".slpk":
            out = out.with_suffix(".slpk")
            self.out_edit.setText(str(out))
        if out.exists():
            answer = QtWidgets.QMessageBox.question(
                self, APP_TITLE, f"{out.name} already exists. Replace it?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        model = self.model
        code = self.crs_info.code
        frame = self._frame()
        adjustment = self._adjustment()
        options = pipeline.ExportOptions(
            layer_name=self.layer_edit.text().strip() or model.opened.stem,
            height_model=self._height_model(),
            max_texture=int(self.texture_combo.currentData()),
            tile_faces=int(self.tile_spin.value()),
            jpeg_quality=int(self.jpeg_spin.value()),
            geoid_path=str(self.geoid.path) if self._geoid_wanted() and self.geoid is not None else "",
        )
        self.settings.setValue("max_texture", options.max_texture)
        backend = self.backend
        self.log(f"   Model      : {model.opened.name} -> {out.name}")
        self.log(f"   CRS        : {self.crs_info.label}"
                 + (f" (worked in it for {self.user_crs.code})" if self._geographic() else ""))
        self.log(f"   Frame      : {frame.describe()}")
        if adjustment is not None and self.solution is not None:
            self.log(f"   Adjustment : {georef.MODE_LABELS[self.solution.mode]}, RMSE 3D {self.solution.rmse_3d:.4f} m")
        self.log(f"   Heights    : {pipeline.HEIGHT_LABELS[options.height_model]}"
                 + (f", converted with {Path(options.geoid_path).name} (H = h - N)" if options.geoid_path else ""))

        def job(progress, log):
            return pipeline.export_any(model, out, code, frame, adjustment, options, backend, progress, log)

        self._start_task(f"Exporting {out.name}", job, self._exported)

    def _exported(self, report: pipeline.ExportReport) -> None:
        for line in report.lines:
            self.log(f"   {line}")
        self._exported_file = str(report.result.path)
        self.log("✅ SLPK ready for ROLAVI Viewer and ArcGIS.")
        text = f"SLPK ready:\n{report.result.path}\n\n{report.summary}."
        if self.viewer_panel.can_send():
            answer = QtWidgets.QMessageBox.question(
                self, APP_TITLE, text + "\n\nSend it to your ROLAVI Viewer space now?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                path = str(report.result.path)
                # after this job has fully ended (the window is idle again)
                QtCore.QTimer.singleShot(0, lambda: self.viewer_panel.send_slpk(path))
            return
        QtWidgets.QMessageBox.information(self, APP_TITLE, text)

    # ================================================================== tasks
    def _start_task(self, title: str, job, on_done) -> None:
        if self.task is not None:
            return
        task = TaskThread(title, job, self)
        task.progressed.connect(self._task_progress)
        task.logged.connect(self.log)
        task.succeeded.connect(lambda result: self._task_done(task, on_done, result))
        task.failed.connect(lambda message, details: self._task_failed(task, message, details))
        task.cancelled.connect(lambda: self._task_cancelled(task))
        task.finished.connect(lambda: self._thread_finished(task))
        self.task = task
        self._threads.append(task)
        self._task_started = time.monotonic()
        self.progress.setValue(0)
        self._set_label(self.task_label, f"{title}…", "Info")
        self.log(f"▶ {title}")
        if self.pick_btn.isChecked():
            self.pick_btn.setChecked(False)
        self._refresh_state()
        task.start()

    def _task_progress(self, fraction: float, text: str) -> None:
        fraction = max(0.0, min(1.0, float(fraction)))
        percent = int(round(fraction * 100))
        self.progress.setValue(percent)
        elapsed = time.monotonic() - self._task_started
        parts = [text, f"{percent}%", f"{_duration(elapsed)} elapsed"]
        if 0.03 < fraction < 1.0:
            parts.append(f"about {_duration(elapsed * (1.0 - fraction) / fraction)} left")
        self._set_label(self.task_label, "  ·  ".join(parts), "Info")

    def _end_task(self, task: TaskThread) -> float:
        if self.task is task:
            self.task = None
        return time.monotonic() - self._task_started

    def _task_done(self, task: TaskThread, on_done, result) -> None:
        elapsed = self._end_task(task)
        self.progress.setValue(100)
        self._set_label(self.task_label, f"{task.title}: done in {_duration(elapsed)}", "Good")
        try:
            on_done(result)
        except Exception as exc:  # a bug after the job must not stop the window
            self.log(f"❌ {exc}\n{traceback.format_exc()}")
            QtWidgets.QMessageBox.critical(self, APP_TITLE, str(exc))
        self._refresh_state()

    def _task_failed(self, task: TaskThread, message: str, details: str) -> None:
        self._end_task(task)
        self.progress.setValue(0)
        self._set_label(self.task_label, f"{task.title}: failed", "Bad")
        self.log(f"❌ {task.title}: {message}")
        self.log(details.rstrip())
        self._refresh_state()
        QtWidgets.QMessageBox.critical(self, APP_TITLE, f"{task.title} failed:\n{message}")

    def _task_cancelled(self, task: TaskThread) -> None:
        self._end_task(task)
        self.progress.setValue(0)
        self._set_label(self.task_label, f"{task.title}: cancelled", "Muted")
        self.log(f"⏹ {task.title}: cancelled.")
        self._refresh_state()

    def _thread_finished(self, task: TaskThread) -> None:
        if task in self._threads:
            self._threads.remove(task)
        task.deleteLater()

    def cancel_task(self) -> None:
        if self.task is not None:
            self.task.cancel()
            self._set_label(self.task_label, f"{self.task.title}: cancelling…", "Muted")
            self._refresh_state()

    def closeEvent(self, event) -> None:
        if self.task is not None:
            answer = QtWidgets.QMessageBox.question(
                self, APP_TITLE, "A job is running. Cancel it and close?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.task.cancel()
            self.task.wait(15000)
        self.viewer_panel.shutdown()
        self.basemap_layer.close()
        if self.view3d is not None:
            try:
                self.view3d.close()
            except Exception:
                pass
        super().closeEvent(event)


# ====================================================================== standalone run
def _excepthook(kind, value, tb) -> None:
    """Unhandled errors in Qt slots: shown and printed instead of closing the program."""
    text = "".join(traceback.format_exception(kind, value, tb))
    print(text, file=sys.stderr, flush=True)
    app = QtWidgets.QApplication.instance()
    if app is not None:
        QtWidgets.QMessageBox.critical(None, APP_TITLE, text[-3000:])


def run(argv: list[str] | None = None) -> int:
    """Standalone window (VS Code: ``python run_dev.py [model.obj | model.ply]``)."""
    argv = list(sys.argv if argv is None else argv)
    sys.excepthook = _excepthook
    if QtWidgets.QApplication.instance() is None:
        attribute = getattr(QtCore.Qt.ApplicationAttribute, "AA_EnableHighDpiScaling", None)
        if attribute is not None:
            try:
                QtWidgets.QApplication.setAttribute(attribute, True)
            except Exception:
                pass
    set_windows_app_id()                       # the taskbar shows our icon, not Python's
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(argv)
    app.setWindowIcon(app_icon())              # every window and dialog of the program
    window = MainWindow()
    window.show()
    obj = next((a for a in argv[1:] if a.lower().endswith((".obj", ".ply"))), None)
    if obj:
        QtCore.QTimer.singleShot(300, lambda: window.load_obj(obj))
    tif = next((a for a in argv[1:] if a.lower().endswith((".tif", ".tiff"))), None)
    if tif and not obj:
        QtCore.QTimer.singleShot(300, lambda: window.load_raster(tif))
    return exec_(app)
