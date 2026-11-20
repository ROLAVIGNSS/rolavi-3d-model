"""Model (OBJ, PLY mesh, point cloud, 3DGS) -> georeferenced preview -> SLPK in WGS84 (Qt-free, QGIS-free).

* ``read_hints``  - CRS / offset / heights found beside the OBJ (GEOStudio
                    ``<model>.obj.georef.json``, ``.prj``).
* ``load_any``    - any model: OBJ (``load_model``), PLY mesh (through the OBJ ROLAVIScan writes
                    beside it, or converted once), point cloud or 3D Gaussian splats (``scan_io``),
                    with the ROLAVIScan georeference when there is one.
* ``load_model``  - two streaming passes over the OBJ (bounded memory, multi-GB files):
                    counts, materials, and coloured preview points (face centres,
                    colour from the texture) in OBJ coordinates.
* ``build_display_mesh`` - the whole OBJ simplified for the 3D view (vertex clustering, as the
                    SLPK levels of detail), coloured from the vertex colours or the textures.
* ``build_display_cloud`` - the points / splat centres for the 3D view.
* ``export_slpk`` - the whole textured OBJ through the frame + adjustment to the
                    project CRS, then to longitude/latitude, written by GEOStudio's
                    ``write_mesh_pyramid_slpk``: IntegratedMesh I3S 1.7, EPSG:4326,
                    level-of-detail pyramid, the format of ROLAVI Viewer.
* ``export_point_cloud_slpk`` - a point cloud (or the centres of the splats, with their colour)
                    as a PointCloud SLPK (I3S 2.0, LEPCC), as GEOStudio / ArcGIS Pro write it.

``progress(fraction, text)`` may raise ``InterruptedError`` (Cancel).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import obj_io, scan_io, slpk_io
from .crs import CrsBackend, CrsError, normalize
from .geoid import GeoidError, GtxGeoid
from .georef import ModelFrame, Similarity
from .scan_io import KIND_MESH, KIND_SPLATS, ScanInfo

Progress = Callable[[float, str], None]
Log = Callable[[str], None]

HEIGHT_ELLIPSOIDAL = slpk_io.HEIGHT_ELLIPSOIDAL
HEIGHT_ORTHOMETRIC = slpk_io.HEIGHT_ORTHOMETRIC
HEIGHT_LABELS = {
    HEIGHT_ELLIPSOIDAL: "Ellipsoidal (GNSS)",
    HEIGHT_ORTHOMETRIC: "Orthometric (geoid)",
}

_BLOCK = 1_000_000


def _sub(progress: Progress | None, start: float, end: float) -> Progress:
    def report(fraction: float, text: str) -> None:
        if progress is not None:
            progress(start + (end - start) * max(0.0, min(1.0, float(fraction))), text)
    return report


def _quiet(_text: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Hints beside the OBJ
# ---------------------------------------------------------------------------

@dataclass
class ModelHints:
    crs: str = ""
    offset: tuple[float, float, float] | None = None
    height_model: str = ""
    sources: list[str] = field(default_factory=list)


def _sidecar_crs(data: dict) -> str:
    candidates = [data.get("export_crs"), data.get("solve_target_crs"), data.get("target_crs")]
    for key in ("crs", "solve"):
        obj = data.get(key)
        if isinstance(obj, dict):
            candidates += [obj.get("export_crs"), obj.get("target_crs"), obj.get("epsg")]
        elif isinstance(obj, str):
            candidates.append(obj)
    for value in candidates:
        if value:
            return normalize(str(value))
    return ""


def read_hints(obj_path: str | Path, backend: CrsBackend | None = None) -> ModelHints:
    obj_path = Path(obj_path)
    hints = ModelHints()
    for sidecar in (Path(str(obj_path) + ".georef.json"), obj_path.with_suffix(".georef.json"),
                    obj_path.parent / "georef.json"):
        if not sidecar.is_file():
            continue
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ValueError):
            continue  # unreadable or not JSON: not a georef sidecar
        if not isinstance(data, dict):
            continue
        hints.sources.append(sidecar.name)
        hints.crs = hints.crs or _sidecar_crs(data)
        origin = data.get("visual_origin_world")
        if isinstance(origin, (list, tuple)) and len(origin) == 3:
            try:
                hints.offset = tuple(float(v) for v in origin)
            except (TypeError, ValueError):
                pass
        height = str(data.get("height_reference") or data.get("height_mode") or "").lower()
        if height.startswith("ortho"):
            hints.height_model = HEIGHT_ORTHOMETRIC
        elif height.startswith("ellip"):
            hints.height_model = HEIGHT_ELLIPSOIDAL
        break
    if not hints.crs and backend is not None:
        for prj in (obj_path.with_suffix(".prj"), Path(str(obj_path) + ".prj")):
            if prj.is_file():
                try:
                    hints.crs = backend.from_wkt(prj.read_text(encoding="utf-8", errors="ignore").strip())
                    hints.sources.append(prj.name)
                except CrsError:
                    pass
                break
    return hints


# ---------------------------------------------------------------------------
# Loading (preview)
# ---------------------------------------------------------------------------

@dataclass
class CloudData:
    """Every point of a point cloud / every Gaussian of a splat file, in file coordinates."""
    xyz: np.ndarray                         # (n, 3) float64
    colors: np.ndarray | None               # (n, 3) uint8
    opacity: np.ndarray | None = None       # splats: (n,) 0..1
    scale: np.ndarray | None = None         # splats: (n, 3) metres
    rotation: np.ndarray | None = None      # splats: (n, 4) quaternion w, x, y, z
    sh_degree: int = 0


SPLAT_MIN_OPACITY = 0.1   # fainter Gaussians are haze: left out of the SLPK and of the views


@dataclass
class LoadedModel:
    path: Path                      # the file the job reads (an OBJ for meshes)
    size_mb: float
    vertices: int
    faces: int
    uvs: int
    materials: list[str]
    textures: dict[str, Path]
    mtl_counts: dict
    bbox_min: np.ndarray            # file coordinates
    bbox_max: np.ndarray
    points_obj: np.ndarray          # (n, 3) float64 preview points, file coordinates
    colors: np.ndarray              # (n, 3) uint8
    hints: ModelHints
    vertex_colors: bool = False     # "v x y z r g b": colour per vertex (no texture image needed)
    kind: str = KIND_MESH           # mesh | points | splats
    source: Path | None = None      # the file the user opened (a PLY mesh is read through an OBJ)
    scan: ScanInfo | None = None    # ROLAVIScan / PLY details (frame, georeference)
    cloud: CloudData | None = None  # points / splats

    @property
    def opened(self) -> Path:
        return self.source or self.path

    def summary_lines(self) -> list[str]:
        if self.kind != KIND_MESH:
            return self._cloud_lines()
        lines = [
            f"OBJ        : {self.path.name} ({self.size_mb:,.1f} MB)",
            f"Mesh       : {self.vertices:,} vertices, {self.faces:,} faces, {self.uvs:,} UVs, "
            f"{len(self.materials)} material(s)",
            f"Textures   : {len(self.textures)} found through the MTL (map_Kd: {self.mtl_counts.get('map_kd', 0)})",
            "OBJ extent : X {0:.3f} .. {1:.3f} | Y {2:.3f} .. {3:.3f} | Z {4:.3f} .. {5:.3f}".format(
                self.bbox_min[0], self.bbox_max[0], self.bbox_min[1], self.bbox_max[1], self.bbox_min[2], self.bbox_max[2]
            ),
            f"Preview    : {len(self.points_obj):,} coloured points",
        ]
        if self.hints.sources:
            lines.append(f"Found      : {', '.join(self.hints.sources)}"
                         + (f" -> CRS {self.hints.crs}" if self.hints.crs else "")
                         + (" + offset" if self.hints.offset else ""))
        if self.source is not None and self.source != self.path:
            lines.insert(0, f"Opened     : {self.source.name} (its mesh is read through {self.path.name})")
        return lines

    def _cloud_lines(self) -> list[str]:
        label = scan_io.KIND_LABELS.get(self.kind, self.kind)
        lines = [
            f"File       : {self.opened.name} ({self.size_mb:,.1f} MB) - {label}",
            "Extent     : X {0:.3f} .. {1:.3f} | Y {2:.3f} .. {3:.3f} | Z {4:.3f} .. {5:.3f}".format(
                self.bbox_min[0], self.bbox_max[0], self.bbox_min[1], self.bbox_max[1], self.bbox_min[2], self.bbox_max[2]
            ),
        ]
        cloud = self.cloud
        if self.kind == KIND_SPLATS and cloud is not None and cloud.opacity is not None:
            kept = int((cloud.opacity >= SPLAT_MIN_OPACITY).sum())
            lines.insert(1, f"Gaussians  : {self.vertices:,} ({kept:,} with opacity >= {SPLAT_MIN_OPACITY:g}); "
                            f"median size {float(np.median(cloud.scale)) * 1000:.1f} mm; "
                            + ("base colour only (SH degree 0)" if cloud.sh_degree == 0
                               else f"view-dependent colour (SH degree {cloud.sh_degree}): the base colour is used"))
        else:
            lines.insert(1, f"Points     : {self.vertices:,}, "
                            + ("RGB colour" if cloud is not None and cloud.colors is not None else "no colour (grey)"))
        lines.append(f"Preview    : {len(self.points_obj):,} points")
        return lines


def _bbox(xyz: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return xyz.min(axis=0).astype(np.float64), xyz.max(axis=0).astype(np.float64)


def _preview(cloud: CloudData, count: int, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Up to ``count`` points of the cloud (the visible Gaussians of a splat file)."""
    index = np.arange(len(cloud.xyz))
    if cloud.opacity is not None:
        index = index[cloud.opacity >= SPLAT_MIN_OPACITY]
    if len(index) > count:
        index = np.sort(np.random.default_rng(seed).choice(index, size=count, replace=False))
    colors = cloud.colors[index] if cloud.colors is not None else np.full((len(index), 3), 190, dtype=np.uint8)
    return cloud.xyz[index].astype(np.float64), colors.astype(np.uint8)


def load_any(path: str | Path, backend: CrsBackend | None = None, preview_points: int = 400_000,
             progress: Progress | None = None, log: Log | None = None) -> LoadedModel:
    """An OBJ, or a PLY (mesh, point cloud or 3D Gaussian splats), with its ROLAVIScan georeference."""
    log = log or _quiet
    path = Path(path)
    if not path.is_file():
        raise obj_io.ObjError(f"The file was not found: {path}")
    if path.suffix.lower() != ".ply":
        model = load_model(path, backend, preview_points, progress)
        model.scan = scan_io.inspect_obj(path)
        return model

    info = scan_io.inspect_ply(path)
    size_mb = path.stat().st_size / (1024.0 * 1024.0)
    for note in info.notes:
        log(f"⚠️ {note}")
    if info.kind == KIND_MESH:
        if info.companion_obj is not None:
            log(f"   Mesh       : read through {info.companion_obj.name} (the same mesh, written by ROLAVIScan)")
            model = load_model(info.companion_obj, backend, preview_points, progress)
            info.frame = info.companion_frame or scan_io.FRAME_ENU
        else:
            log(f"   Mesh       : converting {path.name} to OBJ once (the mesh pipeline reads OBJ)...")
            obj_path = scan_io.ply_mesh_to_obj(info, None, _sub(progress, 0.0, 0.45))
            model = load_model(obj_path, backend, preview_points, _sub(progress, 0.45, 1.0))
        model.source = path
        model.scan = info
        return model

    if info.kind == KIND_SPLATS:
        splats = scan_io.read_splats(info, _sub(progress, 0.0, 0.8))
        cloud = CloudData(xyz=splats.xyz, colors=splats.colors, opacity=splats.opacity, scale=splats.scale,
                          rotation=splats.rotation, sh_degree=splats.sh_degree)
    else:
        xyz, colors = scan_io.read_points(info, _sub(progress, 0.0, 0.8))
        cloud = CloudData(xyz=xyz, colors=colors)
    if not len(cloud.xyz):
        raise obj_io.ObjError(f"{path.name} has no points with valid coordinates.")
    _sub(progress, 0.8, 1.0)(0.5, "Preview points...")
    points, colors = _preview(cloud, preview_points)
    low, high = _bbox(cloud.xyz)
    if progress is not None:
        progress(1.0, "Model read")
    return LoadedModel(
        path=path, size_mb=size_mb, vertices=len(cloud.xyz), faces=0, uvs=0, materials=[], textures={},
        mtl_counts={}, bbox_min=low, bbox_max=high, points_obj=points, colors=colors, hints=ModelHints(),
        vertex_colors=cloud.colors is not None, kind=info.kind, source=path, scan=info, cloud=cloud,
    )


def load_model(path: str | Path, backend: CrsBackend | None = None, preview_points: int = 400_000,
               progress: Progress | None = None) -> LoadedModel:
    path = Path(path)
    if not path.is_file():
        raise obj_io.ObjError(f"The OBJ was not found: {path}")
    size_mb = path.stat().st_size / (1024.0 * 1024.0)
    scan = obj_io.scan_obj(path, preview_points, _sub(progress, 0.0, 0.5))
    if not scan.vertices:
        raise obj_io.ObjError(f"{path.name} has no vertices.")
    if not scan.faces:
        raise obj_io.ObjError(f"{path.name} has no faces: it is not a mesh.")
    origin = np.round(scan.mean, 3)
    gathered = obj_io.write_centered_obj(path, None, origin, scan, _sub(progress, 0.5, 0.92))
    textures, counts = obj_io.mtl_textures(path, scan.mtllib)
    centers, colors = obj_io.preview_points(scan, gathered, textures, 2048, _sub(progress, 0.92, 1.0))
    if not len(centers):
        raise obj_io.ObjError(f"{path.name}: no valid face was found for the preview.")
    return LoadedModel(
        path=path,
        size_mb=size_mb,
        vertices=scan.vertices,
        faces=scan.faces,
        uvs=scan.uvs,
        materials=list(scan.materials),
        textures=textures,
        mtl_counts=counts,
        bbox_min=np.asarray(scan.bbox_min, dtype=np.float64),
        bbox_max=np.asarray(scan.bbox_max, dtype=np.float64),
        points_obj=centers.astype(np.float64) + origin,
        colors=colors,
        hints=read_hints(path, backend),
        vertex_colors=scan.vertex_columns >= 6,
    )


DISPLAY_FACES = 1_500_000            # untextured (or too big) models: simplified to about this many triangles
DISPLAY_TEXTURED_FACES = 4_000_000   # textured models up to this size are shown whole, with their real texture
DISPLAY_TEXTURE_PIXELS = 120_000_000  # texture pixels kept for the 3D view (all textures together)
SNAP_RADIUS = 0.25      # OBJ units: a picked point snaps to the nearest real vertex within this distance


@dataclass
class DisplayPart:
    """One actor of the 3D view: the faces ``face_start .. face_end`` of its DisplayMesh."""
    face_start: int
    face_end: int
    point_vertex: np.ndarray            # (P,) the DisplayMesh vertex of each point of the part
    faces: np.ndarray                   # (face_end - face_start, 3) indices into the part's points
    uv: np.ndarray | None = None        # (P, 2) float32 texture coordinates (OBJ convention, v up)
    image: np.ndarray | None = None     # (H, W, 3) uint8 texture (row 0 = top of the image)


@dataclass
class DisplayMesh:
    """The model for the 3D view, in OBJ coordinates (frame and adjustment not applied).

    A textured model up to ``DISPLAY_TEXTURED_FACES`` triangles is kept whole, one part per
    texture with the uv of every corner (the real texture is drawn). Bigger or untextured
    models are simplified (vertex clustering) and drawn with a colour per vertex.
    """
    path: Path
    vertices_obj: np.ndarray        # (V, 3) float64
    faces: np.ndarray               # (F, 3) int64
    colors: np.ndarray              # (V, 3) uint8
    source_faces: int
    colour_source: str              # "texture", "vertex colours" or "none"
    full_vertices: np.ndarray | None = None   # every OBJ vertex minus full_origin, float32: exact picking
    full_origin: np.ndarray | None = None
    parts: list[DisplayPart] = field(default_factory=list)

    @property
    def textured(self) -> bool:
        return any(part.image is not None for part in self.parts)

    def point_on_face(self, face: int, r: float, s: float) -> np.ndarray:
        """OBJ coordinates of the point (r, s) of a display triangle (parametric, as VTK gives them)."""
        a, b, c = self.vertices_obj[self.faces[int(face)]]
        return a * (1.0 - r - s) + b * r + c * s

    def snap(self, point_obj, radius: float = SNAP_RADIUS) -> np.ndarray:
        """The vertex of the full OBJ nearest to ``point_obj`` (within ``radius``), else the point itself."""
        point = np.asarray(point_obj, dtype=np.float64)
        if self.full_vertices is None or self.full_origin is None:
            return point
        rel = (point - self.full_origin).astype(np.float32)
        v = self.full_vertices
        near = np.flatnonzero((np.abs(v[:, 0] - rel[0]) <= radius) & (np.abs(v[:, 1] - rel[1]) <= radius)
                              & (np.abs(v[:, 2] - rel[2]) <= radius))
        if not len(near):
            return point
        best = near[int(np.argmin(((v[near] - rel) ** 2).sum(axis=1)))]
        return v[best].astype(np.float64) + self.full_origin


def build_display_mesh(path: str | Path, target_faces: int = DISPLAY_FACES,
                       progress: Progress | None = None, log: Log | None = None) -> DisplayMesh:
    """The whole OBJ, simplified to about ``target_faces`` triangles for the 3D view."""
    log = log or _quiet
    path = Path(path)
    mesh = obj_io.read_obj_mesh(path, _sub(progress, 0.0, 0.6))
    count = len(mesh.vertices)
    source_faces = len(mesh.faces)
    _sub(progress, 0.6, 0.75)(0.0, "Textures and colours of the 3D view...")
    images: dict[int, object] = {}
    faces_material = np.asarray(mesh.faces_material)
    if mesh.faces_uv is not None:
        textures, _counts = obj_io.mtl_textures(path, mesh.mtllib)
        names = list(mesh.materials)
        if (faces_material < 0).all() and len(textures) == 1:
            names = list(textures)  # faces without usemtl and one texture in the MTL
            faces_material = np.zeros_like(faces_material)
        used = [index for index, name in enumerate(names) if name in textures]
        side = int(min(8192, max(1024, math.sqrt(DISPLAY_TEXTURE_PIXELS / max(1, len(used))))))
        for index in used:
            try:
                images[index] = load_texture_image(textures[names[index]], side)
            except Exception as exc:
                log(f"⚠️ Texture {textures[names[index]].name} could not be read for the 3D view: {exc}")
    if images:
        colors = slpk_io.vertex_colors_from_textures(mesh.faces, mesh.faces_uv, mesh.uvs, faces_material,
                                                     count, images).astype(np.float32)
        source = "texture"
    elif mesh.vertex_colors is not None:
        colors = np.asarray(mesh.vertex_colors, dtype=np.float32)
        if float(colors.max(initial=0.0)) <= 1.0:
            colors = colors * 255.0
        source = "vertex colours"
    else:
        colors = np.full((count, 3), 190.0, dtype=np.float32)
        source = "none"
    origin = mesh.vertices.mean(axis=0)
    full = (mesh.vertices - origin).astype(np.float32)   # kept to snap the GCPs picked in 3D to the real vertices

    if images and mesh.faces_uv is not None and source_faces <= DISPLAY_TEXTURED_FACES:
        # the whole model with its real texture: one part per texture, a point per (vertex, uv) pair
        _sub(progress, 0.75, 0.98)(0.0, f"Texture coordinates of {source_faces:,} triangles...")
        parts: list[DisplayPart] = []
        order: list[np.ndarray] = []
        start = 0
        done = np.zeros(source_faces, dtype=bool)
        uv_count = max(1, len(mesh.uvs))
        for material, image in images.items():
            rows = np.flatnonzero((faces_material == material) & (mesh.faces_uv >= 0).all(axis=1))
            if not len(rows):
                continue
            key = mesh.faces[rows].reshape(-1).astype(np.int64) * uv_count + mesh.faces_uv[rows].reshape(-1)
            unique, inverse = np.unique(key, return_inverse=True)
            parts.append(DisplayPart(
                face_start=start, face_end=start + len(rows), point_vertex=unique // uv_count,
                faces=np.asarray(inverse).reshape(-1, 3).astype(np.int64),
                uv=np.asarray(mesh.uvs[unique % uv_count], dtype=np.float32)[:, :2],
                image=np.ascontiguousarray(np.asarray(image.convert("RGB"), dtype=np.uint8)),
            ))
            order.append(rows)
            done[rows] = True
            start += len(rows)
        rest = np.flatnonzero(~done)
        if len(rest):   # faces without texture: colour per vertex
            unique, inverse = np.unique(mesh.faces[rest].reshape(-1), return_inverse=True)
            parts.append(DisplayPart(face_start=start, face_end=start + len(rest), point_vertex=unique,
                                     faces=np.asarray(inverse).reshape(-1, 3).astype(np.int64)))
            order.append(rest)
        faces = mesh.faces[np.concatenate(order)].astype(np.int64)
        if progress is not None:
            progress(1.0, "3D view ready")
        return DisplayMesh(path=path, vertices_obj=np.asarray(mesh.vertices, dtype=np.float64), faces=faces,
                           colors=np.clip(colors, 0, 255).astype(np.uint8), source_faces=source_faces,
                           colour_source="texture (full resolution)", full_vertices=full, full_origin=origin,
                           parts=parts)

    _sub(progress, 0.75, 0.98)(0.0, f"Simplifying {source_faces:,} triangles for the 3D view...")
    metric = mesh.vertices - origin
    mesh.vertices = None
    vertices, _same, rgb, faces = slpk_io.cluster_mesh(metric, metric, colors, mesh.faces, int(target_faces),
                                                       compact=False)
    faces = faces.astype(np.int64)
    if progress is not None:
        progress(1.0, "3D view ready")
    return DisplayMesh(path=path, vertices_obj=vertices + origin, faces=faces, colors=rgb,
                       source_faces=source_faces, colour_source=source, full_vertices=full, full_origin=origin,
                       parts=[DisplayPart(face_start=0, face_end=len(faces),
                                          point_vertex=np.arange(len(vertices), dtype=np.int64), faces=faces)])


DISPLAY_POINTS = 2_500_000


@dataclass
class DisplayCloud:
    """Points (or splat centres) for the 3D view, in file coordinates."""
    path: Path
    kind: str
    points_obj: np.ndarray          # (n, 3) float64
    colors: np.ndarray              # (n, 3) uint8
    source_points: int


def build_display_cloud(model: LoadedModel, count: int = DISPLAY_POINTS) -> DisplayCloud:
    if model.cloud is None:
        raise ValueError("The model is not a point cloud.")
    points, colors = _preview(model.cloud, count, seed=1)
    return DisplayCloud(path=model.path, kind=model.kind, points_obj=points, colors=colors,
                        source_points=len(model.cloud.xyz))


def to_world(obj_xyz: np.ndarray, frame: ModelFrame, adjustment: Similarity | None) -> np.ndarray:
    """OBJ coordinates -> project CRS (frame, then the GCP adjustment if any)."""
    out = np.empty((len(obj_xyz), 3), dtype=np.float64)
    for start in range(0, len(obj_xyz), _BLOCK):
        part = slice(start, start + _BLOCK)
        pts = frame.apply(obj_xyz[part])
        out[part] = adjustment.apply(pts) if adjustment is not None else pts
    return out


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@dataclass
class ExportOptions:
    layer_name: str
    height_model: str = HEIGHT_ELLIPSOIDAL
    max_texture: int = 2048           # px of a node texture (ROLAVI_SLPK_MAX_TEXTURE in GEOStudio)
    tile_faces: int = 200_000         # triangles per tile (ROLAVI_SLPK_TILE_FACES)
    jpeg_quality: int = 85
    source_texture_limit: int = 8192  # the source texture is read at most this size (16K Agisoft textures)
    geoid_path: str = ""              # GTX geoid: ellipsoidal Z converted to orthometric (H = h - N)


def load_texture_image(path: Path, limit: int):
    """A texture for the SLPK (big Agisoft textures included), at most ``limit`` px (as GEOStudio)."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None  # 16K x 16K photogrammetry textures are normal
    image = Image.open(path)
    if max(image.size) > limit:
        image.draft("RGB", (limit, limit))  # JPEG: decoded directly at a smaller scale
    image = image.convert("RGB")
    if max(image.size) > limit:
        image.thumbnail((limit, limit))
    return image


@dataclass
class ExportReport:
    result: "slpk_io.SlpkResult | slpk_io.PointCloudResult"
    seconds: float
    lines: list[str]
    summary: str = ""               # one line for the message box: "1,234 triangles, 56.7 MB"


def export_slpk(
    obj_path: str | Path,
    out_path: str | Path,
    crs: str,
    frame: ModelFrame,
    adjustment: Similarity | None,
    options: ExportOptions,
    backend: CrsBackend,
    progress: Progress | None = None,
    log: Log | None = None,
) -> ExportReport:
    """The full textured OBJ as an IntegratedMesh SLPK (I3S 1.7, WGS84), as GEOStudio's _write_obj_slpk."""
    log = log or _quiet
    started = time.monotonic()
    obj_path = Path(obj_path)
    out_path = Path(out_path)
    info = backend.require_projected(crs)

    mesh = obj_io.read_obj_mesh(obj_path, _sub(progress, 0.0, 0.30))
    report = _sub(progress, 0.30, 0.34)
    world = np.empty((len(mesh.vertices), 3), dtype=np.float64)
    for start in range(0, len(world), _BLOCK):
        part = slice(start, start + _BLOCK)
        pts = frame.apply(mesh.vertices[part])
        world[part] = adjustment.apply(pts) if adjustment is not None else pts
        report(min(1.0, (start + _BLOCK) / max(1, len(world))),
               f"Georeferencing {min(len(world), start + _BLOCK):,} of {len(world):,} vertices...")
    mesh.vertices = None  # the copy in world coordinates is enough

    _sub(progress, 0.34, 0.34)(0.0, "Converting to WGS84 longitude / latitude...")
    lon, lat = backend.to_lonlat(info.code, world[:, 0], world[:, 1])
    lonlat_h = np.column_stack([lon, lat, world[:, 2]])
    del lon, lat

    height_model = options.height_model
    geoid_line = ""
    if options.geoid_path:
        _sub(progress, 0.34, 0.36)(0.0, "Geoid: converting to orthometric heights (H = h - N)...")
        geoid = GtxGeoid(options.geoid_path)
        undulation = geoid.undulation(lonlat_h[:, 0], lonlat_h[:, 1])
        missing = ~np.isfinite(undulation)
        if missing.any():
            raise GeoidError(f"{int(missing.sum()):,} of {len(undulation):,} vertices fall outside the geoid model "
                             f"{geoid.path.name} (or on cells without data): use a geoid that covers the model.")
        lonlat_h[:, 2] -= undulation
        world[:, 2] -= undulation
        height_model = HEIGHT_ORTHOMETRIC
        geoid_line = (f"Geoid      : {geoid.path.name}, N {float(undulation.min()):.3f} .. "
                      f"{float(undulation.max()):.3f} m (H = h - N)")
        log(f"   {geoid_line}")
        del undulation

    names = list(mesh.materials)
    faces_material = mesh.faces_material
    textures, _counts = obj_io.mtl_textures(obj_path, mesh.mtllib)
    if (faces_material < 0).all() and len(textures) == 1:
        names = list(textures)  # faces without usemtl and one texture in the MTL
        faces_material = np.zeros_like(faces_material)
    found = sum(1 for name in names if name in textures)
    log(f"   OBJ        : {len(world):,} vertices, {len(mesh.faces):,} triangles, "
        f"{len(names)} material(s), {found} texture(s) found")
    if mesh.faces_uv is not None and not found:
        log("⚠️ No texture image was found through the MTL (map_Kd): the layer will be untextured.")

    def loader(index: int):
        if index < 0 or index >= len(names) or names[index] not in textures:
            return None
        texture = textures[names[index]]
        log(f"   Texture    : {texture.name}")
        try:
            return load_texture_image(texture, options.source_texture_limit)
        except Exception as exc:
            log(f"⚠️ Texture {texture.name} could not be read: {exc}")
            return None

    colors = mesh.vertex_colors if (mesh.vertex_colors is not None and not found) else None
    result = slpk_io.write_mesh_pyramid_slpk(
        out_path,
        lonlat_h,
        world,
        mesh.faces,
        mesh.faces_uv,
        mesh.uvs,
        faces_material,
        loader,
        options.layer_name or obj_path.stem,
        height_model=height_model,
        vertex_colors=colors,
        max_faces=int(options.tile_faces),
        max_texture=int(options.max_texture),
        jpeg_quality=int(options.jpeg_quality),
        progress=_sub(progress, 0.38, 0.98),
    )

    # Read the package back: the layer document must be WGS84 IntegratedMesh (ROLAVI Viewer / ArcGIS).
    layer, nodes = slpk_io.read_layer(out_path)
    spatial = layer.get("spatialReference", {})
    if layer.get("layerType") != "IntegratedMesh" or int(spatial.get("wkid", 0)) != 4326 or len(nodes) != result.nodes:
        raise RuntimeError("The SLPK was written but its layer document is not the expected one.")
    seconds = time.monotonic() - started
    centre_lon = (result.extent[0] + result.extent[2]) / 2.0
    centre_lat = (result.extent[1] + result.extent[3]) / 2.0
    lines = [
        f"SLPK       : {out_path}",
        f"Layer      : {layer.get('name')} - IntegratedMesh I3S {layer.get('store', {}).get('version')}, "
        f"WGS84 (EPSG:4326), heights {HEIGHT_LABELS.get(height_model, height_model).lower()}",
        f"Content    : {result.nodes} nodes, {result.triangles:,} triangles, {result.textures} textured nodes",
        f"Centre     : longitude {centre_lon:.8f}, latitude {centre_lat:.8f}",
        f"Size       : {result.size_bytes / (1024.0 * 1024.0):,.1f} MB in {seconds:,.1f} s",
    ]
    if geoid_line:
        lines.insert(2, geoid_line)
    if progress is not None:
        progress(1.0, "SLPK written")
    return ExportReport(result=result, seconds=seconds, lines=lines,
                        summary=f"{result.triangles:,} triangles, {result.size_bytes / (1024.0 * 1024.0):,.1f} MB")


def _apply_geoid(options: ExportOptions, lonlat_h: np.ndarray, world: np.ndarray | None,
                 progress: Progress | None, log: Log, what: str) -> tuple[str, str]:
    """H = h - N on ``lonlat_h`` (and ``world``) when a geoid is set: (height model, report line)."""
    if not options.geoid_path:
        return options.height_model, ""
    _sub(progress, 0.0, 1.0)(0.0, "Geoid: converting to orthometric heights (H = h - N)...")
    geoid = GtxGeoid(options.geoid_path)
    undulation = geoid.undulation(lonlat_h[:, 0], lonlat_h[:, 1])
    missing = ~np.isfinite(undulation)
    if missing.any():
        raise GeoidError(f"{int(missing.sum()):,} of {len(undulation):,} {what} fall outside the geoid model "
                         f"{geoid.path.name} (or on cells without data): use a geoid that covers the model.")
    lonlat_h[:, 2] -= undulation
    if world is not None:
        world[:, 2] -= undulation
    line = (f"Geoid      : {geoid.path.name}, N {float(undulation.min()):.3f} .. "
            f"{float(undulation.max()):.3f} m (H = h - N)")
    log(f"   {line}")
    return HEIGHT_ORTHOMETRIC, line


def export_point_cloud_slpk(
    model: LoadedModel,
    out_path: str | Path,
    crs: str,
    frame,
    adjustment: Similarity | None,
    options: ExportOptions,
    backend: CrsBackend,
    progress: Progress | None = None,
    log: Log | None = None,
) -> ExportReport:
    """Every point (or every visible Gaussian centre, with its colour) as a PointCloud SLPK (I3S 2.0, WGS84)."""
    log = log or _quiet
    started = time.monotonic()
    out_path = Path(out_path)
    info = backend.require_projected(crs)
    cloud = model.cloud
    if cloud is None:
        raise ValueError("The model is not a point cloud.")
    index = np.arange(len(cloud.xyz))
    if cloud.opacity is not None:
        index = index[cloud.opacity >= SPLAT_MIN_OPACITY]
        log(f"   Gaussians  : {len(index):,} of {len(cloud.xyz):,} with opacity >= {SPLAT_MIN_OPACITY:g} "
            "(their centres and base colour)")
    else:
        log(f"   Points     : {len(index):,}")
    report = _sub(progress, 0.0, 0.25)
    world = np.empty((len(index), 3), dtype=np.float64)
    for start in range(0, len(index), _BLOCK):
        part = slice(start, start + _BLOCK)
        pts = frame.apply(cloud.xyz[index[part]])
        world[part] = adjustment.apply(pts) if adjustment is not None else pts
        report(min(1.0, (start + _BLOCK) / max(1, len(index))),
               f"Georeferencing {min(len(index), start + _BLOCK):,} of {len(index):,} points...")
    _sub(progress, 0.25, 0.3)(0.0, "Converting to WGS84 longitude / latitude...")
    lon, lat = backend.to_lonlat(info.code, world[:, 0], world[:, 1])
    lonlat_h = np.column_stack([lon, lat, world[:, 2]])
    del lon, lat, world
    height_model, geoid_line = _apply_geoid(options, lonlat_h, None, _sub(progress, 0.3, 0.32), log, "points")
    colors = cloud.colors[index] if cloud.colors is not None else None
    result = slpk_io.write_point_cloud_slpk(
        out_path, lonlat_h, colors, options.layer_name or model.opened.stem, height_model=height_model,
        progress=_sub(progress, 0.32, 0.98),
    )
    layer = _read_layer_document(out_path)
    spatial = layer.get("spatialReference", {})
    if layer.get("layerType") != "PointCloud" or int(spatial.get("wkid", 0)) != 4326:
        raise RuntimeError("The SLPK was written but its layer document is not the expected one.")
    seconds = time.monotonic() - started
    centre_lon = (result.extent[0] + result.extent[2]) / 2.0
    centre_lat = (result.extent[1] + result.extent[3]) / 2.0
    lines = [
        f"SLPK       : {out_path}",
        f"Layer      : {layer.get('name')} - PointCloud I3S {layer.get('store', {}).get('version', '2.0')}, "
        f"WGS84 (EPSG:4326), heights {HEIGHT_LABELS.get(height_model, height_model).lower()}",
        f"Content    : {result.nodes} nodes, {result.points:,} points"
        + (" (Gaussian centres)" if model.kind == KIND_SPLATS else ""),
        f"Centre     : longitude {centre_lon:.8f}, latitude {centre_lat:.8f}",
        f"Size       : {result.size_bytes / (1024.0 * 1024.0):,.1f} MB in {seconds:,.1f} s",
    ]
    if geoid_line:
        lines.insert(2, geoid_line)
    if progress is not None:
        progress(1.0, "SLPK written")
    return ExportReport(result=result, seconds=seconds, lines=lines,
                        summary=f"{result.points:,} points, {result.size_bytes / (1024.0 * 1024.0):,.1f} MB")


def _read_layer_document(path: Path) -> dict:
    import gzip
    import zipfile

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "3dSceneLayer.json.gz" in names:
            return json.loads(gzip.decompress(archive.read("3dSceneLayer.json.gz")))
        if "3dSceneLayer.json" in names:
            return json.loads(archive.read("3dSceneLayer.json"))
    raise RuntimeError(f"{path.name} has no 3dSceneLayer document.")


def export_any(model: LoadedModel, out_path: str | Path, crs: str, frame, adjustment: Similarity | None,
               options: ExportOptions, backend: CrsBackend, progress: Progress | None = None,
               log: Log | None = None) -> ExportReport:
    """IntegratedMesh SLPK for a mesh, PointCloud SLPK for a point cloud or splats."""
    if model.kind == KIND_MESH:
        return export_slpk(model.path, out_path, crs, frame, adjustment, options, backend, progress, log)
    return export_point_cloud_slpk(model, out_path, crs, frame, adjustment, options, backend, progress, log)
