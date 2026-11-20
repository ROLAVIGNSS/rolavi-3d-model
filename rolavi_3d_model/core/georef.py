"""Where the OBJ is: model frame + optional GCP adjustment (Qt-free, QGIS-free).

World coordinates (project CRS, metres) of a vertex of the OBJ::

    world = adjustment( frame(obj_xyz) )

* ``ModelFrame``  - up axis of the file (Z up: Agisoft, Pix4D, ODM; Y up: Blender,
                    ARKit / ROLAVIScan) and an offset added to every vertex (Agisoft
                    "shift", GEOStudio ``visual_origin_world``).
* ``Similarity``  - the GCP adjustment: uniform scale, rotation, translation.
* ``solve_gcps``  - least squares of the adjustment from GCPs (OBJ coordinates of the
                    point in the file + its known coordinates in the project CRS).

Adjustment modes (as GEOStudio's "Z-up locked" solve, plus two more):

* ``vertical`` - yaw + scale + shift, the vertical stays vertical (no pitch / roll).
                 2 GCPs at least; the safe choice for survey models.
* ``3d``       - full rotation + scale + shift (a model in an arbitrary orientation),
                 3 non-collinear GCPs at least.
* ``shift``    - translation only (a georeferenced model that is a bit off), 1 GCP at least.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

UP_Z = "Z"
UP_Y = "Y"

MODE_VERTICAL = "vertical"
MODE_3D = "3d"
MODE_SHIFT = "shift"

MODE_LABELS = {
    MODE_VERTICAL: "Keep vertical: yaw + scale + shift",
    MODE_3D: "Full 3D: rotation + scale + shift",
    MODE_SHIFT: "Shift only",
}

MIN_GCPS = {MODE_VERTICAL: 2, MODE_3D: 3, MODE_SHIFT: 1}


class GeorefError(ValueError):
    pass


@dataclass
class ModelFrame:
    up_axis: str = UP_Z
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def apply(self, obj_xyz: np.ndarray) -> np.ndarray:
        """OBJ coordinates (file) -> Z-up coordinates + offset (float64 copy)."""
        pts = np.asarray(obj_xyz, dtype=np.float64).reshape(-1, 3)
        if self.up_axis == UP_Y:
            out = np.column_stack([pts[:, 0], -pts[:, 2], pts[:, 1]])
        else:
            out = np.array(pts, dtype=np.float64, copy=True)
        out += np.asarray(self.offset, dtype=np.float64).reshape(1, 3)
        return out

    def to_dict(self) -> dict:
        return {"up_axis": self.up_axis, "offset": [float(v) for v in self.offset]}

    def describe(self) -> str:
        return f"up axis {self.up_axis}, offset {self.offset[0]:.3f}, {self.offset[1]:.3f}, {self.offset[2]:.3f}"

    @classmethod
    def from_dict(cls, data: dict) -> "ModelFrame":
        offset = data.get("offset") or (0.0, 0.0, 0.0)
        return cls(up_axis=str(data.get("up_axis") or UP_Z), offset=tuple(float(v) for v in offset))


class CrsFrame:
    """A model whose file coordinates are in another CRS (e.g. longitude / latitude in degrees):
    the up axis and offset of ``base``, then exactly, point by point, to the CRS the window
    works in (metres). ``backend`` is a ``crs.CrsBackend``."""

    def __init__(self, base: ModelFrame, source: str, target: str, backend):
        self.base = base
        self.source = source
        self.target = target
        self.backend = backend

    @property
    def up_axis(self) -> str:
        return self.base.up_axis

    @property
    def offset(self) -> tuple[float, float, float]:
        return self.base.offset

    def apply(self, xyz: np.ndarray) -> np.ndarray:
        pts = self.base.apply(xyz)
        lon, lat = self.backend.to_lonlat(self.source, pts[:, 0], pts[:, 1])
        x, y = self.backend.from_lonlat(self.target, lon, lat)
        return np.column_stack([x, y, pts[:, 2]])

    def to_dict(self) -> dict:
        return {**self.base.to_dict(), "source": self.source, "target": self.target}

    def describe(self) -> str:
        return f"{self.base.describe()}; file in {self.source}, worked in {self.target}"


@dataclass
class Similarity:
    scale: float = 1.0
    rotation: np.ndarray = field(default_factory=lambda: np.eye(3))
    translation: np.ndarray = field(default_factory=lambda: np.zeros(3))

    def apply(self, pts: np.ndarray) -> np.ndarray:
        pts = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
        return self.scale * (pts @ np.asarray(self.rotation).T) + np.asarray(self.translation).reshape(1, 3)

    def inverse(self, pts: np.ndarray) -> np.ndarray:
        pts = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
        return ((pts - np.asarray(self.translation).reshape(1, 3)) @ np.asarray(self.rotation)) / self.scale

    @property
    def yaw_deg(self) -> float:
        r = np.asarray(self.rotation)
        return math.degrees(math.atan2(r[1, 0], r[0, 0]))

    @property
    def tilt_deg(self) -> float:
        """Angle between the model's vertical and the world vertical."""
        r = np.asarray(self.rotation)
        return math.degrees(math.acos(max(-1.0, min(1.0, float(r[2, 2])))))

    def to_dict(self) -> dict:
        return {
            "scale": float(self.scale),
            "rotation": np.asarray(self.rotation, dtype=float).tolist(),
            "translation": np.asarray(self.translation, dtype=float).tolist(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Similarity":
        return cls(
            scale=float(data.get("scale", 1.0)),
            rotation=np.asarray(data.get("rotation", np.eye(3)), dtype=float),
            translation=np.asarray(data.get("translation", np.zeros(3)), dtype=float),
        )


@dataclass
class GcpSolution:
    mode: str
    transform: Similarity
    names: list[str]
    residuals: np.ndarray        # (n, 3) known - adjusted (dE, dN, dH)
    rmse_3d: float
    rmse_h: float                # horizontal
    rmse_v: float                # vertical
    report_lines: list[str]


def _check_points(model_xyz, world_xyz) -> tuple[np.ndarray, np.ndarray]:
    src = np.asarray(model_xyz, dtype=np.float64).reshape(-1, 3)
    dst = np.asarray(world_xyz, dtype=np.float64).reshape(-1, 3)
    if src.shape != dst.shape:
        raise GeorefError("Every GCP needs both its OBJ coordinates and its known coordinates.")
    if not (np.isfinite(src).all() and np.isfinite(dst).all()):
        raise GeorefError("A GCP has coordinates that are not numbers.")
    return src, dst


def _solve_vertical(src: np.ndarray, dst: np.ndarray, lock_scale: bool) -> Similarity:
    """2D similarity in XY (complex least squares) + the same scale on Z, Z axis kept vertical."""
    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    zs = (src[:, 0] - src_mean[0]) + 1j * (src[:, 1] - src_mean[1])
    zd = (dst[:, 0] - dst_mean[0]) + 1j * (dst[:, 1] - dst_mean[1])
    denom = float(np.sum(np.abs(zs) ** 2))
    if denom <= 1e-12:
        raise GeorefError("The GCPs are too close together in the model: use well separated points.")
    a = complex(np.sum(zd * np.conj(zs)) / denom)
    if abs(a) <= 1e-15:
        raise GeorefError("The GCP geometry gives no valid scale: check the points.")
    scale = 1.0 if lock_scale else abs(a)
    yaw = math.atan2(a.imag, a.real)
    c, s = math.cos(yaw), math.sin(yaw)
    rotation = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    translation = dst_mean - scale * (rotation @ src_mean)
    return Similarity(scale=scale, rotation=rotation, translation=translation)


def _solve_3d(src: np.ndarray, dst: np.ndarray, lock_scale: bool) -> Similarity:
    """Umeyama: rotation (+ scale) + translation, least squares."""
    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    a = src - src_mean
    b = dst - dst_mean
    if np.linalg.matrix_rank(a, tol=1e-6 * max(1.0, float(np.abs(a).max()))) < 2:
        raise GeorefError("The GCPs are collinear in the model: Full 3D needs 3 points not on one line.")
    h = a.T @ b
    u, singular, vt = np.linalg.svd(h)
    d = np.eye(3)
    if np.linalg.det(vt.T @ u.T) < 0:
        d[2, 2] = -1.0
    rotation = vt.T @ d @ u.T
    denom = float(np.sum(a ** 2))
    scale = 1.0 if lock_scale else float(np.sum(singular * np.diag(d)) / denom)
    if not np.isfinite(scale) or scale <= 1e-12:
        raise GeorefError("The GCP geometry gives no valid scale: check the points.")
    translation = dst_mean - scale * (rotation @ src_mean)
    return Similarity(scale=scale, rotation=rotation, translation=translation)


def residuals(transform: Similarity, model_xyz, world_xyz) -> np.ndarray:
    src, dst = _check_points(model_xyz, world_xyz)
    return dst - transform.apply(src)


def solve_gcps(names: list[str], model_xyz, world_xyz, mode: str = MODE_VERTICAL,
               lock_scale: bool = False) -> GcpSolution:
    """Adjustment frame(OBJ) -> project CRS from the GCPs (model coordinates already through the frame)."""
    src, dst = _check_points(model_xyz, world_xyz)
    needed = MIN_GCPS.get(mode)
    if needed is None:
        raise GeorefError(f"Unknown adjustment mode: {mode}")
    if len(src) < needed:
        raise GeorefError(f"{MODE_LABELS[mode]} needs at least {needed} GCP(s) with both coordinates; there are {len(src)}.")
    if mode == MODE_SHIFT:
        transform = Similarity(translation=(dst - src).mean(axis=0))
    elif mode == MODE_VERTICAL:
        transform = _solve_vertical(src, dst, lock_scale)
    else:
        transform = _solve_3d(src, dst, lock_scale)
    res = dst - transform.apply(src)
    rmse_3d = float(np.sqrt(np.mean(np.sum(res ** 2, axis=1))))
    rmse_h = float(np.sqrt(np.mean(np.sum(res[:, :2] ** 2, axis=1))))
    rmse_v = float(np.sqrt(np.mean(res[:, 2] ** 2)))
    redundant = len(src) > needed
    report = [
        f"Adjustment   : {MODE_LABELS[mode]}" + (" (scale locked to 1.0)" if lock_scale and mode != MODE_SHIFT else ""),
        f"GCPs used    : {len(src)}",
        f"Scale        : {transform.scale:.9f} ({(transform.scale - 1.0) * 1e6:+.1f} ppm)",
        f"Yaw          : {transform.yaw_deg:.6f} deg",
    ]
    if mode == MODE_3D:
        report.append(f"Tilt         : {transform.tilt_deg:.6f} deg")
    report += [
        f"Shift        : X {transform.translation[0]:.3f}  Y {transform.translation[1]:.3f}  Z {transform.translation[2]:.3f}",
        f"RMSE         : horizontal {rmse_h:.4f} m, vertical {rmse_v:.4f} m, 3D {rmse_3d:.4f} m",
    ]
    if not redundant:
        report.append("Note         : only the minimum number of GCPs, so the residuals do not check the adjustment (add one more).")
    return GcpSolution(
        mode=mode,
        transform=transform,
        names=list(names),
        residuals=res,
        rmse_3d=rmse_3d,
        rmse_h=rmse_h,
        rmse_v=rmse_v,
        report_lines=report,
    )
