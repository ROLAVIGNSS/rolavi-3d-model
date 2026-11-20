"""Qt bindings: QGIS' own PyQt inside QGIS (Qt5 in QGIS 3, Qt6 in QGIS 4), PyQt5 in VS Code.

The UI uses fully qualified enums (``Qt.AlignmentFlag.AlignLeft``) and the helpers
below, so the same code runs on PyQt5 and PyQt6.
"""

from __future__ import annotations

try:
    from qgis.PyQt import QtCore, QtGui, QtWidgets  # type: ignore[import-not-found]

    IN_QGIS = True
except ImportError:
    from PyQt5 import QtCore, QtGui, QtWidgets  # type: ignore[no-redef]

    IN_QGIS = False

Signal = QtCore.pyqtSignal


def event_pos(event) -> QtCore.QPointF:
    """Position of a mouse / wheel event in widget pixels (Qt5 and Qt6)."""
    if hasattr(event, "position"):
        return event.position()
    if hasattr(event, "localPos"):
        return event.localPos()
    return QtCore.QPointF(event.pos())


def exec_(obj) -> int:
    """``exec()`` of a dialog / application (``exec_`` in old PyQt5)."""
    method = getattr(obj, "exec", None) or obj.exec_
    return int(method() or 0)


__all__ = ["QtCore", "QtGui", "QtWidgets", "Signal", "IN_QGIS", "event_pos", "exec_"]
