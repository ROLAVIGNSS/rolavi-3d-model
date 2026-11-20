"""GeoTIFF rasters of the project: orthomosaics and DEMs (Qt-free).

* ``inspect(path, backend)`` -> ``RasterInfo``: size, bands, kind (DEM = one band of numbers,
  orthomosaic = a photo), GDAL geotransform, CRS and no-data. Pillow reads the GeoTIFF tags (as
  ROLAVI Viewer does); GDAL (inside QGIS) is used when Pillow cannot open the file.
* ``read_dem(info)`` -> ``Dem``: the elevations (float32, NaN = no data). Very large DEMs are
  averaged in blocks so that they stay under ``DEM_MAX_CELLS`` cells.
* ``ortho_preview(info)`` -> a reduced RGBA copy for the plan view.
* ``write_ortho_zip(info, out_zip)``: the orthomosaic as ROLAVI Viewer takes it, much smaller than
  the GeoTIFF: ``name.jpg`` + ``name.jpg.aux.xml`` (GDAL PAM: SRS + GeoTransform) in ``name.zip``.
  A big one (more than ``SINGLE_MAX_PIXELS``) goes in pieces of ``PIECE_SIZE`` px, each a JPG with its
  .aux.xml, plus ``mosaic.json``: the server tiles one piece at a time, so it needs the memory of a
  piece, not of the whole image, and the original resolution is kept.

Coordinates follow GDAL: the geotransform maps pixel corners, (col, row) -> (x, y).
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import crs as crs_mod
from . import safe_xml
from .crs import CrsBackend, CrsError

KIND_DEM = "dem"
KIND_ORTHO = "ortho"

DEM_MAX_CELLS = 25_000_000          # elevations kept for the volumes (100 MB of float32)
PREVIEW_MAX = 4096                  # longest side of the image drawn in the plan view
JPEG_MAX = 65500                    # JPEG limit per side
ORTHO_JPEG_QUALITY = 80             # GDAL writes 75; ROLAVI Viewer re-tiles at 85
SINGLE_MAX_PIXELS = 150_000_000     # above: in pieces (the server decodes about 3 bytes per pixel)
OLD_SERVER_MAX_PIXELS = 200_000_000 # a server without pieces (before Step 28) takes one image up to this
PIECE_SIZE = 8192                   # px of each piece (about 200 MB decoded on the server)
PIECE_OVERLAP = 8                   # px shared with the neighbour pieces (the server samples across them)
NODATA_GUESSES = (-32767.0, -32768.0, -9999.0, -10000.0, -3.4028234663852886e38)
DEM_MODES = ("F", "I", "I;16", "I;16S", "I;16B", "I;16L", "I;16N", "I;32", "I;32S")

Progress = Callable[[float, str], None]


class RasterError(ValueError):
    """A message for the user."""


# =============================================================================
# Georeference of a grid
# =============================================================================
class GeoGrid:
    """Pixel corner (col, row) <-> map (x, y) of a grid, its CRS and the area of its cells."""

    def __init__(self, gt, width: int, height: int, crs: str, geographic: bool = False):
        self.gt = tuple(float(v) for v in gt)
        self.width = int(width)
        self.height = int(height)
        self.crs = crs
        self.geographic = bool(geographic)
        a, b, d, e = self.gt[1], self.gt[2], self.gt[4], self.gt[5]
        det = a * e - b * d
        if abs(det) < 1e-24:
            raise RasterError("Invalid georeference: the pixel size is zero.")
        self.inv = (e / det, -b / det, -d / det, a / det)

    def map_of(self, col, row):
        g = self.gt
        return g[0] + col * g[1] + row * g[2], g[3] + col * g[4] + row * g[5]

    def pixel_of(self, x, y):
        g, i = self.gt, self.inv
        dx = np.asarray(x, dtype=np.float64) - g[0]
        dy = np.asarray(y, dtype=np.float64) - g[3]
        return i[0] * dx + i[1] * dy, i[2] * dx + i[3] * dy

    def corners(self) -> np.ndarray:
        """Map coordinates of the corners: top-left, top-right, bottom-right, bottom-left."""
        w, h = self.width, self.height
        return np.array([self.map_of(c, r) for c, r in ((0, 0), (w, 0), (w, h), (0, h))], dtype=np.float64)

    def bounds(self) -> tuple[float, float, float, float]:
        c = self.corners()
        return float(c[:, 0].min()), float(c[:, 1].min()), float(c[:, 0].max()), float(c[:, 1].max())

    def window(self, c0: int, r0: int, c1: int, r1: int) -> "GeoGrid":
        g = self.gt
        gt = (g[0] + c0 * g[1] + r0 * g[2], g[1], g[2], g[3] + c0 * g[4] + r0 * g[5], g[4], g[5])
        return GeoGrid(gt, c1 - c0, r1 - r0, self.crs, self.geographic)

    def scaled(self, factor: float, width: int, height: int) -> "GeoGrid":
        """The same area with pixels ``factor`` times larger (a reduced copy)."""
        g = self.gt
        gt = (g[0], g[1] * factor, g[2] * factor, g[3], g[4] * factor, g[5] * factor)
        return GeoGrid(gt, width, height, self.crs, self.geographic)

    def pixel_area(self) -> float:
        g = self.gt
        return abs(g[1] * g[5] - g[2] * g[4])

    def cell_area(self, rows: np.ndarray | None = None) -> np.ndarray | float:
        """Area (m2) of one cell; for a longitude / latitude grid it depends on the row."""
        if not self.geographic:
            return self.pixel_area()
        rows = np.arange(self.height) if rows is None else np.asarray(rows)
        lat = np.radians(self.gt[3] + (rows + 0.5) * self.gt[5])
        a, f = 6378137.0, 1.0 / 298.257223563
        e2 = f * (2.0 - f)
        w = np.sqrt(1.0 - e2 * np.sin(lat) ** 2)
        meridian = a * (1.0 - e2) / w ** 3              # radius of curvature along the meridian
        normal = a / w                                  # ... and across it
        return self.pixel_area() * (math.pi / 180.0) ** 2 * meridian * normal * np.cos(lat)

    def cell_size_m(self) -> float:
        if not self.geographic:
            return math.sqrt(self.pixel_area())
        return float(math.sqrt(float(np.mean(self.cell_area(np.array([self.height // 2]))))))


# =============================================================================
# What a GeoTIFF is
# =============================================================================
@dataclass
class RasterInfo:
    path: Path
    width: int
    height: int
    bands: int
    mode: str                       # Pillow mode, or "gdal" when GDAL reads it
    kind: str                       # KIND_DEM / KIND_ORTHO
    gt: tuple
    crs: str = ""                   # "EPSG:32718", a WKT, or "" when the file does not say it
    crs_source: str = ""            # where the CRS came from (GeoTIFF keys, .aux.xml, name of the CRS)
    nodata: float | None = None
    reader: str = "pillow"
    geographic: bool = False

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def pixel_size(self) -> tuple[float, float]:
        g = self.gt
        return math.hypot(g[1], g[4]), math.hypot(g[2], g[5])

    def grid(self) -> GeoGrid:
        return GeoGrid(self.gt, self.width, self.height, self.crs, self.geographic)

    def describe(self) -> str:
        px, py = self.pixel_size
        unit = "°" if self.geographic else " m"
        size = f"{px:.8f}°" if self.geographic else (f"{px * 100:.2f} cm" if px < 1 else f"{px:.3f} m")
        what = "DEM" if self.kind == KIND_DEM else "Orthomosaic"
        crs = self.crs if self.crs.startswith("EPSG:") or not self.crs else "custom WKT"
        return (f"{what} {self.width:,} × {self.height:,} px · pixel {size}"
                + (f" × {py:.8f}{unit}" if abs(px - py) > 1e-9 * max(px, 1e-12) and not self.geographic else "")
                + f" · {crs or 'CRS unknown'}")


def _geokeys(tags) -> dict:
    raw = tags.get(34735)
    if not raw or len(raw) < 4:
        return {}
    doubles = tags.get(34736) or ()
    if isinstance(doubles, (int, float)):
        doubles = (doubles,)
    text = tags.get(34737) or ""
    if isinstance(text, (tuple, list)):
        text = "".join(str(t) for t in text)
    if isinstance(text, bytes):
        text = text.decode("latin-1", "replace")
    keys = {}
    for i in range(int(raw[3])):
        base = 4 + i * 4
        if base + 3 >= len(raw):
            break
        key, location, count, value = (int(v) for v in raw[base:base + 4])
        if location == 0:
            keys[key] = value
        elif location == 34736 and value + count <= len(doubles):
            keys[key] = doubles[value] if count == 1 else tuple(doubles[value:value + count])
        elif location == 34737:
            keys[key] = text[value:value + count].rstrip("|\0")
    return keys


def _geotransform(tags) -> list[float] | None:
    matrix = tags.get(34264)
    scale = tags.get(33550)
    tie = tags.get(33922)
    if matrix and len(matrix) >= 16:
        t = [float(v) for v in matrix]
        return [t[3], t[0], t[1], t[7], t[4], t[5]]
    if scale and tie and len(scale) >= 2 and len(tie) >= 6:
        s = [float(v) for v in scale]
        t = [float(v) for v in tie]
        return [t[3] - t[0] * s[0], s[0], 0.0, t[4] + t[1] * s[1], 0.0, -s[1]]
    return None


_NAMED_UTM = re.compile(r"(WGS[\W_]*(?:19)?84|SIRGAS(?:[\W_]*2000)?|PSAD[\W_]*(?:19)?56|Peru[\W_]*96)[\W_]+UTM"
                        r"[\W_]*zone[\W_]*(\d{1,2})\s*([NS])", re.IGNORECASE)


def crs_from_name(text: str) -> str:
    """'WGS 84 / UTM zone 18S', 'PSAD56 / UTM zone 18S', ... -> EPSG code ('' when not recognised)."""
    match = _NAMED_UTM.search(str(text or ""))
    if not match:
        return ""
    datum = re.sub(r"\W|_", "", match.group(1)).upper()
    datum = {"WGS1984": "WGS84", "SIRGAS": "SIRGAS2000", "PSAD1956": "PSAD56"}.get(datum, datum)
    try:
        return crs_mod.datum_utm_code(datum, int(match.group(2)), match.group(3).upper() == "S")
    except CrsError:
        return ""


def read_aux_xml(path: Path) -> tuple[list[float] | None, str]:
    """(geotransform or None, SRS WKT or '') of a GDAL .aux.xml."""
    try:
        root = safe_xml.parse_file(path)
    except (OSError, safe_xml.XmlError):
        return None, ""
    gt = None
    node = root.find("GeoTransform")
    if node is not None and node.text:
        try:
            values = [float(v) for v in re.split(r"[,\s]+", node.text.strip()) if v]
            gt = values if len(values) == 6 else None
        except ValueError:
            gt = None
    srs = root.find("SRS")
    return gt, (srs.text or "").strip() if srs is not None else ""


def _crs_of(keys: dict, path: Path, backend: CrsBackend | None) -> tuple[str, str]:
    """(CRS, where it came from) of a GeoTIFF: its keys, its .aux.xml, or the name of the CRS in it."""
    model = keys.get(1024)                  # 1 = projected, 2 = geographic
    if model == 2:
        epsg = keys.get(2048)
    else:
        epsg = keys.get(3072) or (keys.get(2048) if model != 1 else None)
    if isinstance(epsg, int) and 1024 <= epsg < 32767:
        return f"EPSG:{epsg}", "GeoTIFF keys"
    aux = Path(str(path) + ".aux.xml")
    if aux.is_file() and backend is not None:
        _gt, wkt = read_aux_xml(aux)
        if wkt:
            try:
                return backend.from_wkt(wkt), aux.name
            except CrsError:
                pass
    citation = " ".join(str(keys.get(k, "")) for k in (1026, 3073, 2049))
    code = crs_from_name(citation)
    if code:
        return code, "name of the CRS in the GeoTIFF"
    return "", ""


def _is_dem_mode(im) -> bool:
    try:
        sample_format = im.tag_v2.get(339)
    except AttributeError:
        sample_format = None
    if isinstance(sample_format, (tuple, list)):
        sample_format = sample_format[0] if sample_format else None
    return im.mode in DEM_MODES or (im.mode == "L" and sample_format in (2, 3))


def _pillow():
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RasterError("Reading GeoTIFF files needs Pillow (pip install pillow).") from exc
    Image.MAX_IMAGE_PIXELS = None           # orthomosaics are big on purpose
    return Image


def _gdal():
    try:
        from osgeo import gdal  # type: ignore[import-not-found]
    except ImportError:
        return None
    gdal.UseExceptions()
    return gdal


def _set_geographic(info: RasterInfo, backend: CrsBackend | None) -> RasterInfo:
    if info.crs and backend is not None:
        try:
            info.geographic = not backend.info(info.crs).projected
        except CrsError:
            info.geographic = False
    elif not info.crs:
        info.geographic = False
    return info


def inspect(path, backend: CrsBackend | None = None) -> RasterInfo:
    """Size, kind, georeference and CRS of a GeoTIFF (no pixel is read)."""
    path = Path(path)
    if not path.is_file():
        raise RasterError(f"File not found: {path}")
    pillow_error = ""
    try:
        Image = _pillow()
        with Image.open(str(path)) as im:
            tags = im.tag_v2
            gt = _geotransform(tags)
            keys = _geokeys(tags)
            if gt is None:
                aux_gt, _wkt = read_aux_xml(Path(str(path) + ".aux.xml"))
                gt = aux_gt
            if gt is None:
                raise RasterError(f"{path.name} has no georeference (no GeoTIFF tags and no .aux.xml). "
                                  "Export it again as GeoTIFF from QGIS, Metashape or Pix4D.")
            if keys.get(1025) == 2:          # PixelIsPoint: the tie point is the centre of the first pixel
                gt[0] -= (gt[1] + gt[2]) / 2.0
                gt[3] -= (gt[4] + gt[5]) / 2.0
            crs, source = _crs_of(keys, path, backend)
            nodata = None
            raw = tags.get(42113)
            if raw is not None:
                try:
                    nodata = float(str(raw).strip().strip("\0"))
                except ValueError:
                    nodata = None
            kind = KIND_DEM if _is_dem_mode(im) else KIND_ORTHO
            info = RasterInfo(path, im.size[0], im.size[1], len(im.getbands()), im.mode, kind, tuple(gt),
                              crs, source, nodata, "pillow")
            return _set_geographic(info, backend)
    except RasterError:
        raise
    except Exception as exc:          # BigTIFF, an unusual compression...: GDAL when it is there
        pillow_error = str(exc) or exc.__class__.__name__
    gdal = _gdal()
    if gdal is None:
        raise RasterError(f"Could not read {path.name}: {pillow_error}. Save it again from QGIS as a GeoTIFF "
                          "(Raster > Conversion > Translate) and try again.")
    ds = gdal.Open(str(path))
    if ds is None:
        raise RasterError(f"Could not read {path.name}.")
    gt = list(ds.GetGeoTransform(can_return_null=True) or [])
    if len(gt) != 6:
        raise RasterError(f"{path.name} has no georeference.")
    band = ds.GetRasterBand(1)
    numeric = gdal.GetDataTypeName(band.DataType) not in ("Byte",)
    kind = KIND_DEM if ds.RasterCount == 1 and numeric else KIND_ORTHO
    crs, source = "", ""
    wkt = ds.GetProjection() or ""
    if wkt and backend is not None:
        try:
            crs, source = backend.from_wkt(wkt), "GeoTIFF (GDAL)"
        except CrsError:
            crs, source = "", ""
    info = RasterInfo(path, ds.RasterXSize, ds.RasterYSize, ds.RasterCount, "gdal", kind, tuple(gt), crs, source,
                      band.GetNoDataValue(), "gdal")
    ds = None
    return _set_geographic(info, backend)


# =============================================================================
# DEM
# =============================================================================
@dataclass
class Dem:
    info: RasterInfo
    z: np.ndarray                   # float32 (rows, cols), NaN = no data
    grid: GeoGrid
    factor: int = 1                 # cells of the file averaged into one
    _stats: dict | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return self.info.name

    def stats(self) -> dict:
        """Cells with data, z min / max / mean, area with data (m2), cell size (m); computed once."""
        if self._stats is None:
            self._stats = self._compute_stats()
        return self._stats

    def _compute_stats(self) -> dict:
        valid = np.isfinite(self.z)
        if not valid.any():
            return {"cells": 0}
        area = self.grid.cell_area()
        if self.grid.geographic:
            area_m2 = float(np.where(valid, np.asarray(area)[:, None], 0.0).sum())
        else:
            area_m2 = float(valid.sum() * area)
        return {"cells": int(valid.sum()), "zmin": float(np.nanmin(self.z)), "zmax": float(np.nanmax(self.z)),
                "zmean": float(np.nanmean(self.z)), "area": area_m2, "cell": self.grid.cell_size_m()}

    def describe(self) -> str:
        s = self.stats()
        if not s.get("cells"):
            return "no elevation values"
        averaged = f" (averaged {self.factor}×{self.factor})" if self.factor > 1 else ""
        return (f"{self.z.shape[1]:,} × {self.z.shape[0]:,} cells of {s['cell']:.3f} m{averaged} · "
                f"z {s['zmin']:,.2f} – {s['zmax']:,.2f} m · {s['area'] / 10000.0:,.2f} ha with data")

    def elevation_at(self, x: float, y: float) -> float | None:
        """Elevation of the cell under a point of the DEM's CRS (None outside or without data)."""
        col, row = self.grid.pixel_of(x, y)
        col, row = int(math.floor(float(col))), int(math.floor(float(row)))
        if 0 <= row < self.z.shape[0] and 0 <= col < self.z.shape[1]:
            value = float(self.z[row, col])
            return value if math.isfinite(value) else None
        return None


def block_mean(arr: np.ndarray, factor: int) -> np.ndarray:
    """Mean of factor x factor blocks, ignoring NaN (a block without data -> NaN)."""
    if factor <= 1:
        return arr
    h, w = arr.shape
    H, W = -(-h // factor) * factor, -(-w // factor) * factor
    if (H, W) != (h, w):
        pad = np.full((H, W), np.nan, np.float32)
        pad[:h, :w] = arr
        arr = pad
    blocks = arr.reshape(H // factor, factor, W // factor, factor)
    valid = np.isfinite(blocks)
    total = np.where(valid, blocks, 0).sum(axis=(1, 3), dtype=np.float64)
    count = valid.sum(axis=(1, 3))
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (total / count).astype(np.float32)
    out[count == 0] = np.nan
    return out


def _clean(arr: np.ndarray, nodata: float | None) -> np.ndarray:
    arr = np.asarray(arr, dtype=np.float32)
    bad = ~np.isfinite(arr) | (np.abs(arr) > 1e7)
    if nodata is not None and math.isfinite(nodata):
        bad |= arr == np.float32(nodata)
    else:
        for guess in NODATA_GUESSES:
            bad |= arr == np.float32(guess)
    if bad.any():
        arr = arr.copy()
        arr[bad] = np.nan
    return arr


def _pillow_band(part) -> np.ndarray:
    try:
        arr = np.asarray(part)
    except Exception:
        arr = np.asarray(part.convert("F"))
    if arr.dtype.byteorder == ">":
        arr = arr.astype(arr.dtype.newbyteorder("<"))
    return arr


def read_dem(info: RasterInfo, progress: Progress | None = None, max_cells: int = DEM_MAX_CELLS) -> Dem:
    """The elevations of a DEM GeoTIFF (blocks averaged when it has more than ``max_cells`` cells)."""
    if info.kind != KIND_DEM:
        raise RasterError(f"{info.name} is not a DEM (it is a {info.bands}-band image).")
    report = progress or (lambda _f, _t: None)
    w, h = info.width, info.height
    factor = max(1, int(math.ceil(math.sqrt(float(w) * h / max_cells))))
    out_w, out_h = -(-w // factor), -(-h // factor)
    z = np.empty((out_h, out_w), np.float32)
    report(0.02, "Reading the DEM")
    if info.reader == "gdal":
        gdal = _gdal()
        ds = gdal.Open(str(info.path))
        band = ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        strip = max(factor, (1024 // factor) * factor)
        for r0 in range(0, h, strip):
            r1 = min(h, r0 + strip)
            arr = _clean(band.ReadAsArray(0, r0, w, r1 - r0), nodata)
            rows = block_mean(arr, factor)
            z[r0 // factor: r0 // factor + rows.shape[0]] = rows
            report(0.05 + 0.9 * r1 / h, "Reading the DEM")
        ds = None
    else:
        Image = _pillow()
        try:
            with Image.open(str(info.path)) as im:
                im.load()
                strip = max(factor, (2048 // factor) * factor)
                for r0 in range(0, h, strip):
                    r1 = min(h, r0 + strip)
                    arr = _clean(_pillow_band(im.crop((0, r0, w, r1))), info.nodata)
                    rows = block_mean(arr, factor)
                    z[r0 // factor: r0 // factor + rows.shape[0]] = rows
                    report(0.05 + 0.9 * r1 / h, "Reading the DEM")
        except MemoryError:
            raise RasterError(f"Not enough memory to read the {w:,} × {h:,} DEM. Export it with a larger cell "
                              "size (e.g. 0.10 m) and try again.")
    if not np.isfinite(z).any():
        raise RasterError(f"{info.name} has no elevation values (only 'no data').")
    grid = GeoGrid(info.gt, w, h, info.crs, info.geographic)
    if factor > 1:
        grid = grid.scaled(factor, out_w, out_h)
    report(1.0, "DEM read")
    return Dem(info, z, grid, factor)


# =============================================================================
# Orthomosaic
# =============================================================================
def _has_alpha(im) -> bool:
    return im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)


def _to_rgb_black(im, nodata: float | None):
    """The orthomosaic as an RGB Pillow image with the areas without data in black, as ROLAVI Viewer
    expects (it makes the black border transparent). No extra copy for a plain RGB image."""
    Image = _pillow()
    if _has_alpha(im):
        rgba = im if im.mode == "RGBA" else im.convert("RGBA")
        rgb = rgba.convert("RGB")
        hidden = rgba.getchannel("A").point(lambda a: 255 if a < 128 else 0)
        rgb.paste((0, 0, 0), (0, 0) + rgb.size, hidden)
        return rgb
    rgb = im if im.mode == "RGB" else im.convert("RGB")
    if nodata is not None and math.isfinite(nodata) and 0 < nodata <= 255:
        arr = np.array(rgb)
        arr[(arr == np.uint8(nodata)).all(axis=2)] = 0
        rgb = Image.fromarray(arr, "RGB")
    return rgb


def _gdal_rgb(info: RasterInfo, out_w: int, out_h: int) -> tuple[np.ndarray, np.ndarray]:
    """(RGB uint8, alpha uint8) of an orthomosaic read by GDAL, resampled to out_w x out_h."""
    gdal = _gdal()
    ds = gdal.Open(str(info.path))
    count = ds.RasterCount
    bands = [ds.GetRasterBand(i + 1) for i in range(min(count, 4))]
    alpha = None
    colour = []
    for band in bands:
        if band.GetColorInterpretation() == gdal.GCI_AlphaBand:
            alpha = band
        else:
            colour.append(band)
    colour = (colour * 3)[:3] if len(colour) < 3 else colour[:3]
    kw = {"buf_xsize": out_w, "buf_ysize": out_h, "resample_alg": gdal.GRIORA_Average}
    rgb = np.dstack([np.asarray(b.ReadAsArray(0, 0, info.width, info.height, **kw)) for b in colour])
    if rgb.dtype != np.uint8:
        top = float(np.nanpercentile(rgb, 99.5)) or 1.0
        rgb = np.clip(rgb.astype(np.float32) * (255.0 / top), 0, 255).astype(np.uint8)
    if alpha is not None:
        a = np.asarray(alpha.ReadAsArray(0, 0, info.width, info.height, **kw)).astype(np.uint8)
    else:
        a = np.full(rgb.shape[:2], 255, np.uint8)
        nodata = colour[0].GetNoDataValue()
        if nodata is not None:
            a[(rgb == np.uint8(max(0, min(255, nodata)))).all(axis=2)] = 0
    ds = None
    return np.ascontiguousarray(rgb), a


@dataclass
class Preview:
    """A reduced copy of a raster for the plan view: RGBA pixels and where they are."""
    rgba: np.ndarray                # uint8 (rows, cols, 4)
    grid: GeoGrid


def ortho_preview(info: RasterInfo, progress: Progress | None = None, max_side: int = PREVIEW_MAX) -> Preview:
    report = progress or (lambda _f, _t: None)
    factor = max(1, int(math.ceil(max(info.width, info.height) / float(max_side))))
    out_w, out_h = -(-info.width // factor), -(-info.height // factor)
    report(0.05, "Reading the orthomosaic")
    if info.reader == "gdal":
        rgb, alpha = _gdal_rgb(info, out_w, out_h)
        rgba = np.dstack([rgb, alpha])
    else:
        Image = _pillow()
        try:
            with Image.open(str(info.path)) as im:
                im.load()
                report(0.6, "Reducing the orthomosaic for the plan view")
                keep_alpha = _has_alpha(im)
                target = "RGBA" if keep_alpha else "RGB"
                small = im if im.mode == target else im.convert(target)
                if factor > 1:
                    small = small.reduce(factor)
                rgba = np.array(small.convert("RGBA"))
                if not keep_alpha and info.nodata is not None and math.isfinite(info.nodata) \
                        and 0 <= info.nodata <= 255:
                    rgba[(rgba[..., :3] == np.uint8(info.nodata)).all(axis=2), 3] = 0
        except MemoryError:
            raise RasterError(f"Not enough memory to read the {info.width:,} × {info.height:,} orthomosaic.")
        out_h, out_w = rgba.shape[:2]
    report(1.0, "Orthomosaic ready")
    return Preview(np.ascontiguousarray(rgba, dtype=np.uint8), info.grid().scaled(factor, out_w, out_h))


def aux_xml_text(gt, wkt: str, geographic: bool = False) -> str:
    """GDAL PAM .aux.xml of a 3-band JPEG, as QGIS / GDAL write it (SRS + GeoTransform)."""
    mapping = "2,1" if geographic else "1,2"
    srs = (wkt or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    bands = "".join(
        f'  <PAMRasterBand band="{b}">\n    <UnitType>metre</UnitType>\n'
        '    <Metadata domain="IMAGE_STRUCTURE">\n      <MDI key="COMPRESSION">JPEG</MDI>\n'
        '    </Metadata>\n  </PAMRasterBand>\n' for b in (1, 2, 3))
    numbers = ",".join(f"{float(v):24.16e}" for v in gt)        # GDAL's own format
    return ('<PAMDataset>\n'
            f'  <SRS dataAxisToSRSAxisMapping="{mapping}">{srs}</SRS>\n'
            f'  <GeoTransform>{numbers}</GeoTransform>\n'
            '  <Metadata>\n    <MDI key="AREA_OR_POINT">Area</MDI>\n  </Metadata>\n'
            '  <Metadata domain="IMAGE_STRUCTURE">\n    <MDI key="SOURCE_COLOR_SPACE">YCbCr</MDI>\n'
            '    <MDI key="INTERLEAVE">PIXEL</MDI>\n    <MDI key="COMPRESSION">JPEG</MDI>\n  </Metadata>\n'
            f'{bands}</PAMDataset>\n')


_UNSUPPORTED_DATUMS = ("PSAD", "PROVISIONAL_S_AMERICAN", "PROVISIONAL SOUTH AMERICAN", "SAD69",
                       "SOUTH_AMERICAN_1969", "NAD27", "NORTH_AMERICAN_1927", "ED50", "EUROPEAN_1950", "BOGOTA")
_COMPATIBLE_DATUMS = ("WGS", "SIRGAS", "NAD83", "NORTH_AMERICAN_1983", "ETRS", "ETRF", "GRS_1980", "GRS 1980",
                      "MAGNA", "ITRF")


def viewer_crs_problem(wkt: str) -> str:
    """Why ROLAVI Viewer would not take an orthomosaic in this CRS ('' when it takes it)."""
    upper = (wkt or "").upper()
    if not upper:
        return "The orthomosaic has no CRS."
    name = re.search(r'^\s*\w+\s*\[\s*"([^"]*)"', wkt or "")
    name = name.group(1) if name else "this CRS"
    for datum in _UNSUPPORTED_DATUMS:
        if datum in upper:
            return (f"ROLAVI Viewer does not take the datum of {name} yet. Reproject the orthomosaic to "
                    "WGS 84 / UTM in QGIS (Raster > Projections > Warp) and import it again.")
    if upper.startswith(("GEOGCS", "GEOGCRS")):
        return ""
    if "UTM" not in upper or not re.search(r"ZONE[_\s]*\d{1,2}", upper):
        return (f"ROLAVI Viewer takes orthomosaics in UTM or longitude / latitude, not {name}. Reproject it to "
                "WGS 84 / UTM in QGIS (Raster > Projections > Warp) and import it again.")
    if not any(d in upper for d in _COMPATIBLE_DATUMS):
        return f"ROLAVI Viewer does not take the datum of {name} yet. Reproject the orthomosaic to WGS 84 / UTM."
    return ""


@dataclass
class OrthoZip:
    path: Path
    jpg_bytes: int
    width: int
    height: int
    factor: int                     # 1 = full resolution
    pieces: int = 1                 # JPG pieces in the zip (1 = a single JPG, no mosaic.json)


def needs_pieces(info: RasterInfo) -> bool:
    """True when the orthomosaic is too big to go to ROLAVI Viewer as one image."""
    return info.width * info.height > SINGLE_MAX_PIXELS


def _piece_windows(width: int, height: int, size: int = PIECE_SIZE, overlap: int = PIECE_OVERLAP):
    """(row, col, piece window, core window in the piece) of the pieces covering the image."""
    out = []
    for row, r0 in enumerate(range(0, height, size)):
        for col, c0 in enumerate(range(0, width, size)):
            c1, r1 = min(width, c0 + size), min(height, r0 + size)
            pc0, pr0 = max(0, c0 - overlap), max(0, r0 - overlap)
            pc1, pr1 = min(width, c1 + overlap), min(height, r1 + overlap)
            out.append((row, col, (pc0, pr0, pc1, pr1), (c0 - pc0, r0 - pr0, c1 - pc0, r1 - pr0)))
    return out


def write_ortho_zip(info: RasterInfo, out_zip, backend: CrsBackend | None, quality: int = ORTHO_JPEG_QUALITY,
                    progress: Progress | None = None, pieces: bool | None = None) -> OrthoZip:
    """``name.jpg`` + ``name.jpg.aux.xml`` in ``out_zip`` (what ROLAVI Viewer imports as an orthomosaic),
    or, for a big one (``pieces``; by default when ``needs_pieces``), its pieces and ``mosaic.json``."""
    report = progress or (lambda _f, _t: None)
    if pieces is None:
        pieces = needs_pieces(info)
    if info.kind != KIND_ORTHO:
        raise RasterError(f"{info.name} is a DEM: send it as GeoTIFF.")
    if not info.crs:
        raise RasterError(f"{info.name} has no CRS: choose it when importing it.")
    wkt = info.crs if not info.crs.startswith("EPSG:") else ""
    if not wkt:
        if backend is None:
            wkt = crs_mod.wgs84_wkt(info.crs)
        else:
            wkt = backend.to_wkt(info.crs)
    problem = viewer_crs_problem(wkt)
    if problem:
        raise RasterError(problem)
    factor = 1 if pieces else max(1, int(math.ceil(max(info.width, info.height) / float(JPEG_MAX))))
    Image = _pillow()
    report(0.03, "Reading the orthomosaic")
    try:
        if info.reader == "gdal":
            out_w, out_h = -(-info.width // factor), -(-info.height // factor)
            rgb, alpha = _gdal_rgb(info, out_w, out_h)
            rgb[alpha < 128] = 0
            image = Image.fromarray(rgb, "RGB")
        else:
            im = Image.open(str(info.path))
            image = None
            try:
                im.load()                        # (the file itself is closed once it is read)
                report(0.35, "Filling the areas without data in black")
                image = _to_rgb_black(im, info.nodata)
            finally:
                if image is not im:              # a plain RGB image is used as it is: no copy
                    im.close()
            if factor > 1:
                reduced = image.reduce(factor)
                image.close()
                image = reduced
    except MemoryError:
        raise RasterError(f"Not enough memory to convert the {info.width:,} × {info.height:,} orthomosaic. Save it "
                          "from QGIS as JPEG (Raster > Conversion > Translate, format JPEG: it writes the .jpg and "
                          "its .aux.xml), zip the two files and send the zip, or use a larger pixel size.")
    out_zip = Path(out_zip)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w .()-]+", "_", info.path.stem).strip(" .") or "orthomosaic"
    if pieces:
        try:
            return _write_pieces(image, info, out_zip, stem, wkt, quality, report)
        finally:
            image.close()
    jpg_name = stem + ".jpg"
    gt = info.gt
    if factor > 1:
        gt = (gt[0], gt[1] * factor, gt[2] * factor, gt[3], gt[4] * factor, gt[5] * factor)
    report(0.5, f"Writing {jpg_name} (JPEG quality {quality})")
    handle, temp_jpg = tempfile.mkstemp(suffix=".jpg", prefix="rolavi_ortho_")
    os.close(handle)
    try:
        image.save(temp_jpg, "JPEG", quality=int(quality), subsampling=2, optimize=True)
        width, height = image.size
        image.close()
        report(0.9, f"Writing {out_zip.name}")
        with zipfile.ZipFile(str(out_zip), "w") as zf:
            zf.write(temp_jpg, jpg_name, compress_type=zipfile.ZIP_STORED)
            zf.writestr(jpg_name + ".aux.xml", aux_xml_text(gt, wkt, info.geographic),
                        compress_type=zipfile.ZIP_DEFLATED)
        jpg_bytes = os.path.getsize(temp_jpg)
    finally:
        try:
            os.remove(temp_jpg)
        except OSError:
            pass
    report(1.0, "Orthomosaic converted")
    return OrthoZip(out_zip, jpg_bytes, width, height, factor)


def _write_pieces(image, info: RasterInfo, out_zip: Path, stem: str, wkt: str, quality: int,
                  report: Progress) -> OrthoZip:
    """The pieces (JPG + .aux.xml each) and mosaic.json; pieces without any data are left out."""
    width, height = image.size
    g = info.gt
    windows = _piece_windows(width, height)
    listed = []
    total = 0
    handle, temp_jpg = tempfile.mkstemp(suffix=".jpg", prefix="rolavi_piece_")
    os.close(handle)
    try:
        with zipfile.ZipFile(str(out_zip), "w") as zf:
            for number, (row, col, (pc0, pr0, pc1, pr1), core) in enumerate(windows, start=1):
                report(0.4 + 0.55 * (number - 1) / len(windows), f"Writing piece {number} of {len(windows)}")
                piece = image.crop((pc0, pr0, pc1, pr1))
                if piece.getbbox() is None:           # all black: only 'no data'
                    piece.close()
                    continue
                name = f"{stem}_r{row:02d}_c{col:02d}.jpg"
                piece.save(temp_jpg, "JPEG", quality=int(quality), subsampling=2, optimize=True)
                piece.close()
                zf.write(temp_jpg, name, compress_type=zipfile.ZIP_STORED)
                total += os.path.getsize(temp_jpg)
                gt = (g[0] + pc0 * g[1] + pr0 * g[2], g[1], g[2], g[3] + pc0 * g[4] + pr0 * g[5], g[4], g[5])
                zf.writestr(name + ".aux.xml", aux_xml_text(gt, wkt, info.geographic),
                            compress_type=zipfile.ZIP_DEFLATED)
                listed.append({"image": name, "aux": name + ".aux.xml", "offset": [pc0, pr0],
                               "size": [pc1 - pc0, pr1 - pr0], "core": list(core)})
            if not listed:
                raise RasterError(f"{info.name} has no pixels with data.")
            manifest = {"format": "rolavi-ortho-mosaic", "version": 1, "name": stem, "width": width,
                        "height": height, "pieceSize": PIECE_SIZE, "overlap": PIECE_OVERLAP,
                        "crs": info.crs if info.crs.startswith("EPSG:") else "", "pieces": listed}
            zf.writestr("mosaic.json", json.dumps(manifest, indent=1), compress_type=zipfile.ZIP_DEFLATED)
    except BaseException:
        try:
            out_zip.unlink()
        except OSError:
            pass
        raise
    finally:
        try:
            os.remove(temp_jpg)
        except OSError:
            pass
    report(1.0, "Orthomosaic converted")
    return OrthoZip(out_zip, total, width, height, 1, len(listed))
