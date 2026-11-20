"""Coloured LAS 1.2 of the preview points in the project CRS (Qt-free, no laspy).

Used to check the georeferenced model in QGIS (Layer > Add Layer > Point Cloud)
before the SLPK is made: the points must sit on the basemap / survey data in
the project CRS. The CRS is written as GeoTIFF keys (LASF_Projection 34735),
which PDAL / QGIS read; EPSG codes above 65535 cannot be stored that way.
"""

from __future__ import annotations

import struct
from datetime import date
from pathlib import Path

import numpy as np

from .crs import epsg_number

POINT_DTYPE = np.dtype([
    ("x", "<i4"), ("y", "<i4"), ("z", "<i4"),
    ("intensity", "<u2"), ("flags", "u1"), ("classification", "u1"),
    ("scan_angle", "i1"), ("user_data", "u1"), ("point_source", "<u2"),
    ("red", "<u2"), ("green", "<u2"), ("blue", "<u2"),
])  # point data record format 2 (26 bytes)
HEADER_SIZE = 227
SCALE = 0.001


def _text(value: str, size: int) -> bytes:
    return value.encode("ascii", "replace")[:size].ljust(size, b"\0")


def _geokeys_vlr(epsg: int) -> bytes:
    keys = [
        (1024, 0, 1, 1),        # GTModelTypeGeoKey = projected
        (1025, 0, 1, 1),        # GTRasterTypeGeoKey = pixel is area
        (3072, 0, 1, epsg),     # ProjectedCSTypeGeoKey
        (3076, 0, 1, 9001),     # ProjLinearUnitsGeoKey = metre
        (4099, 0, 1, 9001),     # VerticalUnitsGeoKey = metre
    ]
    values = [1, 1, 0, len(keys)]
    for key in keys:
        values.extend(key)
    body = struct.pack(f"<{len(values)}H", *values)
    header = struct.pack("<H16sHH32s", 0, _text("LASF_Projection", 16), 34735, len(body),
                         _text("GeoKeyDirectoryTag", 32))
    return header + body


def write_las(path: str | Path, xyz: np.ndarray, rgb: np.ndarray | None, crs: str = "") -> dict:
    """Write ``xyz`` (project CRS, metres) with ``rgb`` (uint8) as LAS 1.2, point format 2."""
    xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
    if not len(xyz):
        raise ValueError("There are no points to write.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    low = xyz.min(axis=0)
    high = xyz.max(axis=0)
    offset = np.floor(low)
    records = np.zeros(len(xyz), dtype=POINT_DTYPE)
    q = np.rint((xyz - offset) / SCALE)
    if q.max() > 2 ** 31 - 1:
        raise ValueError("The model is too large for a LAS preview at millimetre resolution.")
    records["x"], records["y"], records["z"] = q[:, 0], q[:, 1], q[:, 2]
    records["flags"] = 1 | (1 << 3)  # return 1 of 1
    records["classification"] = 1
    if rgb is not None:
        colors = np.asarray(rgb, dtype=np.uint16)[:, :3] * 257
        records["red"], records["green"], records["blue"] = colors[:, 0], colors[:, 1], colors[:, 2]

    epsg = epsg_number(crs)
    vlr = _geokeys_vlr(epsg) if epsg is not None and epsg <= 65535 else b""
    today = date.today()
    header = b"".join([
        b"LASF",
        struct.pack("<HH", 0, 0),                      # file source id, global encoding
        b"\0" * 16,                                    # project GUID
        struct.pack("<BB", 1, 2),                      # version 1.2
        _text("ROLAVI", 32),
        _text("ROLAVI 3D Model preview", 32),
        struct.pack("<HH", today.timetuple().tm_yday, today.year),
        struct.pack("<H", HEADER_SIZE),
        struct.pack("<I", HEADER_SIZE + len(vlr)),     # offset to the points
        struct.pack("<I", 1 if vlr else 0),            # number of VLRs
        struct.pack("<BH", 2, POINT_DTYPE.itemsize),   # point format 2, 26 bytes
        struct.pack("<I", len(xyz)),
        struct.pack("<5I", len(xyz), 0, 0, 0, 0),
        struct.pack("<3d", SCALE, SCALE, SCALE),
        struct.pack("<3d", *offset),
        struct.pack("<6d", high[0], low[0], high[1], low[1], high[2], low[2]),
    ])
    if len(header) != HEADER_SIZE:
        raise ValueError(f"LAS header is {len(header)} bytes, expected {HEADER_SIZE}")
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "wb") as stream:
        stream.write(header)
        stream.write(vlr)
        stream.write(records.tobytes())
    tmp.replace(path)
    return {"points": int(len(xyz)), "epsg_written": bool(vlr), "path": path}
