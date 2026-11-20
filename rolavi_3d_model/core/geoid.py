"""Geoid model in the NOAA / PROJ ``.gtx`` format: undulation N (metres) at longitude / latitude.

Orthometric height:  H = h - N   (h = ellipsoidal height, e.g. from GNSS).

GTX layout: a 40-byte header (south-west latitude, south-west longitude, latitude step,
longitude step as float64; rows, columns as int32), then rows x columns float32 values,
row 0 at the south edge, column 0 at the west edge. The standard is big-endian; files
written little-endian are recognised too. -88.8888 (or NaN) marks cells without data.

The grid is memory-mapped, so a global EGM2008 at 1' (about 900 MB) is not loaded
into memory. No Qt and no QGIS imports.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import numpy as np

HEADER = 40
NO_DATA = -88.8888
_BLOCK = 2_000_000


class GeoidError(ValueError):
    pass


class GtxGeoid:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise GeoidError(f"Geoid file not found: {self.path}")
        size = os.path.getsize(self.path)
        if size < HEADER + 16:
            raise GeoidError(f"{self.path.name} is not a GTX geoid model (file too small).")
        with open(self.path, "rb") as fh:
            header = fh.read(HEADER)
        for order in (">", "<"):
            lat0, lon0, dlat, dlon = struct.unpack(order + "4d", header[:32])
            rows, cols = struct.unpack(order + "2i", header[32:40])
            if (0 < rows < 10_000_000 and 0 < cols < 10_000_000 and HEADER + rows * cols * 4 == size
                    and 0 < dlat < 180 and 0 < dlon < 360 and -90.01 <= lat0 <= 90.01 and -360 <= lon0 <= 360):
                break
        else:
            raise GeoidError(f"{self.path.name} is not a valid GTX geoid model (header and size do not match).")
        self.byte_order = "big-endian" if order == ">" else "little-endian"
        self.lat0, self.lon0, self.dlat, self.dlon = float(lat0), float(lon0), float(dlat), float(dlon)
        self.rows, self.cols = int(rows), int(cols)
        self.grid = np.memmap(self.path, dtype=order + "f4", mode="r", offset=HEADER, shape=(self.rows, self.cols))
        self.period = int(round(360.0 / self.dlon))
        self.is_global = self.cols >= self.period - 1      # covers every longitude (wrap around)

    # ------------------------------------------------------------------ description
    @property
    def lat_range(self) -> tuple[float, float]:
        return self.lat0, self.lat0 + (self.rows - 1) * self.dlat

    @property
    def lon_range(self) -> tuple[float, float]:
        return self.lon0, self.lon0 + (self.cols - 1) * self.dlon

    def describe(self) -> str:
        minutes = self.dlat * 60.0
        step_text = f"{minutes:g}'" if minutes >= 1.0 else f"{self.dlat * 3600.0:g}\""
        s, n = self.lat_range
        w, e = self.lon_range
        area = "global" if self.is_global else f"lat {s:.2f}..{n:.2f}, lon {w:.2f}..{e:.2f}"
        return f"{self.path.name} · {step_text} grid · {self.rows:,} x {self.cols:,} · {area}"

    # ------------------------------------------------------------------ values
    def undulation(self, lon, lat) -> np.ndarray:
        """N in metres (bilinear), NaN where the model has no value (outside or no data)."""
        lon = np.asarray(lon, dtype=np.float64).ravel()
        lat = np.asarray(lat, dtype=np.float64).ravel()
        out = np.full(lon.shape, np.nan)
        for start in range(0, len(lon), _BLOCK):
            part = slice(start, start + _BLOCK)
            out[part] = self._bilinear(lon[part], lat[part])
        return out

    def _bilinear(self, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        y = (lat - self.lat0) / self.dlat
        x = (lon - self.lon0) / self.dlon
        if self.is_global:
            x = np.mod(x, self.period)
        out = np.full(lon.shape, np.nan)
        inside = np.isfinite(x) & np.isfinite(y) & (y >= 0) & (y <= self.rows - 1) & (x >= 0)
        if not self.is_global:
            inside &= x <= self.cols - 1
        if not inside.any():
            return out
        xi, yi = x[inside], y[inside]
        r0 = np.minimum(np.floor(yi).astype(np.int64), self.rows - 2) if self.rows > 1 else np.zeros(len(yi), np.int64)
        c0 = np.floor(xi).astype(np.int64)
        if self.is_global:
            c0 = np.mod(c0, self.period)
            c1 = np.mod(c0 + 1, self.period)
            c0 = np.minimum(c0, self.cols - 1)
            c1 = np.minimum(c1, self.cols - 1)
        else:
            c0 = np.minimum(c0, self.cols - 2) if self.cols > 1 else np.zeros(len(xi), np.int64)
            c1 = c0 + (1 if self.cols > 1 else 0)
        r1 = r0 + (1 if self.rows > 1 else 0)
        fy = yi - r0
        fx = xi - np.floor(xi) if self.is_global else xi - c0
        v00 = self.grid[r0, c0].astype(np.float64)
        v01 = self.grid[r0, c1].astype(np.float64)
        v10 = self.grid[r1, c0].astype(np.float64)
        v11 = self.grid[r1, c1].astype(np.float64)
        values = (v00 * (1 - fx) * (1 - fy) + v01 * fx * (1 - fy) + v10 * (1 - fx) * fy + v11 * fx * fy)
        corners = np.stack([v00, v01, v10, v11])
        bad = ~np.isfinite(corners).all(axis=0) | (np.abs(corners - NO_DATA) < 1e-3).any(axis=0)
        values[bad] = np.nan
        out[inside] = values
        return out


def open_geoid(path: str | Path) -> GtxGeoid:
    return GtxGeoid(path)


def write_gtx(path: str | Path, lat0: float, lon0: float, dlat: float, dlon: float, values: np.ndarray,
              big_endian: bool = True) -> Path:
    """Write a GTX file (used by the tests; values[row, col], row 0 = south)."""
    values = np.asarray(values, dtype=np.float32)
    rows, cols = values.shape
    order = ">" if big_endian else "<"
    path = Path(path)
    with open(path, "wb") as fh:
        fh.write(struct.pack(order + "4d2i", lat0, lon0, dlat, dlon, rows, cols))
        fh.write(values.astype(order + "f4").tobytes())
    return path
