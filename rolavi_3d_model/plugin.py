"""The QGIS side of ROLAVI 3D Model: a toolbar button and a menu entry that open the window.

The window (``ui.main_window.MainWindow``) is the same one that runs in VS Code with
``run_dev.py``. Here it gets QGIS' coordinate engine when pyproj is not installed in the Python of
QGIS, and it is closed when QGIS unloads the plugin.
"""

from __future__ import annotations

import traceback
from pathlib import Path

from qgis.PyQt import QtCore, QtGui, QtWidgets  # type: ignore[import-not-found]

MENU = "&ROLAVI 3D Model"
TITLE = "ROLAVI 3D Model"
ICON = Path(__file__).resolve().parent / "icon.png"

# QAction is in QtGui with Qt6 (QGIS 4) and in QtWidgets with Qt5 (QGIS 3)
QAction = getattr(QtGui, "QAction", None) or QtWidgets.QAction


def choose_backend():
    """pyproj when the Python of QGIS has it (fast with millions of points), QGIS' own engine otherwise."""
    from .core import crs as crs_mod

    try:
        return crs_mod.PyprojBackend(), ""
    except crs_mod.CrsError:
        pass
    try:
        from .qgis_crs import QgisBackend

        return QgisBackend(), ""
    except Exception as exc:  # pragma: no cover - depends on QGIS
        return None, str(exc) or exc.__class__.__name__


class Rolavi3DModelPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.window = None

    # ------------------------------------------------------------------ QGIS interface
    def initGui(self) -> None:  # noqa: N802 - name required by QGIS
        self.action = QAction(QtGui.QIcon(str(ICON)), TITLE, self.iface.mainWindow())
        self.action.setObjectName("rolavi3DModelAction")
        self.action.setToolTip("ROLAVI 3D Model: georeference 3D models (OBJ, PLY, point clouds, 3DGS), export "
                               "SLPK, DEM volumes and send them to ROLAVI Viewer")
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(MENU, self.action)

    def unload(self) -> None:
        if self.window is not None:
            try:
                self.window.close()
            except RuntimeError:          # already deleted by Qt
                pass
            self.window = None
        if self.action is not None:
            self.iface.removePluginMenu(MENU, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action.deleteLater()
            self.action = None

    # ------------------------------------------------------------------ the window
    def _forget_window(self, *_args) -> None:
        self.window = None

    def run(self, *_args) -> None:
        """Open the window (or bring it to the front when it is already open)."""
        if self.window is not None:
            try:
                self.window.showNormal()
                self.window.raise_()
                self.window.activateWindow()
                return
            except RuntimeError:          # closed and deleted meanwhile
                self.window = None
        try:
            from .ui.main_window import MainWindow

            backend, error = choose_backend()
            window = MainWindow(backend=backend)
            if backend is None:
                window.backend_error = error
        except Exception:
            QtWidgets.QMessageBox.critical(
                self.iface.mainWindow(), TITLE,
                "ROLAVI 3D Model could not start:\n\n" + traceback.format_exc()[-2500:]
                + "\n\nIt needs numpy and Pillow in the Python of QGIS (QGIS for Windows includes them).")
            return
        window.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        window.destroyed.connect(self._forget_window)
        self.window = window
        window.show()
        window.raise_()
        window.activateWindow()
