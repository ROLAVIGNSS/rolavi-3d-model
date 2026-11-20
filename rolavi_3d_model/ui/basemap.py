"""Base map under the 2D view: XYZ tiles drawn in the CRS the window works in.

Each tile is placed from its corners (longitude / latitude -> CRS of the view), so it lines up
with the model whatever the projection. Tiles arrive in background threads and the view is
repainted as they come; while a tile loads, a coarser one already in memory is shown.
"""

from __future__ import annotations

import numpy as np

from ..core import tiles
from ..core.crs import CrsError
from ..core.tiles import Basemap, LruCache, TileFetcher
from .qt import QtCore, QtGui, Signal

PARENT_LEVELS = 4          # a missing tile is drawn from a cached parent up to this many zooms up


class BasemapLayer(QtCore.QObject):
    changed = Signal()
    _arrived = Signal(str, int, int, int, object)      # emitted from the download threads

    def __init__(self, parent=None, opener=None):
        super().__init__(parent)
        self.basemap: Basemap | None = None
        self.backend = None
        self.crs = ""
        self.failed = 0
        self._cache = LruCache(900)
        self._corners: dict = {}
        self._fetcher = TileFetcher(self._from_thread, opener=opener)
        self._arrived.connect(self._store)

    # ------------------------------------------------------------------ settings
    def set_basemap(self, basemap: Basemap | None) -> None:
        if basemap != self.basemap:
            self.basemap = basemap
            self.failed = 0
            self.changed.emit()

    def set_crs(self, backend, crs: str) -> None:
        if backend is not self.backend or crs != self.crs:
            self.backend, self.crs = backend, crs or ""
            self._corners.clear()
            self.changed.emit()

    def active(self) -> bool:
        return self.basemap is not None and self.backend is not None and bool(self.crs)

    def close(self) -> None:
        self._fetcher.close()

    # ------------------------------------------------------------------ tiles
    def _from_thread(self, key: str, z: int, x: int, y: int, data) -> None:
        self._arrived.emit(key, z, x, y, data)

    def _store(self, key: str, z: int, x: int, y: int, data) -> None:
        if data is None:
            self.failed += 1
        else:
            image = QtGui.QImage()
            if image.loadFromData(data) and not image.isNull():
                self._cache.put((key, z, x, y), image)
            else:
                self.failed += 1
        self.changed.emit()

    def _tile_corners(self, wanted: list[tuple[int, int, int]]) -> None:
        """Top-left, top-right, bottom-left corners of tiles in the CRS of the view (kept per CRS)."""
        missing = [t for t in wanted if (self.crs,) + t not in self._corners]
        if not missing:
            return
        lons, lats = [], []
        for z, x, y in missing:
            for cx, cy in ((x, y), (x + 1, y), (x, y + 1)):
                lon, lat = tiles.tile_lonlat(cx, cy, z)
                lons.append(lon)
                lats.append(lat)
        ex, ny = self.backend.from_lonlat(self.crs, np.array(lons), np.array(lats))
        for index, tile in enumerate(missing):
            part = slice(3 * index, 3 * index + 3)
            self._corners[(self.crs,) + tile] = np.column_stack([ex[part], ny[part]])

    def _image_for(self, key: str, z: int, x: int, y: int):
        """(image, source rect) of a tile, or of the part of a cached parent tile that covers it."""
        image = self._cache.get((key, z, x, y))
        if image is not None:
            return image, QtCore.QRectF(0, 0, image.width(), image.height())
        for up in range(1, PARENT_LEVELS + 1):
            if z - up < 0:
                break
            parent = self._cache.get((key, z - up, x >> up, y >> up))
            if parent is not None:
                size = parent.width() / float(1 << up)
                ox = (x - ((x >> up) << up)) * size
                oy = (y - ((y >> up) << up)) * size
                return parent, QtCore.QRectF(ox, oy, size, size)
        return None, None

    # ------------------------------------------------------------------ drawing
    def draw(self, painter: QtGui.QPainter, view) -> None:
        """Draw the tiles under ``view`` (a PlanView: ``to_world``, ``to_screen``, ``scale``)."""
        if not self.active():
            return
        width, height = float(view.width()), float(view.height())
        corners = [view.to_world(px, py) for px, py in ((0, 0), (width, 0), (0, height), (width, height),
                                                         (width / 2.0, height / 2.0))]
        try:
            lon, lat = self.backend.to_lonlat(self.crs, np.array([c[0] for c in corners]),
                                              np.array([c[1] for c in corners]))
        except CrsError:
            return
        basemap = self.basemap
        zoom = tiles.choose_zoom(1.0 / max(view.scale(), 1e-12), float(lat[4]), basemap.max_zoom)
        wanted = tiles.tiles_for(float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()), zoom)
        try:
            self._tile_corners(wanted)
        except CrsError:
            return
        painter.save()
        try:
            painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
            for z, x, y in wanted:
                if self._cache.get((basemap.key, z, x, y)) is None:
                    self._fetcher.request(basemap, z, x, y)
                image, source = self._image_for(basemap.key, z, x, y)
                if image is None:
                    continue
                world = self._corners[(self.crs, z, x, y)]
                p00 = view.to_screen(world[0, 0], world[0, 1])
                p10 = view.to_screen(world[1, 0], world[1, 1])
                p01 = view.to_screen(world[2, 0], world[2, 1])
                size = float(tiles.TILE_SIZE)
                transform = QtGui.QTransform((p10.x() - p00.x()) / size, (p10.y() - p00.y()) / size,
                                             (p01.x() - p00.x()) / size, (p01.y() - p00.y()) / size,
                                             p00.x(), p00.y())
                painter.setTransform(transform)
                # a quarter pixel of overlap hides the seams between tiles
                painter.drawImage(QtCore.QRectF(-0.25, -0.25, size + 0.5, size + 0.5), image, source)
            painter.resetTransform()
            self._draw_attribution(painter, view)
        finally:
            painter.restore()

    def _draw_attribution(self, painter: QtGui.QPainter, view) -> None:
        text = self.basemap.attribution if self.basemap is not None else ""
        if self._fetcher.pending():
            text = f"{text}   ·   loading {self._fetcher.pending()} tiles…" if text else "loading tiles…"
        if not text:
            return
        metrics = QtGui.QFontMetrics(painter.font())
        box_w = metrics.horizontalAdvance(text) + 12 if hasattr(metrics, "horizontalAdvance") else len(text) * 7 + 12
        box = QtCore.QRectF(view.width() - box_w - 6, view.height() - 22, box_w, 18)
        painter.fillRect(box, QtGui.QColor(7, 19, 31, 170))
        painter.setPen(QtGui.QColor(230, 238, 245))
        painter.drawText(box, QtCore.Qt.AlignmentFlag.AlignCenter, text)
