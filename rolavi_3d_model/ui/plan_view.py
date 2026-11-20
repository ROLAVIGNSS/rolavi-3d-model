"""Top (plan) view: the models as coloured points, rasters, polygons, GCP markers; pan / zoom / pick / draw.

The points are rasterised with numpy into one image (hundreds of thousands of
points redraw in a few milliseconds), drawn highest last so the top surface
shows. Coordinates are those of the current frame: the project CRS (E, N) once
the model is georeferenced, the model's own coordinates before.

Drawing order: base map, rasters (orthomosaics, DEM reliefs, cut / fill), points,
polygons, GCP markers, the polygon being drawn.

* drag: pan          * wheel: zoom at the cursor          * double click: fit
* click in pick mode: ``picked(x, y)``
* draw mode (``start_drawing``): click the vertices; double click, right click or Enter
  closes the polygon (``polygon_drawn``); Backspace removes the last vertex; Esc cancels
* optional base map under everything (``set_basemap``: XYZ tiles placed in the view's CRS)
"""

from __future__ import annotations

import math

import numpy as np

from .qt import QtCore, QtGui, QtWidgets, Signal, event_pos

BACKGROUND = (7, 19, 31)          # ROLAVI dark (#07131F)
MODEL_MARK = QtGui.QColor(255, 153, 0)     # OBJ position of a GCP
KNOWN_MARK = QtGui.QColor(0, 200, 255)     # known position of a GCP
RESIDUAL_LINE = QtGui.QColor(255, 70, 70)
TEXT = QtGui.QColor(230, 238, 245)
POLYGON_LINE = QtGui.QColor(0, 200, 255)          # polygons of the project
POLYGON_USED = QtGui.QColor(255, 213, 74)          # the polygon of the volume
DRAW_LINE = QtGui.QColor(255, 153, 0)              # the polygon being drawn


def _nice_length(target: float) -> float:
    if target <= 0 or not math.isfinite(target):
        return 1.0
    power = 10.0 ** math.floor(math.log10(target))
    for step in (1.0, 2.0, 5.0, 10.0):
        if step * power >= target:
            return step * power
    return 10.0 * power


class PlanView(QtWidgets.QWidget):
    cursor_moved = Signal(float, float)
    picked = Signal(float, float)
    polygon_drawn = Signal(object)        # (n, 2) array of the vertices, in the view's coordinates
    drawing_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(420, 320)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self._xy = np.zeros((0, 2))
        self._rgb = np.zeros((0, 3), dtype=np.uint8)
        self._spacing = 1.0
        self._center = (0.0, 0.0)
        self._scale = 1.0                  # pixels per unit
        self._image: QtGui.QImage | None = None
        self._image_size = (0, 0)
        self._dirty = True
        self._markers: list[dict] = []
        self._pick_mode = False
        self._press: QtCore.QPointF | None = None
        self._press_center = (0.0, 0.0)
        self._dragging = False
        self._caption = ""
        self._show_north = False
        self._units = "m"
        self._basemap = None
        self._overlays: list[dict] = []    # rasters: {"image": QImage, "corners": (tl, tr, bl), "opacity"}
        self._polygons: list[dict] = []    # {"rings": [(n, 2) arrays], "used": bool}
        self._drawing = False
        self._draft: list[tuple[float, float]] = []
        self._hover: tuple[float, float] | None = None

    # ------------------------------------------------------------------ data
    def set_points(self, xyz: np.ndarray, rgb: np.ndarray, keep_view: bool = False) -> None:
        xyz = np.asarray(xyz, dtype=np.float64)
        if not len(xyz):
            self.clear()
            return
        order = np.argsort(xyz[:, 2], kind="stable")
        self._xy = np.ascontiguousarray(xyz[order, :2])
        self._rgb = np.ascontiguousarray(np.asarray(rgb, dtype=np.uint8)[order, :3])
        span = np.maximum(self._xy.max(axis=0) - self._xy.min(axis=0), 1e-9)
        self._spacing = math.sqrt(float(span[0] * span[1]) / max(1, len(self._xy)))
        if not keep_view:
            self.fit()
        self._dirty = True
        self.update()

    def clear(self) -> None:
        self._xy = np.zeros((0, 2))
        self._rgb = np.zeros((0, 3), dtype=np.uint8)
        self._markers = []
        self._dirty = True
        self.update()

    def has_points(self) -> bool:
        return len(self._xy) > 0

    def has_content(self) -> bool:
        """Points, rasters or polygons to show (the view can be panned and fitted)."""
        return len(self._xy) > 0 or bool(self._overlays) or bool(self._polygons)

    def set_overlays(self, overlays: list[dict], fit: bool = False) -> None:
        """Rasters under the points: ``[{"image": QImage, "corners": ((x, y) top-left, top-right,
        bottom-left), "opacity": 0..1}]`` in the view's coordinates, drawn in this order."""
        had = self.has_content()
        self._overlays = [o for o in overlays if o.get("image") is not None]
        if fit or (not had and self.has_content()):
            self.fit()
        self.update()

    def set_polygons(self, polygons: list[dict]) -> None:
        """``[{"rings": [(n, 2) arrays in the view's coordinates], "used": bool}]`` (even-odd rule)."""
        had = self.has_content()
        self._polygons = [p for p in polygons if p.get("rings")]
        if not had and self.has_content():
            self.fit()
        self.update()

    # ------------------------------------------------------------------ drawing a polygon
    def start_drawing(self) -> None:
        self._drawing = True
        self._draft = []
        self._pick_mode = False
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self.setFocus()
        self.drawing_changed.emit(True)
        self.update()

    def cancel_drawing(self) -> None:
        if not self._drawing:
            return
        self._drawing = False
        self._draft = []
        self.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        self.drawing_changed.emit(False)
        self.update()

    def is_drawing(self) -> bool:
        return self._drawing

    def draft(self) -> list[tuple[float, float]]:
        return list(self._draft)

    def _add_vertex(self, x: float, y: float) -> None:
        if self._draft:
            lx, ly = self._draft[-1]
            if abs(x - lx) * self._scale < 3 and abs(y - ly) * self._scale < 3:
                return                      # the second click of a double click
        self._draft.append((float(x), float(y)))
        self.update()

    def finish_drawing(self) -> bool:
        """Close the polygon being drawn (three vertices at least): ``polygon_drawn``."""
        if not self._drawing:
            return False
        if len(self._draft) < 3:
            return False                    # the hint at the bottom shows how many vertices there are
        points = np.array(self._draft, dtype=np.float64)
        self._drawing = False
        self._draft = []
        self.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        self.drawing_changed.emit(False)
        self.polygon_drawn.emit(points)
        self.update()
        return True

    def points_xy(self) -> np.ndarray:
        return self._xy

    def set_markers(self, markers: list[dict]) -> None:
        """``[{"name", "model": (x, y) | None, "known": (x, y) | None, "selected": bool}]``."""
        self._markers = list(markers)
        self.update()

    def set_caption(self, text: str, georeferenced: bool) -> None:
        self._caption = text
        self._show_north = georeferenced
        self._units = "m" if georeferenced else "units"
        self.update()

    def set_basemap(self, basemap) -> None:
        """A ``basemap.BasemapLayer`` drawn under the points (it repaints the view as tiles arrive)."""
        self._basemap = basemap
        if basemap is not None:
            basemap.changed.connect(self.update)
        self.update()

    def scale(self) -> float:
        """Pixels per coordinate unit."""
        return self._scale

    def set_pick_mode(self, on: bool) -> None:
        self._pick_mode = bool(on)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor if on else QtCore.Qt.CursorShape.ArrowCursor)

    def pick_tolerance(self) -> float:
        """Search radius of a pick, in coordinate units (12 px)."""
        return 12.0 / max(self._scale, 1e-12)

    # ------------------------------------------------------------------ view
    def extent(self) -> tuple[np.ndarray, np.ndarray] | None:
        """(low, high) of everything shown: points, rasters and polygons."""
        parts = []
        if len(self._xy):
            parts.append(self._xy.min(axis=0))
            parts.append(self._xy.max(axis=0))
        for overlay in self._overlays:
            tl, tr, bl = (np.asarray(c, dtype=np.float64) for c in overlay["corners"])
            parts.extend([tl, tr, bl, tr + bl - tl])
        for polygon in self._polygons:
            for ring in polygon["rings"]:
                parts.append(ring.min(axis=0))
                parts.append(ring.max(axis=0))
        if not parts:
            return None
        pts = np.array(parts, dtype=np.float64)
        return pts.min(axis=0), pts.max(axis=0)

    def fit(self) -> None:
        box = self.extent()
        if box is None:
            return
        low, high = box
        self._center = (float(low[0] + high[0]) / 2.0, float(low[1] + high[1]) / 2.0)
        span = np.maximum(high - low, 1e-6)
        width = max(50, self.width())
        height = max(50, self.height())
        self._scale = 0.9 * min(width / float(span[0]), height / float(span[1]))
        self._dirty = True
        self.update()

    def to_world(self, px: float, py: float) -> tuple[float, float]:
        x = self._center[0] + (px - self.width() / 2.0) / self._scale
        y = self._center[1] - (py - self.height() / 2.0) / self._scale
        return x, y

    def to_screen(self, x: float, y: float) -> QtCore.QPointF:
        return QtCore.QPointF((x - self._center[0]) * self._scale + self.width() / 2.0,
                              self.height() / 2.0 - (y - self._center[1]) * self._scale)

    # ------------------------------------------------------------------ drawing
    def _render(self) -> None:
        """The points as an image with a transparent background (the base map shows between them)."""
        width, height = max(1, self.width()), max(1, self.height())
        image = np.zeros((height, width, 4), dtype=np.uint8)
        if len(self._xy):
            px = np.floor((self._xy[:, 0] - self._center[0]) * self._scale + width / 2.0).astype(np.int64)
            py = np.floor(height / 2.0 - (self._xy[:, 1] - self._center[1]) * self._scale).astype(np.int64)
            size = int(min(8, max(1, round(self._spacing * self._scale * 1.6))))   # closes the gaps of the sample
            low = -(size // 2)
            visible = (px >= -size) & (px < width + size) & (py >= -size) & (py < height + size)
            px, py = px[visible], py[visible]
            rgba = np.column_stack([self._rgb[visible], np.full(int(visible.sum()), 255, dtype=np.uint8)])
            for dx in range(low, low + size):
                for dy in range(low, low + size):
                    x = px + dx
                    y = py + dy
                    inside = (x >= 0) & (x < width) & (y >= 0) & (y < height)
                    image[y[inside], x[inside]] = rgba[inside]
        data = image.tobytes()
        self._image = QtGui.QImage(data, width, height, 4 * width, QtGui.QImage.Format.Format_RGBA8888).copy()
        self._image_size = (width, height)
        self._dirty = False

    def paintEvent(self, _event) -> None:
        if self._dirty or self._image is None or self._image_size != (self.width(), self.height()):
            self._render()
        painter = QtGui.QPainter(self)
        try:
            painter.fillRect(self.rect(), QtGui.QColor(*BACKGROUND))
            if self._basemap is not None and self.has_content() and self._show_north:
                self._basemap.draw(painter, self)          # under everything
            self._draw_overlays(painter)
            painter.drawImage(0, 0, self._image)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            if not self.has_content() and not self._drawing:
                painter.setPen(TEXT)
                painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                                 "Import a model, a GeoTIFF or a polygon to see it here (top view)")
                return
            self._draw_polygons(painter)
            self._draw_markers(painter)
            self._draw_draft(painter)
            self._draw_scale_bar(painter)
            if self._show_north:
                self._draw_north(painter)
            if self._caption:
                painter.setPen(TEXT)
                painter.drawText(QtCore.QRectF(10, 8, self.width() - 20, 20),
                                 QtCore.Qt.AlignmentFlag.AlignLeft, self._caption)
        finally:
            painter.end()

    def _draw_overlays(self, painter: QtGui.QPainter) -> None:
        if not self._overlays:
            return
        painter.save()
        try:
            painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
            for overlay in self._overlays:
                image = overlay["image"]
                width, height = float(image.width()), float(image.height())
                if width <= 0 or height <= 0:
                    continue
                tl, tr, bl = (self.to_screen(float(c[0]), float(c[1])) for c in overlay["corners"])
                painter.setTransform(QtGui.QTransform((tr.x() - tl.x()) / width, (tr.y() - tl.y()) / width,
                                                      (bl.x() - tl.x()) / height, (bl.y() - tl.y()) / height,
                                                      tl.x(), tl.y()))
                painter.setOpacity(float(overlay.get("opacity", 1.0)))
                painter.drawImage(QtCore.QPointF(0.0, 0.0), image)
            painter.resetTransform()
            painter.setOpacity(1.0)
        finally:
            painter.restore()

    def _path(self, rings) -> QtGui.QPainterPath:
        path = QtGui.QPainterPath()
        path.setFillRule(QtCore.Qt.FillRule.OddEvenFill)
        for ring in rings:
            if len(ring) < 2:
                continue
            path.moveTo(self.to_screen(float(ring[0, 0]), float(ring[0, 1])))
            for x, y in ring[1:]:
                path.lineTo(self.to_screen(float(x), float(y)))
            path.closeSubpath()
        return path

    def _draw_polygons(self, painter: QtGui.QPainter) -> None:
        for polygon in self._polygons:
            colour = POLYGON_USED if polygon.get("used") else POLYGON_LINE
            fill = QtGui.QColor(colour)
            fill.setAlpha(38)
            painter.setPen(QtGui.QPen(colour, 2.5 if polygon.get("used") else 1.6))
            painter.setBrush(QtGui.QBrush(fill))
            painter.drawPath(self._path(polygon["rings"]))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)

    def _draw_draft(self, painter: QtGui.QPainter) -> None:
        if not self._drawing:
            return
        points = [self.to_screen(x, y) for x, y in self._draft]
        if self._hover is not None and points:
            points.append(self.to_screen(*self._hover))
        pen = QtGui.QPen(DRAW_LINE, 2)
        pen.setStyle(QtCore.Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        for a, b in zip(points[:-1], points[1:]):
            painter.drawLine(a, b)
        if len(points) >= 3:
            faint = QtGui.QPen(DRAW_LINE, 1)
            faint.setStyle(QtCore.Qt.PenStyle.DotLine)
            painter.setPen(faint)
            painter.drawLine(points[-1], points[0])
        painter.setPen(QtGui.QPen(DRAW_LINE, 2))
        painter.setBrush(QtGui.QBrush(QtGui.QColor(7, 19, 31)))
        for p in points[:len(self._draft)]:
            painter.drawEllipse(p, 4.0, 4.0)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.setPen(TEXT)
        painter.drawText(QtCore.QRectF(10, self.height() - 44, self.width() - 20, 18),
                         QtCore.Qt.AlignmentFlag.AlignLeft,
                         f"Drawing a polygon: {len(self._draft)} vertices · click to add · double click / "
                         "right click / Enter to close · Backspace undo · Esc cancel")

    def _draw_markers(self, painter: QtGui.QPainter) -> None:
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        for marker in self._markers:
            model = marker.get("model")
            known = marker.get("known")
            selected = bool(marker.get("selected"))
            if model is not None and known is not None:
                pen = QtGui.QPen(RESIDUAL_LINE, 2)
                pen.setStyle(QtCore.Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.drawLine(self.to_screen(*model), self.to_screen(*known))
            if known is not None:
                p = self.to_screen(*known)
                painter.setPen(QtGui.QPen(KNOWN_MARK, 3 if selected else 2))
                painter.drawLine(QtCore.QPointF(p.x() - 9, p.y()), QtCore.QPointF(p.x() + 9, p.y()))
                painter.drawLine(QtCore.QPointF(p.x(), p.y() - 9), QtCore.QPointF(p.x(), p.y() + 9))
            if model is not None:
                p = self.to_screen(*model)
                painter.setPen(QtGui.QPen(MODEL_MARK, 3 if selected else 2))
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                radius = 8.0 if selected else 6.0
                painter.drawEllipse(p, radius, radius)
            anchor = model if model is not None else known
            if anchor is not None:
                p = self.to_screen(*anchor)
                painter.setPen(TEXT)
                painter.drawText(QtCore.QPointF(p.x() + 10, p.y() - 8), str(marker.get("name", "")))

    def _draw_scale_bar(self, painter: QtGui.QPainter) -> None:
        length = _nice_length(110.0 / max(self._scale, 1e-12))
        pixels = length * self._scale
        x0, y0 = 16.0, self.height() - 18.0
        painter.setPen(QtGui.QPen(TEXT, 2))
        painter.drawLine(QtCore.QPointF(x0, y0), QtCore.QPointF(x0 + pixels, y0))
        painter.drawLine(QtCore.QPointF(x0, y0 - 5), QtCore.QPointF(x0, y0 + 5))
        painter.drawLine(QtCore.QPointF(x0 + pixels, y0 - 5), QtCore.QPointF(x0 + pixels, y0 + 5))
        label = f"{length:g} {self._units}"
        painter.drawText(QtCore.QPointF(x0, y0 - 8), label)

    def _draw_north(self, painter: QtGui.QPainter) -> None:
        x, y = self.width() - 28.0, 22.0
        painter.setPen(QtGui.QPen(TEXT, 2))
        painter.drawLine(QtCore.QPointF(x, y + 26), QtCore.QPointF(x, y))
        painter.drawLine(QtCore.QPointF(x, y), QtCore.QPointF(x - 6, y + 9))
        painter.drawLine(QtCore.QPointF(x, y), QtCore.QPointF(x + 6, y + 9))
        painter.drawText(QtCore.QPointF(x - 4, y + 42), "N")

    # ------------------------------------------------------------------ events
    def resizeEvent(self, event) -> None:
        self._dirty = True
        super().resizeEvent(event)

    def wheelEvent(self, event) -> None:
        if not self.has_content():
            return
        steps = event.angleDelta().y() / 120.0
        if not steps:
            return
        pos = event_pos(event)
        before = self.to_world(pos.x(), pos.y())
        self._scale *= 1.25 ** steps
        after = self.to_world(pos.x(), pos.y())
        self._center = (self._center[0] + before[0] - after[0], self._center[1] + before[1] - after[1])
        self._dirty = True
        self.update()

    def mousePressEvent(self, event) -> None:
        if self._drawing and event.button() == QtCore.Qt.MouseButton.RightButton:
            self.finish_drawing()
            return
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._press = event_pos(event)
            self._press_center = self._center
            self._dragging = False

    def mouseMoveEvent(self, event) -> None:
        pos = event_pos(event)
        if self.has_content() or self._drawing:
            x, y = self.to_world(pos.x(), pos.y())
            self.cursor_moved.emit(x, y)
            if self._drawing:
                self._hover = (x, y)
                self.update()
        if self._press is None:
            return
        dx = pos.x() - self._press.x()
        dy = pos.y() - self._press.y()
        if not self._dragging and dx * dx + dy * dy > 16:
            self._dragging = True
        if self._dragging:
            self._center = (self._press_center[0] - dx / self._scale, self._press_center[1] + dy / self._scale)
            self._dirty = True
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != QtCore.Qt.MouseButton.LeftButton or self._press is None:
            return
        pos = event_pos(event)
        clicked = not self._dragging
        self._press = None
        self._dragging = False
        if clicked and self._drawing:
            self._add_vertex(*self.to_world(pos.x(), pos.y()))
            return
        if clicked and self._pick_mode and len(self._xy):
            x, y = self.to_world(pos.x(), pos.y())
            self.picked.emit(x, y)

    def mouseDoubleClickEvent(self, _event) -> None:
        if self._drawing:
            self.finish_drawing()
            return
        self.fit()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        keys = QtCore.Qt.Key
        if self._drawing:
            if key in (keys.Key_Return, keys.Key_Enter):
                self.finish_drawing()
                return
            if key == keys.Key_Escape:
                self.cancel_drawing()
                return
            if key in (keys.Key_Backspace, keys.Key_Delete):
                if self._draft:
                    self._draft.pop()
                    self.update()
                return
        super().keyPressEvent(event)
