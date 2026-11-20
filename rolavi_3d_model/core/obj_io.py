# obj_io.py - copied unchanged from ROLAVI GEOStudio (rolavi_sfm_studio_pkg/obj_io.py, 2026-11-12).
# Keep both copies in sync: fixes made in GEOStudio go here too.
"""Fast streaming of large Wavefront OBJ models for the GEOStudio Viewer (Qt-free).

A model of several GB is read in 64 MB blocks: the lines of a block are told
apart with numpy (first characters of each line) and their numbers parsed in C
(``numpy.fromstring``), so an OBJ is read at about 100 MB/s with bounded memory.

* ``scan_obj``           - counts (v, vt, vn, f, textured faces, materials), centre and
                           bounding box of the vertices, a regular sample of faces.
* ``write_centered_obj`` - copy of the OBJ with every vertex shifted by an origin
                           (the GEOStudio "master"), collecting the corners of the
                           sampled faces on the way.
* ``preview_points``     - coloured points (face centres, colour from the texture) that
                           the views show for a model too heavy to draw as a mesh.

``progress(fraction, text)`` may raise (e.g. InterruptedError on Cancel).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

Progress = Callable[[float, str], None]

CHUNK_BYTES = 64 << 20
_OTHER, _V, _VT, _VN, _F, _USEMTL, _MTLLIB = 0, 1, 2, 3, 4, 5, 6
_SPACE, _TAB = 32, 9


class ObjError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Blocks of whole lines
# ---------------------------------------------------------------------------

def iter_line_blocks(path: str | Path, chunk_bytes: int = CHUNK_BYTES) -> Iterator[tuple[bytes, int]]:
    """``(block, bytes read so far)``; a block holds whole lines, without its last newline."""
    rest = b""
    done = 0
    with open(path, "rb") as stream:
        while True:
            data = stream.read(chunk_bytes)
            if not data:
                if rest.strip():
                    yield rest, done
                return
            done += len(data)
            data = rest + data
            cut = data.rfind(b"\n")
            if cut < 0:
                rest = data
                continue
            rest = data[cut + 1:]
            yield data[:cut], done


@dataclass
class _Lines:
    arr: np.ndarray      # uint8 view of the block
    starts: np.ndarray   # first byte of each line
    ends: np.ndarray     # end (exclusive) of each line
    kinds: np.ndarray    # _V, _VT ... per line


def _classify(block: bytes) -> _Lines:
    arr = np.frombuffer(block, dtype=np.uint8)
    newlines = np.flatnonzero(arr == 10)
    starts = np.concatenate([[0], newlines + 1]).astype(np.int64)
    ends = np.concatenate([newlines, [len(arr)]]).astype(np.int64)
    pad = np.concatenate([arr, np.zeros(8, dtype=np.uint8)])
    c = [pad[np.minimum(starts + k, len(pad) - 1)] for k in range(7)]

    def ws(x):
        return (x == _SPACE) | (x == _TAB)

    kinds = np.zeros(len(starts), dtype=np.uint8)
    kinds[(c[0] == ord("v")) & ws(c[1])] = _V
    kinds[(c[0] == ord("v")) & (c[1] == ord("t")) & ws(c[2])] = _VT
    kinds[(c[0] == ord("v")) & (c[1] == ord("n")) & ws(c[2])] = _VN
    kinds[(c[0] == ord("f")) & ws(c[1])] = _F
    word = lambda text: np.logical_and.reduce([c[i] == ord(ch) for i, ch in enumerate(text)])  # noqa: E731
    kinds[word("usemtl") & ws(c[6])] = _USEMTL
    kinds[word("mtllib") & ws(c[6])] = _MTLLIB
    return _Lines(arr=arr, starts=starts, ends=ends, kinds=kinds)


def _bytes_of(lines: _Lines, select: np.ndarray) -> np.ndarray:
    """Copy of the bytes of the selected lines (each followed by its newline)."""
    lengths = lines.ends - lines.starts + 1
    mask = np.repeat(select, lengths)[: len(lines.arr)]
    return lines.arr[mask]


def _numbers(data: np.ndarray, letters: bytes, dtype) -> np.ndarray:
    data = data.copy()
    for letter in letters:
        data[data == letter] = _SPACE
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return np.fromstring(data.tobytes(), dtype=dtype, sep=" ")


def _line_text(lines: _Lines, index: int) -> bytes:
    return lines.arr[lines.starts[index]:lines.ends[index]].tobytes()


def _vertex_values(lines: _Lines, select: np.ndarray, letters: bytes, min_columns: int) -> np.ndarray:
    """(n, columns) floats of the selected lines (all with the same number of values)."""
    count = int(select.sum())
    if not count:
        return np.zeros((0, min_columns))
    values = _numbers(_bytes_of(lines, select), letters, np.float64)
    if len(values) % count == 0 and len(values) // count >= min_columns:
        return values.reshape(count, len(values) // count)
    rows = []  # mixed lines (some with colours): slow but exact
    for index in np.flatnonzero(select):
        rows.append([float(p) for p in _line_text(lines, int(index)).split()[1:]])
    width = max(min_columns, max(len(r) for r in rows))
    return np.array([r + [0.0] * (width - len(r)) for r in rows], dtype=np.float64)


# ---------------------------------------------------------------------------
# Faces
# ---------------------------------------------------------------------------

def _face_layout(line: bytes) -> tuple[int, int, int]:
    """(corners, numbers per corner, position of vt or -1) of a face line."""
    tokens = line.split()[1:]
    if not tokens:
        raise ObjError("empty face line")
    first = tokens[0]
    if b"//" in first:
        return len(tokens), 2, -1
    pieces = first.split(b"/")
    return len(tokens), len(pieces), (1 if len(pieces) >= 2 and pieces[1] else -1)


def _parse_faces(lines: _Lines, face_lines: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """1-based (or negative) v and vt indices of the first 3 corners of the given face lines."""
    if not len(face_lines):
        return np.zeros((0, 3), dtype=np.int64), np.zeros((0, 3), dtype=np.int64)
    corners, per, vt_at = _face_layout(_line_text(lines, int(face_lines[0])))
    select = np.zeros(len(lines.kinds), dtype=bool)
    select[face_lines] = True
    values = _numbers(_bytes_of(lines, select), b"f/", np.int64)
    if corners >= 3 and len(values) == len(face_lines) * corners * per:
        table = values.reshape(len(face_lines), corners, per)[:, :3, :]
        v = table[:, :, 0]
        vt = table[:, :, vt_at] if vt_at >= 0 else np.zeros_like(v)
        return v, vt
    v_rows, vt_rows = [], []  # quads / mixed layouts: line by line
    for index in face_lines:
        tokens = _line_text(lines, int(index)).split()[1:4]
        v_row, vt_row = [], []
        for token in tokens:
            pieces = token.split(b"/")
            v_row.append(int(pieces[0]))
            vt_row.append(int(pieces[1]) if len(pieces) >= 2 and pieces[1] else 0)
        while len(v_row) < 3:
            v_row.append(v_row[-1] if v_row else 1)
            vt_row.append(vt_row[-1] if vt_row else 0)
        v_rows.append(v_row)
        vt_rows.append(vt_row)
    return np.array(v_rows, dtype=np.int64), np.array(vt_rows, dtype=np.int64)


def _absolute(indices: np.ndarray, count_before: np.ndarray) -> np.ndarray:
    """OBJ indices (1-based, negative = relative) -> 0-based; 0 / missing -> -1."""
    out = indices - 1
    negative = indices < 0
    if negative.any():
        out = np.where(negative, count_before[:, None] + indices, out)
    out[indices == 0] = -1
    return out


class _FaceSampler:
    """Regular sample of at most ~2 x target faces (the stride doubles when needed)."""

    def __init__(self, target: int):
        self.target = max(0, int(target))
        self.stride = 1
        self.numbers: list[np.ndarray] = []
        self.v: list[np.ndarray] = []
        self.vt: list[np.ndarray] = []
        self.mat: list[np.ndarray] = []
        self.kept = 0

    def keep_mask(self, numbers: np.ndarray) -> np.ndarray:
        return numbers % self.stride == 0

    def add(self, numbers, v, vt, mat) -> None:
        if not len(numbers):
            return
        self.numbers.append(numbers)
        self.v.append(v)
        self.vt.append(vt)
        self.mat.append(mat)
        self.kept += len(numbers)
        while self.kept > 2 * self.target and self.target > 0:
            self.stride *= 2
            for i, nums in enumerate(self.numbers):
                keep = nums % self.stride == 0
                self.numbers[i], self.v[i], self.vt[i], self.mat[i] = nums[keep], self.v[i][keep], self.vt[i][keep], self.mat[i][keep]
            self.kept = sum(len(n) for n in self.numbers)

    def arrays(self):
        if not self.numbers:
            empty = np.zeros((0, 3), dtype=np.int64)
            return empty, empty.copy(), np.zeros(0, dtype=np.int32)
        return np.concatenate(self.v), np.concatenate(self.vt), np.concatenate(self.mat).astype(np.int32)


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

@dataclass
class ObjScan:
    path: Path
    vertices: int = 0
    uvs: int = 0
    normals: int = 0
    faces: int = 0
    textured_faces: int = 0
    mtllib: list = field(default_factory=list)
    materials: list = field(default_factory=list)
    usemtl_lines: int = 0
    vertex_columns: int = 3
    total: np.ndarray = field(default_factory=lambda: np.zeros(3))
    bbox_min: np.ndarray = field(default_factory=lambda: np.full(3, np.inf))
    bbox_max: np.ndarray = field(default_factory=lambda: np.full(3, -np.inf))
    sample_v: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), dtype=np.int64))
    sample_vt: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), dtype=np.int64))
    sample_mat: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int32))
    sample_stride: int = 1

    @property
    def mean(self) -> np.ndarray:
        if not self.vertices:
            raise ObjError(f"{self.path.name} has no vertices.")
        return self.total / float(self.vertices)

    def stats(self) -> dict:
        """Counts with the keys of GEOStudio's OBJ inspection (MTL keys filled by ``mtl_stats``)."""
        return {
            "v": self.vertices, "vt": self.uvs, "vn": self.normals, "f": self.faces,
            "textured_f": self.textured_faces, "mtllib": len(self.mtllib), "usemtl": self.usemtl_lines,
            "map_kd": 0, "mtl_found": 0, "tex_found": 0,
        }


def scan_obj(path: str | Path, sample_faces: int = 0, progress: Progress | None = None) -> ObjScan:
    path = Path(path)
    size = max(1, path.stat().st_size)
    scan = ObjScan(path=path)
    sampler = _FaceSampler(sample_faces)
    material_ids: dict[str, int] = {}
    current_material = -1
    for block, done in iter_line_blocks(path):
        lines = _classify(block)
        kinds = lines.kinds
        is_v = kinds == _V
        is_vt = kinds == _VT
        is_f = kinds == _F
        v_before = scan.vertices + np.cumsum(is_v) - is_v
        vt_before = scan.uvs + np.cumsum(is_vt) - is_vt

        if is_v.any():
            values = _vertex_values(lines, is_v, b"v", 3)
            scan.vertex_columns = max(scan.vertex_columns, values.shape[1])
            xyz = values[:, :3]
            scan.total += xyz.sum(axis=0)
            scan.bbox_min = np.minimum(scan.bbox_min, xyz.min(axis=0))
            scan.bbox_max = np.maximum(scan.bbox_max, xyz.max(axis=0))
        n_v = int(is_v.sum())
        n_vt = int(is_vt.sum())
        scan.normals += int((kinds == _VN).sum())

        for index in np.flatnonzero(kinds == _MTLLIB):
            text = _line_text(lines, int(index)).decode("utf-8", "replace").split(None, 1)
            if len(text) == 2:
                scan.mtllib.append(text[1].strip())
        usemtl_lines = np.flatnonzero(kinds == _USEMTL)
        chunk_materials = []
        for index in usemtl_lines:
            text = _line_text(lines, int(index)).decode("utf-8", "replace").split(None, 1)
            name = text[1].strip() if len(text) == 2 else ""
            if name not in material_ids:
                material_ids[name] = len(material_ids)
                scan.materials.append(name)
            chunk_materials.append(material_ids[name])
        scan.usemtl_lines += len(usemtl_lines)

        face_lines = np.flatnonzero(is_f)
        if len(face_lines):
            layout = _face_layout(_line_text(lines, int(face_lines[0])))
            if layout[2] >= 0:
                scan.textured_faces += len(face_lines)
            if sampler.target:
                numbers = scan.faces + np.arange(len(face_lines))
                keep = sampler.keep_mask(numbers)
                chosen = face_lines[keep]
                if len(chosen):
                    v, vt = _parse_faces(lines, chosen)
                    v = _absolute(v, v_before[chosen])
                    vt = _absolute(vt, vt_before[chosen]) if layout[2] >= 0 else np.full_like(v, -1)
                    if len(usemtl_lines):
                        slot = np.searchsorted(usemtl_lines, chosen, side="right") - 1
                        lookup = np.array(chunk_materials, dtype=np.int32)
                        mat = np.where(slot >= 0, lookup[np.maximum(slot, 0)], current_material).astype(np.int32)
                    else:
                        mat = np.full(len(chosen), current_material, dtype=np.int32)
                    sampler.add(numbers[keep], v, vt, mat)
            scan.faces += len(face_lines)
        if chunk_materials:
            current_material = chunk_materials[-1]
        scan.vertices += n_v
        scan.uvs += n_vt
        if progress is not None:
            progress(done / size, f"Reading OBJ: {int(100 * done / size)}% ({scan.vertices:,} vertices, {scan.faces:,} faces)")
    scan.sample_v, scan.sample_vt, scan.sample_mat = sampler.arrays()
    scan.sample_stride = sampler.stride
    return scan


# ---------------------------------------------------------------------------
# Whole mesh (for the SLPK of a heavy textured OBJ)
# ---------------------------------------------------------------------------

@dataclass
class ObjMesh:
    vertices: np.ndarray                 # (V, 3) float64
    uvs: np.ndarray                      # (T, 2) float32 (may be empty)
    faces: np.ndarray                    # (F, 3) int32, 0-based
    faces_uv: np.ndarray | None          # (F, 3) int32, -1 = none
    faces_material: np.ndarray           # (F,) int16, -1 = none
    materials: list
    mtllib: list
    vertex_colors: np.ndarray | None = None  # (V, 3) float32 (0..1 or 0..255)


def _all_triangles(lines: _Lines, face_lines: np.ndarray):
    """Triangles (fan of each polygon) of the face lines: v, vt (1-based / negative) and the line of each."""
    corners, per, vt_at = _face_layout(_line_text(lines, int(face_lines[0])))
    select = np.zeros(len(lines.kinds), dtype=bool)
    select[face_lines] = True
    values = _numbers(_bytes_of(lines, select), b"f/", np.int64)
    n = len(face_lines)
    if corners >= 3 and len(values) == n * corners * per:
        table = values.reshape(n, corners, per)
        v = table[:, :, 0]
        vt = table[:, :, vt_at] if vt_at >= 0 else None
        if corners == 3:
            return v, vt, np.arange(n)
        fan = np.array([(0, i, i + 1) for i in range(1, corners - 1)])
        line_of = np.repeat(np.arange(n), corners - 2)
        return v[:, fan].reshape(-1, 3), (vt[:, fan].reshape(-1, 3) if vt is not None else None), line_of
    v_rows, vt_rows, line_of = [], [], []  # mixed polygons: line by line
    any_vt = False
    for local, index in enumerate(face_lines):
        tokens = _line_text(lines, int(index)).split()[1:]
        cv, ct = [], []
        for token in tokens:
            pieces = token.split(b"/")
            cv.append(int(pieces[0]))
            has = len(pieces) >= 2 and bool(pieces[1])
            any_vt = any_vt or has
            ct.append(int(pieces[1]) if has else 0)
        for i in range(1, len(cv) - 1):
            v_rows.append((cv[0], cv[i], cv[i + 1]))
            vt_rows.append((ct[0], ct[i], ct[i + 1]))
            line_of.append(local)
    v = np.array(v_rows, dtype=np.int64).reshape(-1, 3)
    vt = np.array(vt_rows, dtype=np.int64).reshape(-1, 3) if any_vt else None
    return v, vt, np.array(line_of, dtype=np.int64)


def read_obj_mesh(path: str | Path, progress: Progress | None = None) -> ObjMesh:
    """All vertices, UVs and triangles (with their material) of an OBJ, read by blocks."""
    path = Path(path)
    size = max(1, path.stat().st_size)
    v_parts, vt_parts, color_parts = [], [], []
    f_parts, ft_parts, fm_parts = [], [], []
    materials: list[str] = []
    material_ids: dict[str, int] = {}
    mtllib: list[str] = []
    current_material = -1
    n_v = n_vt = 0
    has_colors = False
    for block, done in iter_line_blocks(path):
        lines = _classify(block)
        kinds = lines.kinds
        is_v = kinds == _V
        is_vt = kinds == _VT
        v_before = n_v + np.cumsum(is_v) - is_v
        vt_before = n_vt + np.cumsum(is_vt) - is_vt
        if is_v.any():
            values = _vertex_values(lines, is_v, b"v", 3)
            v_parts.append(values[:, :3].copy())
            if values.shape[1] >= 6:
                has_colors = True
                color_parts.append(values[:, 3:6].astype(np.float32))
            else:
                color_parts.append(None)
        if is_vt.any():
            vt_parts.append(_vertex_values(lines, is_vt, b"vt", 2)[:, :2].astype(np.float32))
        for index in np.flatnonzero(kinds == _MTLLIB):
            text = _line_text(lines, int(index)).decode("utf-8", "replace").split(None, 1)
            if len(text) == 2:
                mtllib.append(text[1].strip())
        usemtl_lines = np.flatnonzero(kinds == _USEMTL)
        chunk_materials = []
        for index in usemtl_lines:
            text = _line_text(lines, int(index)).decode("utf-8", "replace").split(None, 1)
            name = text[1].strip() if len(text) == 2 else ""
            if name not in material_ids:
                material_ids[name] = len(materials)
                materials.append(name)
            chunk_materials.append(material_ids[name])
        face_lines = np.flatnonzero(kinds == _F)
        if len(face_lines):
            v, vt, line_of = _all_triangles(lines, face_lines)
            rows = face_lines[line_of]
            f_parts.append(_absolute(v, v_before[rows]).astype(np.int32))
            ft_parts.append(_absolute(vt, vt_before[rows]).astype(np.int32) if vt is not None
                            else np.full((len(v), 3), -1, dtype=np.int32))
            if len(usemtl_lines):
                slot = np.searchsorted(usemtl_lines, rows, side="right") - 1
                lookup = np.array(chunk_materials, dtype=np.int16)
                fm_parts.append(np.where(slot >= 0, lookup[np.maximum(slot, 0)], current_material).astype(np.int16))
            else:
                fm_parts.append(np.full(len(v), current_material, dtype=np.int16))
        if chunk_materials:
            current_material = chunk_materials[-1]
        n_v += int(is_v.sum())
        n_vt += int(is_vt.sum())
        if progress is not None:
            progress(done / size, f"Reading the OBJ: {int(100 * done / size)}% ({n_v:,} vertices)")
    if not v_parts:
        raise ObjError(f"{path.name} has no vertices.")
    if not f_parts:
        raise ObjError(f"{path.name} has no faces.")
    vertices = np.concatenate(v_parts)
    colors = None
    if has_colors:
        colors = np.concatenate([c if c is not None else np.full((len(v), 3), 0.75, dtype=np.float32)
                                 for c, v in zip(color_parts, v_parts)])
    faces = np.concatenate(f_parts)
    faces_uv = np.concatenate(ft_parts)
    keep = (faces >= 0).all(axis=1) & (faces < len(vertices)).all(axis=1)
    if not keep.all():
        faces, faces_uv = faces[keep], faces_uv[keep]
    faces_material = np.concatenate(fm_parts)[keep] if not keep.all() else np.concatenate(fm_parts)
    uvs = np.concatenate(vt_parts) if vt_parts else np.zeros((0, 2), dtype=np.float32)
    if (faces_uv < 0).all() or not len(uvs):
        faces_uv = None
    else:
        faces_uv[faces_uv >= len(uvs)] = -1
    return ObjMesh(vertices, uvs, faces, faces_uv, faces_material, materials, mtllib, colors)


# ---------------------------------------------------------------------------
# Centred copy (vectorised text output)
# ---------------------------------------------------------------------------

def _fixed_field(values: np.ndarray, decimals: int) -> np.ndarray:
    """(n, width) characters of right-aligned fixed-point numbers ('  -12.345678')."""
    scale = 10 ** decimals
    q = np.rint(np.abs(values) * scale).astype(np.int64)
    negative = (values < 0) & (q != 0)
    integer, fraction = np.divmod(q, scale)
    top = int(integer.max()) if len(integer) else 0
    digits = max(1, len(str(top)))
    width = 1 + digits + 1 + decimals
    out = np.full((len(values), width), _SPACE, dtype=np.uint8)
    rest = fraction.copy()
    for i in range(decimals):
        out[:, width - 1 - i] = 48 + rest % 10
        rest //= 10
    point = width - 1 - decimals
    out[:, point] = ord(".")
    rest = integer.copy()
    count = np.ones(len(values), dtype=np.int64)
    for i in range(digits):
        column = point - 1 - i
        show = (rest > 0) | (i == 0)
        out[:, column] = np.where(show, 48 + rest % 10, _SPACE)
        count += (rest >= 10)
        rest //= 10
    rows = np.flatnonzero(negative)
    out[rows, point - 1 - count[rows]] = ord("-")
    return out


def format_vertex_lines(values: np.ndarray, decimals: int = 6, prefix: bytes = b"v") -> list[bytes]:
    """``v x y z [...]`` lines (no newline) of an (n, k) array, formatted with numpy."""
    values = np.asarray(values, dtype=np.float64)
    if not len(values):
        return []
    if not np.isfinite(values).all():
        return [prefix + b" " + b" ".join(repr(float(x)).encode() for x in row) for row in values]
    parts = [np.frombuffer(prefix, dtype=np.uint8)[None, :].repeat(len(values), axis=0)]
    for column in range(values.shape[1]):
        parts.append(np.full((len(values), 1), _SPACE, dtype=np.uint8))
        parts.append(_fixed_field(values[:, column], decimals))
    table = np.ascontiguousarray(np.concatenate(parts, axis=1))
    return table.view(np.dtype((np.bytes_, table.shape[1]))).ravel().tolist()


@dataclass
class CenteredResult:
    path: Path
    vertices: int
    corner_positions: np.ndarray   # (k, 3, 3) shifted positions of the sampled faces' corners
    corner_uvs: np.ndarray | None  # (k, 3, 2) or None
    corner_colors: np.ndarray | None  # (k, 3, 3) vertex colours 0..255 or None


def _gather(needed: np.ndarray, first: int, count: int, values: np.ndarray, store: np.ndarray) -> None:
    if not len(needed) or not count:
        return
    low = np.searchsorted(needed, first)
    high = np.searchsorted(needed, first + count)
    if high > low:
        store[low:high] = values[needed[low:high] - first]


def write_centered_obj(
    src: str | Path,
    dst: str | Path | None,
    origin: np.ndarray,
    scan: ObjScan | None = None,
    progress: Progress | None = None,
) -> CenteredResult:
    """Copy of ``src`` with ``v`` shifted by ``-origin`` and ``mtllib`` reduced to the file name.

    With ``dst=None`` nothing is written: only the corners of the sampled faces are read.
    """
    src = Path(src)
    dst = Path(dst) if dst is not None else None
    if dst is not None:
        dst.parent.mkdir(parents=True, exist_ok=True)
    origin = np.asarray(origin, dtype=np.float64).reshape(3)
    size = max(1, src.stat().st_size)

    sample_v = scan.sample_v if scan is not None else np.zeros((0, 3), dtype=np.int64)
    sample_vt = scan.sample_vt if scan is not None else np.zeros((0, 3), dtype=np.int64)
    need_v = np.unique(sample_v[sample_v >= 0])
    need_vt = np.unique(sample_vt[sample_vt >= 0])
    positions = np.zeros((len(need_v), 3))
    colors = np.zeros((len(need_v), 3)) if scan is not None and scan.vertex_columns >= 6 else None
    uvs = np.zeros((len(need_vt), 2))

    vertices = 0
    uv_count = 0
    tmp = dst.with_suffix(dst.suffix + ".part") if dst is not None else None
    out = open(tmp, "wb") if tmp is not None else None
    try:
        if out is not None:
            out.write(b"# ROLAVI centered import OBJ\n")
            out.write(f"# shift_origin_world: {origin[0]:.9f} {origin[1]:.9f} {origin[2]:.9f}\n".encode())
        for block, done in iter_line_blocks(src):
            lines = _classify(block)
            text = block.split(b"\n") if out is not None else None
            is_v = lines.kinds == _V
            if is_v.any():
                values = _vertex_values(lines, is_v, b"v", 3)
                values[:, :3] -= origin
                _gather(need_v, vertices, len(values), values[:, :3], positions)
                if colors is not None and values.shape[1] >= 6:
                    _gather(need_v, vertices, len(values), values[:, 3:6], colors)
                if out is not None:
                    for index, line in zip(np.flatnonzero(is_v), format_vertex_lines(values)):
                        text[index] = line
                vertices += len(values)
            is_vt = lines.kinds == _VT
            n_vt = int(is_vt.sum())
            if n_vt and len(need_vt):
                low = np.searchsorted(need_vt, uv_count)
                if np.searchsorted(need_vt, uv_count + n_vt) > low:
                    values = _vertex_values(lines, is_vt, b"vt", 2)
                    _gather(need_vt, uv_count, n_vt, values[:, :2], uvs)
            uv_count += n_vt
            if out is not None:
                for index in np.flatnonzero(lines.kinds == _MTLLIB):
                    parts = text[index].decode("utf-8", "replace").split(None, 1)
                    if len(parts) == 2:
                        text[index] = f"mtllib {Path(parts[1].strip()).name}".encode("utf-8")
                out.write(b"\n".join(text))
                out.write(b"\n")
            if progress is not None:
                label = "Writing the centred master OBJ" if out is not None else "Reading the OBJ for the preview"
                progress(done / size, f"{label}: {int(100 * done / size)}%")
    except BaseException:
        if out is not None:
            out.close()
            tmp.unlink(missing_ok=True)
        raise
    if out is not None:
        out.close()
        tmp.replace(dst)

    def corners(needed, table, store):
        index = np.searchsorted(needed, np.maximum(table, 0))
        index = np.minimum(index, max(0, len(needed) - 1))
        return store[index] if len(store) else np.zeros(table.shape + (store.shape[1],))

    corner_positions = corners(need_v, sample_v, positions)
    corner_uvs = corners(need_vt, sample_vt, uvs) if len(need_vt) else None
    corner_colors = corners(need_v, sample_v, colors) if colors is not None else None
    return CenteredResult(dst or src, vertices, corner_positions, corner_uvs, corner_colors)


# ---------------------------------------------------------------------------
# Materials and the coloured preview
# ---------------------------------------------------------------------------

def mtl_textures(obj_path: str | Path, mtllib_names: list[str]) -> tuple[dict[str, Path], dict]:
    """``{material: texture path}`` from the MTL files, and the MTL counts of the OBJ inspection."""
    obj_dir = Path(obj_path).parent
    textures: dict[str, Path] = {}
    counts = {"map_kd": 0, "mtl_found": 0, "tex_found": 0}
    for name in mtllib_names:
        mtl = Path(name)
        if not mtl.is_absolute():
            mtl = obj_dir / mtl
        if not mtl.is_file():
            mtl = obj_dir / Path(name).name
        if not mtl.is_file():
            continue
        counts["mtl_found"] += 1
        material = ""
        for raw in mtl.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            key = parts[0].lower()
            if key == "newmtl":
                material = parts[1].strip() if len(parts) > 1 else ""
            elif key == "map_kd" and len(parts) > 1:
                counts["map_kd"] += 1
                reference = parts[1].strip()
                candidates = [reference, reference.split()[-1]]
                found = None
                for candidate in candidates:
                    texture = Path(candidate)
                    if not texture.is_absolute():
                        texture = mtl.parent / texture
                    if texture.is_file():
                        found = texture
                        break
                if found is None:
                    found = _renamed_texture(mtl.parent, reference.split()[-1])
                if found is not None:
                    counts["tex_found"] += 1
                    textures.setdefault(material, found)
    return textures, counts


def _renamed_texture(folder: Path, name: str) -> Path | None:
    """The texture after a copy renamed it (``texture.jpg`` -> ``texture.jpg.jpeg`` / ``texture.jpeg``)."""
    name = Path(name.strip().strip('"').replace("\\", "/")).name
    if not name:
        return None
    stem = Path(name).stem
    for candidate in (name + ".jpeg", name + ".jpg", name + ".png", stem + ".jpg", stem + ".jpeg", stem + ".png",
                      stem + ".JPG", stem + ".JPEG", stem + ".PNG"):
        if (folder / candidate).is_file():
            return folder / candidate
    return None


def _texture_array(path: Path, max_size: int) -> np.ndarray | None:
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = None
        with Image.open(path) as image:
            image.draft("RGB", (max_size, max_size))  # JPEG: decoded directly at a smaller scale
            image = image.convert("RGB")
            if max(image.size) > max_size:
                image.thumbnail((max_size, max_size))
            return np.asarray(image)
    except Exception:
        return None


def preview_points(
    scan: ObjScan,
    result: CenteredResult,
    textures: dict[str, Path],
    max_texture: int = 2048,
    progress: Progress | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Face centres (float32, centred) and their colours (uint8) of the sampled faces."""
    valid = (scan.sample_v >= 0).all(axis=1)
    centers = result.corner_positions[valid].mean(axis=1)
    count = len(centers)
    colors = np.full((count, 3), 185, dtype=np.uint8)
    if result.corner_colors is not None:
        rgb = result.corner_colors[valid].mean(axis=1)
        if rgb.max() <= 1.0:
            rgb = rgb * 255.0
        colors = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    if result.corner_uvs is not None and textures:
        uv = result.corner_uvs[valid].mean(axis=1)
        has_uv = (scan.sample_vt[valid] >= 0).all(axis=1)
        materials = scan.sample_mat[valid]
        for number, (index, name) in enumerate(enumerate(scan.materials)):
            texture = textures.get(name)
            rows = np.flatnonzero((materials == index) & has_uv)
            if texture is None or not len(rows):
                continue
            if progress is not None:
                progress(number / max(1, len(scan.materials)), f"Colouring the preview from {texture.name}...")
            image = _texture_array(texture, max_texture)
            if image is None:
                continue
            height, width = image.shape[:2]
            u = np.mod(uv[rows, 0], 1.0)
            v = np.mod(uv[rows, 1], 1.0)
            px = np.clip((u * (width - 1)).astype(np.int64), 0, width - 1)
            py = np.clip(((1.0 - v) * (height - 1)).astype(np.int64), 0, height - 1)
            colors[rows] = image[py, px, :3]
        if len(scan.materials) == 0 and len(textures) == 1:  # one texture, faces without usemtl
            image = _texture_array(next(iter(textures.values())), max_texture)
            if image is not None:
                height, width = image.shape[:2]
                rows = np.flatnonzero(has_uv)
                px = np.clip((np.mod(uv[rows, 0], 1.0) * (width - 1)).astype(np.int64), 0, width - 1)
                py = np.clip(((1.0 - np.mod(uv[rows, 1], 1.0)) * (height - 1)).astype(np.int64), 0, height - 1)
                colors[rows] = image[py, px, :3]
    return centers.astype(np.float32), colors

