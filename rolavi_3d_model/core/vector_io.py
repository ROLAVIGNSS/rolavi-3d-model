"""Polygons for the volumes (Qt-free): Shapefile (.shp or zipped), KML, KMZ, DXF, or drawn.

``read_polygons(path, backend)`` -> ``PolygonSet``: every ring of the file in its own CRS. The rings
are used with the even-odd rule (a ring inside another one is a hole), as ROLAVI Viewer does.

* Shapefile: Polygon / PolygonZ / PolygonM, and closed polylines; the CRS comes from the .prj.
* KML / KMZ: Polygon (outer and inner boundaries) and closed LineStrings; always WGS 84 lon / lat.
* DXF (ASCII): closed LWPOLYLINE / POLYLINE (bulges become arcs) and CIRCLE of the model space.
  A DXF does not say its CRS: ``needs_crs`` is True and the user chooses the datum (ellipsoid) and
  the UTM zone (``crs.datum_utm_code``).
"""

from __future__ import annotations

import math
import struct
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import crs as crs_mod
from .crs import CrsBackend, CrsError

POLYGON_EXTENSIONS = (".shp", ".zip", ".kml", ".kmz", ".dxf")
CURVE_STEP_DEG = 2.0
CIRCLE_POINTS = 180
CLOSE_TOLERANCE = 1e-6          # relative to the size of the ring


class VectorError(ValueError):
    """A message for the user."""


@dataclass
class PolygonSet:
    name: str
    crs: str                                   # "" while unknown (a DXF, a shapefile without .prj)
    rings: list[np.ndarray]                    # (n, 2) each, closed or not; even-odd rule
    source: str                                # "Shapefile", "KML", "KMZ", "DXF", "Drawn"
    notes: list[str] = field(default_factory=list)
    needs_crs: bool = False

    @property
    def count(self) -> int:
        """Polygons (outer rings) in the set."""
        return sum(1 for depth in self.depths() if depth % 2 == 0)

    def depths(self) -> list[int]:
        """How many other rings contain each ring (0 = outer, 1 = hole, 2 = island...)."""
        out = []
        for i, ring in enumerate(self.rings):
            x, y = float(ring[0, 0]), float(ring[0, 1])
            out.append(sum(1 for j, other in enumerate(self.rings) if j != i and point_in_ring(x, y, other)))
        return out

    def area(self) -> float:
        """Planar area (units of the CRS squared) with the even-odd rule."""
        total = 0.0
        for ring, depth in zip(self.rings, self.depths()):
            total += ring_area(ring) * (1.0 if depth % 2 == 0 else -1.0)
        return abs(total)

    def bounds(self) -> tuple[float, float, float, float]:
        pts = np.concatenate(self.rings)
        return float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max())

    def in_crs(self, backend: CrsBackend, target: str) -> "PolygonSet":
        """The same rings in another CRS."""
        if not self.crs:
            raise VectorError(f"{self.name}: choose its CRS first.")
        if crs_mod.normalize(self.crs) == crs_mod.normalize(target):
            return self
        rings = []
        for ring in self.rings:
            x, y = crs_mod.transform(backend, self.crs, target, ring[:, 0], ring[:, 1])
            rings.append(np.column_stack([x, y]))
        return PolygonSet(self.name, target, rings, self.source, list(self.notes), False)

    def describe(self, metres: bool = True) -> str:
        n = self.count
        what = f"{n} polygon{'s' if n != 1 else ''}"
        holes = len(self.rings) - n
        if holes:
            what += f" ({holes} hole{'s' if holes != 1 else ''})"
        if metres and self.crs:
            area = self.area()
            what += f" · {area / 10000.0:,.3f} ha" if area >= 10000 else f" · {area:,.1f} m²"
        return f"{what} · {self.source}"


def ring_area(ring: np.ndarray) -> float:
    x, y = ring[:, 0], ring[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def point_in_ring(x: float, y: float, ring: np.ndarray) -> bool:
    xs, ys = ring[:, 0], ring[:, 1]
    xj, yj = np.roll(xs, 1), np.roll(ys, 1)
    cross = (ys > y) != (yj > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        at = (xj - xs) * (y - ys) / (yj - ys) + xs
    return bool(np.count_nonzero(cross & (x < at)) % 2)


def _clean_ring(points) -> np.ndarray | None:
    """(n, 2) without the repeated closing point and duplicates; None if it is not a polygon."""
    arr = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    arr = arr[np.isfinite(arr).all(axis=1)]
    if len(arr) < 3:
        return None
    keep = np.ones(len(arr), bool)
    keep[1:] = np.any(np.abs(np.diff(arr, axis=0)) > 0, axis=1)
    arr = arr[keep]
    size = float(np.ptp(arr, axis=0).max()) or 1.0
    if len(arr) >= 2 and float(np.abs(arr[0] - arr[-1]).max()) <= CLOSE_TOLERANCE * size:
        arr = arr[:-1]                      # the closing point repeats the first one
    if len(arr) < 3 or ring_area(arr) <= 0:
        return None
    return arr


def _is_closed(points) -> bool:
    arr = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(arr) < 4:
        return False
    size = float(np.ptp(arr, axis=0).max()) or 1.0
    return float(np.hypot(*(arr[0] - arr[-1]))) <= CLOSE_TOLERANCE * size


def _unique(rings: list[np.ndarray]) -> list[np.ndarray]:
    """Without repeated rings (the same outline twice would cancel itself with the even-odd rule)."""
    seen, out = set(), []
    for ring in rings:
        start = int(np.lexsort((ring[:, 1], ring[:, 0]))[0])
        key = np.round(np.roll(ring, -start, axis=0), 6)
        keys = {key.tobytes(), np.vstack([key[:1], key[1:][::-1]]).tobytes()}
        if seen & keys:
            continue
        seen |= keys
        out.append(ring)
    return out


def drawn(points, crs: str, name: str = "Drawn polygon") -> PolygonSet:
    ring = _clean_ring(points)
    if ring is None:
        raise VectorError("A polygon needs at least three different points.")
    return PolygonSet(name, crs, [ring], "Drawn")


# =============================================================================
# Shapefile
# =============================================================================
def _shp_rings(data: bytes) -> tuple[list[np.ndarray], int, int]:
    """(rings, polygons read, closed polylines read) of a .shp."""
    if len(data) < 100 or struct.unpack(">i", data[0:4])[0] != 9994:
        raise VectorError("The .shp file is not valid.")
    rings: list[np.ndarray] = []
    polygons = lines = 0
    pos = 100
    while pos + 8 <= len(data):
        _num, words = struct.unpack(">2i", data[pos:pos + 8])
        pos += 8
        content = data[pos:pos + words * 2]
        pos += words * 2
        if len(content) < 44:
            continue
        shape = struct.unpack("<i", content[0:4])[0]
        if shape not in (3, 5, 13, 15, 23, 25):
            continue
        parts_n, points_n = struct.unpack("<2i", content[36:44])
        parts = list(struct.unpack(f"<{parts_n}i", content[44:44 + 4 * parts_n])) + [points_n]
        off = 44 + 4 * parts_n
        xy = np.frombuffer(content, dtype="<f8", count=2 * points_n, offset=off).reshape(-1, 2)
        for a, b in zip(parts[:-1], parts[1:]):
            part = xy[a:b]
            if shape in (3, 13, 23) and not _is_closed(part):
                continue
            ring = _clean_ring(part)
            if ring is not None:
                rings.append(ring)
                if shape in (5, 15, 25):
                    polygons += 1
                else:
                    lines += 1
    return rings, polygons, lines


def _crs_from_prj(text: str, backend: CrsBackend | None) -> str:
    if not text.strip() or backend is None:
        return ""
    try:
        return backend.from_wkt(text)
    except CrsError:
        return ""


def _read_shapefile(path: Path, backend: CrsBackend | None) -> PolygonSet:
    notes: list[str] = []
    if path.suffix.lower() == ".zip":
        try:
            zf = zipfile.ZipFile(str(path))
        except zipfile.BadZipFile:
            raise VectorError(f"{path.name} is not a valid .zip.")
        with zf:
            names = {n.lower(): n for n in zf.namelist() if "__MACOSX" not in n and not n.endswith("/")}
            shps = sorted(n for n in names if n.endswith(".shp"))
            if not shps:
                kml = sorted(n for n in names if n.endswith(".kml"))
                if kml:
                    return _from_kml_bytes(zf.read(names[kml[0]]), path.stem, "KMZ")
                raise VectorError(f"No shapefile (.shp) was found inside {path.name}.")
            if len(shps) > 1:
                notes.append(f"{len(shps)} shapefiles in the zip: {Path(names[shps[0]]).name} was used.")
            stem = shps[0][:-4]
            shp = zf.read(names[shps[0]])
            prj = zf.read(names[stem + ".prj"]).decode("latin-1", "replace") if stem + ".prj" in names else ""
            name = Path(names[shps[0]]).stem
    else:
        shp = path.read_bytes()
        prj_path = next((p for p in (path.with_suffix(".prj"), path.with_suffix(".PRJ")) if p.is_file()), None)
        prj = prj_path.read_text("latin-1", "replace") if prj_path else ""
        name = path.stem
    rings, polygons, lines = _shp_rings(shp)
    if not rings:
        raise VectorError(f"{name} has no polygons (only points or open lines).")
    if lines:
        notes.append(f"{lines} closed polyline{'s' if lines != 1 else ''} used as polygon{'s' if lines != 1 else ''}.")
    crs = _crs_from_prj(prj, backend)
    if not prj:
        notes.append("The shapefile has no .prj: choose its CRS.")
    elif not crs:
        notes.append("The CRS of the .prj was not recognised: choose it.")
    return PolygonSet(name, crs, rings, "Shapefile", notes, needs_crs=not crs)


# =============================================================================
# KML / KMZ
# =============================================================================
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _kml_coords(text: str) -> np.ndarray:
    values = []
    for item in (text or "").replace("\n", " ").replace("\t", " ").split():
        parts = item.split(",")
        if len(parts) >= 2:
            try:
                values.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    return np.array(values, dtype=np.float64).reshape(-1, 2)


def _from_kml_bytes(data: bytes, name: str, source: str) -> PolygonSet:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise VectorError(f"The KML is not valid XML: {exc}")
    rings: list[np.ndarray] = []
    lines = 0
    for elem in root.iter():
        tag = _local(elem.tag)
        if tag == "Polygon":
            for boundary in elem:
                if _local(boundary.tag) not in ("outerBoundaryIs", "innerBoundaryIs"):
                    continue
                for node in boundary.iter():
                    if _local(node.tag) == "coordinates":
                        ring = _clean_ring(_kml_coords(node.text))
                        if ring is not None:
                            rings.append(ring)
        elif tag == "LineString":
            for node in elem:
                if _local(node.tag) == "coordinates":
                    coords = _kml_coords(node.text)
                    if _is_closed(coords):
                        ring = _clean_ring(coords)
                        if ring is not None:
                            rings.append(ring)
                            lines += 1
    if not rings:
        raise VectorError(f"{name} has no polygons (draw them as polygons, or close the paths).")
    notes = [f"{lines} closed path{'s' if lines != 1 else ''} used as polygon{'s' if lines != 1 else ''}."] if lines else []
    return PolygonSet(name, crs_mod.WGS84, rings, source, notes)


def _read_kmz(path: Path) -> PolygonSet:
    try:
        with zipfile.ZipFile(str(path)) as zf:
            kmls = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not kmls:
                raise VectorError(f"No .kml inside {path.name}.")
            kmls.sort(key=lambda n: (Path(n).name.lower() != "doc.kml", n))
            return _from_kml_bytes(zf.read(kmls[0]), path.stem, "KMZ")
    except zipfile.BadZipFile:
        raise VectorError(f"{path.name} is not a valid KMZ.")


# =============================================================================
# DXF (ASCII)
# =============================================================================
def _dxf_pairs(data: bytes):
    if data[:22].startswith(b"AutoCAD Binary DXF"):
        raise VectorError("Binary DXF is not supported: save the drawing as ASCII DXF.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252", "replace")
    lines = text.splitlines()
    for i in range(0, len(lines) - 1, 2):
        code_text = lines[i].strip()
        if not code_text:
            continue
        try:
            code = int(code_text)
        except ValueError:
            raise VectorError("The file is not a valid ASCII DXF.")
        yield code, lines[i + 1].strip()


def _entities(pairs) -> list[tuple[str, list[tuple[int, str]]]]:
    """(type, tags) of the entities of the ENTITIES section (the model space)."""
    out = []
    section = None
    current = None
    expect_name = False
    for code, value in pairs:
        if code == 0:
            if current is not None and section == "ENTITIES":
                out.append(current)
            current = None
            if value == "SECTION":
                expect_name = True
                continue
            if value == "ENDSEC":
                section = None
                continue
            if section == "ENTITIES":
                current = (value, [])
            continue
        if expect_name and code == 2:
            section = value
            expect_name = False
            continue
        if current is not None:
            current[1].append((code, value))
    if current is not None and section == "ENTITIES":
        out.append(current)
    return out


def _bulge_points(p1, p2, bulge: float) -> list[tuple[float, float]]:
    """Points of the arc of a polyline segment with a bulge (without p1, with p2)."""
    if abs(bulge) < 1e-9:
        return [p2]
    (x1, y1), (x2, y2) = p1, p2
    chord = math.hypot(x2 - x1, y2 - y1)
    if chord < 1e-12:
        return [p2]
    theta = 4.0 * math.atan(bulge)
    radius = chord / (2.0 * math.sin(theta / 2.0))
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    d = radius * math.cos(theta / 2.0)
    nx, ny = -(y2 - y1) / chord, (x2 - x1) / chord
    cx, cy = mx + nx * d, my + ny * d
    a1 = math.atan2(y1 - cy, x1 - cx)
    steps = max(2, int(abs(math.degrees(theta)) / CURVE_STEP_DEG))
    r = abs(radius)
    out = [(cx + r * math.cos(a1 + theta * i / steps), cy + r * math.sin(a1 + theta * i / steps))
           for i in range(1, steps + 1)]
    out[-1] = p2
    return out


def _with_bulges(vertices: list[tuple[float, float]], bulges: list[float]) -> list[tuple[float, float]]:
    """The closed outline, with the arcs of the bulges (the last bulge closes back to the first vertex)."""
    out = [vertices[0]]
    n = len(vertices)
    for i in range(n):
        p1, p2 = vertices[i], vertices[(i + 1) % n]
        out.extend(_bulge_points(p1, p2, bulges[i] if i < len(bulges) else 0.0))
    return out


def _float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except ValueError:
        return default


def _mirror(tags) -> bool:
    """Extrusion (0, 0, -1): the object coordinates have their X mirrored."""
    for code, value in tags:
        if code == 230:
            return _float(value, 1.0) < 0
    return False


def _read_dxf(path: Path) -> PolygonSet:
    entities = _entities(_dxf_pairs(path.read_bytes()))
    rings: list[np.ndarray] = []
    skipped = {"open": 0, "hatch": 0, "line": 0, "spline": 0, "ellipse": 0}
    i = 0
    while i < len(entities):
        etype, tags = entities[i]
        i += 1
        if etype == "LWPOLYLINE":
            flags = int(_float(next((v for c, v in tags if c == 70), "0")))
            vertices, bulges = [], []
            x = None
            for code, value in tags:
                if code == 10:
                    x = _float(value)
                elif code == 20 and x is not None:
                    vertices.append((x, _float(value)))
                    bulges.append(0.0)
                    x = None
                elif code == 42 and bulges:
                    bulges[-1] = _float(value)
            closed = bool(flags & 1) or _is_closed(vertices)
            if not closed:
                skipped["open"] += 1
                continue
            if _is_closed(vertices):
                vertices, bulges = vertices[:-1], bulges[:-1]
            if len(vertices) < 3 and not any(bulges):
                continue
            points = _with_bulges(vertices, bulges)
            if _mirror(tags):
                points = [(-px, py) for px, py in points]
            ring = _clean_ring(points)
            if ring is not None:
                rings.append(ring)
        elif etype == "POLYLINE":
            flags = int(_float(next((v for c, v in tags if c == 70), "0")))
            mirror = _mirror(tags) and not flags & 8
            vertices, bulges = [], []
            while i < len(entities) and entities[i][0] in ("VERTEX", "SEQEND"):
                vtype, vtags = entities[i]
                i += 1
                if vtype == "SEQEND":
                    break
                vflags = int(_float(next((v for c, v in vtags if c == 70), "0")))
                if vflags & 128 and not vflags & 64:
                    continue                      # face record of a polyface mesh
                vx = _float(next((v for c, v in vtags if c == 10), "0"))
                vy = _float(next((v for c, v in vtags if c == 20), "0"))
                vertices.append((vx, vy))
                bulges.append(_float(next((v for c, v in vtags if c == 42), "0")))
            if flags & (16 | 64) or not vertices:
                continue                          # meshes are not outlines
            if not (flags & 1 or _is_closed(vertices)):
                skipped["open"] += 1
                continue
            if _is_closed(vertices):
                vertices, bulges = vertices[:-1], bulges[:-1]
            points = _with_bulges(vertices, bulges)
            if mirror:
                points = [(-px, py) for px, py in points]
            ring = _clean_ring(points)
            if ring is not None:
                rings.append(ring)
        elif etype == "CIRCLE":
            values = {c: _float(v) for c, v in tags if c in (10, 20, 40)}
            radius = values.get(40, 0.0)
            if radius <= 0:
                continue
            cx = -values.get(10, 0.0) if _mirror(tags) else values.get(10, 0.0)
            angles = np.linspace(0.0, 2.0 * math.pi, CIRCLE_POINTS, endpoint=False)
            rings.append(np.column_stack([cx + radius * np.cos(angles), values.get(20, 0.0) + radius * np.sin(angles)]))
        elif etype == "HATCH":
            skipped["hatch"] += 1
        elif etype in ("LINE", "ARC"):
            skipped["line"] += 1
        elif etype == "SPLINE":
            skipped["spline"] += 1
        elif etype == "ELLIPSE":
            skipped["ellipse"] += 1
    notes = []
    labels = {"open": "open polyline(s) (close them in the CAD program to use them)",
              "hatch": "hatch(es) (use their boundary polyline instead)",
              "line": "line(s) / arc(s) not joined into a polyline (join them in the CAD program)",
              "spline": "spline(s)", "ellipse": "ellipse(s)"}
    for key, count in skipped.items():
        if count:
            notes.append(f"{count} {labels[key]} ignored.")
    if not rings:
        raise VectorError(f"{path.name} has no closed polylines or circles to use as polygons."
                          + (" " + " ".join(notes) if notes else ""))
    notes.append("A DXF does not store its CRS: choose the datum (ellipsoid) and the UTM zone.")
    return PolygonSet(path.stem, "", rings, "DXF", notes, needs_crs=True)


# =============================================================================
def read_polygons(path, backend: CrsBackend | None = None) -> PolygonSet:
    """The polygons of a Shapefile (.shp / .zip), KML, KMZ or DXF."""
    path = Path(path)
    if not path.is_file():
        raise VectorError(f"File not found: {path}")
    ext = path.suffix.lower()
    if ext in (".shp", ".zip"):
        result = _read_shapefile(path, backend)
    elif ext == ".kml":
        result = _from_kml_bytes(path.read_bytes(), path.stem, "KML")
    elif ext == ".kmz":
        result = _read_kmz(path)
    elif ext == ".dxf":
        result = _read_dxf(path)
    else:
        raise VectorError("Polygons are read from Shapefile (.shp or .zip), KML, KMZ or DXF.")
    before = len(result.rings)
    result.rings = _unique(result.rings)
    if len(result.rings) < before:
        result.notes.append(f"{before - len(result.rings)} repeated outline(s) used once.")
    return result

