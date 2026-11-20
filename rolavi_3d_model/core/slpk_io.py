# slpk_io.py - copied unchanged from ROLAVI GEOStudio (rolavi_sfm_studio_pkg/slpk_io.py, 2026-11-12).
# Keep both copies in sync: fixes made in GEOStudio go here too.
"""Esri Scene Layer Packages (SLPK, OGC I3S) for meshes and point clouds (Qt-free).

* ``write_slpk``             - IntegratedMesh layer (I3S 1.7) of textured / coloured meshes.
* ``write_point_cloud_slpk`` - PointCloud layer (I3S 2.0, LEPCC-compressed points + RGB).
* ``tile_mesh``              - splits a large untextured mesh in parts of bounded size.
* ``write_mesh_pyramid_slpk``- heavy textured mesh (full resolution) as a level-of-detail
                               pyramid: textured leaf tiles + simplified, vertex-coloured
                               parents, so ArcGIS draws far views light and near views sharp.

The model is written as an **IntegratedMesh** scene layer (the layer type of
photogrammetry meshes in ArcGIS) in WGS84 (EPSG:4326):

* ``metadata.json``                         - package description (STORE archive, GZIP resources)
* ``3dSceneLayer.json.gz``                  - layer document (CRS, height model, schema, materials)
* ``nodepages/<n>.json.gz``                 - node pages (bounding boxes, LOD, mesh references)
* ``nodes/<id>/geometries/0.bin.gz``        - uncompressed geometry buffer of a node
* ``nodes/<id>/textures/0.jpg``             - JPEG texture of a node

Node 0 is a root without geometry; every mesh part (one per texture / material
of the model) is a child node.  Vertex positions are offsets from the node's
oriented-bounding-box centre: degrees for longitude/latitude, metres for the
height (I3S convention for global scenes); normals are in ECEF.
"""

from __future__ import annotations

import gzip
import io
import json
import math
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

I3S_VERSION = "1.7"
_A = 6378137.0
_F = 1.0 / 298.257223563
_E2 = _F * (2.0 - _F)

HEIGHT_ELLIPSOIDAL = "ellipsoidal"
HEIGHT_ORTHOMETRIC = "orthometric"

Progress = Callable[[float, str], None]


@dataclass
class MeshPart:
    """One textured (or coloured) piece of the model in WGS84."""

    lonlat_h: np.ndarray            # (N, 3) longitude, latitude (degrees), height (m)
    faces: np.ndarray               # (M, 3) vertex indices
    uv: np.ndarray | None = None    # (N, 2) texture coordinates, OBJ convention (v up)
    image: object | None = None     # PIL image of the texture
    colors: np.ndarray | None = None  # (N, 3|4) uint8 vertex colours (untextured parts)
    name: str = ""
    corner_uv: np.ndarray | None = None  # (M*3, 2) uv per face corner (textures with seams)


# ---------------------------------------------------------------------------
# Geodesy (WGS84)
# ---------------------------------------------------------------------------

def geodetic_to_ecef(lon_deg, lat_deg, h) -> np.ndarray:
    lon = np.radians(np.asarray(lon_deg, dtype=np.float64))
    lat = np.radians(np.asarray(lat_deg, dtype=np.float64))
    h = np.asarray(h, dtype=np.float64)
    n = _A / np.sqrt(1.0 - _E2 * np.sin(lat) ** 2)
    return np.stack([
        (n + h) * np.cos(lat) * np.cos(lon),
        (n + h) * np.cos(lat) * np.sin(lon),
        (n * (1.0 - _E2) + h) * np.sin(lat),
    ], axis=-1)


def enu_basis(lon_deg: float, lat_deg: float) -> np.ndarray:
    """3x3 matrix whose columns are East, North, Up expressed in ECEF."""
    lon = math.radians(lon_deg)
    lat = math.radians(lat_deg)
    east = [-math.sin(lon), math.cos(lon), 0.0]
    north = [-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)]
    up = [math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)]
    return np.array([east, north, up], dtype=np.float64).T


def quaternion_from_matrix(m: np.ndarray) -> list[float]:
    """Unit quaternion [x, y, z, w] of a rotation matrix."""
    m = np.asarray(m, dtype=np.float64)
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[0, 1] + m[1, 0]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[0, 1] + m[1, 0]) / s
        y = 0.25 * s
        z = (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[1, 2] + m[2, 1]) / s
        z = 0.25 * s
    q = np.array([x, y, z, w], dtype=np.float64)
    q /= np.linalg.norm(q)
    if q[3] < 0:
        q = -q
    return [float(v) for v in q]


def _obb(lonlat_h: np.ndarray, margin: float = 0.01) -> tuple[dict, np.ndarray, np.ndarray]:
    """OBB aligned with the local East-North-Up frame; also returns the centre and the ENU basis."""
    low = lonlat_h.min(axis=0)
    high = lonlat_h.max(axis=0)
    center = (low + high) / 2.0
    basis = enu_basis(center[0], center[1])
    ecef = geodetic_to_ecef(lonlat_h[:, 0], lonlat_h[:, 1], lonlat_h[:, 2])
    origin = geodetic_to_ecef(center[0], center[1], center[2])
    enu = (ecef - origin) @ basis
    half = np.max(np.abs(enu), axis=0) * (1.0 + margin) + 0.01
    obb = {
        "center": [float(center[0]), float(center[1]), float(center[2])],
        "halfSize": [float(half[0]), float(half[1]), float(half[2])],
        "quaternion": quaternion_from_matrix(basis),
    }
    return obb, center, basis


# ---------------------------------------------------------------------------
# Node content
# ---------------------------------------------------------------------------

def _vertex_normals_enu(enu: np.ndarray, faces: np.ndarray) -> np.ndarray:
    tri = enu[faces]
    face_normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    normals = np.zeros_like(enu)
    for corner in range(3):
        np.add.at(normals, faces[:, corner], face_normals)
    length = np.linalg.norm(normals, axis=1)
    missing = length < 1e-12
    normals[missing] = (0.0, 0.0, 1.0)
    length[missing] = 1.0
    return normals / length[:, None]


def _power_of_two_at_most(value: int, limit: int) -> int:
    size = 1
    while size * 2 <= min(value, limit):
        size *= 2
    return max(size, 1)


def _texture_jpeg(image, max_size: int, quality: int) -> tuple[bytes, int, int]:
    from PIL import Image

    img = image if isinstance(image, Image.Image) else Image.fromarray(np.asarray(image))
    img = img.convert("RGB")
    width = _power_of_two_at_most(img.width, max_size)
    height = _power_of_two_at_most(img.height, max_size)
    if (width, height) != img.size:
        img = img.resize((width, height), Image.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=int(quality), optimize=True)
    return buffer.getvalue(), width, height


def geometry_buffer(part: MeshPart, center: np.ndarray, basis: np.ndarray, flip_v: bool = True) -> tuple[bytes, int, int]:
    """Legacy (uncompressed) I3S geometry buffer of a part: header + per-vertex arrays + one feature.

    Returns ``(bytes, vertex_count, face_count)``.  Vertices are unwelded: three
    per triangle, as the ``PerAttributeArray`` topology requires.
    """
    faces = np.asarray(part.faces, dtype=np.int64)
    points = np.asarray(part.lonlat_h, dtype=np.float64)
    ecef = geodetic_to_ecef(points[:, 0], points[:, 1], points[:, 2])
    origin = geodetic_to_ecef(center[0], center[1], center[2])
    enu = (ecef - origin) @ basis
    normals_ecef = _vertex_normals_enu(enu, faces) @ basis.T

    corners = faces.reshape(-1)
    count = len(corners)
    position = (points[corners] - center).astype("<f4")
    normal = normals_ecef[corners].astype("<f4")
    if part.corner_uv is not None and len(part.corner_uv) == count:
        uv = np.asarray(part.corner_uv, dtype=np.float64)[:, :2].copy()
        if flip_v:
            uv[:, 1] = 1.0 - uv[:, 1]
        uv = uv.astype("<f4")
    elif part.uv is not None and len(part.uv) == len(points):
        uv = np.asarray(part.uv, dtype=np.float64)[corners, :2].copy()
        if flip_v:
            uv[:, 1] = 1.0 - uv[:, 1]  # OBJ (origin bottom-left) -> I3S (origin top-left)
        uv = uv.astype("<f4")
    else:
        uv = np.zeros((count, 2), dtype="<f4")
    if part.colors is not None and len(part.colors) == len(points):
        rgb = np.asarray(part.colors)[corners]
        color = np.full((count, 4), 255, dtype=np.uint8)
        color[:, : min(4, rgb.shape[1])] = rgb[:, :4]
    else:
        color = np.full((count, 4), 255, dtype=np.uint8)

    header = np.array([count, 1], dtype="<u4")
    feature_id = np.array([0], dtype="<u8")
    face_range = np.array([0, max(0, len(faces) - 1)], dtype="<u4")
    data = b"".join([
        header.tobytes(), position.tobytes(), normal.tobytes(), uv.tobytes(), color.tobytes(),
        feature_id.tobytes(), face_range.tobytes(),
    ])
    return data, count, len(faces)


# ---------------------------------------------------------------------------
# Layer documents
# ---------------------------------------------------------------------------

def _vertical(height_model: str) -> tuple[dict, dict]:
    if height_model == HEIGHT_ORTHOMETRIC:
        spatial = {"wkid": 4326, "latestWkid": 4326, "vcsWkid": 3855, "latestVcsWkid": 3855}
        info = {"heightModel": "gravity_related_height", "vertCRS": "EGM2008_Geoid", "heightUnit": "meter"}
    else:
        spatial = {"wkid": 4326, "latestWkid": 4326, "vcsWkid": 115700, "latestVcsWkid": 115700}
        info = {"heightModel": "ellipsoidal", "vertCRS": "WGS_84", "heightUnit": "meter"}
    return spatial, info


def scene_layer_document(name: str, extent: list[float], height_model: str, nodes_per_page: int,
                         textured: bool = True) -> dict:
    spatial, height_info = _vertical(height_model)
    crs_url = "http://www.opengis.net/def/crs/EPSG/0/4326"
    return {
        "id": 0,
        "version": "{" + str(uuid.uuid4()).upper() + "}",
        "name": name,
        "alias": name,
        "layerType": "IntegratedMesh",
        "description": "Created with ROLAVI GEOStudio",
        "copyrightText": "",
        "capabilities": ["View", "Query"],
        "ZFactor": 1.0,
        "disablePopup": True,
        "spatialReference": spatial,
        "heightModelInfo": height_info,
        "store": {
            "id": "{" + str(uuid.uuid4()).upper() + "}",
            "profile": "meshpyramids",
            "version": I3S_VERSION,
            "resourcePattern": ["Geometry", "Texture"] if textured else ["Geometry"],
            "rootNode": "./nodes/root",
            "extent": extent,
            "indexCRS": crs_url,
            "vertexCRS": crs_url,
            "normalReferenceFrame": "earth-centered",
            "attributeEncoding": "application/octet-stream; version=1.6",
            "textureEncoding": ["image/jpeg"],
            "lodType": "MeshPyramid",
            "lodModel": "node-switching",
            "defaultGeometrySchema": {
                "geometryType": "triangles",
                "header": [
                    {"property": "vertexCount", "type": "UInt32"},
                    {"property": "featureCount", "type": "UInt32"},
                ],
                "topology": "PerAttributeArray",
                "ordering": ["position", "normal", "uv0", "color"],
                "vertexAttributes": {
                    "position": {"valueType": "Float32", "valuesPerElement": 3},
                    "normal": {"valueType": "Float32", "valuesPerElement": 3},
                    "uv0": {"valueType": "Float32", "valuesPerElement": 2},
                    "color": {"valueType": "UInt8", "valuesPerElement": 4},
                },
                "featureAttributeOrder": ["id", "faceRange"],
                "featureAttributes": {
                    "id": {"valueType": "UInt64", "valuesPerElement": 1},
                    "faceRange": {"valueType": "UInt32", "valuesPerElement": 2},
                },
            },
        },
        "nodePages": {"nodesPerPage": nodes_per_page, "lodSelectionMetricType": "maxScreenThresholdSQ"},
        "materialDefinitions": [
            {
                "doubleSided": True,
                "pbrMetallicRoughness": {
                    "baseColorTexture": {"textureSetDefinitionId": 0, "texCoord": 0},
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
            },
            {
                "doubleSided": True,
                "pbrMetallicRoughness": {"baseColorFactor": [1.0, 1.0, 1.0, 1.0], "metallicFactor": 0.0, "roughnessFactor": 1.0},
            },
        ],
        "textureSetDefinitions": [{"formats": [{"name": "0", "format": "jpg"}]}],
        "geometryDefinitions": [
            {
                "geometryBuffers": [
                    {
                        "offset": 8,
                        "position": {"type": "Float32", "component": 3},
                        "normal": {"type": "Float32", "component": 3},
                        "uv0": {"type": "Float32", "component": 2},
                        "color": {"type": "UInt8", "component": 4},
                        "featureId": {"type": "UInt64", "component": 1, "binding": "per-feature"},
                        "faceRange": {"type": "UInt32", "component": 2, "binding": "per-feature"},
                    }
                ]
            }
        ],
    }


def _write_hash_index(archive: zipfile.ZipFile) -> None:
    """``@specialIndexFileHASH128@``: MD5 of every path + offset of its zip entry, sorted as ArcGIS Pro does."""
    import hashlib
    import struct as _struct

    entries = []
    for info in archive.infolist():
        key = hashlib.md5(info.filename.encode("utf-8")).digest()
        entries.append((key, int(info.header_offset)))
    entries.sort(key=lambda item: (_struct.unpack("<Q", item[0][:8])[0], _struct.unpack("<Q", item[0][8:])[0]))
    archive.writestr("@specialIndexFileHASH128@", b"".join(key + _struct.pack("<Q", offset) for key, offset in entries))


def _gz(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=6, mtime=0)


def _json_gz(obj) -> bytes:
    return _gz(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


# ---------------------------------------------------------------------------
# Package
# ---------------------------------------------------------------------------

@dataclass
class SlpkResult:
    path: Path
    nodes: int
    vertices: int
    triangles: int
    textures: int
    extent: list[float]
    size_bytes: int


#: lodThreshold of mesh nodes (metric maxScreenThresholdSQ): screen area in px² of the node's
#: bounding sphere, pi * 250² as in Esri's integrated meshes; a node is replaced by its
#: children when it covers more than that on screen.
LEAF_LOD_THRESHOLD = math.pi * 250.0 ** 2


def write_mesh_node(archive: zipfile.ZipFile, number: int, part: MeshPart, parent: int | None, lod_threshold: float,
                    max_texture: int, jpeg_quality: int, flip_v: bool = True,
                    children: list[int] | None = None) -> tuple[dict, int, int, bool]:
    """Geometry (+ texture) of one node into the archive; returns (node, vertices, triangles, textured)."""
    points = np.asarray(part.lonlat_h, dtype=np.float64)
    obb, center, basis = _obb(points)
    data, vertex_count, face_count = geometry_buffer(part, center, basis, flip_v=flip_v)
    archive.writestr(f"nodes/{number}/geometries/0.bin.gz", _gz(data))
    mesh: dict = {"geometry": {"definition": 0, "resource": number, "vertexCount": vertex_count, "featureCount": 1}}
    textured = part.image is not None and (part.uv is not None or part.corner_uv is not None)
    if textured:
        jpeg, width, height = _texture_jpeg(part.image, max_texture, jpeg_quality)
        archive.writestr(f"nodes/{number}/textures/0.jpg", jpeg)
        mesh["material"] = {"definition": 0, "resource": number, "texelCountHint": int(width * height)}
    else:
        mesh["material"] = {"definition": 1, "resource": number}
    node = {"index": number, "lodThreshold": float(lod_threshold), "obb": obb, "children": list(children or []), "mesh": mesh}
    if parent is not None:
        node["parentIndex"] = int(parent)
    return node, vertex_count, face_count, textured


def write_slpk(
    path: str | Path,
    parts: list[MeshPart],
    layer_name: str,
    height_model: str = HEIGHT_ELLIPSOIDAL,
    max_texture: int = 4096,
    jpeg_quality: int = 90,
    flip_v: bool = True,
    progress: Progress | None = None,
) -> SlpkResult:
    """Write ``parts`` as an IntegratedMesh SLPK (I3S 1.7, WGS84)."""
    parts = [p for p in parts if p.faces is not None and len(p.faces) and len(p.lonlat_h)]
    if not parts:
        raise ValueError("The model has no triangles to write.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    nodes_per_page = 64

    all_points = np.concatenate([np.asarray(p.lonlat_h, dtype=np.float64) for p in parts])
    if not np.isfinite(all_points).all():
        raise ValueError("The model has coordinates that are not numbers.")
    if np.abs(all_points[:, 0]).max() > 180.0 or np.abs(all_points[:, 1]).max() > 90.0:
        raise ValueError("The coordinates are not longitude/latitude: check the coordinate system of the project.")
    extent = [
        float(all_points[:, 0].min()), float(all_points[:, 1].min()),
        float(all_points[:, 0].max()), float(all_points[:, 1].max()),
    ]
    root_obb, _root_center, _root_basis = _obb(all_points)

    nodes: list[dict] = [{
        "index": 0,
        "lodThreshold": 1.0,  # the root has no geometry: its children are shown at once
        "obb": root_obb,
        "children": list(range(1, len(parts) + 1)),
    }]
    total_vertices = total_triangles = textures = 0

    tmp = path.with_suffix(path.suffix + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for number, part in enumerate(parts, start=1):
            if progress is not None:
                progress((number - 1) / len(parts), f"Writing SLPK node {number} of {len(parts)}...")
            node, vertex_count, face_count, textured = write_mesh_node(
                archive, number, part, 0, LEAF_LOD_THRESHOLD, max_texture, jpeg_quality, flip_v
            )
            nodes.append(node)
            textures += int(textured)
            total_vertices += vertex_count
            total_triangles += face_count

        for page in range(0, len(nodes), nodes_per_page):
            archive.writestr(f"nodepages/{page // nodes_per_page}.json.gz", _json_gz({"nodes": nodes[page:page + nodes_per_page]}))
        archive.writestr("3dSceneLayer.json.gz", _json_gz(
            scene_layer_document(layer_name, extent, height_model, nodes_per_page, textured=textures > 0)
        ))
        archive.writestr("metadata.json", json.dumps({
            "folderPattern": "basic",
            "archiveCompressionType": "STORE",
            "resourceCompressionType": "GZIP",
            "I3SVersion": I3S_VERSION,
            "nodeCount": len(nodes),
        }, indent=1))
        _write_hash_index(archive)
    tmp.replace(path)
    if progress is not None:
        progress(1.0, "SLPK written")
    return SlpkResult(
        path=path,
        nodes=len(nodes),
        vertices=total_vertices,
        triangles=total_triangles,
        textures=textures,
        extent=extent,
        size_bytes=path.stat().st_size,
    )


# ---------------------------------------------------------------------------
# Reading back (checks; the SLPK import of GEOStudio builds on this)
# ---------------------------------------------------------------------------

def read_layer(path: str | Path) -> tuple[dict, list[dict]]:
    """Scene layer document and nodes of an SLPK."""
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        layer = json.loads(_read_member(archive, names, "3dSceneLayer.json"))
        nodes: list[dict] = []
        page = 0
        while True:
            name = f"nodepages/{page}.json"
            if name not in names and name + ".gz" not in names:
                break
            nodes.extend(json.loads(_read_member(archive, names, name)).get("nodes", []))
            page += 1
    return layer, nodes


def _read_member(archive: zipfile.ZipFile, names: set, name: str) -> bytes:
    if name + ".gz" in names:
        return gzip.decompress(archive.read(name + ".gz"))
    return archive.read(name)


def read_legacy_geometry(path: str | Path, resource: int) -> dict:
    """Arrays of an uncompressed geometry buffer (position offsets, normals, uv0, colour)."""
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        data = _read_member(archive, names, f"nodes/{resource}/geometries/0.bin")
    count, features = np.frombuffer(data[:8], dtype="<u4")
    offset = 8
    out = {}
    for key, dtype, comps in (("position", "<f4", 3), ("normal", "<f4", 3), ("uv0", "<f4", 2), ("color", "u1", 4)):
        size = int(count) * comps * np.dtype(dtype).itemsize
        out[key] = np.frombuffer(data[offset:offset + size], dtype=dtype).reshape(int(count), comps)
        offset += size
    out["feature_ids"] = np.frombuffer(data[offset:offset + 8 * int(features)], dtype="<u8")
    offset += 8 * int(features)
    out["face_ranges"] = np.frombuffer(data[offset:offset + 8 * int(features)], dtype="<u4").reshape(-1, 2)
    out["vertex_count"] = int(count)
    return out


# ---------------------------------------------------------------------------
# Large untextured meshes (e.g. PLY with vertex colours) -> parts of bounded size
# ---------------------------------------------------------------------------

def tile_mesh(lonlat_h: np.ndarray, faces: np.ndarray, colors: np.ndarray | None, metric_xy: np.ndarray,
              max_faces: int = 250_000, name: str = "tile") -> list[MeshPart]:
    """Split a mesh in a regular XY grid of tiles (by face centroid); vertices are re-indexed per tile."""
    faces = np.asarray(faces, dtype=np.int64)
    count = len(faces)
    tiles = max(1, int(math.ceil(count / float(max_faces))))
    if tiles == 1:
        return [MeshPart(lonlat_h=lonlat_h, faces=faces, colors=colors, name=name)]
    centroid = metric_xy[faces].mean(axis=1)
    low = centroid.min(axis=0)
    span = np.maximum(centroid.max(axis=0) - low, 1e-9)
    nx = max(1, int(round(math.sqrt(tiles * span[0] / span[1]))))
    ny = max(1, int(math.ceil(tiles / nx)))
    cx = np.minimum((centroid[:, 0] - low[0]) / span[0] * nx, nx - 1).astype(np.int64)
    cy = np.minimum((centroid[:, 1] - low[1]) / span[1] * ny, ny - 1).astype(np.int64)
    cell = cy * nx + cx
    parts = []
    for value in np.unique(cell):
        tile_faces = faces[cell == value]
        used, inverse = np.unique(tile_faces.reshape(-1), return_inverse=True)
        parts.append(MeshPart(
            lonlat_h=lonlat_h[used],
            faces=inverse.reshape(-1, 3),
            colors=None if colors is None else colors[used],
            name=f"{name}_{len(parts)}",
        ))
    return parts


# ---------------------------------------------------------------------------
# Heavy textured meshes: level-of-detail pyramid
# ---------------------------------------------------------------------------

def _face_centroids_xy(metric: np.ndarray, faces: np.ndarray, block: int = 2_000_000) -> np.ndarray:
    out = np.empty((len(faces), 2), dtype=np.float64)
    xy = metric[:, :2]
    for start in range(0, len(faces), block):
        f = faces[start:start + block]
        out[start:start + block] = (xy[f[:, 0]] + xy[f[:, 1]] + xy[f[:, 2]]) / 3.0
    return out


def split_faces(centroids: np.ndarray, max_faces: int, max_depth: int = 24) -> dict:
    """Quadtree (median splits) of face indices: ``{"faces": idx}`` leaves or ``{"children": [...]}``."""
    def build(index: np.ndarray, depth: int) -> dict:
        if len(index) <= max_faces or depth >= max_depth:
            return {"faces": index}
        c = centroids[index]
        mx = np.median(c[:, 0])
        left = c[:, 0] <= mx
        children = []
        for half in (left, ~left):
            sub = index[half]
            if not len(sub):
                continue
            my = np.median(centroids[sub, 1])
            low = centroids[sub, 1] <= my
            for quarter in (low, ~low):
                if quarter.any():
                    children.append(build(sub[quarter], depth + 1))
        if len(children) == 1:  # identical centroids: cannot split further
            return children[0] if "faces" in children[0] else {"faces": index}
        return {"children": children}

    return build(np.arange(len(centroids), dtype=np.int64), 0)


def _surface_area(metric: np.ndarray, faces: np.ndarray, block: int = 1_000_000) -> float:
    area = 0.0
    for start in range(0, len(faces), block):
        tri = metric[faces[start:start + block]]
        area += 0.5 * float(np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1).sum())
    return area


def cluster_mesh(metric: np.ndarray, lonlat_h: np.ndarray, colors: np.ndarray, faces: np.ndarray,
                 target_faces: int, compact: bool = True) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Vertex clustering (3D grid) down to about ``target_faces`` triangles; colours are averaged.

    ``compact=False`` skips dropping unused vertices first (a whole model: saves memory and time).
    """
    if compact:
        used, inverse = np.unique(faces.reshape(-1), return_inverse=True)
        metric, lonlat_h, colors = metric[used], lonlat_h[used], colors[used]
        faces = inverse.reshape(-1, 3)
    colors = np.asarray(colors, dtype=np.float64)
    if len(faces) <= target_faces:
        used, inverse = np.unique(faces.reshape(-1), return_inverse=True)
        return metric[used], lonlat_h[used], np.clip(np.rint(colors[used]), 0, 255).astype(np.uint8), inverse.reshape(-1, 3)
    area = _surface_area(metric, faces)
    low = metric.min(axis=0)
    span = np.maximum(metric.max(axis=0) - low, 1e-9)
    cell = max(math.sqrt(2.0 * max(area, 1e-9) / max(1, target_faces)), float(span.max()) / 4096.0, 1e-6)
    result = None
    for _attempt in range(6):
        dims = np.floor(span / cell).astype(np.int64) + 1
        key3 = np.floor((metric - low) / cell).astype(np.int64)
        key = (key3[:, 2] * dims[1] + key3[:, 1]) * dims[0] + key3[:, 0]
        _cells, member = np.unique(key, return_inverse=True)
        count = np.bincount(member).astype(np.float64)
        centre = np.column_stack([np.bincount(member, weights=metric[:, k]) / count for k in range(3)])
        geo = np.column_stack([np.bincount(member, weights=lonlat_h[:, k]) / count for k in range(3)])
        rgb = np.column_stack([np.bincount(member, weights=colors[:, k]) / count for k in range(3)])
        new_faces = member[faces]
        keep = (new_faces[:, 0] != new_faces[:, 1]) & (new_faces[:, 1] != new_faces[:, 2]) & (new_faces[:, 0] != new_faces[:, 2])
        new_faces = new_faces[keep]
        if len(new_faces):
            _rows, first = np.unique(np.sort(new_faces, axis=1), axis=0, return_index=True)
            new_faces = new_faces[np.sort(first)]
        result = (centre, geo, np.clip(np.rint(rgb), 0, 255).astype(np.uint8), new_faces)
        if len(new_faces) <= 1.35 * target_faces or len(new_faces) < 64:
            break
        cell *= math.sqrt(len(new_faces) / float(target_faces))
    centre, geo, rgb, new_faces = result
    used, inverse = np.unique(new_faces.reshape(-1), return_inverse=True)
    return centre[used], geo[used], rgb[used], inverse.reshape(-1, 3)


def vertex_colors_from_textures(faces: np.ndarray, faces_uv: np.ndarray | None, uvs: np.ndarray,
                                faces_material: np.ndarray, vertex_count: int, texture_images: dict,
                                default=(190, 190, 190), block: int = 2_000_000,
                                max_size: int = 8192) -> np.ndarray:
    """One colour per vertex for simplified meshes: the mean texture colour of its triangles.

    Each triangle is sampled at the centre of its texture patch (full resolution, up to
    ``max_size`` px), then every vertex takes the mean of the triangles around it. Taking
    one corner's texel instead (and from a small thumbnail) paints squares on atlases made of
    small patches (ROLAVIScan: 2 x 2 px per triangle).
    """
    colors = np.empty((vertex_count, 3), dtype=np.uint8)
    colors[:] = default
    if faces_uv is None or not texture_images:
        return colors
    faces_material = np.asarray(faces_material)
    sums = np.zeros((vertex_count, 3), dtype=np.float64)
    counts = np.zeros(vertex_count, dtype=np.float64)
    for material, image in texture_images.items():
        if image is None:
            continue
        rows = np.flatnonzero(faces_material == material)
        if not len(rows):
            continue
        img = image.convert("RGB")
        if max(img.size) > max_size:
            img = img.copy()
            img.thumbnail((max_size, max_size))
        pixels = np.asarray(img)
        h, w = pixels.shape[:2]
        for start in range(0, len(rows), block):  # by blocks: no copies of every corner of a heavy model
            part = rows[start:start + block]
            corner_t = faces_uv[part]
            good = (corner_t >= 0).all(axis=1)
            if not good.any():
                continue
            part, corner_t = part[good], corner_t[good]
            uv = uvs[corner_t].mean(axis=1)                       # centre of the triangle's texture patch
            px = np.clip((np.mod(uv[:, 0], 1.0) * w).astype(np.int64), 0, w - 1)
            py = np.clip(((1.0 - np.mod(uv[:, 1], 1.0)) * h).astype(np.int64), 0, h - 1)
            face_rgb = pixels[py, px, :3].astype(np.float64)
            corner_v = faces[part].reshape(-1)
            for channel in range(3):
                sums[:, channel] += np.bincount(corner_v, weights=np.repeat(face_rgb[:, channel], 3),
                                                minlength=vertex_count)
            counts += np.bincount(corner_v, minlength=vertex_count)
    seen = counts > 0
    colors[seen] = np.clip(np.rint(sums[seen] / counts[seen, None]), 0, 255).astype(np.uint8)
    return colors


def _crop_texture(image, corner_uv: np.ndarray, pad: int = 2):
    """The part of the texture used by a tile, and the uv remapped to it (OBJ convention)."""
    u = corner_uv[:, 0]
    v = corner_uv[:, 1]
    if u.min() < -0.001 or u.max() > 1.001 or v.min() < -0.001 or v.max() > 1.001:
        return image, corner_uv  # repeating texture: keep it whole
    width, height = image.size
    x0 = max(0, int(math.floor(u.min() * width)) - pad)
    x1 = min(width, int(math.ceil(u.max() * width)) + pad)
    y0 = max(0, int(math.floor((1.0 - v.max()) * height)) - pad)
    y1 = min(height, int(math.ceil((1.0 - v.min()) * height)) + pad)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return image, corner_uv
    crop = image.crop((x0, y0, x1, y1))
    new_uv = np.empty_like(corner_uv, dtype=np.float64)
    new_uv[:, 0] = (u * width - x0) / float(x1 - x0)
    new_uv[:, 1] = 1.0 - ((1.0 - v) * height - y0) / float(y1 - y0)
    return crop, new_uv


PACK_WHEN_USED_BELOW = 0.35   # repack a tile's texture when it uses less than this share of its crop


def _tile_texture(image, corner_uv: np.ndarray, max_size: int):
    """The texture of a leaf tile: the crop of the atlas it uses, or, when the tile's patches are
    scattered over the atlas (the crop would be mostly unused and get shrunk to ``max_size``),
    only its patches copied into a compact atlas. Returns (image, corner_uv)."""
    crop, crop_uv = _crop_texture(image, corner_uv)
    u = corner_uv[:, 0]
    v = corner_uv[:, 1]
    if len(corner_uv) < 3 or u.min() < -0.001 or u.max() > 1.001 or v.min() < -0.001 or v.max() > 1.001:
        return crop, crop_uv
    width, height = image.size
    px = (corner_uv[:, 0] * width).reshape(-1, 3)
    py = ((1.0 - corner_uv[:, 1]) * height).reshape(-1, 3)
    x0 = np.clip(np.floor(px.min(axis=1)).astype(np.int64) - 1, 0, width - 1)
    x1 = np.clip(np.ceil(px.max(axis=1)).astype(np.int64) + 1, 1, width)
    y0 = np.clip(np.floor(py.min(axis=1)).astype(np.int64) - 1, 0, height - 1)
    y1 = np.clip(np.ceil(py.max(axis=1)).astype(np.int64) + 1, 1, height)
    x1 = np.maximum(x1, x0 + 1)
    y1 = np.maximum(y1, y0 + 1)
    rects, which = np.unique(np.column_stack([x0, y0, x1, y1]), axis=0, return_inverse=True)
    which = np.asarray(which).reshape(-1)
    rw = rects[:, 2] - rects[:, 0]
    rh = rects[:, 3] - rects[:, 1]
    used = float((rw * rh).sum())
    crop_area = float(crop.size[0] * crop.size[1])
    fits = max(crop.size) <= max_size and crop.size == (_power_of_two_at_most(crop.size[0], max_size),
                                                         _power_of_two_at_most(crop.size[1], max_size))
    if fits or used >= PACK_WHEN_USED_BELOW * crop_area or len(rects) > 4_000_000:
        return crop, crop_uv
    # shelf packing: tallest first, rows of the atlas width
    atlas_w = 1 << int(math.ceil(math.log2(max(int(rw.max()), math.sqrt(used * 1.15), 2.0))))
    order = np.argsort(-rh, kind="stable")
    ax = np.zeros(len(rects), dtype=np.int64)
    ay = np.zeros(len(rects), dtype=np.int64)
    start, y = 0, 0
    while start < len(order):
        widths = np.cumsum(rw[order[start:]])
        count = max(1, int(np.searchsorted(widths, atlas_w, side="right")))
        row = order[start:start + count]
        ax[row] = np.concatenate([[0], widths[:count - 1]])
        ay[row] = y
        y += int(rh[row].max())
        start += count
    atlas_h = 1 << int(math.ceil(math.log2(max(y, 2))))
    pixels = np.asarray(image.convert("RGB"))
    atlas = np.zeros((atlas_h, atlas_w, 3), dtype=np.uint8)
    for (a, b, c, d), x, yy in zip(rects.tolist(), ax.tolist(), ay.tolist()):
        atlas[yy:yy + d - b, x:x + c - a] = pixels[b:d, a:c]
    corner_rect = np.repeat(which, 3)
    new_uv = np.empty_like(corner_uv, dtype=np.float64)
    new_uv[:, 0] = (px.reshape(-1) - rects[corner_rect, 0] + ax[corner_rect]) / float(atlas_w)
    new_uv[:, 1] = 1.0 - (py.reshape(-1) - rects[corner_rect, 1] + ay[corner_rect]) / float(atlas_h)
    from PIL import Image
    return Image.fromarray(atlas), new_uv


def write_mesh_pyramid_slpk(
    path: str | Path,
    lonlat_h: np.ndarray,
    metric: np.ndarray,
    faces: np.ndarray,
    faces_uv: np.ndarray | None,
    uvs: np.ndarray,
    faces_material: np.ndarray,
    texture_loader: Callable[[int], object] | None,
    layer_name: str,
    height_model: str = HEIGHT_ELLIPSOIDAL,
    vertex_colors: np.ndarray | None = None,
    max_faces: int = 200_000,
    max_texture: int = 2048,
    jpeg_quality: int = 85,
    progress: Progress | None = None,
) -> SlpkResult:
    """Full-resolution textured mesh as an IntegratedMesh pyramid (I3S 1.7, WGS84).

    Leaves: tiles of at most ``max_faces`` triangles with the part of the texture they
    use. Parents: the same area simplified (vertex clustering) to about ``max_faces / 2``
    triangles with vertex colours from the texture. ``texture_loader(material)`` returns a
    PIL image (or None); ``metric`` are metric coordinates (projected x, y, height) of the
    vertices, used for tiling and simplification.
    """
    faces = np.asarray(faces, dtype=np.int64)
    if not len(faces):
        raise ValueError("The model has no triangles to write.")
    lonlat_h = np.asarray(lonlat_h, dtype=np.float64)
    if np.abs(lonlat_h[:, 0]).max() > 180.0 or np.abs(lonlat_h[:, 1]).max() > 90.0:
        raise ValueError("The coordinates are not longitude/latitude: check the coordinate system of the project.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    faces_material = np.asarray(faces_material, dtype=np.int64) if faces_material is not None else np.full(len(faces), -1)
    report = progress or (lambda fraction, text: None)

    report(0.0, "Tiling the mesh...")
    tree = split_faces(_face_centroids_xy(metric, faces), max_faces)
    materials = sorted(set(int(m) for m in np.unique(faces_material)))
    images = {}
    if texture_loader is not None and faces_uv is not None:
        for material in materials:
            if material >= 0:
                images[material] = texture_loader(material)
    if vertex_colors is None:
        report(0.03, "Colours of the simplified levels...")
        vertex_colors = vertex_colors_from_textures(faces, faces_uv, uvs, faces_material, len(lonlat_h), images)
    vertex_colors = np.asarray(vertex_colors)[:, :3]
    if vertex_colors.dtype != np.uint8:
        scale = 255.0 if vertex_colors.max() <= 1.0 else 1.0
        vertex_colors = np.clip(np.rint(vertex_colors * scale), 0, 255).astype(np.uint8)

    # Node numbers (breadth first). A leaf of the quadtree becomes one node per material.
    nodes: list[dict] = []
    jobs_internal: list[tuple[int, dict]] = []
    jobs_leaf: list[tuple[int, np.ndarray, int]] = []

    def leaf_groups(leaf_faces: np.ndarray) -> list[tuple[int, np.ndarray]]:
        mats = faces_material[leaf_faces]
        return [(int(m), leaf_faces[mats == m]) for m in np.unique(mats)]

    single_leaf = "faces" in tree
    queue = [(tree, None)]
    if single_leaf:  # light model: a root without geometry, its parts shown at once
        nodes.append({"index": 0, "lodThreshold": 1.0, "children": []})
        queue = [(tree, 0)]
    while queue:
        item, parent = queue.pop(0)
        if "faces" in item:
            for material, group in leaf_groups(item["faces"]):
                number = len(nodes)
                nodes.append({"index": number, "parentIndex": parent, "lodThreshold": LEAF_LOD_THRESHOLD, "children": []})
                nodes[parent]["children"].append(number)
                jobs_leaf.append((number, group, material))
            continue
        number = len(nodes)
        node = {"index": number, "lodThreshold": LEAF_LOD_THRESHOLD, "children": []}
        if parent is not None:
            node["parentIndex"] = parent
            nodes[parent]["children"].append(number)
        nodes.append(node)
        item["_number"] = number
        jobs_internal.append((number, item))
        for child in item["children"]:
            queue.append((child, number))

    total_jobs = len(jobs_internal) + len(jobs_leaf)
    done_jobs = 0
    totals = {"vertices": 0, "triangles": 0, "textures": 0}
    target = max(1000, max_faces // 2)
    tmp = path.with_suffix(path.suffix + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:

        def store(number: int, part: MeshPart) -> None:
            nonlocal done_jobs
            info = nodes[number]
            node, vertex_count, face_count, textured = write_mesh_node(
                archive, number, part, info.get("parentIndex"), info["lodThreshold"], max_texture, jpeg_quality,
                children=info["children"],
            )
            nodes[number] = node
            totals["vertices"] += vertex_count
            totals["triangles"] += face_count
            totals["textures"] += int(textured)
            done_jobs += 1
            report(0.05 + 0.93 * done_jobs / max(1, total_jobs), f"Writing SLPK node {done_jobs} of {total_jobs}...")

        # Simplified parents, bottom-up (each from its children's simplified meshes).
        simplified: dict[int, tuple] = {}
        for number, item in reversed(jobs_internal):
            pieces = []
            for child in item["children"]:
                if "faces" in child:
                    leaf = child["faces"]
                    pieces.append((metric, lonlat_h, vertex_colors, faces[leaf]))
                else:
                    pieces.append(simplified.pop(child["_number"]))
            offset = 0
            m_list, g_list, c_list, f_list = [], [], [], []
            for p_metric, p_geo, p_rgb, p_faces in pieces:
                used, inverse = np.unique(p_faces.reshape(-1), return_inverse=True)
                m_list.append(p_metric[used])
                g_list.append(p_geo[used])
                c_list.append(p_rgb[used])
                f_list.append(inverse.reshape(-1, 3) + offset)
                offset += len(used)
            merged = cluster_mesh(np.concatenate(m_list), np.concatenate(g_list), np.concatenate(c_list),
                                  np.concatenate(f_list), target)
            simplified[number] = merged
            _m, geo, rgb, tri = merged
            store(number, MeshPart(lonlat_h=geo, faces=tri, colors=rgb, name=f"lod_{number}"))
        simplified.clear()

        # Full-resolution leaves, one material (texture) at a time.
        for material in sorted(set(job[2] for job in jobs_leaf)):
            image = images.get(material)
            for number, group, mat in jobs_leaf:
                if mat != material:
                    continue
                tile = faces[group]
                used, inverse = np.unique(tile.reshape(-1), return_inverse=True)
                local = inverse.reshape(-1, 3)
                part = MeshPart(lonlat_h=lonlat_h[used], faces=local, colors=vertex_colors[used], name=f"tile_{number}")
                if image is not None and faces_uv is not None:
                    corner_t = faces_uv[group].reshape(-1)
                    corner_uv = np.where(corner_t[:, None] >= 0, uvs[np.maximum(corner_t, 0)], 0.0).astype(np.float64)
                    crop, corner_uv = _tile_texture(image, corner_uv, max_texture)
                    # White vertex colours: the colour attribute multiplies the texture.
                    part.image, part.corner_uv, part.colors = crop, corner_uv, None
                store(number, part)
            images.pop(material, None)

        if single_leaf:
            nodes[0]["obb"] = _obb(lonlat_h)[0]
        extent_low = lonlat_h.min(axis=0)
        extent_high = lonlat_h.max(axis=0)
        extent = [float(extent_low[0]), float(extent_low[1]), float(extent_high[0]), float(extent_high[1])]
        nodes_per_page = 64
        for page in range(0, len(nodes), nodes_per_page):
            archive.writestr(f"nodepages/{page // nodes_per_page}.json.gz",
                             _json_gz({"nodes": nodes[page:page + nodes_per_page]}))
        archive.writestr("3dSceneLayer.json.gz", _json_gz(
            scene_layer_document(layer_name, extent, height_model, nodes_per_page, textured=totals["textures"] > 0)
        ))
        archive.writestr("metadata.json", json.dumps({
            "folderPattern": "basic",
            "archiveCompressionType": "STORE",
            "resourceCompressionType": "GZIP",
            "I3SVersion": I3S_VERSION,
            "nodeCount": len(nodes),
        }, indent=1))
        _write_hash_index(archive)
    tmp.replace(path)
    report(1.0, "SLPK written")
    return SlpkResult(
        path=path,
        nodes=len(nodes),
        vertices=totals["vertices"],
        triangles=totals["triangles"],
        textures=totals["textures"],
        extent=extent,
        size_bytes=path.stat().st_size,
    )


# ---------------------------------------------------------------------------
# Point cloud scene layer (I3S 2.0 PointCloud, LEPCC)
# ---------------------------------------------------------------------------

@dataclass
class PointCloudResult:
    path: Path
    nodes: int
    points: int
    stored_points: int
    extent: list[float]
    size_bytes: int


def _effective_area(metric: np.ndarray, voxel: float) -> float:
    """Surface covered by the points: occupied voxels x voxel face (works for walls and ground alike).

    The voxel grows until it holds a few points on average, so a sparse cloud is
    not counted as one tiny square per point.
    """
    if not len(metric):
        return 1.0
    origin = metric.min(axis=0)
    size = float(voxel)
    while True:
        cells = np.floor((metric - origin) / size).astype(np.int64)
        span = cells.max(axis=0) + 1
        keys = (cells[:, 0] * span[1] + cells[:, 1]) * span[2] + cells[:, 2]
        occupied = max(1, len(np.unique(keys)))
        if len(metric) / occupied >= 4.0 or size >= 2.0:
            return float(occupied) * size * size
        size *= 2.0


def _octree(metric: np.ndarray, leaf_max: int, max_depth: int = 16) -> list[dict]:
    """Nodes in breadth-first order: {"points": indices, "children": [...]} (children contiguous)."""
    root = {"points": np.arange(len(metric)), "low": metric.min(axis=0), "high": metric.max(axis=0), "depth": 0}
    order = [root]
    cursor = 0
    while cursor < len(order):
        node = order[cursor]
        cursor += 1
        node["children"] = []
        idx = node["points"]
        if len(idx) <= leaf_max or node["depth"] >= max_depth or float(np.max(node["high"] - node["low"])) < 0.05:
            continue
        mid = (node["low"] + node["high"]) / 2.0
        pts = metric[idx]
        code = (pts[:, 0] > mid[0]).astype(np.int64) | ((pts[:, 1] > mid[1]).astype(np.int64) << 1) | (
            (pts[:, 2] > mid[2]).astype(np.int64) << 2)
        for octant in range(8):
            chosen = idx[code == octant]
            if not len(chosen):
                continue
            low = np.where([octant & 1, octant & 2, octant & 4], mid, node["low"])
            high = np.where([octant & 1, octant & 2, octant & 4], node["high"], mid)
            child = {"points": chosen, "low": low, "high": high, "depth": node["depth"] + 1}
            node["children"].append(child)
            order.append(child)
    return order


def _elevation_statistics(z: np.ndarray) -> dict:
    counts, edges = np.histogram(z, bins=256)
    return {
        "attribute": "ELEVATION",
        "stats": {
            "min": float(z.min()), "max": float(z.max()), "avg": float(z.mean()), "stddev": float(z.std()),
            "count": float(len(z)), "sum": float(z.sum()), "variance": float(z.var()),
            "histogram": {"minimum": float(edges[0]), "maximum": float(edges[-1]), "counts": [int(c) for c in counts]},
        },
    }


def point_cloud_layer_document(name: str, extent: list[float], height_model: str, has_rgb: bool,
                               z_range: tuple[float, float]) -> dict:
    spatial, height_info = _vertical(height_model)
    attributes = [{"key": "1", "name": "ELEVATION", "encoding": "embedded-elevation"}]
    if has_rgb:
        attributes.append({
            "key": "4",
            "name": "RGB",
            "ordering": ["attributeValues"],
            "attributeValues": {"valueType": "UInt8", "valuesPerElement": 3},
            "encoding": "lepcc-rgb",
        })
    splat = {"type": "pointCloudSplatAlgorithm", "scaleFactor": 1.0}
    if has_rgb:
        renderer = {"type": "pointCloudRGBRenderer", "field": "RGB", "pointsPerInch": 25.0, "pointSizeAlgorithm": splat}
    else:
        z0, z1 = z_range
        step = (z1 - z0) / 3.0 if z1 > z0 else 1.0
        renderer = {
            "type": "pointCloudStretchRenderer", "field": "ELEVATION", "fieldTransformType": "none",
            "pointsPerInch": 25.0, "pointSizeAlgorithm": splat,
            "stops": [
                {"value": z0, "color": [88, 19, 252, 255]}, {"value": z0 + step, "color": [8, 252, 253, 255]},
                {"value": z0 + 2 * step, "color": [242, 254, 42, 255]}, {"value": z1, "color": [255, 43, 24, 255]},
            ],
        }
    return {
        "id": 0,
        "layerType": "PointCloud",
        "name": name,
        "alias": name,
        "desc": "Created with ROLAVI GEOStudio",
        "copyrightText": "",
        "capabilities": ["View"],
        "spatialReference": spatial,
        "store": {
            "id": "",
            "profile": "PointCloud",
            "version": "2.0",
            "extent": extent,
            "index": {
                "nodeVersion": 1,
                "boundingVolumeType": "obb",
                "nodesPerPage": 64,
                "lodSelectionMetricType": "density-threshold",
            },
            "defaultGeometrySchema": {
                "geometryType": "points",
                "header": [],
                "topology": "PerAttributeArray",
                "encoding": "lepcc-xyz",
                "vertexAttributes": {"position": {"valueType": "Float64", "valuesPerElement": 3}},
                "ordering": ["position"],
            },
        },
        "attributeStorageInfo": attributes,
        "drawingInfo": {"renderer": renderer},
        "elevationInfo": {"mode": "absoluteHeight", "offset": 0.0},
        "heightModelInfo": height_info,
    }


def write_point_cloud_slpk(
    path: str | Path,
    lonlat_h: np.ndarray,
    colors: np.ndarray | None,
    layer_name: str,
    height_model: str = HEIGHT_ELLIPSOIDAL,
    max_error_m: float = 0.005,
    leaf_points: int = 60_000,
    node_points: int = 30_000,
    voxel_m: float = 0.05,
    progress: Progress | None = None,
    seed: int = 0,
) -> PointCloudResult:
    """Write a point cloud as an I3S 2.0 PointCloud SLPK (LEPCC xyz + RGB), as ArcGIS Pro does.

    Leaves hold every point once; inner nodes hold a random sample of the points
    below them (``node_points``), so the cloud draws quickly from far away.
    """
    try:
        from . import lepcc
    except ImportError:  # loaded outside the package (tests)
        import lepcc  # type: ignore[no-redef]

    points = np.ascontiguousarray(lonlat_h, dtype=np.float64)
    if not len(points) or not np.isfinite(points).all():
        raise ValueError("The point cloud is empty or has coordinates that are not numbers.")
    if np.abs(points[:, 0]).max() > 180.0 or np.abs(points[:, 1]).max() > 90.0:
        raise ValueError("The coordinates are not longitude/latitude: check the coordinate system of the project.")
    rgb = None if colors is None else np.ascontiguousarray(np.asarray(colors)[:, :3], dtype=np.uint8)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    low = points.min(axis=0)
    high = points.max(axis=0)
    center = (low + high) / 2.0
    basis = enu_basis(center[0], center[1])
    metric = (geodetic_to_ecef(points[:, 0], points[:, 1], points[:, 2]) - geodetic_to_ecef(*center)) @ basis
    if progress is not None:
        progress(0.05, "Building the point cloud levels of detail...")
    tree = _octree(metric, leaf_points)
    rng = np.random.default_rng(seed)
    index_of = {id(node): i for i, node in enumerate(tree)}

    # effective areas: leaves from their voxels, parents = sum of children
    for node in reversed(tree):
        if node["children"]:
            node["area"] = float(sum(child["area"] for child in node["children"]))
        else:
            node["area"] = _effective_area(metric[node["points"]], voxel_m)

    error_deg = lepcc.meters_to_degrees(max_error_m)
    max_error = (error_deg, error_deg, float(max_error_m))
    page_nodes: list[dict] = []
    stored = 0
    tmp = path.with_suffix(path.suffix + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for number, node in enumerate(tree):
            if progress is not None and number % 4 == 0:
                progress(0.1 + 0.85 * number / len(tree), f"Writing point cloud node {number + 1} of {len(tree)}...")
            idx = node["points"]
            if node["children"] and len(idx) > node_points:
                idx = np.sort(rng.choice(idx, size=node_points, replace=False))
            node_points_lonlat = points[idx]
            blob, order = lepcc.encode_xyz(node_points_lonlat, max_error)
            archive.writestr(f"nodes/{number}/geometries/0.bin.pccxyz", blob)
            if rgb is not None:
                archive.writestr(f"nodes/{number}/attributes/4.bin.pccrgb", lepcc.encode_rgb(rgb[idx][order]))
            obb, _c, _b = _obb(node_points_lonlat if not node["children"] else points[node["points"]])
            children = node["children"]
            page_nodes.append({
                "resourceId": number,
                "obb": obb,
                "firstChild": index_of[id(children[0])] if children else 0,
                "childCount": len(children),
                "vertexCount": int(len(idx)),
                "lodThreshold": float(node["area"]),
            })
            stored += len(idx)
        for page in range(0, len(page_nodes), 64):
            archive.writestr(f"nodepages/{page // 64}.json.gz", _json_gz({"nodes": page_nodes[page:page + 64]}))
        archive.writestr("metadata.json", json.dumps({"nodeCount": len(page_nodes)}))
        extent = [float(low[0]), float(low[1]), float(high[0]), float(high[1])]
        archive.writestr("3dSceneLayer.json.gz", _json_gz(point_cloud_layer_document(
            layer_name, extent, height_model, rgb is not None, (float(low[2]), float(high[2]))
        )))
        archive.writestr("statistics/1.json.gz", _json_gz(_elevation_statistics(points[:, 2])))
        _write_hash_index(archive)
    tmp.replace(path)
    if progress is not None:
        progress(1.0, "Point cloud SLPK written")
    return PointCloudResult(
        path=path,
        nodes=len(page_nodes),
        points=len(points),
        stored_points=int(stored),
        extent=extent,
        size_bytes=path.stat().st_size,
    )
