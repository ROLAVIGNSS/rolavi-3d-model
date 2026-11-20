"""PLY models and ROLAVIScan georeferences (Qt-free, QGIS-free).

What a PLY holds, decided from its header:

* ``mesh``   - a ``face`` element (ROLAVIScan ``*_mesh.ply`` / ``*_mesh_ENU.ply`` / ``*_mesh_UTM.ply``,
               or any triangle PLY). The plugin works on meshes as OBJ, so a PLY mesh is used through
               the OBJ ROLAVIScan writes beside it, or converted once to an OBJ.
* ``splats`` - 3D Gaussian Splatting (``f_dc_*``, ``opacity``, ``scale_*``, ``rot_*``), e.g. ROLAVIScan
               ``*_3DGS.ply``. Colour = the base colour (SH degree 0), opacity = sigmoid, size = exp.
* ``points`` - anything else: a point cloud (ROLAVIScan ``PointCloud/<session>_live_cloud.ply``).

Where the coordinates are (``frame``):

* ``ar``     - ARKit world metres, Y up (ROLAVIScan ``*_mesh.ply``, ``*_3DGS.ply``, ``AR_WORLD`` clouds).
* ``enu``    - local East / North / Up metres at the georeference origin (``*_mesh_ENU.*``).
* ``utm``    - ROLAVIScan UTM: origin UTM + ENU, as the app writes it (``*_mesh_UTM.ply``, ``UTM`` clouds).
* ``lonlat`` - longitude, latitude, ellipsoidal height (``WGS84_GEOGRAPHIC`` clouds).

ROLAVIScan georeference (``<capture>_georef.json``, or the ``ROLAVI_*`` comments of a cloud), checked
against ROLAVIScan's own ENU and UTM files (0.000001 m)::

    E = c*(x - x0) + s*(z - z0) + residualEast        c, s = cos / sin(yawRadians)
    N = s*(x - x0) - c*(z - z0) + residualNorth       (x0, y0, z0) = arOrigin
    U =    y - y0                + residualUp          scale: not applied (as ROLAVIScan)

``ScanFrame`` then takes ENU to the project CRS exactly: ENU (tangent plane at the origin) ->
ECEF -> longitude / latitude / ellipsoidal height -> CRS. ROLAVIScan's own UTM files add ENU to the
origin UTM without the grid convergence (0.44 deg in Lima: 7.7 cm per 10 m from the origin), so
the coordinates here differ from those files by that rotation; these ones are the right ones.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

Progress = Callable[[float, str], None]

KIND_MESH = "mesh"
KIND_POINTS = "points"
KIND_SPLATS = "splats"
KIND_LABELS = {KIND_MESH: "Mesh", KIND_POINTS: "Point cloud", KIND_SPLATS: "3D Gaussian splats"}

FRAME_AR = "ar"
FRAME_ENU = "enu"
FRAME_UTM = "utm"
FRAME_LONLAT = "lonlat"
FRAME_NONE = ""
FRAME_LABELS = {
    FRAME_AR: "ARKit world (metres, Y up)",
    FRAME_ENU: "local ENU at the georeference origin (metres)",
    FRAME_UTM: "UTM (origin + ENU, as ROLAVIScan writes it)",
    FRAME_LONLAT: "longitude / latitude / ellipsoidal height",
    FRAME_NONE: "not stated in the file",
}

WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
SH_C0 = 0.28209479177387814           # degree-0 spherical harmonic: colour = 0.5 + C0 * f_dc
_BLOCK = 1_000_000


class PlyError(ValueError):
    pass


def _quiet(_fraction: float, _text: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Geodesy (WGS84, as ROLAVIScan's Geodesy.swift)
# ---------------------------------------------------------------------------

def geodetic_to_ecef(lat_deg, lon_deg, h) -> np.ndarray:
    lat = np.radians(np.asarray(lat_deg, dtype=np.float64))
    lon = np.radians(np.asarray(lon_deg, dtype=np.float64))
    h = np.asarray(h, dtype=np.float64)
    sin_lat = np.sin(lat)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    return np.stack([(n + h) * np.cos(lat) * np.cos(lon),
                     (n + h) * np.cos(lat) * np.sin(lon),
                     (n * (1.0 - WGS84_E2) + h) * sin_lat], axis=-1)


def ecef_to_geodetic(xyz: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(latitude deg, longitude deg, ellipsoidal height m); iterated to well under 1 mm."""
    xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
    x, y, z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1.0 - WGS84_E2))
    h = np.zeros_like(lat)
    for _ in range(5):
        sin_lat = np.sin(lat)
        n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1.0 - WGS84_E2 * n / (n + h)))
    return np.degrees(lat), np.degrees(lon), h


def enu_basis(lat_deg: float, lon_deg: float) -> np.ndarray:
    """Rows: East, North, Up unit vectors in ECEF at (lat, lon)."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    sl, cl, so, co = math.sin(lat), math.cos(lat), math.sin(lon), math.cos(lon)
    return np.array([[-so, co, 0.0], [-sl * co, -sl * so, cl], [cl * co, cl * so, sl]])


def utm_epsg(lat_deg: float, lon_deg: float) -> int:
    zone = int(math.floor((lon_deg + 180.0) / 6.0)) % 60 + 1
    return (32700 if lat_deg < 0 else 32600) + zone


# ---------------------------------------------------------------------------
# PLY header
# ---------------------------------------------------------------------------

_PLY_TYPES = {
    "char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
    "short": "i2", "int16": "i2", "ushort": "u2", "uint16": "u2",
    "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
    "float": "f4", "float32": "f4", "double": "f8", "float64": "f8",
}


@dataclass
class PlyProperty:
    name: str
    dtype: str                    # numpy code without byte order, e.g. "f4"
    count_dtype: str = ""         # list properties: dtype of the count ("" = scalar)


@dataclass
class PlyElement:
    name: str
    count: int
    properties: list[PlyProperty] = field(default_factory=list)

    def names(self) -> list[str]:
        return [p.name for p in self.properties]

    @property
    def fixed(self) -> bool:
        return all(not p.count_dtype for p in self.properties)


@dataclass
class PlyHeader:
    path: Path
    format: str                   # binary_little_endian | binary_big_endian | ascii
    size: int                     # bytes of the header (start of the data)
    comments: list[str]
    elements: list[PlyElement]

    @property
    def order(self) -> str:
        return ">" if self.format == "binary_big_endian" else "<"

    def element(self, name: str) -> PlyElement | None:
        return next((e for e in self.elements if e.name == name), None)

    def comment_text(self) -> str:
        return "\n".join(self.comments)


def read_ply_header(path: str | Path) -> PlyHeader:
    path = Path(path)
    with open(path, "rb") as handle:
        if handle.readline().strip() != b"ply":
            raise PlyError(f"{path.name} is not a PLY file.")
        size = 4
        fmt = ""
        comments: list[str] = []
        elements: list[PlyElement] = []
        while True:
            raw = handle.readline()
            if not raw:
                raise PlyError(f"{path.name}: the PLY header has no end_header line.")
            size += len(raw)
            if size > 1_000_000:
                raise PlyError(f"{path.name}: the PLY header is too long.")
            line = raw.decode("latin-1").strip()
            if line == "end_header":
                break
            words = line.split()
            if not words:
                continue
            key = words[0]
            if key == "format" and len(words) >= 2:
                fmt = words[1]
            elif key in ("comment", "obj_info"):
                comments.append(line.split(None, 1)[1] if len(words) > 1 else "")
            elif key == "element" and len(words) >= 3:
                elements.append(PlyElement(words[1], int(words[2])))
            elif key == "property" and elements:
                if len(words) >= 5 and words[1] == "list":
                    count, value = _PLY_TYPES.get(words[2]), _PLY_TYPES.get(words[3])
                    if count is None or value is None:
                        raise PlyError(f"{path.name}: unknown PLY type in '{line}'.")
                    elements[-1].properties.append(PlyProperty(words[4], value, count))
                elif len(words) >= 3:
                    dtype = _PLY_TYPES.get(words[1])
                    if dtype is None:
                        raise PlyError(f"{path.name}: unknown PLY type in '{line}'.")
                    elements[-1].properties.append(PlyProperty(words[2], dtype))
    if fmt not in ("binary_little_endian", "binary_big_endian", "ascii"):
        raise PlyError(f"{path.name}: unknown PLY format '{fmt}'.")
    return PlyHeader(path, fmt, size, comments, elements)


def _element_dtype(header: PlyHeader, element: PlyElement) -> np.dtype:
    if not element.fixed:
        raise PlyError(f"{header.path.name}: the '{element.name}' element has list properties.")
    return np.dtype([(p.name, header.order + p.dtype) for p in element.properties])


def _element_offset(header: PlyHeader, name: str) -> int:
    """Byte offset of an element of a binary PLY (the elements before it must be fixed size)."""
    offset = header.size
    for element in header.elements:
        if element.name == name:
            return offset
        offset += element.count * _element_dtype(header, element).itemsize
    raise PlyError(f"{header.path.name} has no '{name}' element.")


def vertex_table(header: PlyHeader) -> np.ndarray:
    """The vertex element as a structured array (a read-only memory map for binary PLY)."""
    vertex = header.element("vertex")
    if vertex is None or vertex.count <= 0:
        raise PlyError(f"{header.path.name} has no vertices.")
    dtype = _element_dtype(header, vertex)
    if header.format == "ascii":
        if header.elements[0].name != "vertex":
            raise PlyError(f"{header.path.name}: ASCII PLY whose first element is not the vertices.")
        values = np.loadtxt(header.path, dtype=np.float64, skiprows=_ascii_header_lines(header.path),
                            max_rows=vertex.count, ndmin=2)
        names = dtype.names or ()
        if values.shape[1] < len(names):
            raise PlyError(f"{header.path.name}: the ASCII vertex lines have fewer values than the header.")
        table = np.empty(vertex.count, dtype=dtype.newbyteorder("="))
        for column, name in enumerate(names):
            table[name] = values[:, column]
        return table
    offset = _element_offset(header, "vertex")
    return np.memmap(header.path, dtype=dtype, mode="r", offset=offset, shape=(vertex.count,))


def _ascii_header_lines(path: Path) -> int:
    with open(path, "rb") as handle:
        for number, raw in enumerate(handle, start=1):
            if raw.strip() == b"end_header":
                return number
    raise PlyError(f"{path.name}: no end_header line.")


def ply_kind(header: PlyHeader) -> str:
    vertex = header.element("vertex")
    names = set(vertex.names()) if vertex else set()
    face = header.element("face")
    if face is not None and face.count > 0:
        return KIND_MESH
    if {"f_dc_0", "f_dc_1", "f_dc_2", "opacity", "scale_0", "scale_1", "scale_2",
            "rot_0", "rot_1", "rot_2", "rot_3"} <= names:
        return KIND_SPLATS
    return KIND_POINTS


# ---------------------------------------------------------------------------
# ROLAVIScan georeference
# ---------------------------------------------------------------------------

def _ar_linear(yaw: float) -> np.ndarray:
    """AR world -> ENU rotation of ROLAVIScan (rows E, N, U; columns AR x, y, z)."""
    c, s = math.cos(yaw), math.sin(yaw)
    return np.array([[c, 0.0, s], [s, 0.0, -c], [0.0, 1.0, 0.0]])


@dataclass
class ScanGeoref:
    lat0: float
    lon0: float
    h0: float
    linear: np.ndarray = field(default_factory=lambda: np.eye(3))   # ENU = linear @ ar + offset
    offset: np.ndarray = field(default_factory=lambda: np.zeros(3))
    epsg: int | None = None              # UTM of the capture (ROLAVIScan's UTM files)
    utm_origin: tuple[float, float] | None = None   # E0, N0 written by ROLAVIScan
    source: str = ""                     # where it was read
    method: str = ""                     # captureSaved, cameraRTKFit, ...
    absolute: bool = True
    heading_solved: bool | None = None
    yaw_deg: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def origin_epsg(self) -> int:
        return self.epsg or utm_epsg(self.lat0, self.lon0)

    def ar_to_enu(self, xyz: np.ndarray) -> np.ndarray:
        xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
        return xyz @ self.linear.T + self.offset

    @classmethod
    def from_json(cls, path: str | Path) -> "ScanGeoref | None":
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if isinstance(data.get("georeference"), dict):   # a manifest
            data = data["georeference"]
        try:
            lat, lon, h = (float(data["originLatitude"]), float(data["originLongitude"]),
                           float(data["originEllipsoidHeight"]))
            yaw = float(data.get("yawRadians", 0.0))
        except (KeyError, TypeError, ValueError):
            return None
        ar_origin = np.array([float(data.get(k, 0.0) or 0.0) for k in ("arOriginX", "arOriginY", "arOriginZ")])
        residual = np.array([float(data.get(k, 0.0) or 0.0) for k in ("residualEast", "residualNorth", "residualUp")])
        linear = _ar_linear(yaw)
        method = str(data.get("source") or "")
        samples = int(data.get("fitSampleCount", 1) or 0)
        absolute = (method != "localVIO" and samples >= 1
                    and all(math.isfinite(v) for v in (lat, lon, h, yaw)) and not (lat == 0 and lon == 0))
        epsg = data.get("epsg")
        geo = cls(lat0=lat, lon0=lon, h0=h, linear=linear, offset=residual - linear @ ar_origin,
                  epsg=int(epsg) if epsg else None, source=path.name, method=method, absolute=absolute,
                  heading_solved=None if method in ("cameraRTKFit", "cameraGNSSFit") else method != "localVIO",
                  yaw_deg=math.degrees(yaw))
        if method == "cameraGNSSFit":
            geo.notes.append("approximate camera GNSS fit (not RTK): check it with GCPs")
        if method in ("cameraRTKFit", "cameraGNSSFit"):
            geo.notes.append(f"fit RMS horizontal {float(data.get('fitHorizontalRMSMeters', 0)):.3f} m, vertical "
                             f"{float(data.get('fitVerticalRMSMeters', 0)):.3f} m, {samples} cameras, baseline "
                             f"{float(data.get('maximumBaselineMeters', 0)):.1f} m")
        if not absolute:
            geo.notes.append("ROLAVIScan processed it in the local AR frame: NOT georeferenced (place it with GCPs)")
        return geo

    @classmethod
    def from_comments(cls, comments: list[str], source: str) -> "ScanGeoref | None":
        """``ROLAVI_*`` comments of a ROLAVIScan point cloud (ROLAVIProjectPLY.swift)."""
        values: dict[str, list[str]] = {}
        for comment in comments:
            words = comment.split()
            if words and words[0].startswith("ROLAVI_"):
                values[words[0]] = words[1:]

        def numbers(key: str, count: int) -> list[float] | None:
            try:
                out = [float(v) for v in values.get(key, [])[:count]]
            except ValueError:
                return None
            return out if len(out) == count and all(math.isfinite(v) for v in out) else None

        origin = numbers("ROLAVI_ORIGIN_LLA", 3)
        if origin is None:
            return None
        geo = cls(lat0=origin[0], lon0=origin[1], h0=origin[2], source=source, method="PLY header")
        affine = numbers("ROLAVI_AR_TO_ENU", 12)
        if affine is not None:
            geo.linear = np.array(affine[:9]).reshape(3, 3).T     # written column by column
            geo.offset = np.array(affine[9:12])
        utm = numbers("ROLAVI_UTM_ORIGIN", 2)
        if utm is not None:
            geo.utm_origin = (utm[0], utm[1])
        label = " ".join(values.get("ROLAVI_UTM", []))
        match = re.search(r"EPSG:?\s*(\d{4,5})", label) or None
        if match:
            geo.epsg = int(match.group(1))
        else:
            zone = re.search(r"(\d{1,2})\s*([NS])", label)
            if zone:
                geo.epsg = (32700 if zone.group(2) == "S" else 32600) + int(zone.group(1))
        heading = (values.get("ROLAVI_HEADING") or [""])[0]
        geo.heading_solved = True if heading == "SOLVED" else (False if heading == "UNSOLVED" else None)
        yaw = numbers("ROLAVI_NORTH_YAW_DEG", 1)
        geo.yaw_deg = yaw[0] if yaw else None
        if geo.heading_solved is False:
            geo.notes.append("true north was NOT solved in the capture: the cloud may be rotated about its "
                             "origin; fix it with GCPs (Keep vertical)")
        return geo

    def describe(self) -> list[str]:
        lines = [f"origin lat {self.lat0:.9f}, lon {self.lon0:.9f}, h {self.h0:.3f} m (ellipsoidal)"]
        if self.yaw_deg is not None:
            lines.append(f"yaw {self.yaw_deg:.4f}°" + (f" · {self.method}" if self.method else ""))
        return lines + list(self.notes)


@dataclass
class ScanInfo:
    """What a model file is and where its coordinates are."""
    path: Path
    kind: str
    frame: str = FRAME_NONE
    georef: ScanGeoref | None = None
    rolaviscan: bool = False
    texture: Path | None = None          # PLY mesh: the photo texture
    companion_obj: Path | None = None    # PLY mesh: the OBJ ROLAVIScan wrote beside it (same mesh)
    companion_frame: str = FRAME_NONE
    count: int = 0
    notes: list[str] = field(default_factory=list)
    header: PlyHeader | None = None

    @property
    def placeable(self) -> bool:
        """The georeference can place the file (absolute and its frame known)."""
        if self.georef is None or not self.georef.absolute:
            return False
        if self.frame == FRAME_AR:
            return True
        return self.frame in (FRAME_ENU, FRAME_UTM, FRAME_LONLAT)


def _frame_from_comments(text: str) -> str:
    low = text.lower()
    rolavi = re.search(r"rolavi_crs\s+(\w+)", low)
    if rolavi:
        return {"utm": FRAME_UTM, "wgs84_geographic": FRAME_LONLAT, "local_enu": FRAME_ENU,
                "ar_world": FRAME_AR}.get(rolavi.group(1), FRAME_NONE)
    if "arkit_world" in low or re.search(r"frame:\s*arkit world", low):
        return FRAME_AR
    if re.search(r"frame:\s*local enu", low):
        return FRAME_ENU
    if re.search(r"frame:\s*utm", low):
        return FRAME_UTM
    return FRAME_NONE


def _origin_from_frame_comment(text: str) -> tuple[float, float, float] | None:
    """``frame: local ENU ... origin lat -11.857688097 lon -77.153997162 ellipsoidal height 32.018 m``."""
    match = re.search(r"origin lat\s+(-?[\d.]+)\s+lon\s+(-?[\d.]+)\s+ellipsoidal height\s+(-?[\d.]+)", text)
    if not match:
        return None
    try:
        return float(match.group(1)), float(match.group(2)), float(match.group(3))
    except ValueError:
        return None


def _named_file(folder: Path, name: str) -> Path | None:
    name = name.strip().strip('"')
    if not name:
        return None
    candidate = Path(name) if Path(name).is_absolute() else folder / Path(name).name
    return candidate if candidate.is_file() else None


def find_sidecar_json(path: Path, suffix: str, comment_text: str = "") -> Path | None:
    """``<capture>_georef.json`` (or ``_manifest.json``) of a ROLAVIScan file."""
    folder = path.parent
    if suffix == "_georef.json":
        match = re.search(r"georeference:?\s+(\S+\.json)", comment_text)
        if match:
            named = _named_file(folder, match.group(1))
            if named is not None:
                return named
    best, best_len = None, 0
    stem = path.name
    for candidate in folder.glob(f"*{suffix}"):
        prefix = candidate.name[: -len(suffix)]
        if prefix and stem.startswith(prefix) and len(prefix) > best_len:
            best, best_len = candidate, len(prefix)
    return best


def find_texture(folder: Path, name: str) -> Path | None:
    """A texture by name, also when a copy added an extension (``texture.jpg.jpeg``)."""
    name = Path(name.strip().strip('"')).name
    if not name:
        return None
    for candidate in (name, name + ".jpeg", name + ".jpg", name + ".png"):
        if (folder / candidate).is_file():
            return folder / candidate
    stem = Path(name).stem
    for ext in (".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG"):
        if (folder / (stem + ext)).is_file():
            return folder / (stem + ext)
    return None


def _georef_for(path: Path, comment_text: str, comments: list[str]) -> ScanGeoref | None:
    geo = ScanGeoref.from_comments(comments, f"{path.name} (header)")
    if geo is not None:
        return geo
    sidecar = find_sidecar_json(path, "_georef.json", comment_text)
    if sidecar is not None:
        geo = ScanGeoref.from_json(sidecar)
        if geo is not None:
            return geo
    manifest = find_sidecar_json(path, "_manifest.json", comment_text)
    if manifest is not None:
        geo = ScanGeoref.from_json(manifest)
        if geo is not None:
            return geo
    origin = _origin_from_frame_comment(comment_text)
    if origin is not None:
        return ScanGeoref(lat0=origin[0], lon0=origin[1], h0=origin[2], source=f"{path.name} (header)",
                          method="origin in the file")
    return None


def _companion_obj(path: Path, georef: ScanGeoref | None) -> Path | None:
    """The OBJ ROLAVIScan wrote with a PLY mesh (manifest ``objFile``, else ``<capture>_mesh_ENU.obj``)."""
    manifest = find_sidecar_json(path, "_manifest.json")
    if manifest is not None:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8", errors="ignore"))
            named = _named_file(path.parent, str(data.get("objFile") or ""))
            if named is not None:
                return named
        except (OSError, ValueError, AttributeError, TypeError):
            pass  # unreadable manifest: look for the OBJ by its name below
    for candidate in sorted(path.parent.glob("*_mesh_ENU.obj")):
        prefix = candidate.name[: -len("_mesh_ENU.obj")]
        if path.name.startswith(prefix):
            return candidate
    return None


def inspect_ply(path: str | Path) -> ScanInfo:
    path = Path(path)
    header = read_ply_header(path)
    kind = ply_kind(header)
    text = header.comment_text()
    vertex = header.element("vertex")
    info = ScanInfo(path=path, kind=kind, frame=_frame_from_comments(text), header=header,
                    rolaviscan="rolavi" in text.lower(), count=vertex.count if vertex else 0)
    info.georef = _georef_for(path, text, header.comments)
    if info.georef is not None:
        info.rolaviscan = True
        if info.frame == FRAME_NONE and info.georef.method == "PLY header":
            info.frame = FRAME_AR
    if kind == KIND_MESH:
        texture = re.search(r"^TextureFile\s+(.+)$", text, re.MULTILINE)
        if texture:
            info.texture = find_texture(path.parent, texture.group(1))
            if info.texture is None:
                info.notes.append(f"texture {texture.group(1).strip()} not found beside the PLY: vertex colours used")
        info.companion_obj = _companion_obj(path, info.georef) if info.rolaviscan else None
        if info.companion_obj is not None:
            obj_text = _obj_comments(info.companion_obj)
            info.companion_frame = _frame_from_comments(obj_text) or FRAME_ENU
    return info


def _obj_comments(path: Path, lines: int = 40) -> str:
    out = []
    try:
        with open(path, "rb") as handle:
            for _ in range(lines):
                raw = handle.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", "replace").strip()
                if text.startswith("#"):
                    out.append(text.lstrip("#").strip())
                elif text and not text.startswith(("mtllib", "usemtl", "o ", "g ")):
                    break
    except OSError:
        pass
    return "\n".join(out)


def inspect_obj(path: str | Path) -> ScanInfo | None:
    """ROLAVIScan placement of an OBJ (``# frame: local ENU ...`` + ``<capture>_georef.json``), if any."""
    path = Path(path)
    text = _obj_comments(path)
    frame = _frame_from_comments(text)
    if "rolavi" not in text.lower() and frame == FRAME_NONE:
        return None
    georef = _georef_for(path, text, [])
    if georef is None:
        return None
    return ScanInfo(path=path, kind=KIND_MESH, frame=frame or FRAME_ENU, georef=georef, rolaviscan=True)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def _xyz(table: np.ndarray, start: int = 0, stop: int | None = None) -> np.ndarray:
    part = table[start:stop]
    return np.column_stack([np.asarray(part["x"], dtype=np.float64), np.asarray(part["y"], dtype=np.float64),
                            np.asarray(part["z"], dtype=np.float64)])


def _rgb(table: np.ndarray) -> np.ndarray | None:
    names = table.dtype.names or ()
    for keys in (("red", "green", "blue"), ("r", "g", "b"), ("diffuse_red", "diffuse_green", "diffuse_blue")):
        if all(k in names for k in keys):
            values = np.column_stack([np.asarray(table[k]) for k in keys])
            if values.dtype.kind == "f":
                scale = 255.0 if float(np.nanmax(values[: 100_000], initial=0.0)) <= 1.0 else 1.0
                return np.clip(np.nan_to_num(values * scale), 0, 255).astype(np.uint8)
            if values.dtype.itemsize > 1:
                return (values.astype(np.float64) / float(np.iinfo(values.dtype).max) * 255.0).astype(np.uint8)
            return values.astype(np.uint8)
    return None


def read_points(info: ScanInfo, progress: Progress | None = None) -> tuple[np.ndarray, np.ndarray | None]:
    """All points (float64, file coordinates) and their colours (uint8 or None)."""
    progress = progress or _quiet
    header = info.header or read_ply_header(info.path)
    table = vertex_table(header)
    n = len(table)
    xyz = np.empty((n, 3), dtype=np.float64)
    for start in range(0, n, _BLOCK):
        xyz[start:start + _BLOCK] = _xyz(table, start, start + _BLOCK)
        progress(min(1.0, (start + _BLOCK) / max(1, n)), f"Reading {min(n, start + _BLOCK):,} of {n:,} points...")
    good = np.isfinite(xyz).all(axis=1)
    colors = _rgb(table)
    if not good.all():
        xyz, colors = xyz[good], (colors[good] if colors is not None else None)
    return xyz, colors


@dataclass
class Splats:
    xyz: np.ndarray          # (n, 3) float64, file coordinates
    colors: np.ndarray       # (n, 3) uint8: base colour (SH degree 0)
    opacity: np.ndarray      # (n,) float32, 0..1
    scale: np.ndarray        # (n, 3) float32, metres (exp of the stored log scale)
    rotation: np.ndarray     # (n, 4) float32, quaternion w, x, y, z (normalised)
    sh_degree: int = 0       # view-dependent colour stored in the file (0 = none)


def read_splats(info: ScanInfo, progress: Progress | None = None) -> Splats:
    progress = progress or _quiet
    header = info.header or read_ply_header(info.path)
    table = vertex_table(header)
    n = len(table)
    progress(0.1, f"Reading {n:,} Gaussians...")
    xyz = _xyz(table)
    dc = np.column_stack([np.asarray(table[f"f_dc_{i}"], dtype=np.float32) for i in range(3)])
    colors = (np.clip(0.5 + SH_C0 * dc, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    progress(0.5, "Opacity, size and orientation of the Gaussians...")
    opacity = (1.0 / (1.0 + np.exp(-np.clip(np.asarray(table["opacity"], dtype=np.float32), -40, 40)))).astype(np.float32)
    scale = np.exp(np.clip(np.column_stack([np.asarray(table[f"scale_{i}"], dtype=np.float32) for i in range(3)]),
                           -30, 10)).astype(np.float32)
    rotation = np.column_stack([np.asarray(table[f"rot_{i}"], dtype=np.float32) for i in range(4)])
    norm = np.linalg.norm(rotation, axis=1, keepdims=True)
    rotation = np.where(norm > 0, rotation / np.maximum(norm, 1e-12), np.array([1, 0, 0, 0], dtype=np.float32))
    rest = [name for name in (table.dtype.names or ()) if name.startswith("f_rest_")]
    sh_degree = 0
    if rest:
        sample = np.column_stack([np.asarray(table[name][: 200_000]) for name in rest])
        if np.abs(sample).max(initial=0.0) > 1e-6:
            sh_degree = {9: 1, 24: 2, 45: 3}.get(len(rest), 3)
    good = np.isfinite(xyz).all(axis=1)
    progress(1.0, "Gaussians read")
    return Splats(xyz=xyz[good], colors=colors[good], opacity=opacity[good], scale=scale[good],
                  rotation=rotation[good].astype(np.float32), sh_degree=sh_degree)


# ---------------------------------------------------------------------------
# PLY mesh -> OBJ (the plugin's mesh pipeline reads OBJ)
# ---------------------------------------------------------------------------

def _face_records(header: PlyHeader) -> tuple[np.ndarray, np.ndarray | None]:
    """Triangles (F, 3) int64 and per-corner UVs (F, 3, 2) float32 of a binary PLY mesh."""
    face = header.element("face")
    if face is None or face.count <= 0:
        raise PlyError(f"{header.path.name} has no faces.")
    if header.format == "ascii":
        raise PlyError(f"{header.path.name}: ASCII PLY meshes are not read; save it as binary PLY.")
    order = header.order
    index_prop = next((p for p in face.properties if p.name in ("vertex_indices", "vertex_index")), None)
    if index_prop is None or not index_prop.count_dtype:
        raise PlyError(f"{header.path.name}: the faces have no vertex_indices list.")
    with open(header.path, "rb") as handle:
        handle.seek(_element_offset(header, "face"))
        first = handle.read(64)
    # every face has the same corner count (3, or 4 = quads): read them as fixed records
    fields: list[tuple] = []
    pos = 0
    sizes = {}
    for prop in face.properties:
        if prop.count_dtype:
            count_type = np.dtype(order + prop.count_dtype)
            count = int(np.frombuffer(first[pos:pos + count_type.itemsize], dtype=count_type)[0])
            sizes[prop.name] = count
            fields.append((prop.name + "_n", order + prop.count_dtype))
            fields.append((prop.name, order + prop.dtype, (count,)))
            pos += count_type.itemsize + count * np.dtype(prop.dtype).itemsize
        else:
            fields.append((prop.name, order + prop.dtype))
            pos += np.dtype(prop.dtype).itemsize
    dtype = np.dtype(fields)
    table = np.memmap(header.path, dtype=dtype, mode="r", offset=_element_offset(header, "face"), shape=(face.count,))
    corners = sizes[index_prop.name]
    if corners not in (3, 4) or not (np.asarray(table[index_prop.name + "_n"]) == corners).all():
        raise PlyError(f"{header.path.name}: only meshes of triangles (or of quads) are read.")
    indices = np.asarray(table[index_prop.name], dtype=np.int64)
    uv_name = next((p.name for p in face.properties if p.name in ("texcoord", "texcoords") and p.count_dtype), None)
    uvs = None
    if uv_name is not None and sizes.get(uv_name) == 2 * corners \
            and (np.asarray(table[uv_name + "_n"]) == 2 * corners).all():
        uvs = np.asarray(table[uv_name], dtype=np.float32).reshape(-1, corners, 2)
    if corners == 4:   # quads -> two triangles
        indices = np.concatenate([indices[:, [0, 1, 2]], indices[:, [0, 2, 3]]])
        if uvs is not None:
            uvs = np.concatenate([uvs[:, [0, 1, 2]], uvs[:, [0, 2, 3]]])
    return indices, uvs


def default_cache_dir() -> Path:
    folder = Path(tempfile.gettempdir()) / "rolavi_3d_model"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def ply_mesh_to_obj(info: ScanInfo, out_dir: str | Path | None = None, progress: Progress | None = None) -> Path:
    """Write the PLY mesh as an OBJ (+ MTL with its texture), once: reused while the PLY is unchanged."""
    progress = progress or _quiet
    header = info.header or read_ply_header(info.path)
    stat = info.path.stat()
    out_dir = Path(out_dir) if out_dir else default_cache_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{int(stat.st_mtime)}_{stat.st_size}"
    obj_path = out_dir / f"{info.path.stem}_{stamp}.obj"
    if obj_path.is_file() and obj_path.stat().st_size > 0:
        return obj_path
    table = vertex_table(header)
    progress(0.05, "Reading the PLY mesh...")
    vertices = _xyz(table)
    colors = _rgb(table)
    faces, corner_uvs = _face_records(header)
    if faces.size and (faces.min() < 0 or faces.max() >= len(vertices)):
        raise PlyError(f"{info.path.name}: a face uses a vertex that does not exist.")
    texture = info.texture if corner_uvs is not None else None
    mtl_path = obj_path.with_suffix(".mtl")
    tmp = obj_path.with_suffix(".obj.part")
    with open(tmp, "w", encoding="utf-8", newline="\n") as out:
        out.write(f"# {info.path.name} written as OBJ by ROLAVI 3D Model\n")
        out.write(f"# frame: {FRAME_LABELS.get(info.frame, info.frame)}\n")
        if texture is not None:
            mtl_path.write_text("newmtl texture\nKa 1 1 1\nKd 1 1 1\nKs 0 0 0\nd 1\nillum 1\n"
                                f"map_Kd {texture.resolve()}\n", encoding="utf-8")
            out.write(f"mtllib {mtl_path.name}\n")
        progress(0.2, f"Writing {len(vertices):,} vertices...")
        if colors is not None:
            data = np.column_stack([vertices, colors.astype(np.float64) / 255.0])
            np.savetxt(out, data, fmt="v %.6f %.6f %.6f %.4f %.4f %.4f")
        else:
            np.savetxt(out, vertices, fmt="v %.6f %.6f %.6f")
        progress(0.45, f"Writing {len(faces):,} triangles...")
        if texture is not None and corner_uvs is not None:
            flat = corner_uvs.reshape(-1, 2)
            unique, inverse = np.unique(flat, axis=0, return_inverse=True)
            np.savetxt(out, unique, fmt="vt %.6f %.6f")
            out.write("usemtl texture\n")
            uv_index = inverse.reshape(-1, 3) + 1
            data = np.column_stack([faces[:, 0] + 1, uv_index[:, 0], faces[:, 1] + 1, uv_index[:, 1],
                                    faces[:, 2] + 1, uv_index[:, 2]])
            np.savetxt(out, data, fmt="f %d/%d %d/%d %d/%d")
        else:
            np.savetxt(out, faces + 1, fmt="f %d %d %d")
    tmp.replace(obj_path)
    progress(1.0, "OBJ written")
    return obj_path


# ---------------------------------------------------------------------------
# File coordinates -> project CRS
# ---------------------------------------------------------------------------

class ScanFrame:
    """File coordinates -> project CRS through the ROLAVIScan georeference (exact, per point).

    Used where the window uses a ``ModelFrame``: ``apply``, ``to_dict``, ``describe``.
    """

    def __init__(self, info: ScanInfo, crs: str, backend):
        if info.georef is None:
            raise ValueError("The file has no georeference.")
        self.info = info
        self.georef = info.georef
        self.frame = info.frame
        self.crs = crs
        self.backend = backend
        g = self.georef
        self._ecef0 = geodetic_to_ecef(g.lat0, g.lon0, g.h0)
        self._basis = enu_basis(g.lat0, g.lon0)
        self._utm0: tuple[float, float] | None = None
        if self.frame == FRAME_UTM:
            if g.utm_origin is not None:
                self._utm0 = g.utm_origin
            else:
                e, n = backend.from_lonlat(f"EPSG:{g.origin_epsg}", np.array([g.lon0]), np.array([g.lat0]))
                self._utm0 = (float(e[0]), float(n[0]))

    def to_enu(self, xyz: np.ndarray) -> np.ndarray:
        xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
        g = self.georef
        if self.frame == FRAME_AR:
            return g.ar_to_enu(xyz)
        if self.frame == FRAME_ENU:
            return np.array(xyz, dtype=np.float64, copy=True)
        if self.frame == FRAME_UTM:
            e0, n0 = self._utm0 or (0.0, 0.0)
            return xyz - np.array([e0, n0, g.h0])
        if self.frame == FRAME_LONLAT:
            ecef = geodetic_to_ecef(xyz[:, 1], xyz[:, 0], xyz[:, 2])
            return (ecef - self._ecef0) @ self._basis.T
        raise ValueError(f"Unknown coordinates of the file: {self.frame!r}")

    def lonlat_h(self, xyz: np.ndarray) -> np.ndarray:
        """(longitude, latitude, ellipsoidal height) of file coordinates."""
        enu = self.to_enu(xyz)
        lat, lon, h = ecef_to_geodetic(self._ecef0 + enu @ self._basis)
        return np.column_stack([lon, lat, h])

    def apply(self, xyz: np.ndarray) -> np.ndarray:
        xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
        out = np.empty((len(xyz), 3), dtype=np.float64)
        for start in range(0, len(xyz), _BLOCK):
            part = slice(start, start + _BLOCK)
            llh = self.lonlat_h(xyz[part])
            x, y = self.backend.from_lonlat(self.crs, llh[:, 0], llh[:, 1])
            out[part, 0], out[part, 1], out[part, 2] = x, y, llh[:, 2]
        return out

    def to_dict(self) -> dict:
        g = self.georef
        return {"rolaviscan": g.source, "frame": self.frame, "crs": self.crs,
                "origin": [g.lat0, g.lon0, g.h0], "offset": [float(v) for v in g.offset]}

    def describe(self) -> str:
        return (f"ROLAVIScan georeference ({self.georef.source}), file in "
                f"{FRAME_LABELS.get(self.frame, self.frame)}, exact ENU -> {self.crs}")
