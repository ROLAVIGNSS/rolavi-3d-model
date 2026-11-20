"""Step 6 "ROLAVI Viewer": sign in with a ROLAVI Viewer account and send files to its space.

* Sign in / Sign out. *Keep me signed in* keeps the session (30 days) in the settings of this
  computer; the password is never saved.
* Send exported SLPK (the file of step 4), or any SLPK, DEM / orthomosaic (GeoTIFF) or vector
  layer (Shapefile, KML, KMZ, DXF) from the disk. Inside QGIS the next step adds "send this layer".
* An orthomosaic GeoTIFF is converted first to what ROLAVI Viewer stores: a .zip with a JPG and its
  .aux.xml (SRS + GeoTransform), several times smaller than the GeoTIFF; a DEM goes as GeoTIFF.
  A big orthomosaic goes in pieces (JPG + .aux.xml each, and mosaic.json) at its full resolution,
  when the server takes them (``/api/info`` orthoMosaic, ROLAVI Viewer Step 28): the server makes
  the tiles one piece at a time, so it never needs the memory of the whole image.
* The uploads use the progress bar and Cancel of the window; sign-in and the checks run in a
  quiet thread so the window never freezes.
"""

from __future__ import annotations

import datetime
import os
import shutil
import tempfile
from pathlib import Path

from ..core import raster_io
from ..core import viewer_client as vc
from .datum_dialog import ask_crs
from .qt import QtCore, QtGui, QtWidgets, exec_
from .widgets import card, field_label
from .worker import TaskThread

QUIET_WAIT_MS = 25_000
FILTER_SLPK = "Scene Layer Package (*.slpk)"
FILTER_RASTER = "GeoTIFF: DEM or orthomosaic (*.tif *.tiff)"
FILTER_VECTOR = "Vector layer (*.shp *.zip *.kml *.kmz *.dxf)"


def _date(ms) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ms) / 1000.0).strftime("%d %b %Y")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


class ViewerPanel(QtWidgets.QWidget):
    """Two cards: the account, and what to send to its space."""

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host                      # MainWindow: settings, log(), _start_task(), task, out_edit
        self.settings = host.settings
        remember = bool(self.settings.value("viewer/remember", True, type=bool))
        token = str(self.settings.value("viewer/token", "", type=str) or "") if remember else ""
        url = str(self.settings.value("viewer/url", vc.DEFAULT_URL, type=str) or vc.DEFAULT_URL)
        self.client = vc.ViewerClient(url, token)
        self._quiet_tasks: list[TaskThread] = []
        self._host_busy = False
        self._lost = False                    # set from a worker thread when the server says 401
        self._temp_dirs: list[str] = []       # orthomosaics converted to JPG for sending
        self._build(remember)
        self._connect()
        self.refresh_state(False)
        if token:
            QtCore.QTimer.singleShot(0, self.refresh_session)

    # ================================================================== UI
    def _build(self, remember: bool) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        account_card, account_layout = card("Account")
        account_layout.addWidget(field_label("Server"))
        self.url_edit = QtWidgets.QLineEdit(self.client.base_url)
        self.url_edit.setPlaceholderText(vc.DEFAULT_URL)
        account_layout.addWidget(self.url_edit)

        self.stack = QtWidgets.QStackedWidget()
        signed_out = QtWidgets.QWidget()
        out_layout = QtWidgets.QVBoxLayout(signed_out)
        out_layout.setContentsMargins(0, 0, 0, 0)
        out_layout.setSpacing(8)
        out_layout.addWidget(field_label("User"))
        self.user_edit = QtWidgets.QLineEdit(str(self.settings.value("viewer/user", "", type=str) or ""))
        out_layout.addWidget(self.user_edit)
        out_layout.addWidget(field_label("Password"))
        self.pass_edit = QtWidgets.QLineEdit()
        self.pass_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        out_layout.addWidget(self.pass_edit)
        self.remember_check = QtWidgets.QCheckBox("Keep me signed in (30 days)")
        self.remember_check.setChecked(remember)
        self.login_btn = QtWidgets.QPushButton("Sign in")
        self.login_btn.setObjectName("Primary")
        login_row = QtWidgets.QHBoxLayout()
        login_row.addWidget(self.remember_check)
        login_row.addStretch(1)
        login_row.addWidget(self.login_btn)
        out_layout.addLayout(login_row)

        signed_in = QtWidgets.QWidget()
        in_layout = QtWidgets.QVBoxLayout(signed_in)
        in_layout.setContentsMargins(0, 0, 0, 0)
        in_layout.setSpacing(8)
        self.account_label = self.host._label("", "Info")
        account_row = QtWidgets.QHBoxLayout()
        self.open_btn = QtWidgets.QPushButton("Open ROLAVI Viewer  ↗")
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.logout_btn = QtWidgets.QPushButton("Sign out")
        self.logout_btn.setObjectName("Ghost")
        account_row.addWidget(self.open_btn)
        account_row.addStretch(1)
        account_row.addWidget(self.refresh_btn)
        account_row.addWidget(self.logout_btn)
        in_layout.addWidget(self.account_label)
        in_layout.addLayout(account_row)
        self.stack.addWidget(signed_out)
        self.stack.addWidget(signed_in)
        account_layout.addWidget(self.stack)
        layout.addWidget(account_card)

        send_card, send_layout = card("Send to your space")
        self.send_export_btn = QtWidgets.QPushButton("Send exported SLPK")
        self.send_export_btn.setObjectName("Primary")
        send_layout.addWidget(self.send_export_btn)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        self.send_slpk_btn = QtWidgets.QPushButton("SLPK…")
        self.send_slpk_btn.setToolTip("A 3D model (.slpk) from the disk")
        self.send_raster_btn = QtWidgets.QPushButton("DEM / ortho…")
        self.send_raster_btn.setToolTip("A DEM (sent as GeoTIFF) or an orthomosaic (sent as JPG + .aux.xml, "
                                        "much smaller) from a GeoTIFF file")
        self.send_vector_btn = QtWidgets.QPushButton("Vector…")
        self.send_vector_btn.setToolTip("Shapefile (.shp or zipped), KML, KMZ or DXF")
        grid.addWidget(self.send_slpk_btn, 0, 0)
        grid.addWidget(self.send_raster_btn, 0, 1)
        grid.addWidget(self.send_vector_btn, 0, 2)
        send_layout.addLayout(grid)

        crs_row = QtWidgets.QHBoxLayout()
        crs_row.addWidget(self.host._label("If the file has no CRS: WGS 84 / UTM zone"))
        self.zone_spin = QtWidgets.QSpinBox()
        self.zone_spin.setRange(1, 60)
        self.zone_spin.setValue(int(self.settings.value("viewer/zone", 18, type=int) or 18))
        self.zone_spin.setAccessibleName("UTM zone")
        self.hemi_combo = QtWidgets.QComboBox()
        self.hemi_combo.addItem("South", True)
        self.hemi_combo.addItem("North", False)
        self.hemi_combo.setAccessibleName("Hemisphere")
        self.hemi_combo.setCurrentIndex(0 if self.settings.value("viewer/south", True, type=bool) else 1)
        crs_row.addWidget(self.zone_spin)
        crs_row.addWidget(self.hemi_combo)
        crs_row.addStretch(1)
        send_layout.addLayout(crs_row)

        self.status_label = self.host._label("")
        send_layout.addWidget(self.status_label)
        layout.addWidget(send_card)

    def _connect(self) -> None:
        self.url_edit.editingFinished.connect(self._url_changed)
        self.login_btn.clicked.connect(self.login)
        self.pass_edit.returnPressed.connect(self.login)
        self.logout_btn.clicked.connect(self.logout)
        self.refresh_btn.clicked.connect(self.refresh_session)
        self.open_btn.clicked.connect(self.open_viewer)
        self.send_export_btn.clicked.connect(self.send_exported)
        self.send_slpk_btn.clicked.connect(self.choose_slpk)
        self.send_raster_btn.clicked.connect(self.choose_raster)
        self.send_vector_btn.clicked.connect(self.choose_vector)
        self.zone_spin.valueChanged.connect(lambda v: self.settings.setValue("viewer/zone", int(v)))
        self.hemi_combo.currentIndexChanged.connect(
            lambda _i: self.settings.setValue("viewer/south", bool(self.hemi_combo.currentData())))

    # ================================================================== state
    def can_send(self) -> bool:
        return bool(self.client.token)

    def _status(self, text: str, kind: str = "Muted") -> None:
        self.host._set_label(self.status_label, text, kind)

    def _exported_path(self) -> str:
        text = self.host.out_edit.text().strip()
        return text if text and Path(text).is_file() and text.lower().endswith(".slpk") else ""

    def refresh_state(self, busy: bool | None = None) -> None:
        """Called by the window whenever its state changes (busy = a job of the window is running)."""
        if busy is not None:
            self._host_busy = bool(busy)
        if self._lost:
            self._lost = False
            self._forget_session("Your ROLAVI Viewer session ended: sign in again.")
        quiet = bool(self._quiet_tasks)
        idle = not self._host_busy and not quiet
        signed = bool(self.client.token)
        self.stack.setCurrentIndex(1 if signed else 0)
        self._show_account()
        self.url_edit.setEnabled(idle)
        for widget in (self.user_edit, self.pass_edit, self.remember_check, self.login_btn,
                       self.logout_btn, self.refresh_btn):
            widget.setEnabled(idle)
        for widget in (self.send_slpk_btn, self.send_raster_btn, self.send_vector_btn):
            widget.setEnabled(idle and signed)
        exported = self._exported_path()
        self.send_export_btn.setEnabled(idle and signed and bool(exported))
        self.send_export_btn.setText(f"Send exported SLPK  ·  {Path(exported).name}" if exported else "Send exported SLPK")
        self.send_export_btn.setToolTip(exported or "Export the SLPK in step 4 first.")
        notify = getattr(self.host, "viewer_state_changed", None)
        if notify is not None:
            notify()

    def _show_account(self) -> None:
        s = self.client.session
        if not self.client.token:
            self.account_label.setText("")
            return
        if not s:
            user = self.user_edit.text().strip() or "?"
            note = "checking…" if self._quiet_tasks else "not confirmed: press Refresh"
            self.account_label.setText(f"Saved session of {user} on {self.client.base_url} ({note}).")
            return
        user = s.get("user") or {}
        role = "administrator" if user.get("role") == "admin" else "user"
        lines = [f"Signed in as {user.get('name') or user.get('username')} ({user.get('username')}), {role}"]
        space = (s.get("space") or {}).get("label") or "My private space"
        storage = s.get("storage") or {}
        used = vc.fmt_bytes(storage.get("used"))
        quota = storage.get("quota")
        text = f"Space: {space}  ·  {used} used of {vc.fmt_bytes(quota)}" if quota else \
            f"Space: {space}  ·  {used} used (no cap)"
        end = _date((s.get("plan") or {}).get("end"))
        if end:
            text += f"  ·  plan until {end}"
        lines.append(text)
        self.account_label.setText("\n".join(lines))

    def _forget_session(self, message: str = "") -> None:
        self.client.token, self.client.session = "", None
        self.settings.remove("viewer/token")
        if message:
            self._status(message, "Bad")
            self.host.log(f"⚠ {message}")

    def _save_session(self) -> None:
        self.settings.setValue("viewer/url", self.client.base_url)
        self.settings.setValue("viewer/user", self.user_edit.text().strip())
        self.settings.setValue("viewer/remember", self.remember_check.isChecked())
        if self.remember_check.isChecked() and self.client.token:
            self.settings.setValue("viewer/token", self.client.token)
        else:
            self.settings.remove("viewer/token")

    def _url_changed(self) -> None:
        url = vc.ViewerClient.normalize_url(self.url_edit.text())
        self.url_edit.setText(url)
        if url == self.client.base_url:
            return
        if self.client.token:
            self._forget_session()
            self._status("Server changed: sign in again.", "Muted")
        self.client = vc.ViewerClient(url)
        self.settings.setValue("viewer/url", url)
        self.refresh_state()

    # ================================================================== quiet jobs
    def _quiet(self, job, on_ok, on_fail=None, watch_session: bool = True) -> None:
        """Run job() in a thread without the window's progress bar; on_ok(result) / on_fail(message).
        With watch_session, an answer 401 means the saved session ended (not for the sign-in itself)."""
        def run(_progress, _log):
            try:
                return job()
            except vc.ViewerError as exc:
                if exc.needs_sign_in and watch_session:
                    self._lost = True
                raise

        task = TaskThread("ROLAVI Viewer", run, self)
        task.succeeded.connect(lambda result: self._quiet_end(task, on_ok, result))
        task.failed.connect(lambda message, _details: self._quiet_end(task, on_fail, message, failed=True))
        task.finished.connect(task.deleteLater)
        self._quiet_tasks.append(task)
        self.refresh_state()
        task.start()

    def _quiet_end(self, task, callback, value, failed: bool = False) -> None:
        if task in self._quiet_tasks:
            self._quiet_tasks.remove(task)
        if failed and self._lost:
            self.refresh_state()      # shows "session ended" instead of the raw message
            return
        try:
            if callback is not None:
                callback(value)
            elif failed:
                self._status(str(value), "Bad")
                self.host.log(f"❌ ROLAVI Viewer: {value}")
        finally:
            self.refresh_state()

    def shutdown(self) -> None:
        """The window closes: let a sign-in or a check that is still running end first."""
        for task in list(self._quiet_tasks):
            task.wait(QUIET_WAIT_MS)
        for folder in self._temp_dirs:
            shutil.rmtree(folder, ignore_errors=True)
        self._temp_dirs = []

    # ================================================================== account
    def login(self) -> None:
        if self._quiet_tasks or self._host_busy:
            return
        user = self.user_edit.text().strip()
        password = self.pass_edit.text()
        if not user or not password:
            self._status("Write the user and the password of your ROLAVI Viewer account.", "Bad")
            return
        self._url_changed()
        remember = self.remember_check.isChecked()
        client = self.client
        self._status(f"Signing in to {client.base_url}…", "Info")

        def done(session) -> None:
            self.pass_edit.clear()
            self._save_session()
            name = (session.get("user") or {}).get("username", user)
            self._status("Signed in.", "Good")
            self.host.log(f"   ROLAVI Viewer: signed in as {name} on {client.base_url}")

        def failed(message) -> None:
            self.pass_edit.selectAll()
            self.pass_edit.setFocus()
            self._status(str(message), "Bad")
            self.host.log(f"❌ ROLAVI Viewer: {message}")

        self._quiet(lambda: client.login(user, password, remember), done, failed, watch_session=False)

    def logout(self) -> None:
        client = self.client

        def done(_result) -> None:
            self._forget_session()
            self._status("Signed out.", "Muted")
            self.host.log("   ROLAVI Viewer: signed out.")

        self._quiet(client.logout, done, done)

    def refresh_session(self) -> None:
        if not self.client.token or self._quiet_tasks:
            return
        client = self.client

        def done(session) -> None:
            if session is None:
                self._forget_session("Your ROLAVI Viewer session ended: sign in again.")
            else:
                self._save_session()
                self._status("")

        def failed(message) -> None:
            self._status(f"Could not reach ROLAVI Viewer: {message}", "Bad")

        self._quiet(client.check_session, done, failed)

    def open_viewer(self) -> None:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(self.client.base_url + "/"))

    # ================================================================== sending
    def _last_dir(self) -> str:
        return str(self.settings.value("viewer/last_dir", "", type=str) or "") or str(Path.home())

    def _choose(self, title: str, file_filter: str) -> str:
        path, _f = QtWidgets.QFileDialog.getOpenFileName(self, title, self._last_dir(), file_filter)
        if path:
            self.settings.setValue("viewer/last_dir", str(Path(path).parent))
        return path

    def send_exported(self) -> None:
        path = self._exported_path()
        if not path:
            QtWidgets.QMessageBox.information(self, self.host.windowTitle(), "Export the SLPK in step 4 first.")
            return
        self.send_slpk(path)

    def choose_slpk(self) -> None:
        path = self._choose("Send an SLPK to ROLAVI Viewer", FILTER_SLPK)
        if path:
            self.send_slpk(path)

    def choose_raster(self) -> None:
        path = self._choose("Send a DEM or an orthomosaic (GeoTIFF) to ROLAVI Viewer", FILTER_RASTER)
        if path:
            self.send_raster(path)

    def send_raster(self, path: str, info: raster_io.RasterInfo | None = None) -> None:
        """A GeoTIFF: a DEM goes as it is; an orthomosaic is converted to JPG + .aux.xml first."""
        if self._quiet_tasks or self._host_busy or self.host.task is not None:
            return
        if not self.client.token:
            self._status("Sign in to send it to ROLAVI Viewer.", "Bad")
            return
        backend = self.host.backend
        if info is None:
            try:
                info = raster_io.inspect(path, backend)
            except (raster_io.RasterError, OSError) as exc:
                self._status(str(exc), "Bad")
                QtWidgets.QMessageBox.warning(self, self.host.windowTitle(), str(exc))
                return
        if info.kind != raster_io.KIND_ORTHO:
            self.send_layer(path)
            return
        if not info.crs:
            code = ask_crs(self, backend, info.name, ["The GeoTIFF has no coordinate system."],
                           self.host._project_crs_code(), self.host._recent_crs())
            if not code:
                return
            info.crs, info.crs_source = code, "chosen by you"
            raster_io._set_geographic(info, backend)
        wkt = info.crs if not info.crs.startswith("EPSG:") else ""
        try:
            problem = raster_io.viewer_crs_problem(wkt or (backend.to_wkt(info.crs) if backend else ""))
        except Exception as exc:          # the CRS library cannot write it: let the conversion say so
            problem = "" if backend is None else str(exc)
        if problem:
            self._status(problem, "Bad")
            QtWidgets.QMessageBox.warning(self, self.host.windowTitle(), problem)
            return
        client = self.client
        big = raster_io.needs_pieces(info)
        self._status(f"Checking what ROLAVI Viewer takes for {info.name}…", "Info")

        def checked(server: dict) -> None:
            self._status("")
            mosaic = bool(server.get("orthoMosaic"))
            pixels = info.width * info.height
            if big and not mosaic and pixels > raster_io.OLD_SERVER_MAX_PIXELS:
                message = (f"{info.name} has {info.width:,} × {info.height:,} px ({pixels / 1e6:,.0f} million): too "
                           "big for the ROLAVI Viewer installed on the server, which makes the tiles of the whole "
                           "image at once and runs out of memory. Update the server (ROLAVI Viewer Step 28 takes "
                           "orthomosaics in pieces, at full resolution) and send it again.")
                self._status(message, "Bad")
                QtWidgets.QMessageBox.warning(self, self.host.windowTitle(), message)
                return
            self._convert_and_send(path, info, big and mosaic)

        self._quiet(client.server_info, checked)

    def _convert_and_send(self, path: str, info: raster_io.RasterInfo, pieces: bool) -> None:
        backend = self.host.backend
        folder = tempfile.mkdtemp(prefix="rolavi_ortho_")
        self._temp_dirs.append(folder)
        stem = Path(path).stem
        zip_path = os.path.join(folder, stem + ".zip")
        source = os.path.getsize(path)
        how = f"in pieces of {raster_io.PIECE_SIZE:,} px, at full resolution" if pieces else "as JPG + .aux.xml"
        self.host.log(f"   {info.name}: the orthomosaic goes to ROLAVI Viewer {how} (GeoTIFF {vc.fmt_bytes(source)}).")

        def convert(progress, _log):
            return raster_io.write_ortho_zip(info, zip_path, backend, progress=progress, pieces=pieces)

        def converted(result: raster_io.OrthoZip) -> None:
            size = os.path.getsize(zip_path)
            if result.pieces > 1:
                what = f"{result.pieces} JPG pieces, {result.width:,} × {result.height:,} px in all"
            else:
                reduced = f", reduced 1/{result.factor} (JPEG limit)" if result.factor > 1 else ""
                what = f"{result.width:,} × {result.height:,} px JPG{reduced}"
            self.host.log(f"   {stem}.zip: {vc.fmt_bytes(size)} ({what}) instead of {vc.fmt_bytes(source)}.")
            self._host_busy = self.host.task is not None
            self.send_layer(zip_path, temp_folder=folder)

        self.host._start_task(f"Converting {info.name} to JPG + .aux.xml", convert, converted)

    def choose_vector(self) -> None:
        path = self._choose("Send a vector layer to ROLAVI Viewer", FILTER_VECTOR)
        if path:
            self.send_layer(path)

    def _size_of(self, path: str) -> int:
        if path.lower().endswith(".shp"):
            parts, _missing = vc.shapefile_parts(path)
            return sum(os.path.getsize(p) for p in parts)
        return os.path.getsize(path)

    def _precheck(self, path: str, then, check_model: bool = False) -> None:
        """Confirm the session and the free space of the plan (and whether the model exists) before sending."""
        client = self.client
        size = self._size_of(path)
        name = os.path.basename(path)
        self._status(f"Checking your ROLAVI Viewer space for {name}…", "Info")

        def job():
            session = client.check_session()
            exists = client.model_exists(name) if (session and check_model) else False
            return session, exists

        def done(result) -> None:
            session, exists = result
            if session is None:
                self._forget_session("Your ROLAVI Viewer session ended: sign in again.")
                return
            storage = session.get("storage") or {}
            quota, used = storage.get("quota"), storage.get("used") or 0
            if quota and used + size > quota:
                message = (f"{name} needs {vc.fmt_bytes(size)}, but your plan has {vc.fmt_bytes(quota - used)} "
                           f"free ({vc.fmt_bytes(used)} used of {vc.fmt_bytes(quota)}). Delete something in "
                           "ROLAVI Viewer or contact ROLAVI to enlarge the plan.")
                self._status(message, "Bad")
                QtWidgets.QMessageBox.warning(self, self.host.windowTitle(), message)
                return
            self._status("")
            then(exists)

        self._quiet(job, done)

    def send_slpk(self, path: str) -> None:
        if self._quiet_tasks or self._host_busy:
            return
        name = os.path.basename(path)

        def go(exists: bool) -> None:
            overwrite = False
            if exists:
                answer = QtWidgets.QMessageBox.question(
                    self, self.host.windowTitle(),
                    f"A model named '{Path(name).stem}' is already in your ROLAVI Viewer space. Replace it?",
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                )
                if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                    return
                overwrite = True
            client = self.client
            self.host.log(f"   ROLAVI Viewer: {name} ({vc.fmt_bytes(os.path.getsize(path))})"
                          + (", replacing the model" if overwrite else ""))
            self._start(f"Sending {name} to ROLAVI Viewer",
                        lambda progress, _log: client.upload_slpk(path, overwrite, progress),
                        lambda model: self._sent(f"3D model '{model.get('name') or Path(name).stem}'"))

        self._precheck(path, go, check_model=True)

    def send_layer(self, path: str, temp_folder: str = "") -> None:
        if self._quiet_tasks or self._host_busy:
            return
        name = os.path.basename(path)
        kind = vc.kind_of(path)
        if not kind or kind == "slpk":
            QtWidgets.QMessageBox.warning(self, self.host.windowTitle(),
                                          "ROLAVI Viewer takes GeoTIFF, Shapefile, KML, KMZ and DXF here.")
            return
        if path.lower().endswith(".shp"):
            parts, missing = vc.shapefile_parts(path)
            if missing:
                message = f"The shapefile is incomplete: {', '.join(missing)} missing beside {name}."
                self._status(message, "Bad")
                QtWidgets.QMessageBox.warning(self, self.host.windowTitle(), message)
                return
            if not any(p.lower().endswith(".prj") for p in parts):
                self.host.log(f"   {name} has no .prj: ROLAVI Viewer will use {self._zone_text()}.")
        zone, south = int(self.zone_spin.value()), bool(self.hemi_combo.currentData())

        def go(_exists: bool) -> None:
            client = self.client
            self.host.log(f"   ROLAVI Viewer: {name} ({vc.fmt_bytes(self._size_of(path))})")

            def job(progress, log):
                try:
                    return client.import_file(path, zone, south, "auto", progress, log)
                finally:
                    if temp_folder:
                        shutil.rmtree(temp_folder, ignore_errors=True)

            self._start(f"Sending {name} to ROLAVI Viewer", job, self._layer_sent)

        self._precheck(path, go)

    def _zone_text(self) -> str:
        return f"WGS 84 / UTM zone {self.zone_spin.value()}{'S' if self.hemi_combo.currentData() else 'N'}"

    def _start(self, title: str, job, on_done) -> None:
        def run(progress, log):
            try:
                return job(progress, log)
            except vc.ViewerError as exc:
                if exc.needs_sign_in:
                    self._lost = True
                raise

        self.host._start_task(title, run, on_done)

    def _layer_sent(self, layer: dict) -> None:
        name = layer.get("name") or "layer"
        kind = layer.get("kind")
        if kind == "dem":
            what = (f"DEM '{name}' (z {float(layer.get('zMin', 0)):.2f} – {float(layer.get('zMax', 0)):.2f} m, "
                    f"{layer.get('crs', '')})")
        elif kind == "ortho":
            what = f"Orthomosaic '{name}' ({layer.get('tileCount', 0)} tiles)"
        else:
            what = f"Layer '{name}' ({layer.get('featureCount', 0)} features, {layer.get('crs', '')})"
        for warning in layer.get("warnings") or []:
            self.host.log(f"   ⚠ {warning}")
        self._sent(what)

    def _sent(self, what: str) -> None:
        self.host.log(f"✅ In ROLAVI Viewer: {what}")
        self._status(f"Sent: {what}.", "Good")
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Information)
        box.setWindowTitle(self.host.windowTitle())
        box.setText(f"{what} is in your ROLAVI Viewer space.")
        open_btn = box.addButton("Open ROLAVI Viewer", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Close", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        exec_(box)
        if box.clickedButton() is open_btn:
            self.open_viewer()
        QtCore.QTimer.singleShot(0, self.refresh_session)     # the space used changed
