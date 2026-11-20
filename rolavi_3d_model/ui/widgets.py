"""Small widgets of the ROLAVI look: segmented buttons, workflow steps, cards.

``Segmented`` keeps the part of the QComboBox API the window uses (addItem, currentIndex,
setCurrentIndex, currentData, findData, currentIndexChanged), so a combo box can be swapped
for it without touching the window's logic.
"""

from __future__ import annotations

from pathlib import Path

from .qt import QtCore, QtGui, QtWidgets, Signal

ASSETS = Path(__file__).resolve().parent / "assets"
RAIL_WIDTH = 252
SUB_WIDTH = 168          # room for the status line of a step in the rail


def repolish(*widgets) -> None:
    """Apply the style sheet again after a dynamic property changed."""
    for widget in widgets:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()


def card(title: str = "", spacing: int = 10) -> tuple[QtWidgets.QFrame, QtWidgets.QVBoxLayout]:
    """A white card (QFrame#Card) and its layout, with an optional bold title."""
    frame = QtWidgets.QFrame()
    frame.setObjectName("Card")
    layout = QtWidgets.QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 14)
    layout.setSpacing(spacing)
    if title:
        label = QtWidgets.QLabel(title)
        label.setObjectName("CardTitle")
        layout.addWidget(label)
    return frame, layout


def field_label(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setObjectName("FieldLabel")
    label.setWordWrap(True)
    return label


def logo_label(height: int = 20) -> QtWidgets.QLabel:
    """The white ROLAVI logo (crisp on high-DPI screens); plain text if the image is missing."""
    label = QtWidgets.QLabel()
    pixmap = QtGui.QPixmap(str(ASSETS / "rolavi-logo-white.png"))
    if pixmap.isNull():
        label.setText("ROLAVI")
        label.setObjectName("Product")
        return label
    pixmap = pixmap.scaledToHeight(height * 2, QtCore.Qt.TransformationMode.SmoothTransformation)
    pixmap.setDevicePixelRatio(2.0)
    label.setPixmap(pixmap)
    label.setFixedHeight(height + 4)
    return label


class Segmented(QtWidgets.QFrame):
    """A row of mutually exclusive buttons that behaves like a QComboBox."""

    currentIndexChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Segmented")
        self._layout = QtWidgets.QHBoxLayout(self)
        self._layout.setContentsMargins(3, 3, 3, 3)
        self._layout.setSpacing(4)
        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: list[QtWidgets.QPushButton] = []
        self._data: list[object] = []
        self._index = -1

    # ---------------------------------------------------------------- QComboBox-like API
    def addItem(self, text: str, data=None, tooltip: str = "") -> None:
        button = QtWidgets.QPushButton(text)
        button.setObjectName("SegmentButton")
        button.setCheckable(True)
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        if tooltip:
            button.setToolTip(tooltip)
        index = len(self._buttons)
        button.clicked.connect(lambda _checked=False, i=index: self._clicked(i))
        self._group.addButton(button)
        self._layout.addWidget(button, 1)
        self._buttons.append(button)
        self._data.append(data)
        if self._index < 0:
            self._index = 0
            button.setChecked(True)

    def addItems(self, texts) -> None:
        for text in texts:
            self.addItem(str(text))

    def count(self) -> int:
        return len(self._buttons)

    def currentIndex(self) -> int:
        return self._index

    def currentData(self):
        return self._data[self._index] if 0 <= self._index < len(self._data) else None

    def itemData(self, index: int):
        return self._data[index] if 0 <= index < len(self._data) else None

    def findData(self, data) -> int:
        for index, value in enumerate(self._data):
            if value == data:
                return index
        return -1

    def setCurrentIndex(self, index: int) -> None:
        if not 0 <= index < len(self._buttons):
            return
        self._buttons[index].setChecked(True)
        if index != self._index:
            self._index = index
            self.currentIndexChanged.emit(index)

    def _clicked(self, index: int) -> None:
        self.setCurrentIndex(index)


class StepItem(QtWidgets.QFrame):
    """One step of the workflow rail: number badge (a check when done), title and a status line."""

    clicked = Signal(int)

    def __init__(self, number: int, title: str, parent=None):
        super().__init__(parent)
        self.number = number
        self.setObjectName("StepItem")
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(56)
        self.setAccessibleName(f"Step {number}: {title}")
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(12)
        self.badge = QtWidgets.QLabel(str(number))
        self.badge.setObjectName("StepBadge")
        self.badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.badge.setFixedSize(28, 28)
        texts = QtWidgets.QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(0)
        self.title_label = QtWidgets.QLabel(title)
        self.title_label.setObjectName("StepTitle")
        self.sub_label = QtWidgets.QLabel("")
        self.sub_label.setObjectName("StepSub")
        texts.addWidget(self.title_label)
        texts.addWidget(self.sub_label)
        layout.addWidget(self.badge)
        layout.addLayout(texts, 1)
        self._state = ""
        self.set_state("todo", "")

    def set_state(self, state: str, sub: str) -> None:
        """state: 'active' (the page shown), 'done' or 'todo'."""
        metrics = QtGui.QFontMetrics(self.sub_label.font())
        self.sub_label.setText(metrics.elidedText(sub, QtCore.Qt.TextElideMode.ElideRight, SUB_WIDTH))
        self.sub_label.setToolTip(sub)
        self.badge.setText("✓" if state == "done" else str(self.number))
        if state != self._state:
            self._state = state
            for widget in (self, self.badge, self.title_label):
                widget.setProperty("state", state)
            repolish(self, self.badge, self.title_label)

    def mousePressEvent(self, event) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.clicked.emit(self.number)
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter, QtCore.Qt.Key.Key_Space):
            self.clicked.emit(self.number)
            return
        super().keyPressEvent(event)
