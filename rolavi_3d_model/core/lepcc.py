#lepcc.py
"""LEPCC (Limited Error Point Cloud Compression) blobs for I3S point cloud scene layers.

Python port of the encoders and decoders of Esri's LEPCC library
(https://github.com/Esri/lepcc, Copyright 2016 Esri, Apache License 2.0,
contributors Thomas Maurer and Lucian Plesea): ``LEPCC.cpp`` (xyz),
``BitStuffer2.cpp`` (bit stuffing), ``Common.cpp`` (Fletcher-32 checksum) and
``ClusterRGB.cpp`` (colours, written as raw RGB per point, the "None" colour
lookup method of the library).

The byte layout is identical to the C++ library (little endian):
xyz blob = TopHeader("LEPCC     ", version 1, checksum) + Header1(blobSize,
extent, maxError, numPoints) + 4 bit-stuffed integer arrays cut in sections
of 128 (row deltas, points per row, column deltas, z cells).

The xyz encoder re-orders the points (row by row of the quantization grid);
``encode_xyz`` returns that order so the attributes can follow it.
"""

from __future__ import annotations

import math
import struct

import numpy as np

SECTION_SIZE = 128
_XYZ_KEY = b"LEPCC     "
_RGB_KEY = b"ClusterRGB"


class LepccError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Fletcher-32 (Common::ComputeChecksumFletcher32)
# ---------------------------------------------------------------------------

def fletcher32(data: bytes) -> int:
    raw = np.frombuffer(data, dtype=np.uint8)
    length = len(raw)
    words = (raw[: length - (length & 1)].reshape(-1, 2).astype(np.int64) * [256, 1]).sum(axis=1)
    sum1 = 0xFFFF
    sum2 = 0xFFFF
    for start in range(0, len(words), 359):
        block = words[start:start + 359]
        running = np.cumsum(block)
        sum2 += len(block) * sum1 + int(running.sum())
        sum1 += int(running[-1])
        sum1 = (sum1 & 0xFFFF) + (sum1 >> 16)
        sum2 = (sum2 & 0xFFFF) + (sum2 >> 16)
    if length & 1:
        sum1 += int(raw[-1]) << 8
        sum2 += sum1
    sum1 = (sum1 & 0xFFFF) + (sum1 >> 16)
    sum2 = (sum2 & 0xFFFF) + (sum2 >> 16)
    return ((sum2 << 16) | sum1) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# BitStuffer2 (simple mode)
# ---------------------------------------------------------------------------

def _num_bytes_uint(k: int) -> int:
    return 1 if k < 256 else (2 if k < 65536 else 4)


def _bit_stuff(values: np.ndarray, num_bits: int) -> bytes:
    """Elements LSB-first into little-endian uint32 words, trailing unused bytes dropped."""
    if num_bits <= 0 or not len(values):
        return b""
    words = np.ascontiguousarray(values, dtype="<u4")
    bits = np.unpackbits(words.view(np.uint8).reshape(-1, 4), axis=1, bitorder="little")[:, :num_bits]
    return np.packbits(bits.reshape(-1), bitorder="little").tobytes()


def encode_simple(values: np.ndarray) -> bytes:
    values = np.asarray(values, dtype=np.uint64)
    if not len(values):
        raise LepccError("empty array")
    max_elem = int(values.max())
    num_bits = max_elem.bit_length()
    if num_bits >= 32:
        raise LepccError("value too large for the bit stuffer")
    count = len(values)
    nb = _num_bytes_uint(count)
    bits67 = 0 if nb == 4 else 3 - nb
    header = bytes([num_bits | (bits67 << 6)]) + count.to_bytes(nb, "little")
    return header + _bit_stuff(values.astype(np.uint32), num_bits)


def decode_simple(data: bytes, pos: int) -> tuple[np.ndarray, int]:
    byte = data[pos]
    pos += 1
    bits67 = byte >> 6
    nb = 4 if bits67 == 0 else 3 - bits67
    do_lut = bool(byte & 0x20)
    num_bits = byte & 31
    count = int.from_bytes(data[pos:pos + nb], "little")
    pos += nb

    def unstuff(n: int, bits: int) -> np.ndarray:
        nonlocal pos
        if bits == 0 or n == 0:
            return np.zeros(n, dtype=np.uint32)
        nbytes = (n * bits + 7) // 8
        raw = np.frombuffer(data[pos:pos + nbytes], dtype=np.uint8)
        pos += nbytes
        stream = np.unpackbits(raw, bitorder="little")[: n * bits].reshape(n, bits)
        return (stream.astype(np.uint64) << np.arange(bits, dtype=np.uint64)).sum(axis=1).astype(np.uint32)

    if not do_lut:
        return unstuff(count, num_bits), pos
    n_lut = data[pos] - 1
    pos += 1
    lut = np.concatenate([[0], unstuff(n_lut, num_bits)]).astype(np.uint32)
    lut_bits = int(n_lut).bit_length()
    return lut[unstuff(count, lut_bits)], pos


def _encode_cut_in_segments(values: np.ndarray) -> bytes:
    values = np.asarray(values, dtype=np.int64)
    sections = [values[i:i + SECTION_SIZE] for i in range(0, len(values), SECTION_SIZE)]
    mins = np.array([int(section.min()) for section in sections], dtype=np.int64)
    out = [encode_simple(mins)]
    out.extend(encode_simple(section - low) for section, low in zip(sections, mins))
    return b"".join(out)


def _decode_cut_in_segments(data: bytes, pos: int) -> tuple[np.ndarray, int]:
    mins, pos = decode_simple(data, pos)
    parts = []
    for low in mins:
        section, pos = decode_simple(data, pos)
        parts.append(section.astype(np.int64) + int(low))
    return (np.concatenate(parts) if parts else np.zeros(0, dtype=np.int64)), pos


# ---------------------------------------------------------------------------
# XYZ (LEPCC.cpp)
# ---------------------------------------------------------------------------

def encode_xyz(points: np.ndarray, max_error: tuple[float, float, float]) -> tuple[bytes, np.ndarray]:
    """LEPCC xyz blob of ``points`` (N, 3) float64; returns ``(blob, order)``.

    ``order[i]`` is the index in ``points`` of the i-th decoded point.
    """
    pts = np.ascontiguousarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 3 or not len(pts):
        raise LepccError("expected an (N, 3) array of points")
    err = np.asarray(max_error, dtype=np.float64)
    if (err <= 0).any():
        raise LepccError("the maximum error must be > 0")
    lower = pts.min(axis=0)
    upper = pts.max(axis=0)
    cw = 2.0 * err
    n_cells = np.floor((upper - lower) / cw + 0.5).astype(np.int64) + 1
    if (n_cells <= 0).any() or (n_cells > 2**31 - 1).any():
        raise LepccError("quantization grid too big")
    cells = np.floor((pts - lower) / cw + 0.5).astype(np.int64)
    if (cells >= n_cells).any():
        raise LepccError("quantization index out of range")
    cell_index = cells[:, 1] * n_cells[0] + cells[:, 0]
    order = np.argsort(cell_index, kind="stable")
    ix, iy, iz = cells[order, 0], cells[order, 1], cells[order, 2]

    row_start = np.concatenate([[True], iy[1:] != iy[:-1]])
    rows = iy[row_start]
    y_delta = np.diff(np.concatenate([[0], rows]))
    starts = np.flatnonzero(row_start)
    per_row = np.diff(np.concatenate([starts, [len(iy)]]))
    prev_col = np.empty_like(ix)
    prev_col[0] = 0
    prev_col[1:] = ix[:-1]
    prev_col[row_start] = 0
    x_delta = ix - prev_col

    body = b"".join(
        _encode_cut_in_segments(array) for array in (y_delta, per_row, x_delta, iz)
    )
    header1 = struct.pack(
        "<q6d3dII", 0, *lower, *upper, *err, len(pts), 0
    )
    blob_size = 16 + len(header1) + len(body)
    header1 = struct.pack("<q", blob_size) + header1[8:]
    checksum = fletcher32(header1 + body)
    top = _XYZ_KEY + struct.pack("<HI", 1, checksum)
    return top + header1 + body, order


def decode_xyz(blob: bytes) -> np.ndarray:
    if blob[:10] != _XYZ_KEY:
        raise LepccError("not a LEPCC xyz blob")
    _version, checksum = struct.unpack("<HI", blob[10:16])
    blob_size, *values = struct.unpack("<q6d3dII", blob[16:104])
    lower = np.array(values[0:3])
    upper = np.array(values[3:6])
    err = np.array(values[6:9])
    count = values[9]
    if fletcher32(blob[16:blob_size]) != checksum:
        raise LepccError("LEPCC checksum mismatch")
    pos = 104
    y_delta, pos = _decode_cut_in_segments(blob, pos)
    per_row, pos = _decode_cut_in_segments(blob, pos)
    x_delta, pos = _decode_cut_in_segments(blob, pos)
    iz, pos = _decode_cut_in_segments(blob, pos)
    iy = np.repeat(np.cumsum(y_delta), per_row)
    row_id = np.repeat(np.arange(len(per_row)), per_row)
    cumulative = np.cumsum(x_delta)
    row_offsets = np.concatenate([[0], np.cumsum(per_row)[:-1]])
    before_row = np.where(row_offsets > 0, cumulative[np.maximum(row_offsets - 1, 0)], 0)
    ix = cumulative - before_row[row_id]
    cw = 2.0 * err
    xyz = np.column_stack([lower[0] + ix * cw[0], lower[1] + iy * cw[1], lower[2] + iz * cw[2]])
    xyz = np.minimum(xyz, upper)
    if len(xyz) != count:
        raise LepccError("LEPCC point count mismatch")
    return xyz


# ---------------------------------------------------------------------------
# RGB (ClusterRGB.cpp, colours stored per point without colour map)
# ---------------------------------------------------------------------------

def encode_rgb(colors: np.ndarray) -> bytes:
    rgb = np.ascontiguousarray(np.asarray(colors)[:, :3], dtype=np.uint8)
    count = len(rgb)
    # Header1: blobSize (int64), numPoints, numColorsInColormap (0 = raw RGB), lookup 0, index compression 0
    header1_tail = struct.pack("<IHBB", count, 0, 0, 0)
    body = rgb.tobytes()
    blob_size = 16 + 8 + len(header1_tail) + len(body)
    header1 = struct.pack("<q", blob_size) + header1_tail
    checksum = fletcher32(header1 + body)
    return _RGB_KEY + struct.pack("<HI", 1, checksum) + header1 + body


def decode_rgb(blob: bytes) -> np.ndarray:
    if blob[:10] != _RGB_KEY:
        raise LepccError("not a LEPCC rgb blob")
    _version, checksum = struct.unpack("<HI", blob[10:16])
    blob_size, count, colors, method, _compression = struct.unpack("<qIHBB", blob[16:32])
    if fletcher32(blob[16:blob_size]) != checksum:
        raise LepccError("LEPCC rgb checksum mismatch")
    if colors == 0:
        return np.frombuffer(blob[32:32 + 3 * count], dtype=np.uint8).reshape(count, 3)
    colormap = np.frombuffer(blob[32:32 + 3 * colors], dtype=np.uint8).reshape(colors, 3)
    if _compression == 1:  # all points share the first colour
        return np.repeat(colormap[:1], count, axis=0)
    indexes = np.frombuffer(blob[32 + 3 * colors:32 + 3 * colors + count], dtype=np.uint8)
    return colormap[indexes]


def meters_to_degrees(meters: float) -> float:
    """Metres on the equator in degrees (what ArcGIS Pro uses for the LEPCC error of WGS84 layers)."""
    return float(meters) / (2.0 * math.pi * 6378137.0 / 360.0)
