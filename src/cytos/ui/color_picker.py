"""One color-picker popup, shared by every place a color is chosen — image
channels (`cytos.ui.channel_panel`) and segment category swatches
(`cytos.ui.segment_panel`).

Three labelled sections: Preset (the standard color set), Colormap (ramp
names, image channels only), and Custom — the user's own colors. Custom
colors live in one per-window `CustomColors` pool, saved in the session, so
a color created while tuning one layer is a one-click swatch on every other
picker in the window. Hovering a swatch names it in the label at the
bottom; nothing changes until a swatch is clicked, and the popup stays open
(clicking anywhere outside dismisses it) so choices can be compared.

Adding a custom color runs the modal color dialog, which necessarily closes
the popup (Qt.Popup gives up when another window takes the grab) — so the
popup reopens itself afterwards, now with the new color as a swatch.
"""

from __future__ import annotations

from typing import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from cytos.render.image import (
    CHANNEL_COLOR_PRESETS,
    CHANNEL_RAMP_CHOICES,
    channel_color_hex,
    colormap_lut_array,
)

SWATCH_SIZE = 18


def swatch_style(background: str, selected: bool = False) -> str:
    # Selected wears the theme's text color (white on dark, black on light)
    # and outranks hover; unselected swatches show the highlight color on
    # hover.
    if selected:
        border, hover = "2px solid palette(text)", ""
    else:
        border = "1px solid #777777"
        hover = "QToolButton:hover { border: 2px solid palette(highlight); }"
    return (
        f"QToolButton {{ background: {background}; border: {border}; "
        f"border-radius: 2px; }} {hover}"
    )


def gradient_css(name: str, stops: int = 8) -> str:
    """A colormap as a Qt stylesheet gradient, so a swatch can be *filled*
    with the ramp edge to edge — an icon would sit inside the button's
    padding as a strip."""
    lut = colormap_lut_array(name)
    parts = []
    for i in range(stops):
        t = i / (stops - 1)
        r, g, b = (int(round(float(c) * 255)) for c in lut[int(t * 255), :3])
        parts.append(f"stop:{t:.3f} #{r:02x}{g:02x}{b:02x}")
    return "qlineargradient(x1:0, y1:0, x2:1, y2:0, " + ", ".join(parts) + ")"


def show_value_on(button: QtWidgets.QToolButton, value: str, selected: bool = False) -> None:
    """Make a swatch button display a color value: a solid fill for
    "#rrggbb", the full gradient for a colormap name."""
    button.setStyleSheet(
        swatch_style(value if value.startswith("#") else gradient_css(value), selected)
    )


class CustomColors:
    """The window's pool of user-created colors — plain data, saved in the
    session (`custom_colors`) and shared by every picker in the window."""

    def __init__(self, colors: list[str] | None = None):
        self.colors: list[str] = []
        self.set(colors or [])

    def set(self, colors: list[str]) -> None:
        self.colors = []
        for c in colors:
            self.add(c)

    def add(self, hex_color: str) -> None:
        hex_color = str(hex_color).lower()
        if hex_color not in self.colors:
            self.colors.append(hex_color)

    def remove(self, hex_color: str) -> None:
        hex_color = str(hex_color).lower()
        if hex_color in self.colors:
            self.colors.remove(hex_color)


class _HueStrip(QtWidgets.QWidget):
    """A horizontal rainbow; click or drag to pick the hue."""

    changed = QtCore.Signal(float)

    def __init__(self):
        super().__init__()
        self._hue = 0.0
        self.setFixedHeight(12)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed
        )

    def set_hue(self, hue: float) -> None:
        self._hue = hue
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt spelling
        painter = QtGui.QPainter(self)
        gradient = QtGui.QLinearGradient(0, 0, self.width(), 0)
        for i in range(7):
            gradient.setColorAt(i / 6, QtGui.QColor.fromHsvF(min(i / 6, 0.999), 1, 1))
        painter.fillRect(self.rect(), gradient)
        x = int(self._hue * self.width())
        painter.setPen(QtGui.QPen(QtGui.QColor("#000000"), 1))
        painter.drawRect(x - 2, 0, 4, self.height() - 1)
        painter.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1))
        painter.drawRect(x - 1, 1, 2, self.height() - 3)

    def _pick(self, event) -> None:
        self._hue = min(max(event.position().x() / max(self.width(), 1), 0.0), 0.999)
        self.update()
        self.changed.emit(self._hue)

    mousePressEvent = _pick  # noqa: N815 - Qt spelling
    mouseMoveEvent = _pick  # noqa: N815


class _ShadeSquare(QtWidgets.QWidget):
    """Saturation left-to-right, value top-to-bottom, for the current hue —
    the body of every color picker, at pocket size."""

    changed = QtCore.Signal(float, float)  # saturation, value

    def __init__(self):
        super().__init__()
        self._hue, self._s, self._v = 0.0, 1.0, 1.0
        self.setFixedHeight(96)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed
        )

    def set_hsv(self, hue: float, s: float, v: float) -> None:
        self._hue, self._s, self._v = hue, s, v
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        saturation = QtGui.QLinearGradient(0, 0, self.width(), 0)
        saturation.setColorAt(0, QtGui.QColor("#ffffff"))
        saturation.setColorAt(1, QtGui.QColor.fromHsvF(self._hue, 1, 1))
        painter.fillRect(self.rect(), saturation)
        darkness = QtGui.QLinearGradient(0, 0, 0, self.height())
        darkness.setColorAt(0, QtGui.QColor(0, 0, 0, 0))
        darkness.setColorAt(1, QtGui.QColor(0, 0, 0, 255))
        painter.fillRect(self.rect(), darkness)
        x, y = self._s * self.width(), (1 - self._v) * self.height()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor("#000000"), 1))
        painter.drawEllipse(QtCore.QPointF(x, y), 5, 5)
        painter.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1))
        painter.drawEllipse(QtCore.QPointF(x, y), 4, 4)

    def _pick(self, event) -> None:
        self._s = min(max(event.position().x() / max(self.width(), 1), 0.0), 1.0)
        self._v = 1 - min(max(event.position().y() / max(self.height(), 1), 0.0), 1.0)
        self.update()
        self.changed.emit(self._s, self._v)

    mousePressEvent = _pick  # noqa: N815
    mouseMoveEvent = _pick  # noqa: N815


class ColorPopup(QtWidgets.QWidget):
    """See the module docstring. `on_pick` is called with the chosen value —
    a "#rrggbb", or a ramp colormap name when `include_ramps`."""

    _PER_ROW = 8

    def __init__(
        self,
        current: str,
        anchor: QtWidgets.QWidget,
        custom: CustomColors,
        include_ramps: bool,
        on_pick: Callable[[str], None],
    ):
        super().__init__(anchor.window(), QtCore.Qt.WindowType.Popup)
        self._current = current
        self._anchor = anchor
        self._custom = custom
        self._include_ramps = include_ramps
        self._on_pick = on_pick
        self._name_of: dict[QtWidgets.QToolButton, str] = {}
        # button -> its value, for marking the selected swatch.
        self._value_of: dict[QtWidgets.QToolButton, str] = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        # Exactly as wide as the swatch grid — adjustSize() alone would pad
        # a top-level widget out to Qt's 200 px floor.
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetFixedSize)

        def section(text: str) -> None:
            box = QtWidgets.QHBoxLayout()
            label = QtWidgets.QLabel(text)
            font = label.font()
            font.setPointSizeF(font.pointSizeF() * 0.85)
            label.setFont(font)
            line = QtWidgets.QFrame()
            line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
            line.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
            box.addWidget(label)
            box.addWidget(line, 1)
            layout.addLayout(box)

        def swatch() -> QtWidgets.QToolButton:
            button = QtWidgets.QToolButton()
            button.setFixedSize(SWATCH_SIZE, SWATCH_SIZE)
            button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            button.installEventFilter(self)
            return button

        def grid_of(entries: list[tuple[str, str]]) -> QtWidgets.QGridLayout:
            # Same fixed columns in every section, left to right; unfilled
            # cells stay empty, so the sections' swatches line up.
            grid = QtWidgets.QGridLayout()
            grid.setSpacing(4)
            for i, (name, value) in enumerate(entries):
                button = swatch()
                show_value_on(button, value)
                button.clicked.connect(lambda _=False, v=value: self._choose(v))
                self._name_of[button] = name
                self._value_of[button] = value
                grid.addWidget(button, i // self._PER_ROW, i % self._PER_ROW)
            for col in range(self._PER_ROW):
                grid.setColumnMinimumWidth(col, SWATCH_SIZE)
            grid.setColumnStretch(self._PER_ROW, 1)
            return grid

        section("Preset")
        layout.addLayout(grid_of(list(CHANNEL_COLOR_PRESETS)))

        if include_ramps:
            section("Colormap")
            layout.addLayout(grid_of([(n, n) for n in CHANNEL_RAMP_CHOICES]))

        section("Custom")
        layout.addLayout(grid_of([(c, c) for c in custom.colors]))
        # Delete and add on their own line, right-aligned.
        self._delete_button = swatch()
        self._delete_button.setText("−")
        self._delete_button.clicked.connect(self._delete_custom)
        self._name_of[self._delete_button] = "Delete color"
        add_button = swatch()
        add_button.setText("+")
        add_button.clicked.connect(self._pick_custom)
        self._name_of[add_button] = "New color…"
        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(4)
        buttons.addStretch(1)
        buttons.addWidget(self._delete_button)
        buttons.addWidget(add_button)
        layout.addLayout(buttons)

        # The pocket color picker [+] unfolds: shade square, hue strip, hex
        # box, an Add button. Inline rather than a dialog — Qt's own color
        # dialog is a battleship, and a modal dialog would close the popup.
        self._picker = QtWidgets.QWidget()
        picker_layout = QtWidgets.QVBoxLayout(self._picker)
        picker_layout.setContentsMargins(0, 0, 0, 0)
        picker_layout.setSpacing(4)
        self._shade = _ShadeSquare()
        self._hue_strip = _HueStrip()
        self._hex_edit = QtWidgets.QLineEdit()
        self._hex_edit.setFixedWidth(72)
        confirm = QtWidgets.QToolButton()
        confirm.setText("Add")
        confirm.clicked.connect(self._add_picked)
        self._shade.changed.connect(self._on_shade)
        self._hue_strip.changed.connect(self._on_hue)
        self._hex_edit.textEdited.connect(self._on_hex_edited)
        picker_layout.addWidget(self._shade)
        picker_layout.addWidget(self._hue_strip)
        hex_row = QtWidgets.QHBoxLayout()
        hex_row.setSpacing(4)
        hex_row.addWidget(self._hex_edit)
        hex_row.addStretch(1)
        hex_row.addWidget(confirm)
        picker_layout.addLayout(hex_row)
        self._picker.hide()
        layout.addWidget(self._picker)

        # The hovered swatch's name, immediately — a tooltip would lag.
        self._hover_label = QtWidgets.QLabel(" ")
        layout.addWidget(self._hover_label)
        self._refresh_marks()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt spelling
        name = self._name_of.get(obj)
        if name is not None:
            if event.type() == QtCore.QEvent.Type.Enter:
                self._hover_label.setText(name)
            elif event.type() == QtCore.QEvent.Type.Leave:
                self._hover_label.setText(" ")
        return False

    def _refresh_marks(self) -> None:
        """Mark the selected swatch, and only let the delete button act when
        the selection is a custom color (presets aren't deletable)."""
        for button, value in self._value_of.items():
            show_value_on(button, value, selected=value == self._current)
        self._delete_button.setEnabled(self._current in self._custom.colors)

    def _choose(self, value: str) -> None:
        # The popup stays open so colors can be tried against the image;
        # clicking anywhere outside is what dismisses it (Qt.Popup).
        self._current = value
        self._on_pick(value)
        self._refresh_marks()

    def _reopen(self) -> None:
        """A fresh popup — how the panel 'stays open' across anything that
        rebuilds the custom section. Anchored to the swatch again, not to
        wherever the old popup sat: the window system nudges a popup that
        brushes the screen edge, and reopening at the nudged spot would
        compound the drift on every add."""
        open_color_popup(
            self._anchor, self._current, self._custom, self._include_ramps, self._on_pick
        )

    def _delete_custom(self) -> None:
        self._custom.remove(self._current)
        self.close()
        self._reopen()

    # -- the inline picker ---------------------------------------------------

    def _pick_custom(self) -> None:
        if self._picker.isVisible():
            self._picker.hide()
        else:
            color = QtGui.QColor(channel_color_hex(self._current))
            hue, s, v, _a = color.getHsvF()
            hue = max(hue, 0.0)  # grays report hue -1
            self._shade.set_hsv(hue, s, v)
            self._hue_strip.set_hue(hue)
            self._hex_edit.setText(color.name())
            self._picker.show()
        self.adjustSize()

    def _picked_hex(self) -> str:
        return QtGui.QColor.fromHsvF(
            self._shade._hue, self._shade._s, self._shade._v
        ).name()

    def _on_hue(self, hue: float) -> None:
        self._shade.set_hsv(hue, self._shade._s, self._shade._v)
        self._hex_edit.setText(self._picked_hex())

    def _on_shade(self, s: float, v: float) -> None:
        self._hex_edit.setText(self._picked_hex())

    def _on_hex_edited(self, text: str) -> None:
        color = QtGui.QColor(text.strip())
        if not color.isValid():
            return
        hue, s, v, _a = color.getHsvF()
        hue = max(hue, 0.0)
        self._shade.set_hsv(hue, s, v)
        self._hue_strip.set_hue(hue)

    def _add_picked(self) -> None:
        color = QtGui.QColor(self._hex_edit.text().strip())
        value = color.name() if color.isValid() else self._picked_hex()
        self._custom.add(value)
        self._current = value
        self._on_pick(value)
        self.close()
        self._reopen()


def open_color_popup(
    anchor: QtWidgets.QWidget,
    current: str,
    custom: CustomColors,
    include_ramps: bool,
    on_pick: Callable[[str], None],
) -> ColorPopup:
    """Open the picker just under `anchor`. The popup dismisses itself on
    any click outside it."""
    popup = ColorPopup(current, anchor, custom, include_ramps, on_pick)
    popup.adjustSize()
    popup.move(anchor.mapToGlobal(QtCore.QPoint(0, anchor.height() + 2)))
    popup.show()
    return popup
