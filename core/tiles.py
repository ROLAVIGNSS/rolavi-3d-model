"""XYZ base maps of the 2D view (Qt-free): presets, tile maths and a background tile fetcher.

The 2D view is drawn in the CRS the window works in (metres); each Web Mercator tile is
placed there from its corners (longitude / latitude -> CRS), so it lines up with the model.

Tiles are kept in memory only (never written to disk). Google's terms only allow its maps
through its APIs (Map Tiles API, with a key) and forbid storing them: these XYZ addresses are
the ones QGIS users add by hand; check the terms of each provider before distributing them.
"""

from __future__ import annotations

import math
import threading
import time
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from . import net

TILE_SIZE = 256
EARTH_CIRCUMFERENCE = 40075016.685578488    # metres at the equator (Web Mercator)
MAX_TILES = 96                               # per view: beyond it a coarser zoom is used
USER_AGENT = "ROLAVI-3D-Model (QGIS plugin; +https://viewer.rolavi.net)"


@dataclass(frozen=True)
class Basemap:
    key: str
    label: str
    url: str                 # with {x} {y} {z}
    max_zoom: int = 20
    attribution: str = ""


PRESETS: list[Basemap] = [
    Basemap("google_satellite", "Google Satellite", "http://www.google.cn/maps/vt?lyrs=s@189&gl=cn&x={x}&y={y}&z={z}",
            20, "Imagery © Google"),
    Basemap("google_hybrid", "Google Hybrid", "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}", 20,
            "Imagery and map © Google"),
    Basemap("google_maps", "Google Maps", "https://mt1.google.com/vt/lyrs=r&x={x}&y={y}&z={z}", 20, "Map © Google"),
    Basemap("google_terrain", "Google Terrain", "https://mt1.google.com/vt/lyrs=t&x={x}&y={y}&z={z}", 15,
            "Map © Google"),
    Basemap("google_roads", "Google Roads", "https://mt1.google.com/vt/lyrs=h&x={x}&y={y}&z={z}", 20,
            "Map © Google"),
    Basemap("google_traffic", "Google Traffic",
            "https://mt1.google.com/vt?lyrs=h@159000000,traffic|seconds_into_week:-1&style=3&x={x}&y={y}&z={z}", 20,
            "Map © Google"),
    Basemap("osm", "OpenStreetMap", "https://tile.openstreetmap.org/{z}/{x}/{y}.png", 19,
            "© OpenStreetMap contributors"),
]


def custom_basemap(label: str, url: str, max_zoom: int = 20) -> Basemap:
    key = "custom_" + "".join(c if c.isalnum() else "_" for c in label.lower())[:40]
    return Basemap(key, label, url.strip(), max_zoom, label)


# ---------------------------------------------------------------------------
# Web Mercator tile maths (XYZ / "slippy map": y = 0 at the top)
# ---------------------------------------------------------------------------

def tile_xy(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Fractional tile coordinates of a point."""
    lat = max(-85.0511287798, min(85.0511287798, lat))
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    phi = math.radians(lat)
    y = (1.0 - math.log(math.tan(phi) + 1.0 / math.cos(phi)) / math.pi) / 2.0 * n
    return x, y


def tile_lonlat(x: float, y: float, zoom: int) -> tuple[float, float]:
    """Longitude / latitude of a tile corner (x, y may be fractional)."""
    n = 2 ** zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lon, lat


def choose_zoom(metres_per_pixel: float, lat: float, max_zoom: int) -> int:
    """The zoom whose tile pixels are about the size of a screen pixel."""
    if metres_per_pixel <= 0 or not math.isfinite(metres_per_pixel):
        return 0
    ground = EARTH_CIRCUMFERENCE * math.cos(math.radians(lat)) / TILE_SIZE    # metres per pixel at zoom 0
    zoom = int(round(math.log2(max(ground / metres_per_pixel, 1.0))))
    return max(0, min(max_zoom, zoom))


def tiles_for(lon_min: float, lat_min: float, lon_max: float, lat_max: float, zoom: int) -> list[tuple[int, int, int]]:
    """(z, x, y) of the tiles covering a longitude / latitude box (coarser if there are too many)."""
    while True:
        x0, y0 = tile_xy(lon_min, lat_max, zoom)
        x1, y1 = tile_xy(lon_max, lat_min, zoom)
        n = 2 ** zoom
        xs = range(max(0, int(math.floor(x0))), min(n - 1, int(math.floor(x1))) + 1)
        ys = range(max(0, int(math.floor(y0))), min(n - 1, int(math.floor(y1))) + 1)
        if len(xs) * len(ys) <= MAX_TILES or zoom == 0:
            return [(zoom, x, y) for y in ys for x in xs]
        zoom -= 1


# ---------------------------------------------------------------------------
# Fetching (background threads, memory cache)
# ---------------------------------------------------------------------------

class TileFetcher:
    """Downloads tiles in background threads; ``done(key, z, x, y, data | None)`` is called from them."""

    def __init__(self, done: Callable[[str, int, int, int, bytes | None], None], workers: int = 6,
                 opener: Callable[[str], bytes] | None = None):
        self._done = done
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rolavi-tiles")
        self._pending: set[tuple] = set()
        self._failed: dict[tuple, float] = {}
        self._lock = threading.Lock()
        self._open = opener or self._download
        self._closed = False

    @staticmethod
    def _download(url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
        with net.open_url(request, timeout=12) as response:
            return response.read()

    def request(self, basemap: Basemap, z: int, x: int, y: int) -> None:
        tile = (basemap.key, z, x, y)
        with self._lock:
            if self._closed or tile in self._pending or time.monotonic() - self._failed.get(tile, -1e9) < 60.0:
                return
            self._pending.add(tile)
        url = basemap.url.replace("{x}", str(x)).replace("{y}", str(y)).replace("{z}", str(z))
        self._pool.submit(self._run, tile, url)

    def _run(self, tile: tuple, url: str) -> None:
        data = None
        try:
            data = self._open(url)
        except Exception:
            data = None
        with self._lock:
            self._pending.discard(tile)
            if data is None:
                self._failed[tile] = time.monotonic()
            if self._closed:
                return
        self._done(tile[0], tile[1], tile[2], tile[3], data)

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)


class LruCache:
    """At most ``size`` items, the least recently used dropped first."""

    def __init__(self, size: int = 800):
        self.size = size
        self._items: OrderedDict = OrderedDict()

    def get(self, key):
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
        return None

    def put(self, key, value) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()
