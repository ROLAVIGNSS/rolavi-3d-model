"""Layers: every model imported (OBJ, PLY mesh, point cloud, 3DGS) is a layer, and so is every
map layer (orthomosaic, DEM, polygon, cut / fill result).

* The list shows each layer with a tick (shown / hidden in the 2D and 3D views), its file
  and its kind. The selected model is the *active* layer: the workflow (coordinates, GCPs,
  export, sending to ROLAVI Viewer) works on it. Selecting a map layer leaves the active model
  as it is (the Rasters & volumes page uses the map layers).
* Each model keeps its own settings (coordinates, GCPs and their adjustment, output) while
  another one is active. The CRS of the project, the heights and the geoid are shared.
* "+ Add" imports a model, a GeoTIFF (orthomosaic or DEM) or a polygon, or draws a polygon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core import pipeline, scan_io
from ..core.georef import GcpSolution
from .qt import QtCore, QtWidgets, Signal


@dataclass
class Layer:
    key: int
    model: pipeline.LoadedModel
    visible: bool = True
    display: Any = None                      # pipeline.DisplayMesh / DisplayCloud of the 3D view
    state: dict = field(default_factory=dict)   # its widget settings while another layer is active
    solution: GcpSolution | None = None
    solution_key: str | None = None
    solution_stale: bool = False
    exported: str = ""                       # last SLPK written from it
    preview: tuple[str, Any] = ("", None)    # (signature, preview points in the project CRS)
    pushed: str = ""                         # signature of what the 3D view shows of it

    @property
    def title(self) -> str:
        return self.model.opened.name

    @property
    def subtitle(self) -> str:
        model = self.model
        if model.kind == scan_io.KIND_SPLATS:
            return f"3DGS · {model.vertices:,} Gaussians"
        if model.kind == scan_io.KIND_POINTS:
            return f"Point cloud · {model.vertices:,} points"
        textured = " · textured" if model.textures else ""
        return f"Mesh · {model.faces:,} triangles{textured}"


MAP_ORTHO = "ortho"
MAP_DEM = "dem"
MAP_POLYGON = "polygon"
MAP_VOLUME = "volume"


@dataclass
class MapLayer:
    """A 2D layer: an orthomosaic, a DEM, polygons, or the cut / fill of a volume."""
    key: int
    kind: str                                # MAP_ORTHO / MAP_DEM / MAP_POLYGON / MAP_VOLUME
    name: str
    data: Any                                # RasterInfo / Dem / PolygonSet / VolumeResult
    preview: Any = None                      # raster_io.Preview of a raster
    path: str = ""
    visible: bool = True
    image: Any = None                        # QImage of the preview (made once)
    placed: tuple = ("", None)               # (CRS of the view, corners of the image in it)

    @property
    def title(self) -> str:
        return self.name

    @property
    def subtitle(self) -> str:
        data = self.data
        if self.kind == MAP_ORTHO:
            px = data.pixel_size[0]
            size = f"{px * 100:.1f} cm" if not data.geographic and px < 1 else (
                f"{px:.2f} m" if not data.geographic else f"{px:.7f}°")
            return f"Orthomosaic · {data.width:,} × {data.height:,} px · {size}"
        if self.kind == MAP_DEM:
            s = data.stats()
            return f"DEM · {s.get('cell', 0):.2f} m · z {s.get('zmin', 0):,.1f} – {s.get('zmax', 0):,.1f}"
        if self.kind == MAP_POLYGON:
            return f"Polygon · {data.describe()}"
        if self.kind == MAP_VOLUME:
            st = data.stats
            return f"Cut / fill · cut {st['cut']:,.1f} m³ · fill {st['fill']:,.1f} m³"
        return self.kind


class LayersPanel(QtWidgets.QFrame):
    """Layer list of the left rail: tick = shown, selected = active, Remove, + Add."""

    activated = Signal(int)
    visibility_changed = Signal(int, bool)
    remove_requested = Signal(int)
    add_requested = Signal(str)          # "model", "raster", "polygon" or "draw"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LayersPanel")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(4)
        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        title = QtWidgets.QLabel("LAYERS")
        title.setObjectName("RailHeader")
        head.addWidget(title)
        head.addStretch(1)
        self.add_btn = QtWidgets.QPushButton("+ Add")
        self.add_btn.setObjectName("LayerAdd")
        self.add_btn.setToolTip("Import a model, a GeoTIFF (orthomosaic or DEM) or a polygon, or draw a polygon")
        self.add_menu = QtWidgets.QMenu(self.add_btn)
        for text, kind in (("3D model (OBJ, PLY: mesh, point cloud, 3DGS)…", "model"),
                           ("GeoTIFF: orthomosaic or DEM…", "raster"),
                           ("Polygon: Shapefile, DXF, KML, KMZ…", "polygon"),
                           ("Draw a polygon on the plan view", "draw")):
            action = self.add_menu.addAction(text)
            action.triggered.connect(lambda _checked=False, k=kind: self.add_requested.emit(k))
        self.add_btn.setMenu(self.add_menu)
        head.addWidget(self.add_btn)
        layout.addLayout(head)
        self.list = QtWidgets.QListWidget()
        self.list.setObjectName("LayerList")
        self.list.setToolTip("Tick: shown in the views. Selected: the layer the workflow works on.")
        layout.addWidget(self.list, 1)
        self.empty = QtWidgets.QLabel("Each model, GeoTIFF or polygon you import is a layer here.")
        self.empty.setObjectName("RailFoot")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        self.remove_btn = QtWidgets.QPushButton("Remove layer")
        self.remove_btn.setObjectName("Ghost")
        self.remove_btn.setToolTip("Take the selected layer out of the project (the files are not touched)")
        layout.addWidget(self.remove_btn)
        self._keys: list[int] = []
        self._updating = False
        self.remove_btn.clicked.connect(self._remove_clicked)
        self.list.itemChanged.connect(self._item_changed)
        self.list.currentRowChanged.connect(self._row_changed)
        self.set_layers([], None)

    def set_layers(self, layers: list[Layer], active: int | None, map_layers=(), selected: int | None = None) -> None:
        """Models first (the active one marked when a map layer is selected), then the map layers."""
        self._updating = True
        try:
            self.list.clear()
            self._keys = []
            rows = list(layers) + list(map_layers)
            current = selected if selected is not None else active
            for layer in rows:
                subtitle = layer.subtitle
                if isinstance(layer, Layer) and layer.key == active and current != active:
                    subtitle = "Active · " + subtitle
                item = QtWidgets.QListWidgetItem(f"{layer.title}\n{subtitle}")
                item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.CheckState.Checked if layer.visible else QtCore.Qt.CheckState.Unchecked)
                item.setToolTip(str(layer.model.opened) if isinstance(layer, Layer) else (layer.path or layer.name))
                self.list.addItem(item)
                self._keys.append(layer.key)
            if current in self._keys:
                self.list.setCurrentRow(self._keys.index(current))
            self.empty.setVisible(not rows)
            self.list.setVisible(bool(rows))
            self.remove_btn.setEnabled(bool(rows))
            self.list.setFixedHeight(min(300, 26 + 44 * max(1, len(rows))))
        finally:
            self._updating = False

    def set_enabled(self, enabled: bool) -> None:
        for widget in (self.list, self.add_btn, self.remove_btn):
            widget.setEnabled(enabled and (widget is not self.remove_btn or bool(self._keys)))

    def _item_changed(self, item) -> None:
        if self._updating:
            return
        row = self.list.row(item)
        if 0 <= row < len(self._keys):
            self.visibility_changed.emit(self._keys[row], item.checkState() == QtCore.Qt.CheckState.Checked)

    def _row_changed(self, row: int) -> None:
        if not self._updating and 0 <= row < len(self._keys):
            self.activated.emit(self._keys[row])

    def _remove_clicked(self, *_args) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self._keys):
            self.remove_requested.emit(self._keys[row])
