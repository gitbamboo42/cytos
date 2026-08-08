"""A named section of the dock panel, grouping it by layer kind (Images,
Segments, Points, ...) the way napari's own layer list groups by layer type,
rather than one flat stack of rows.

Two independent controls, deliberately not coupled to each other: a
disclosure arrow that shows/hides the section's *contents in the panel*
(a UI-only concern), and a checkbox that shows/hides what that section
*renders on screen* (emits `visibility_changed`) — clicking one must never
also change the other.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

_HEADER_POINT_SIZE = 13  # default QCheckBox text reads too small as a section header


class CollapsibleSection(QtWidgets.QWidget):
    visibility_changed = QtCore.Signal(bool)

    def __init__(self, title: str, expanded: bool = True, checked: bool = True):
        super().__init__()

        self._expand_button = QtWidgets.QToolButton()
        self._expand_button.setCheckable(True)
        self._expand_button.setChecked(expanded)
        self._expand_button.setArrowType(QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow)
        self._expand_button.setStyleSheet("QToolButton { border: none; }")
        self._expand_button.toggled.connect(self._on_expand_toggled)

        # A bare checkbox (no text) -- text on a QCheckBox is itself part of
        # its clickable/toggle area, which is exactly the coupling we don't
        # want between the title and the visibility toggle. The title is a
        # separate, inert QLabel right next to it.
        self._visible_check = QtWidgets.QCheckBox()
        self._visible_check.setChecked(checked)
        self._visible_check.toggled.connect(self.visibility_changed)

        title_label = QtWidgets.QLabel(title)
        font = QtGui.QFont(title_label.font())
        font.setPointSize(_HEADER_POINT_SIZE)
        font.setBold(True)
        title_label.setFont(font)

        header = QtWidgets.QWidget()
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.addWidget(self._expand_button)
        header_layout.addWidget(self._visible_check)
        header_layout.addWidget(title_label)
        header_layout.addStretch()

        self.content = QtWidgets.QWidget()
        self.content.setVisible(expanded)
        self.content_layout = QtWidgets.QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(16, 0, 0, 0)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(header)
        layout.addWidget(self.content)

    def _on_expand_toggled(self, expanded: bool) -> None:
        self._expand_button.setArrowType(QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow)
        self.content.setVisible(expanded)

    def is_checked(self) -> bool:
        return self._visible_check.isChecked()

    def is_expanded(self) -> bool:
        return self._expand_button.isChecked()

    def apply(self, expanded: bool, checked: bool) -> None:
        """Restore both controls (saved session, or reset to defaults). Setting
        the checkbox re-emits `visibility_changed`, which is what pushes the
        change through to the layers -- so this must run after the handlers are
        connected."""
        self._expand_button.setChecked(expanded)
        self._visible_check.setChecked(checked)

    def add_widget(self, widget: QtWidgets.QWidget) -> None:
        self.content_layout.addWidget(widget)

    def insert_widget(self, index: int, widget: QtWidgets.QWidget) -> None:
        self.content_layout.insertWidget(index, widget)
