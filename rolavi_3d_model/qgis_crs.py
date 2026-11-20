"""QGIS' own coordinate engine, for the Python of QGIS when it has no pyproj.

Same interface as ``core.crs.PyprojBackend``: CRS information, WKT, conversions to and from WGS 84
longitude / latitude, and the search of the EPSG database. QGIS keeps x = east (longitude) and
y = north (latitude) whatever the axis order of the CRS, as pyproj's ``always_xy``.

The conversions go point by point (PyQGIS has no array call), so they are slower than pyproj
with millions of points; the plugin uses pyproj whenever QGIS' Python has it.
"""

from __future__ import annotations

import threading

import numpy as np
from qgis.core import (  # type: ignore[import-not-found]
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsPointXY,
    QgsProject,
    QgsUnitTypes,
)

from .core.crs import WGS84, CrsBackend, CrsEntry, CrsError, CrsInfo, normalize, search_entries


def _wkt1_variant():
    """WKT1 as GDAL writes it (the enum moved in QGIS 3.36 / 4)."""
    try:
        from qgis.core import Qgis  # type: ignore[import-not-found]

        return Qgis.CrsWktVariant.Wkt1Gdal
    except AttributeError:
        return QgsCoordinateReferenceSystem.WKT1_GDAL


# Qgis.CrsType values: 3 / 4 = geographic 2D / 3D, 6 = projected, 12 = derived projected
_KIND_BY_VALUE = {3: "geographic", 4: "geographic", 6: "projected", 12: "projected"}


def _kind(value) -> str | None:
    """'projected', 'geographic', or None for the other kinds of CRS (any QGIS version)."""
    text = str(value or "").lower()
    if "geographic" in text:
        return "geographic"
    if "projected" in text:
        return "projected"
    try:
        return _KIND_BY_VALUE.get(int(getattr(value, "value", value)))
    except (TypeError, ValueError):
        return None


class QgisBackend(CrsBackend):
    name = "QGIS"

    def __init__(self):
        self._infos: dict[str, CrsInfo] = {}
        self._catalog: list[CrsEntry] | None = None
        self._local = threading.local()          # transforms are kept per thread (UI / worker)
        try:
            self._context = QgsCoordinateTransformContext(QgsProject.instance().transformContext())
        except Exception:
            self._context = QgsCoordinateTransformContext()
        self._wgs84 = QgsCoordinateReferenceSystem(WGS84)

    def _crs(self, crs: str) -> QgsCoordinateReferenceSystem:
        code = normalize(crs)
        if not code:
            raise CrsError("No CRS was given.")
        if code.startswith("EPSG:"):
            obj = QgsCoordinateReferenceSystem(code)
        else:
            obj = QgsCoordinateReferenceSystem.fromWkt(code)
        if not obj.isValid():
            raise CrsError(f"Unknown CRS: {crs}")
        return obj

    def info(self, crs: str) -> CrsInfo:
        code = normalize(crs)
        if code in self._infos:
            return self._infos[code]
        obj = self._crs(code)
        try:
            metres = QgsUnitTypes.encodeUnit(obj.mapUnits()) in ("meters", "metres")
        except Exception:
            metres = not obj.isGeographic()
        authid = obj.authid()
        info = CrsInfo(code=authid if authid.upper().startswith("EPSG:") else code, name=obj.description(),
                       projected=not obj.isGeographic(), metres=bool(metres))
        self._infos[code] = info
        return info

    def from_wkt(self, wkt: str) -> str:
        obj = QgsCoordinateReferenceSystem.fromWkt(wkt)
        if not obj.isValid():
            raise CrsError("The coordinate system (WKT) was not recognised.")
        authid = obj.authid()
        return authid if authid.upper().startswith("EPSG:") else obj.toWkt(_wkt1_variant())

    def to_wkt(self, crs: str) -> str:
        return self._crs(crs).toWkt(_wkt1_variant())

    def _transform(self, crs: str, inverse: bool) -> QgsCoordinateTransform:
        code = normalize(crs)
        if not hasattr(self._local, "cache"):
            self._local.cache = {}
        key = (code, inverse)
        if key not in self._local.cache:
            source, target = (self._wgs84, self._crs(code)) if inverse else (self._crs(code), self._wgs84)
            transform = QgsCoordinateTransform(source, target, self._context)
            if not transform.isValid():
                raise CrsError(f"No transformation between {code} and WGS84.")
            self._local.cache[key] = transform
        return self._local.cache[key]

    def _run(self, transform: QgsCoordinateTransform, a, b) -> tuple[np.ndarray, np.ndarray]:
        a = np.asarray(a, dtype=np.float64).reshape(-1)
        b = np.asarray(b, dtype=np.float64).reshape(-1)
        out_a = np.empty_like(a)
        out_b = np.empty_like(b)
        try:
            for i in range(len(a)):
                point = transform.transform(QgsPointXY(float(a[i]), float(b[i])))
                out_a[i], out_b[i] = point.x(), point.y()
        except Exception as exc:
            raise CrsError(f"Some coordinates could not be converted: {exc}") from exc
        if len(out_a) and not (np.isfinite(out_a).all() and np.isfinite(out_b).all()):
            raise CrsError("Some coordinates could not be converted: check the CRS of the model.")
        return out_a, out_b

    def to_lonlat(self, crs, x, y):
        if normalize(crs) == WGS84:
            return np.asarray(x, dtype=np.float64).reshape(-1).copy(), np.asarray(y, dtype=np.float64).reshape(-1).copy()
        return self._run(self._transform(crs, inverse=False), x, y)

    def from_lonlat(self, crs, lon, lat):
        if normalize(crs) == WGS84:
            return (np.asarray(lon, dtype=np.float64).reshape(-1).copy(),
                    np.asarray(lat, dtype=np.float64).reshape(-1).copy())
        return self._run(self._transform(crs, inverse=True), lon, lat)

    def _load_catalog(self) -> list[CrsEntry]:
        """EPSG projected and geographic CRS known to QGIS (read once, not deprecated)."""
        if self._catalog is not None:
            return self._catalog
        catalog: list[CrsEntry] = []
        try:
            records = QgsApplication.coordinateReferenceSystemRegistry().crsDbRecords()
            for record in records:
                if str(record.authName).upper() != "EPSG" or record.deprecated:
                    continue
                kind = _kind(getattr(record, "type", None))
                if kind is None:
                    continue                   # vertical, compound, geocentric...
                catalog.append(CrsEntry(f"EPSG:{record.authId}", str(record.description), kind, ""))
        except Exception:
            catalog = []                       # the CRS can still be typed by its code
        self._catalog = catalog
        return catalog

    def search(self, text, kinds=("projected", "geographic"), limit=300):
        return search_entries(self._load_catalog(), text, kinds, limit)
