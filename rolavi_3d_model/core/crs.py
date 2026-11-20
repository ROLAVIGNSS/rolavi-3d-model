"""Coordinate reference systems of the converter (Qt-free, QGIS-free).

The core never calls pyproj or QGIS itself: it receives a ``CrsBackend``.

* ``PyprojBackend`` - standalone / VS Code run (pyproj, as GEOStudio).
* The QGIS plugin will add a backend built on ``QgsCoordinateTransform``.

The plugin works in a projected CRS in metres (the GCP adjustment, the plan view and the
SLPK tiling are in metres). The user may choose a geographic CRS too (degrees): the window
then works in the UTM zone of the model and shows / takes latitude and longitude.
``search`` finds CRS by name, code or area (pyproj's EPSG database). The SLPK itself is
always written in WGS84 (EPSG:4326).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

import numpy as np

WGS84 = "EPSG:4326"

#: CRS offered in the list (any other can be searched or typed).
COMMON_CRS: list[tuple[str, str]] = [
    ("EPSG:32717", "WGS 84 / UTM zone 17S"),
    ("EPSG:32718", "WGS 84 / UTM zone 18S"),
    ("EPSG:32719", "WGS 84 / UTM zone 19S"),
    ("EPSG:5839", "Peru96 / UTM zone 17S"),
    ("EPSG:5387", "Peru96 / UTM zone 18S"),
    ("EPSG:5389", "Peru96 / UTM zone 19S"),
    ("EPSG:4326", "WGS 84 (latitude / longitude)"),
    ("EPSG:9377", "MAGNA-SIRGAS 2018 / Origen-Nacional (Colombia)"),
    ("EPSG:3116", "MAGNA-SIRGAS / Colombia Bogota zone"),
    ("EPSG:3115", "MAGNA-SIRGAS / Colombia West zone"),
    ("EPSG:3117", "MAGNA-SIRGAS / Colombia East Central zone"),
    ("EPSG:32617", "WGS 84 / UTM zone 17N"),
    ("EPSG:32618", "WGS 84 / UTM zone 18N"),
    ("EPSG:32619", "WGS 84 / UTM zone 19N"),
    ("EPSG:31983", "SIRGAS 2000 / UTM zone 23S (Brazil)"),
    ("EPSG:32632", "WGS 84 / UTM zone 32N (Italy, Switzerland)"),
    ("EPSG:32633", "WGS 84 / UTM zone 33N (Italy)"),
]

_BLOCK = 1_000_000


class CrsError(ValueError):
    pass


@dataclass
class CrsEntry:
    """A search result: code, name, kind ("projected" / "geographic") and area of use."""
    code: str
    name: str
    kind: str
    area: str = ""


@dataclass
class CrsInfo:
    code: str          # "EPSG:9377" (or WKT when the CRS has no EPSG code)
    name: str          # "MAGNA-SIRGAS 2018 / Origen-Nacional"
    projected: bool
    metres: bool

    @property
    def label(self) -> str:
        units = "metres" if self.metres else "units are not metres"
        kind = "projected" if self.projected else "geographic"
        return f"{self.code} - {self.name} ({kind}, {units})"


def normalize(text: str) -> str:
    """'9377', 'epsg:9377', 'EPSG:9377 - MAGNA...' -> 'EPSG:9377'; WKT is returned as it is."""
    value = str(text or "").strip()
    if not value:
        return ""
    if value[:6].upper() in ("PROJCS", "GEOGCS", "PROJCR", "GEOGCR", "COMPOU", "COMPDC", "BOUNDC"):
        return value
    match = re.match(r"^\s*(?:EPSG\s*:\s*)?(\d{4,6})\b", value, flags=re.IGNORECASE)
    if match:
        return f"EPSG:{match.group(1)}"
    return value.upper()


def epsg_number(code: str) -> int | None:
    match = re.match(r"^EPSG:(\d+)$", normalize(code))
    return int(match.group(1)) if match else None


def is_query(text: str) -> bool:
    """Text that is a name to search ("UTM 18S", "Peru96"), not a code or a WKT."""
    value = normalize(text)
    return bool(value) and not value.startswith("EPSG:") and value[:6] not in (
        "PROJCS", "GEOGCS", "PROJCR", "GEOGCR", "COMPOU", "COMPDC", "BOUNDC")


def utm_code(lon: float, lat: float) -> str:
    """WGS 84 / UTM zone of a point (the metric CRS the window works in for a geographic CRS)."""
    zone = int((float(lon) + 180.0) // 6.0) % 60 + 1
    return f"EPSG:{(32700 if float(lat) < 0 else 32600) + zone}"


def search_entries(catalog: list[CrsEntry], text: str, kinds=("projected", "geographic"),
                   limit: int = 300) -> list[CrsEntry]:
    """Entries whose code, name or area contain every word of ``text`` (exact code first)."""
    words = [w for w in re.split(r"[\s,;/]+", str(text or "").lower().replace("epsg:", "")) if w]
    found = []
    for entry in catalog:
        if entry.kind not in kinds:
            continue
        hay = f"{entry.code} {entry.name} {entry.area}".lower()
        if all(word in hay for word in words):
            found.append(entry)
    query = " ".join(words)

    def rank(entry: CrsEntry):
        number = entry.code.split(":")[-1]
        return (number != query, not entry.name.lower().startswith(query), len(entry.name), entry.code)

    found.sort(key=rank)
    return found[:limit]


class CrsBackend:
    """What the core needs from a CRS library."""

    name = "none"

    def info(self, crs: str) -> CrsInfo:
        raise NotImplementedError

    def from_wkt(self, wkt: str) -> str:
        """EPSG code of a WKT (e.g. a .prj), or the WKT itself when it has none."""
        raise NotImplementedError

    def to_lonlat(self, crs: str, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        raise NotImplementedError

    def from_lonlat(self, crs: str, lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        raise NotImplementedError

    def search(self, text: str, kinds=("projected", "geographic"), limit: int = 300) -> list[CrsEntry]:
        """CRS whose name, code or area match ``text`` (empty when the backend has no database)."""
        return []

    def to_wkt(self, crs: str) -> str:
        """WKT1 (as GDAL writes it in a .aux.xml / .prj). Without a CRS library: WGS 84 and its UTM zones."""
        wkt = wgs84_wkt(crs)
        if not wkt:
            raise CrsError(f"Cannot write the definition of {crs} without a CRS library.")
        return wkt

    def require_projected(self, crs: str) -> CrsInfo:
        info = self.info(crs)
        if not info.projected:
            raise CrsError(
                f"{info.code} is a geographic CRS (degrees). Choose the projected CRS of the model "
                "(metres): e.g. EPSG:9377 MAGNA-SIRGAS 2018 / Origen-Nacional or a UTM zone."
            )
        if not info.metres:
            raise CrsError(f"{info.code}: the units of this CRS are not metres. Use a CRS in metres.")
        return info


class PyprojBackend(CrsBackend):
    name = "pyproj"

    def __init__(self):
        try:
            from pyproj import CRS, Transformer
        except Exception as exc:  # pragma: no cover - depends on the environment
            raise CrsError("pyproj is required to convert coordinates (pip install pyproj).") from exc
        self._CRS = CRS
        self._Transformer = Transformer
        self._local = threading.local()  # pyproj transformers are kept per thread (UI / worker)
        self._infos: dict[str, CrsInfo] = {}
        self._catalog: list[CrsEntry] | None = None

    def _crs(self, crs: str):
        code = normalize(crs)
        if not code:
            raise CrsError("No CRS was given.")
        try:
            return self._CRS.from_user_input(code)
        except Exception as exc:
            raise CrsError(f"Unknown CRS: {crs}") from exc

    def info(self, crs: str) -> CrsInfo:
        code = normalize(crs)
        if code in self._infos:
            return self._infos[code]
        obj = self._crs(code)
        horizontal = obj
        try:
            if obj.is_compound and obj.sub_crs_list:
                horizontal = obj.sub_crs_list[0]
        except Exception:
            pass
        metres = False
        try:
            axis = horizontal.axis_info[0]
            metres = (axis.unit_name or "").lower() in ("metre", "meter", "m") or float(axis.unit_conversion_factor or 0) == 1.0
        except Exception:
            pass
        epsg = None
        try:
            epsg = obj.to_epsg()
        except Exception:
            pass
        info = CrsInfo(
            code=f"EPSG:{epsg}" if epsg else code,
            name=str(obj.name),
            projected=bool(horizontal.is_projected),
            metres=bool(metres),
        )
        self._infos[code] = info
        return info

    def from_wkt(self, wkt: str) -> str:
        obj = self._crs(wkt)
        try:
            epsg = obj.to_epsg(min_confidence=70)
        except Exception:
            epsg = None
        return f"EPSG:{epsg}" if epsg else obj.to_wkt()

    def _transformer(self, crs: str, inverse: bool):
        code = normalize(crs)
        if not hasattr(self._local, "forward"):
            self._local.forward, self._local.inverse = {}, {}
        cache = self._local.inverse if inverse else self._local.forward
        if code not in cache:
            source, target = (WGS84, code) if inverse else (code, WGS84)
            try:
                cache[code] = self._Transformer.from_crs(source, target, always_xy=True)
            except Exception as exc:
                raise CrsError(f"No transformation between {code} and WGS84: {exc}") from exc
        return cache[code]

    def _run(self, transformer, a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        a = np.asarray(a, dtype=np.float64).reshape(-1)
        b = np.asarray(b, dtype=np.float64).reshape(-1)
        out_a = np.empty_like(a)
        out_b = np.empty_like(b)
        for start in range(0, len(a), _BLOCK):
            part = slice(start, start + _BLOCK)
            out_a[part], out_b[part] = transformer.transform(a[part], b[part])
        if len(out_a) and not (np.isfinite(out_a).all() and np.isfinite(out_b).all()):
            raise CrsError("Some coordinates could not be converted: check the CRS of the model.")
        return out_a, out_b

    def _load_catalog(self) -> list[CrsEntry]:
        """Projected and geographic CRS of the EPSG database (read once, not deprecated)."""
        if self._catalog is not None:
            return self._catalog
        catalog: list[CrsEntry] = []
        try:
            from pyproj.database import query_crs_info
            from pyproj.enums import PJType

            kinds = {PJType.PROJECTED_CRS: "projected", PJType.GEOGRAPHIC_2D_CRS: "geographic",
                     PJType.GEOGRAPHIC_3D_CRS: "geographic"}
            for item in query_crs_info(auth_name="EPSG", pj_types=list(kinds), allow_deprecated=False):
                area = item.area_of_use.name if item.area_of_use is not None else ""
                catalog.append(CrsEntry(f"EPSG:{item.code}", str(item.name), kinds.get(item.type, "projected"),
                                        str(area or "")))
        except Exception:
            catalog = []          # old pyproj: the CRS can still be typed by its code
        self._catalog = catalog
        return catalog

    def search(self, text, kinds=("projected", "geographic"), limit=300):
        return search_entries(self._load_catalog(), text, kinds, limit)

    def to_wkt(self, crs):
        obj = self._crs(crs)
        try:
            return obj.to_wkt("WKT1_GDAL")
        except Exception:
            return obj.to_wkt()

    def to_lonlat(self, crs, x, y):
        return self._run(self._transformer(crs, inverse=False), x, y)

    def from_lonlat(self, crs, lon, lat):
        return self._run(self._transformer(crs, inverse=True), lon, lat)


#: Datums offered when a file brings no CRS (a DXF, a shapefile without .prj): name -> (south, north) UTM codes.
#: PSAD56 is on the International 1924 ellipsoid; SIRGAS 2000 and Peru96 on GRS 80; WGS 84 on its own.
DATUMS: list[tuple[str, str]] = [
    ("WGS84", "WGS 84 (ellipsoid WGS 84)"),
    ("PSAD56", "PSAD56 (ellipsoid International 1924)"),
    ("SIRGAS2000", "SIRGAS 2000 (ellipsoid GRS 80)"),
    ("PERU96", "Peru96 (ellipsoid GRS 80)"),
]


def datum_utm_code(datum: str, zone: int, south: bool) -> str:
    """EPSG code of '<datum> / UTM zone <zone><S|N>'. Raises CrsError when that datum has no such zone."""
    zone = int(zone)
    key = str(datum or "").upper().replace(" ", "").replace("_", "")
    hemi = "S" if south else "N"
    code = None
    if not 1 <= zone <= 60:
        raise CrsError("The UTM zone must be between 1 and 60.")
    if key in ("WGS84", "WGS1984"):
        code = (32700 if south else 32600) + zone
    elif key in ("PSAD56", "PSAD1956"):
        if south and 17 <= zone <= 22:
            code = 24860 + zone           # 24877 = 17S ... 24882 = 22S
        elif not south and 17 <= zone <= 21:
            code = 24800 + zone           # 24817 = 17N ... 24821 = 21N
    elif key in ("SIRGAS2000", "SIRGAS"):
        if south and 17 <= zone <= 25:
            code = 31960 + zone           # 31977 = 17S ... 31985 = 25S
        elif not south and 11 <= zone <= 22:
            code = 31954 + zone           # 31965 = 11N ... 31976 = 22N
    elif key == "PERU96":
        code = {17: 5839, 18: 5387, 19: 5389}.get(zone) if south else None
    else:
        raise CrsError(f"Unknown datum: {datum}")
    if code is None:
        name = dict(DATUMS).get(key, datum).split(" (")[0]
        raise CrsError(f"{name} has no UTM zone {zone}{hemi} in the EPSG list. Check the zone, or choose another "
                       "CRS with 'Other CRS…'.")
    return f"EPSG:{code}"


def transform(backend: CrsBackend, source: str, target: str, x, y) -> tuple[np.ndarray, np.ndarray]:
    """Coordinates from one CRS to another (through WGS 84 longitude / latitude)."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if normalize(source) == normalize(target):
        return x.copy(), y.copy()
    lon, lat = backend.to_lonlat(source, x.reshape(-1), y.reshape(-1))
    out_x, out_y = backend.from_lonlat(target, lon, lat)
    return out_x.reshape(x.shape), out_y.reshape(y.shape)


_WGS84_GEOGCS = ('GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
                 'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
                 'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]]')


def wgs84_wkt(crs: str) -> str:
    """WKT1 of EPSG:4326 and of the WGS 84 / UTM zones (EPSG:326xx / 327xx), as GDAL writes it; '' otherwise."""
    number = epsg_number(crs)
    if number == 4326:
        return _WGS84_GEOGCS
    if number is None or not (32601 <= number <= 32660 or 32701 <= number <= 32760):
        return ""
    south = number > 32700
    zone = number - (32700 if south else 32600)
    return (f'PROJCS["WGS 84 / UTM zone {zone}{"S" if south else "N"}",{_WGS84_GEOGCS},'
            'PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",0],'
            f'PARAMETER["central_meridian",{zone * 6 - 183}],PARAMETER["scale_factor",0.9996],'
            f'PARAMETER["false_easting",500000],PARAMETER["false_northing",{10000000 if south else 0}],'
            'UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Easting",EAST],AXIS["Northing",NORTH],'
            f'AUTHORITY["EPSG","{number}"]]')


def default_backend() -> CrsBackend:
    """The backend of the standalone run (pyproj)."""
    return PyprojBackend()
