"""GCP CSV files (Qt-free).

A GCP has a name, its **OBJ coordinates** (the numbers of the point in the OBJ
file, picked on the model or typed) and its **known coordinates** E, N, H in the
project CRS (total station, GNSS RTK / static), or longitude, latitude, h when the
project CRS is geographic (``lon/lat`` headers are read as E / N).

Reading is tolerant: comma, semicolon, tab or space separated; headers in any
case (``name/point/id``, ``e/x/east/easting``, ``n/y/north/northing``,
``h/z/height/elevation``, ``obj_x/model_x`` ...). A file without a header is read
as Name, E, N, H (``PENZ``) or Name, N, E, H (``PNEZ``, Civil 3D / Topcon), as
chosen.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

ORDER_PENZ = "PENZ"
ORDER_PNEZ = "PNEZ"

_NAME = ["name", "point", "id", "gcp", "label", "pt", "punto", "nombre"]
_E = ["e", "east", "easting", "x", "world_x", "este", "lon", "long", "lng", "longitude", "longitud"]
_N = ["n", "north", "northing", "y", "world_y", "norte", "lat", "latitude", "latitud"]
_H = ["h", "z", "height", "elevation", "elev", "world_z", "ellipsoidal_height", "orthometric_height", "cota", "altura"]
_OX = ["obj_x", "model_x", "mesh_x", "local_x"]
_OY = ["obj_y", "model_y", "mesh_y", "local_y"]
_OZ = ["obj_z", "model_z", "mesh_z", "local_z"]
_USE = ["use", "enabled", "on"]


@dataclass
class Gcp:
    name: str = ""
    enabled: bool = True
    obj: tuple[float, float, float] | None = None     # coordinates in the OBJ file
    known: tuple[float, float, float] | None = None   # E, N, H in the project CRS
    residual: tuple[float, float, float] | None = None  # dE, dN, dH after the adjustment / check

    def usable(self) -> bool:
        return self.enabled and self.obj is not None and self.known is not None


def _number(text) -> float | None:
    value = str(text if text is not None else "").strip().replace(" ", "")
    if not value:
        return None
    if value.count(",") == 1 and "." not in value:
        value = value.replace(",", ".")  # decimal comma
    try:
        return float(value)
    except ValueError:
        return None


def _key(text: str) -> str:
    return str(text or "").strip().lower().replace(" ", "_").replace("-", "_")


def _sniff_rows(text: str) -> list[list[str]]:
    sample = text[:4096]
    for delimiter in (";", "\t", ","):
        if delimiter in sample:
            return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in row)]
    return [line.split() for line in text.splitlines() if line.strip()]


def _find(header: list[str], names: list[str]) -> int | None:
    for name in names:
        if name in header:
            return header.index(name)
    return None


def _triple(row: list[str], columns: tuple[int | None, int | None, int | None]):
    if any(c is None or c >= len(row) for c in columns):
        return None
    values = [_number(row[c]) for c in columns]
    if any(v is None for v in values):
        return None
    return (values[0], values[1], values[2])


def read_gcp_csv(path: str | Path, order: str = ORDER_PENZ) -> list[Gcp]:
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    rows = _sniff_rows(text)
    if not rows:
        return []
    header = [_key(c) for c in rows[0]]
    has_header = sum(_number(c) is not None for c in rows[0]) < 2
    gcps: list[Gcp] = []
    if has_header:
        name_c = _find(header, _NAME)
        e_c, n_c, h_c = _find(header, _E), _find(header, _N), _find(header, _H)
        ox, oy, oz = _find(header, _OX), _find(header, _OY), _find(header, _OZ)
        use_c = _find(header, _USE)
        for number, row in enumerate(rows[1:], start=1):
            gcp = Gcp(name=(row[name_c].strip() if name_c is not None and name_c < len(row) else f"GCP{number}"))
            gcp.known = _triple(row, (e_c, n_c, h_c))
            gcp.obj = _triple(row, (ox, oy, oz))
            if use_c is not None and use_c < len(row):
                gcp.enabled = row[use_c].strip().lower() not in ("0", "false", "no", "off")
            if gcp.known is not None or gcp.obj is not None:
                gcps.append(gcp)
        return gcps
    e_i, n_i = (1, 2) if order == ORDER_PENZ else (2, 1)
    for number, row in enumerate(rows, start=1):
        known = _triple(row, (e_i, n_i, 3))
        if known is None:
            continue
        gcps.append(Gcp(name=row[0].strip() or f"GCP{number}", known=known))
    return gcps


def write_gcp_csv(path: str | Path, gcps: list[Gcp], crs: str = "", geographic: bool = False) -> None:
    """``geographic``: the known coordinates are longitude, latitude (degrees) and height."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def fmt(value, decimals: int = 4) -> str:
        return "" if value is None else f"{value:.{decimals}f}"

    with open(path, "w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        known_names = ["lon", "lat", "h"] if geographic else ["e", "n", "h"]
        writer.writerow(["name", "use", "obj_x", "obj_y", "obj_z", *known_names, "de", "dn", "dh", "crs"])
        for g in gcps:
            obj = g.obj or (None, None, None)
            known = g.known or (None, None, None)
            res = g.residual or (None, None, None)
            known_text = [fmt(known[0], 9 if geographic else 4), fmt(known[1], 9 if geographic else 4), fmt(known[2])]
            writer.writerow([g.name, int(bool(g.enabled)), *map(fmt, obj), *known_text, *map(fmt, res), crs])
