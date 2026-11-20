"""ROLAVI Viewer client: sign in with a ROLAVI Viewer account and send files to its space.

No Qt and no QGIS: only the Python standard library (urllib), so the same code runs in
VS Code and inside QGIS.

The server API it uses (ROLAVI Viewer, step 27):

* ``POST /api/login {username, password, remember}``  -> session cookie ``rolavi_session``
* ``GET  /api/session`` / ``POST /api/logout``
* ``GET  /api/models``                                -> 3D models of the space in use
* ``POST /api/upload-part?id=&offset=&total=``        -> one piece of a big file
  (Cloudflare Free lets at most 100 MB through in one request: pieces of 48 MB, like the web page)
* ``POST /api/upload?name=<file>.slpk[&overwrite=1][&part=<id>&size=<bytes>]``
* ``POST /api/import?name=<file>&crs=utm&zone=18&south=1&height=auto``
  GeoTIFF (DEM or orthomosaic) -> ``{"job"}`` processed in the background; Shapefile (.zip),
  KML, KMZ, DXF -> the layer at once
* ``GET  /api/jobs/<id>``                             -> progress of that background job

Everything goes to the space of the signed-in account (for the administrator: his own
private space, unless he chose another one in this same session).

``progress(fraction, text)`` may raise ``InterruptedError`` (Cancel), like in ``pipeline``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.cookies import SimpleCookie
from typing import Callable

from .. import __version__

Progress = Callable[[float, str], None]

DEFAULT_URL = "https://viewer.rolavi.net"
COOKIE = "rolavi_session"
PIECE_LIMIT = 32 * 1024 * 1024      # up to this size the file goes in one request
PIECE_SIZE = 32 * 1024 * 1024       # bigger files go in pieces of this size (Cloudflare: 100 MB at most)
RETRY_WAITS = (3, 6, 12, 20)        # seconds between attempts when the server does not answer
#: answers of Cloudflare / the tunnel when the viewer service behind it did not answer
GATEWAY_STATUS = (502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530)
TIMEOUT = 180                       # seconds without an answer on the socket
FINISH_TIMEOUT = 900                # the last call of an upload: the server checks / converts the file
JOB_POLL = 1.5
USER_AGENT = f"ROLAVI-3D-Model/{__version__} (QGIS plugin; Python urllib)"

SLPK_EXT = (".slpk",)
RASTER_EXT = (".tif", ".tiff")
VECTOR_EXT = (".shp", ".zip", ".kml", ".kmz", ".dxf")
SHP_PARTS = (".shp", ".shx", ".dbf", ".prj", ".cpg")
SHP_REQUIRED = (".shp", ".shx", ".dbf")


class ViewerError(Exception):
    """An answer of the server (status = HTTP code) or a network problem (status = 0)."""

    def __init__(self, message: str, status: int = 0, body: dict | None = None):
        super().__init__(message)
        self.status = int(status or 0)
        self.body = body if isinstance(body, dict) else {}

    @property
    def needs_sign_in(self) -> bool:
        return self.status == 401


class _Cancelled(Exception):
    """Cancel inside urllib: not an OSError, so urllib does not turn it into a network error."""


def fmt_bytes(n: float | None) -> str:
    n = float(n or 0)
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024:.0f} KB"


def model_id_for(filename: str) -> str:
    """Id the server gives to an uploaded .slpk (same rules as server.py)."""
    stem = os.path.splitext(os.path.basename(filename.replace("\\", "/")).strip())[0]
    stem = re.sub(r"[^A-Za-z0-9 _().-]+", "_", stem).strip(" .")[:120]
    return re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-") or "model"


def kind_of(path: str) -> str:
    """'slpk', 'raster' or 'vector' by extension ('' if ROLAVI Viewer does not take it)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in SLPK_EXT:
        return "slpk"
    if ext in RASTER_EXT:
        return "raster"
    if ext in VECTOR_EXT:
        return "vector"
    return ""


def shapefile_parts(shp_path: str) -> tuple[list[str], list[str]]:
    """(files of the shapefile found beside the .shp, required extensions that are missing)."""
    folder = os.path.dirname(os.path.abspath(shp_path))
    stem = os.path.splitext(os.path.basename(shp_path))[0].lower()
    found = {}
    for name in os.listdir(folder):
        base, ext = os.path.splitext(name)
        if base.lower() == stem and ext.lower() in SHP_PARTS:
            found[ext.lower()] = os.path.join(folder, name)
    missing = [ext for ext in SHP_REQUIRED if ext not in found]
    return [found[ext] for ext in SHP_PARTS if ext in found], missing


def zip_shapefile(shp_path: str) -> str:
    """Zip the .shp with its .shx, .dbf, .prj, .cpg into a temporary <name>.zip (the format the server takes)."""
    parts, missing = shapefile_parts(shp_path)
    if missing:
        raise ViewerError("The shapefile is incomplete: %s missing beside %s."
                          % (", ".join(missing), os.path.basename(shp_path)))
    stem = os.path.splitext(os.path.basename(shp_path))[0]
    folder = tempfile.mkdtemp(prefix="rolavi_shp_")
    zip_path = os.path.join(folder, stem + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for part in parts:
            zf.write(part, os.path.basename(part))
    return zip_path


class _Reporter:
    """Turns bytes sent into progress(fraction, text) calls, a few hundred per file at most.
    check() repeats the last state: it is how Cancel is noticed while waiting."""

    def __init__(self, progress: Progress | None, total: int, label: str):
        self.progress = progress
        self.total = max(int(total), 1)
        self.label = label
        self._fraction = 0.0
        self._text = f"{label}…"

    def _emit(self) -> None:
        if self.progress:
            try:
                self.progress(self._fraction, self._text)
            except InterruptedError:
                raise _Cancelled()

    def check(self) -> None:
        self._emit()

    def set(self, fraction: float, text: str) -> None:
        self._fraction, self._text = max(0.0, min(float(fraction), 1.0)), text
        self._emit()

    def sent(self, done: int) -> None:
        fraction = min(done / self.total, 1.0)
        if 0 <= fraction - self._fraction < 0.003 and fraction < 1.0:
            return
        self.set(fraction, f"{self.label}: {fmt_bytes(done)} of {fmt_bytes(self.total)}")


class _SliceReader:
    """File-like object over bytes [offset, offset + length) of a file, reporting what was read.
    With ``last=True`` (the request that ends the upload) it says so when everything was read."""

    def __init__(self, path: str, offset: int, length: int, report: _Reporter, base: int, last: bool):
        self._fh = open(path, "rb")
        self._fh.seek(offset)
        self._left = length
        self._read = 0
        self._report = report
        self._base = base
        self._last = last

    def read(self, size: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        n = self._left if size is None or size < 0 else min(size, self._left)
        data = self._fh.read(min(n, 1024 * 1024))
        self._left -= len(data)
        self._read += len(data)
        self._report.sent(self._base + self._read)
        if self._left <= 0 and self._last:
            self._report.set(1.0, "Checking the file on the server…")
        return data

    def close(self) -> None:
        self._fh.close()


class ViewerClient:
    """One ROLAVI Viewer account on one server. Not thread-safe: one call at a time."""

    def __init__(self, base_url: str = DEFAULT_URL, token: str = ""):
        self.base_url = self.normalize_url(base_url)
        self.token = token or ""
        self.session: dict | None = None
        self.piece_limit = PIECE_LIMIT
        self.piece_size = PIECE_SIZE
        self._ssl = ssl.create_default_context()

    # ------------------------------------------------------------------ basics
    @staticmethod
    def normalize_url(url: str) -> str:
        url = (url or "").strip().rstrip("/")
        if not url:
            return DEFAULT_URL
        if not re.match(r"^https?://", url, re.I):
            local = re.match(r"^(localhost|127\.|192\.168\.|10\.)", url)
            url = ("http://" if local else "https://") + url
        return url

    @property
    def signed_in(self) -> bool:
        return bool(self.token and self.session and self.session.get("authenticated"))

    def _take_cookie(self, headers) -> None:
        for value in headers.get_all("Set-Cookie") or []:
            jar = SimpleCookie()
            try:
                jar.load(value)
            except Exception:
                continue
            morsel = jar.get(COOKIE)
            if morsel is not None:
                self.token = morsel.value or ""

    def _request(self, method: str, path: str, query: dict | None = None, json_body: dict | None = None,
                 body=None, length: int = 0, timeout: float = TIMEOUT) -> tuple[int, dict | list]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif body is not None:
            data = body
            headers["Content-Type"] = "application/octet-stream"
            headers["Content-Length"] = str(int(length))
        elif method in ("POST", "PUT"):
            data = b""
        if self.token:
            headers["Cookie"] = f"{COOKIE}={self.token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl) as resp:
                status, raw = resp.status, resp.read()
                self._take_cookie(resp.headers)
                content_type = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read() or b""
            self._take_cookie(exc.headers)
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, InterruptedError):
                raise _Cancelled()
            if isinstance(exc.reason, ssl.SSLError):
                raise ViewerError(f"Secure connection to {self.base_url} failed: {exc.reason}")
            raise ViewerError(f"Cannot reach {self.base_url}: {exc.reason}")
        except (socket.timeout, TimeoutError):
            raise ViewerError("The server did not answer in time.")
        except (ConnectionError, OSError) as exc:
            if isinstance(exc, InterruptedError):
                raise _Cancelled()
            raise ViewerError(f"Connection lost: {exc}")
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            payload = {}
            if status in (403, 503) and "json" not in content_type:
                payload = {"error": f"The request was blocked before reaching ROLAVI Viewer (HTTP {status}). "
                                    "If it is Cloudflare, turn off 'Bot Fight Mode' for this site, or test "
                                    "with the server running on your PC (http://localhost:8080)."}
            elif status == 413:
                payload = {"error": "The file is too big to send in one piece."}
            elif status in GATEWAY_STATUS:
                payload = {"error": f"ROLAVI Viewer did not answer (HTTP {status} from Cloudflare): the viewer "
                                    "service on the server stopped or is restarting. Try again in a minute; if it "
                                    "happens again, look at the server log (journalctl -u rolavi-viewer)."}
        if not isinstance(payload, (dict, list)):
            payload = {}
        if status == 401 and path != "/api/login":
            self.session = None     # the session ended (plan expired, signed out elsewhere, server restarted)
        return status, payload

    @staticmethod
    def _fail(status: int, body, default: str) -> ViewerError:
        body = body if isinstance(body, dict) else {}
        return ViewerError(body.get("error") or f"{default} (HTTP {status}).", status, body)

    # ------------------------------------------------------------------ account
    def login(self, username: str, password: str, remember: bool = True) -> dict:
        self.token = ""
        status, body = self._request("POST", "/api/login", json_body={
            "username": (username or "").strip(), "password": password or "", "remember": bool(remember)})
        if status != 200 or not self.token or not isinstance(body, dict):
            self.token = ""
            raise self._fail(status, body, "Sign-in failed")
        self.session = body
        return body

    def check_session(self) -> dict | None:
        """The session of the saved token, or None (and the token is dropped) if it ended."""
        if not self.token:
            self.session = None
            return None
        status, body = self._request("GET", "/api/session")
        if status != 200 or not isinstance(body, dict):
            raise self._fail(status, body, "Could not read the session")
        if not body.get("authenticated"):
            self.token, self.session = "", None
            return None
        self.session = body
        return body

    def logout(self) -> None:
        try:
            if self.token:
                self._request("POST", "/api/logout", timeout=20)
        except ViewerError:
            pass
        finally:
            self.token, self.session = "", None

    def server_info(self) -> dict:
        """What the server can take (``/api/info``): e.g. ``orthoMosaic`` = orthomosaics in pieces."""
        status, body = self._request("GET", "/api/info")
        if status != 200 or not isinstance(body, dict):
            raise self._fail(status, body, "Could not read the server information")
        return body

    def models(self) -> list:
        status, body = self._request("GET", "/api/models")
        if status != 200 or not isinstance(body, list):
            raise self._fail(status, body, "Could not list the models")
        return body

    def model_exists(self, filename: str) -> bool:
        mid = model_id_for(filename)
        return any(str(m.get("id", "")).split("~")[-1] == mid for m in self.models() if isinstance(m, dict))

    # ------------------------------------------------------------------ sending
    def _post_slice(self, endpoint: str, query: dict, path: str, offset: int, length: int,
                    report: _Reporter, base: int, timeout: float, last: bool = False) -> tuple[int, dict]:
        reader = _SliceReader(path, offset, length, report, base, last)
        try:
            status, body = self._request("POST", endpoint, query, body=reader, length=length, timeout=timeout)
        finally:
            reader.close()
        return status, body if isinstance(body, dict) else {}

    @staticmethod
    def _retryable(status: int) -> bool:
        """No answer, or the tunnel / server failed (not 507 = disk full, which does not get better)."""
        return status == 0 or status in GATEWAY_STATUS or (status >= 500 and status != 507)

    @staticmethod
    def _wait(report: _Reporter, seconds: float, why: str) -> None:
        steps = int(seconds * 10)
        for i in range(steps):
            if i % 10 == 0:
                report.set(report._fraction, f"{why}: trying again in {int((steps - i) / 10)} s…")
            time.sleep(0.1)

    def _try_post(self, endpoint: str, query: dict, path: str | None, offset: int, length: int,
                  report: _Reporter, timeout: float, last: bool = False) -> tuple[int, dict]:
        """One request; a network problem comes back as status 0 instead of an exception."""
        try:
            if path is None:
                status, answer = self._request("POST", endpoint, query, timeout=timeout)
                return status, answer if isinstance(answer, dict) else {}
            return self._post_slice(endpoint, query, path, offset, length, report, offset, timeout, last)
        except ViewerError as exc:
            return 0, {"error": str(exc)}

    def _send_file(self, path: str, endpoint: str, query: dict, report: _Reporter) -> tuple[int, dict, bool]:
        """Send the file to endpoint. Returns (status, answer, uncertain): uncertain = an earlier attempt of
        the last request may have reached the server although its answer was lost."""
        total = os.path.getsize(path)
        if total <= 0:
            raise ViewerError("The file is empty.")

        if total <= self.piece_limit:
            uncertain = False
            for attempt in range(len(RETRY_WAITS) + 1):
                status, body = self._try_post(endpoint, query, path, 0, total, report, FINISH_TIMEOUT, last=True)
                if not self._retryable(status) or attempt == len(RETRY_WAITS):
                    return status, body, uncertain
                uncertain = True
                self._wait(report, RETRY_WAITS[attempt], f"No answer from the server ({status or 'network'})")

        # In pieces. After a cut (or a restart of the service) the server says how much it already has
        # (409 + size) and the upload goes on from there.
        part_id = secrets.token_hex(16)
        offset, failures = 0, 0
        while offset < total:
            length = min(self.piece_size, total - offset)
            piece = {"id": part_id, "offset": offset, "total": total}
            status, body = self._try_post("/api/upload-part", piece, path, offset, length, report, TIMEOUT)
            if status == 200:
                offset, failures = offset + length, 0
                continue
            have = body.get("size")
            if status == 409 and isinstance(have, int) and have != offset and -1 <= have <= total:
                failures += 1
                if failures > len(RETRY_WAITS):
                    return status, body, False
                offset = max(have, 0)          # -1 = the server lost the pieces: start again
                continue
            # 400 "Upload interrupted": the piece did not arrive whole and the server dropped it: send it again
            cut = status == 400 and "interrupted" in str(body.get("error", "")).lower()
            if not (cut or self._retryable(status)) or failures >= len(RETRY_WAITS):
                return status, body, False
            self._wait(report, RETRY_WAITS[failures], f"No answer from the server ({status or 'network'})")
            failures += 1

        final = dict(query, part=part_id, size=total)
        uncertain = False
        for attempt in range(len(RETRY_WAITS) + 1):
            report.set(1.0, "Checking the file on the server…")
            status, answer = self._try_post(endpoint, final, None, 0, 0, report, FINISH_TIMEOUT)
            if not self._retryable(status) or attempt == len(RETRY_WAITS):
                return status, answer, uncertain
            uncertain = True
            self._wait(report, RETRY_WAITS[attempt], f"No answer from the server ({status or 'network'})")
        return status, answer, uncertain

    def upload_slpk(self, path: str, overwrite: bool = False, progress: Progress | None = None) -> dict:
        """Send a .slpk to the space; returns the model as the server lists it."""
        name = os.path.basename(path)
        if kind_of(path) != "slpk":
            raise ViewerError("The file must have the .slpk extension.")
        query = {"name": name}
        if overwrite:
            query["overwrite"] = "1"
        report = _Reporter(progress, os.path.getsize(path), f"Uploading {name}")
        try:
            status, body, uncertain = self._send_file(path, "/api/upload", query, report)
            if status == 200 and body.get("ok"):
                return body.get("model") or {}
            if uncertain:      # an attempt whose answer was lost may have saved it: look before failing
                report.set(1.0, "Checking whether the model arrived…")
                model = self._find_model(name)
                if model:
                    return model
        except _Cancelled:
            raise InterruptedError("Cancelled by the user.")
        raise self._fail(status, body, "Upload failed")

    def _find_model(self, filename: str) -> dict | None:
        mid = model_id_for(filename)
        try:
            models = self.models()
        except ViewerError:
            return None
        return next((m for m in models if isinstance(m, dict) and str(m.get("id", "")).split("~")[-1] == mid), None)

    def import_file(self, path: str, utm_zone: int | None = 18, south: bool = True, height: str = "auto",
                    progress: Progress | None = None, log: Callable[[str], None] | None = None) -> dict:
        """Send a DEM / orthomosaic (GeoTIFF) or a vector layer (Shapefile, KML, KMZ, DXF).

        ``utm_zone`` / ``south`` are only used when the file brings no coordinate system
        (GeoTIFF without EPSG, shapefile without .prj, DXF). Returns the layer metadata
        (for a GeoTIFF, after the server finished processing it).
        """
        kind = kind_of(path)
        if kind not in ("raster", "vector"):
            raise ViewerError("ROLAVI Viewer takes GeoTIFF (.tif), Shapefile (.shp or .zip), KML, KMZ and DXF.")
        send_path, temp = path, None
        if path.lower().endswith(".shp"):
            send_path = temp = zip_shapefile(path)
        try:
            name = os.path.basename(send_path)
            query = {"name": name, "height": height or "auto"}
            if utm_zone:
                query.update({"crs": "utm", "zone": str(int(utm_zone)), "south": "1" if south else "0"})
            report = _Reporter(progress, os.path.getsize(send_path), f"Uploading {name}")
            try:
                status, body, uncertain = self._send_file(send_path, "/api/import", query, report)
                if status == 202 and isinstance(body.get("job"), dict):
                    if log:
                        log("   Uploaded. The server is processing the GeoTIFF (it can take a few minutes)…")
                    return self._follow_job(body["job"]["id"], progress)
            except _Cancelled:
                raise InterruptedError("Cancelled by the user.")
        finally:
            if temp:
                shutil.rmtree(os.path.dirname(temp), ignore_errors=True)
        if status == 200 and body.get("ok"):
            return body.get("layer") or {}
        if uncertain and status == 400:
            raise ViewerError(f"The connection was lost while the server was saving {os.path.basename(path)}. "
                              "Look in ROLAVI Viewer whether the layer is there; if not, send it again.", status, body)
        raise self._fail(status, body, "Import failed")

    def _follow_job(self, job_id: str, progress: Progress | None) -> dict:
        failures = 0
        report = _Reporter(progress, 1, "Server: processing")
        while True:
            for _ in range(int(JOB_POLL * 10)):
                report.check()
                time.sleep(0.1)
            try:
                status, job = self._request("GET", "/api/jobs/" + urllib.parse.quote(job_id), timeout=30)
            except ViewerError:
                failures += 1
                if failures > 20:
                    raise ViewerError("Lost the connection with the server while it was processing the file. "
                                      "Look in ROLAVI Viewer: the layer may still appear.")
                continue
            failures = 0
            if status != 200 or not isinstance(job, dict):
                raise self._fail(status, job, "Could not follow the processing")
            state = job.get("state")
            if state == "done":
                return job.get("layer") or {}
            if state == "error":
                raise ViewerError(job.get("error") or "The server could not process the file.")
            report.set(float(job.get("progress") or 0.0), f"Server: {job.get('stage') or 'Processing'}")
