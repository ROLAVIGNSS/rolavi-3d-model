"""Icon and settings of ROLAVI 3D Model: title bar of the window, taskbar, dialogs.

The files are in ``rolavi_3d_model/ui/assets/icons``: PNG from 16 to 256 px (16-32 px are a bolder
drawing that stays readable that small), the .ico for Windows shortcuts and the SVG sources.

On Windows the taskbar groups a Python script under python.exe and shows the Python icon;
``set_windows_app_id`` gives the process its own identity first, so the taskbar shows ours.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .qt import QtCore, QtGui

ICON_DIR = Path(__file__).resolve().parent / "assets" / "icons"
SIZES = (16, 24, 32, 48, 64, 128, 256)
APP_ID = "ROLAVI.3DModel"
SETTINGS = ("ROLAVI", "3DModel")
OLD_SETTINGS = ("ROLAVI", "OBJ2SLPK")          # the name before 1.0.0

_icon = None


def app_icon() -> QtGui.QIcon:
    """The icon with every size (Qt picks the right one for the title bar, the taskbar...)."""
    global _icon
    if _icon is None:
        icon = QtGui.QIcon()
        for size in SIZES:
            path = ICON_DIR / f"rolavi_3d_model_{size}.png"
            if path.is_file():
                icon.addFile(str(path), QtCore.QSize(size, size))
        _icon = icon
    return _icon


def set_windows_app_id() -> None:
    """Windows: the taskbar shows our icon instead of Python's (call it before the window is shown)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass                 # an old Windows: the icon still shows in the title bar


def app_settings() -> QtCore.QSettings:
    """The settings of the program (window, folders, ROLAVI Viewer session). The first time, the
    settings saved under the old name (ROLAVI OBJ to SLPK) are copied, so nothing is lost."""
    settings = QtCore.QSettings(*SETTINGS)
    if not settings.allKeys() and not settings.value("_migrated", False, type=bool):
        old = QtCore.QSettings(*OLD_SETTINGS)
        for key in old.allKeys():
            settings.setValue(key, old.value(key))
        settings.setValue("_migrated", True)
    return settings
