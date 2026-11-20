"""Cut and fill volumes from DEMs (Qt-free), computed as ROLAVI Viewer does.

* Two DEMs: ``dz = after - before`` on the grid of the "before" DEM (the reference). The "after" DEM is
  sampled bilinearly at the centre of each cell, in its own CRS (so the two may differ).
* A DEM and an elevation (the base or design level, e.g. a mean elevation): ``dz = elevation - DEM``.

``dz < 0`` is cut (material above the final surface, red), ``dz > 0`` is fill (green). With polygons
only the cells whose centre is inside them count (even-odd rule: holes are left out). Changes
smaller than ``threshold`` are left out of both. Volumes in m³, areas in m² (planimetric).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from . import crs as crs_mod
from .crs import CrsBackend
from .raster_io import PREVIEW_MAX, Dem, GeoGrid, Preview, block_mean
from .vector_io import PolygonSet

CUT_RGB = (229, 72, 77)                   # red
FILL_RGB = (46, 158, 68)                  # green
MODE_DEMS = "dems"
MODE_LEVEL = "level"

Progress = Callable[[float, str], None]


class VolumeError(ValueError):
    """A message for the user."""


# =============================================================================
# Grid helpers
# =============================================================================
def sample(arr: np.ndarray, px: np.ndarray, py: np.ndarray) -> np.ndarray:
    """Bilinear value at pixel coordinates (centres at i + 0.5); next to no-data, the nearest cell."""
    h, w = arr.shape
    fx, fy = px - 0.5, py - 0.5
    x0 = np.floor(fx).astype(np.int64)
    y0 = np.floor(fy).astype(np.int64)
    tx = (fx - x0).astype(np.float32)
    ty = (fy - y0).astype(np.float32)
    inside = (px >= 0) & (py >= 0) & (px < w) & (py < h)
    xa, xb = np.clip(x0, 0, w - 1), np.clip(x0 + 1, 0, w - 1)
    ya, yb = np.clip(y0, 0, h - 1), np.clip(y0 + 1, 0, h - 1)
    out = (arr[ya, xa] * (1 - tx) * (1 - ty) + arr[ya, xb] * tx * (1 - ty)
           + arr[yb, xa] * (1 - tx) * ty + arr[yb, xb] * tx * ty).astype(np.float32)
    bad = ~np.isfinite(out)
    if bad.any():
        nx = np.clip(np.floor(px).astype(np.int64), 0, w - 1)
        ny = np.clip(np.floor(py).astype(np.int64), 0, h - 1)
        out[bad] = arr[ny[bad], nx[bad]]
    out[~inside] = np.nan
    return out


def polygon_mask(rings_px: list[np.ndarray], width: int, height: int) -> np.ndarray:
    """Cells (rows, cols) whose centre is inside the rings (pixel coordinates), even-odd rule."""
    mask = np.zeros((height, width), bool)
    edges = []
    for ring in rings_px:
        if len(ring) < 3:
            continue
        px, py = ring[:, 0], ring[:, 1]
        edges.append(np.stack([px, py, np.roll(px, -1), np.roll(py, -1)], axis=1))
    if not edges:
        return mask
    e = np.concatenate(edges)
    x1, y1, x2, y2 = e[:, 0], e[:, 1], e[:, 2], e[:, 3]
    sloped = y1 != y2
    x1, y1, x2, y2 = x1[sloped], y1[sloped], x2[sloped], y2[sloped]
    for j in range(height):
        y = j + 0.5
        cross = (y1 <= y) != (y2 <= y)
        if not cross.any():
            continue
        xs = np.sort(x1[cross] + (y - y1[cross]) * (x2[cross] - x1[cross]) / (y2[cross] - y1[cross]))
        for k in range(0, len(xs) - 1, 2):
            i0 = max(0, int(math.ceil(xs[k] - 0.5)))
            i1 = min(width, int(math.ceil(xs[k + 1] - 0.5)))
            if i1 > i0:
                mask[j, i0:i1] = True
    return mask


def _rings_in(polygons: PolygonSet, grid: GeoGrid, backend: CrsBackend | None) -> list[np.ndarray]:
    """The rings in pixel coordinates of the grid."""
    if not polygons.crs:
        raise VolumeError(f"Choose the CRS of {polygons.name} first.")
    if crs_mod.normalize(polygons.crs) != crs_mod.normalize(grid.crs):
        if backend is None:
            raise VolumeError("A coordinate engine is needed to use polygons in another CRS.")
        polygons = polygons.in_crs(backend, grid.crs)
    out = []
    for ring in polygons.rings:
        px, py = grid.pixel_of(ring[:, 0], ring[:, 1])
        out.append(np.column_stack([px, py]))
    return out


def _clip_window(window, box) -> tuple[int, int, int, int]:
    c0, r0, c1, r1 = window
    bx0, by0, bx1, by1 = box
    return (max(c0, int(math.floor(bx0)) - 1), max(r0, int(math.floor(by0)) - 1),
            min(c1, int(math.ceil(bx1)) + 1), min(r1, int(math.ceil(by1)) + 1))


def _area_weights(grid: GeoGrid, r0: int, r1: int):
    if not grid.geographic:
        return float(grid.cell_area())
    return np.asarray(grid.cell_area(np.arange(r0, r1)))[:, None]


# =============================================================================
# Relief (DEM in the plan view) and the cut / fill picture
# =============================================================================
def hillshade(z: np.ndarray, grid: GeoGrid, azimuth: float = 315.0, altitude: float = 45.0) -> np.ndarray:
    if grid.geographic:
        cell_x = cell_y = grid.cell_size_m()
    else:
        cell_x = math.hypot(grid.gt[1], grid.gt[4]) or 1.0
        cell_y = math.hypot(grid.gt[2], grid.gt[5]) or 1.0
    finite = np.isfinite(z)
    fill = float(np.nanmean(z)) if finite.any() else 0.0
    zz = np.where(finite, z, fill).astype(np.float32)
    if min(zz.shape) < 2:
        return np.ones(zz.shape, np.float32)
    dzdy, dzdx = np.gradient(zz, cell_y, cell_x)
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(-dzdx, dzdy)
    az = math.radians(360.0 - azimuth + 90.0)
    alt = math.radians(altitude)
    shade = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(shade, 0, 1).astype(np.float32)


# Hypsometric tint low -> high: blue, sand, brown, white. No green and no red: those mean fill and cut.
_RAMP = np.array([[0.00, 43, 74, 140], [0.22, 86, 140, 196], [0.45, 190, 220, 228],
                  [0.65, 238, 222, 160], [0.82, 184, 140, 96], [1.00, 246, 242, 236]], np.float32)


def relief_rgba(z: np.ndarray, grid: GeoGrid) -> np.ndarray:
    valid = np.isfinite(z)
    out = np.zeros(z.shape + (4,), np.uint8)
    if not valid.any():
        return out
    lo, hi = np.nanpercentile(z, [2, 98])
    if hi - lo < 1e-6:
        hi = lo + 1.0
    t = np.clip((np.where(valid, z, lo) - lo) / (hi - lo), 0, 1)
    rgb = np.empty(z.shape + (3,), np.float32)
    for c in range(3):
        rgb[..., c] = np.interp(t, _RAMP[:, 0], _RAMP[:, c + 1])
    rgb *= (0.45 + 0.65 * hillshade(z, grid))[..., None]
    out[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    out[..., 3] = np.where(valid, 255, 0)
    return out


def dem_preview(dem: Dem, max_side: int = PREVIEW_MAX) -> Preview:
    """The DEM as a coloured, shaded relief for the plan view."""
    h, w = dem.z.shape
    factor = max(1, int(math.ceil(max(w, h) / float(max_side))))
    z = block_mean(dem.z, factor)
    grid = dem.grid.scaled(factor, z.shape[1], z.shape[0])
    return Preview(relief_rgba(z, grid), grid)


def cut_fill_rgba(dz: np.ndarray, surface: np.ndarray, grid: GeoGrid, threshold: float, scale: float) -> np.ndarray:
    """Grey hillshade of the final surface, red where it was cut and green where it was filled
    (stronger colour for a deeper cut / a higher fill), transparent outside the compared area."""
    valid = np.isfinite(dz)
    cut_m = valid & (dz < -threshold)
    fill_m = valid & (dz > threshold)
    shade = hillshade(np.where(valid, surface, np.nan), grid)
    grey = 75.0 + 165.0 * shade
    change = np.abs(np.where(valid, dz, 0))
    k = (0.55 + 0.45 * np.clip(change / scale, 0, 1)).astype(np.float32)
    lit = (0.62 + 0.48 * shade).astype(np.float32)
    rgba = np.zeros(dz.shape + (4,), np.uint8)
    for c in range(3):
        band = grey.copy()
        for m, rgb in ((cut_m, CUT_RGB), (fill_m, FILL_RGB)):
            band[m] = grey[m] * (1 - k[m]) + np.minimum(255.0, rgb[c] * lit[m]) * k[m]
        rgba[..., c] = np.clip(band, 0, 255).astype(np.uint8)
    rgba[..., 3] = np.where(valid, 255, 0).astype(np.uint8)
    return rgba


# =============================================================================
# Result
# =============================================================================
@dataclass
class VolumeResult:
    mode: str                       # MODE_DEMS / MODE_LEVEL
    before: str                     # name of the reference DEM
    after: str                      # name of the after DEM, or "Elevation 3,401.200 m"
    elevation: float | None
    polygon: str                    # name of the polygons, or "" for the whole common area
    threshold: float
    grid: GeoGrid                   # the compared window of the "before" grid
    dz: np.ndarray                  # float32, NaN outside
    stats: dict
    preview: Preview
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def crs(self) -> str:
        return self.grid.crs

    def dz_at(self, x: float, y: float) -> float | None:
        col, row = self.grid.pixel_of(x, y)
        col, row = int(math.floor(float(col))), int(math.floor(float(row)))
        if 0 <= row < self.dz.shape[0] and 0 <= col < self.dz.shape[1]:
            value = float(self.dz[row, col])
            return value if math.isfinite(value) else None
        return None

    def title(self) -> str:
        return f"Cut / fill · {self.before} → {self.after}"

    def lines(self) -> list[str]:
        s = self.stats
        area = self.polygon or "the whole common area"
        out = [
            f"Cut  {s['cut']:,.3f} m³   on {s['cutArea']:,.2f} m²   (max {s['maxCut']:,.3f} m)",
            f"Fill {s['fill']:,.3f} m³   on {s['fillArea']:,.2f} m²   (max {s['maxFill']:,.3f} m)",
            f"Net (fill − cut) {s['net']:+,.3f} m³   ·   cut + fill {s['total']:,.3f} m³",
            f"Area compared {s['area']:,.2f} m² ({s['area'] / 10000.0:,.4f} ha) in {area}; "
            f"unchanged {s['unchangedArea']:,.2f} m²",
            f"{s['cells']:,} cells of {s['cellSize']:.3f} m · mean change {s['meanChange']:+.4f} m"
            + (f" · changes under {self.threshold:.3f} m ignored" if self.threshold > 0 else ""),
        ]
        return out

    def csv_text(self) -> str:
        s = self.stats
        rows = [
            ("Surface before (reference)", self.before),
            ("Surface after", self.after),
            ("Base elevation (m)", "" if self.elevation is None else f"{self.elevation:.3f}"),
            ("Area", self.polygon or "Whole common area"),
            ("CRS", self.crs),
            ("Threshold (m)", f"{self.threshold:.3f}"),
            ("Cut (m3)", f"{s['cut']:.3f}"), ("Fill (m3)", f"{s['fill']:.3f}"),
            ("Net fill - cut (m3)", f"{s['net']:.3f}"), ("Cut + fill (m3)", f"{s['total']:.3f}"),
            ("Cut area (m2)", f"{s['cutArea']:.3f}"), ("Fill area (m2)", f"{s['fillArea']:.3f}"),
            ("Unchanged area (m2)", f"{s['unchangedArea']:.3f}"), ("Area compared (m2)", f"{s['area']:.3f}"),
            ("Max cut (m)", f"{s['maxCut']:.3f}"), ("Max fill (m)", f"{s['maxFill']:.3f}"),
            ("Mean change (m)", f"{s['meanChange']:.4f}"), ("Cell size (m)", f"{s['cellSize']:.4f}"),
            ("Cells", str(s["cells"])), ("Computed", time.strftime("%Y-%m-%d %H:%M:%S")),
        ]
        return "Item,Value\n" + "".join(f'"{k}","{v}"\n' for k, v in rows)


# =============================================================================
# Computation
# =============================================================================
def _window(before: Dem, after: Dem | None, rings_px, backend: CrsBackend | None) -> tuple[int, int, int, int]:
    h, w = before.z.shape
    window = (0, 0, w, h)
    if after is not None:
        corners = after.grid.corners()
        # densify the edges: the outline of the after DEM may bend in another CRS
        t = np.linspace(0.0, 1.0, 9)[:-1, None]
        edge = np.concatenate([corners[i] + t * (corners[(i + 1) % 4] - corners[i]) for i in range(4)])
        x, y = edge[:, 0], edge[:, 1]
        if crs_mod.normalize(after.grid.crs) != crs_mod.normalize(before.grid.crs):
            x, y = crs_mod.transform(backend, after.grid.crs, before.grid.crs, x, y)
        px, py = before.grid.pixel_of(x, y)
        window = _clip_window(window, (px.min(), py.min(), px.max(), py.max()))
    if rings_px:
        pts = np.concatenate(rings_px)
        window = _clip_window(window, (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()))
    return window


def compare(before: Dem, after: Dem | None = None, elevation: float | None = None,
            polygons: PolygonSet | None = None, threshold: float = 0.0, backend: CrsBackend | None = None,
            progress: Progress | None = None) -> VolumeResult:
    """Cut and fill between the DEM ``before`` and the DEM ``after``, or the level ``elevation``."""
    report = progress or (lambda _f, _t: None)
    started = time.time()
    if (after is None) == (elevation is None):
        raise VolumeError("Compare the DEM with another DEM or with an elevation.")
    if after is not None and backend is None and \
            crs_mod.normalize(after.grid.crs) != crs_mod.normalize(before.grid.crs):
        raise VolumeError("The two DEMs are in different CRS: a coordinate engine is needed.")
    if not before.grid.crs:
        raise VolumeError(f"Choose the CRS of {before.name} first.")
    report(0.02, "Preparing the comparison")
    full = before.grid
    rings_px = _rings_in(polygons, full, backend) if polygons is not None else None
    c0, r0, c1, r1 = _window(before, after, rings_px, backend)
    if c1 <= c0 or r1 <= r0:
        raise VolumeError("The surfaces (and the polygon) do not overlap.")
    win_w, win_h = c1 - c0, r1 - r0
    mask = None
    if rings_px is not None:
        mask = polygon_mask([ring - np.array([c0, r0], float) for ring in rings_px], win_w, win_h)
        if not mask.any():
            raise VolumeError("The polygon does not cover any cell of the DEM.")
    win = full.window(c0, r0, c1, r1)
    A = before.z
    dz = np.full((win_h, win_w), np.nan, np.float32)
    cols = np.arange(c0, c1, dtype=np.float64) + 0.5
    chunk = max(1, 2_000_000 // max(1, win_w))
    same = after is not None and crs_mod.normalize(after.grid.crs) == crs_mod.normalize(full.crs)
    for r in range(r0, r1, chunk):
        re_ = min(r1, r + chunk)
        za = np.asarray(A[r:re_, c0:c1], np.float32)
        if after is None:
            zb = np.full(za.shape, np.float32(elevation), np.float32)
        else:
            cc, rr = np.meshgrid(cols, np.arange(r, re_, dtype=np.float64) + 0.5)
            mx, my = full.map_of(cc, rr)
            if not same:
                mx, my = crs_mod.transform(backend, full.crs, after.grid.crs, mx, my)
            bx, by = after.grid.pixel_of(mx, my)
            zb = sample(after.z, bx, by)
        part = zb - za
        if mask is not None:
            part[~mask[r - r0:re_ - r0]] = np.nan
        dz[r - r0:re_ - r0] = part
        report(0.05 + 0.6 * (re_ - r0) / max(1, win_h), "Comparing the surfaces")
    valid = np.isfinite(dz)
    if not valid.any():
        raise VolumeError("No cell has an elevation in both surfaces inside the area compared.")
    weights = _area_weights(win, 0, win_h)
    thr = max(0.0, float(threshold or 0.0))
    cut_m = valid & (dz < -thr)
    fill_m = valid & (dz > thr)

    def area_of(m):
        return float(np.where(m, weights, 0.0).sum()) if win.geographic else float(m.sum() * weights)

    def volume_of(m):
        return float(np.where(m, dz.astype(np.float64) * weights, 0.0).sum())

    cut = -volume_of(cut_m) + 0.0
    fill = volume_of(fill_m)
    stats = {
        "cut": cut, "fill": fill, "net": fill - cut, "total": fill + cut,
        "cutArea": area_of(cut_m), "fillArea": area_of(fill_m), "area": area_of(valid),
        "unchangedArea": area_of(valid & ~cut_m & ~fill_m),
        "maxCut": float(-dz[valid].min()) if (dz[valid] < 0).any() else 0.0,
        "maxFill": float(dz[valid].max()) if (dz[valid] > 0).any() else 0.0,
        "meanChange": float(np.nanmean(dz)), "threshold": thr, "cellSize": win.cell_size_m(),
        "cells": int(valid.sum()),
    }
    report(0.7, "Painting cut and fill")
    surface = np.asarray(A[r0:r1, c0:c1], np.float32)
    if after is not None:
        surface = np.where(valid, surface + dz, surface)
    factor = max(1, int(math.ceil(max(win_w, win_h) / float(PREVIEW_MAX))))
    dz_s, surf_s = block_mean(dz, factor), block_mean(surface, factor)
    grid_s = win.scaled(factor, dz_s.shape[1], dz_s.shape[0])
    changed = np.abs(dz[cut_m | fill_m])
    scale = max(float(np.percentile(changed, 95)) if changed.size else 1.0, thr * 2.0, 0.05)
    rgba = cut_fill_rgba(dz_s, surf_s, grid_s, thr, scale)
    polygon_name = polygons.name if polygons is not None else ""
    after_name = after.name if after is not None else f"Elevation {float(elevation):,.3f} m"
    notes = []
    if before.factor > 1:
        notes.append(f"{before.name} was averaged in blocks of {before.factor}×{before.factor} cells.")
    report(1.0, "Volumes computed")
    return VolumeResult(MODE_DEMS if after is not None else MODE_LEVEL, before.name, after_name,
                        None if elevation is None else float(elevation), polygon_name, thr, win, dz, stats,
                        Preview(rgba, grid_s), time.time() - started, notes)


def mean_elevation(dem: Dem, polygons: PolygonSet | None, backend: CrsBackend | None = None) -> tuple[float, int]:
    """(mean elevation, cells) of the DEM inside the polygons (the whole DEM without polygons)."""
    if polygons is None:
        valid = np.isfinite(dem.z)
        if not valid.any():
            raise VolumeError(f"{dem.name} has no elevations.")
        return float(np.nanmean(dem.z)), int(valid.sum())
    rings = _rings_in(polygons, dem.grid, backend)
    c0, r0, c1, r1 = _window(dem, None, rings, backend)
    if c1 <= c0 or r1 <= r0:
        raise VolumeError("The polygon is outside the DEM.")
    mask = polygon_mask([ring - np.array([c0, r0], float) for ring in rings], c1 - c0, r1 - r0)
    values = dem.z[r0:r1, c0:c1][mask]
    values = values[np.isfinite(values)]
    if not values.size:
        raise VolumeError("The polygon does not cover any cell of the DEM with an elevation.")
    return float(values.mean(dtype=np.float64)), int(values.size)
