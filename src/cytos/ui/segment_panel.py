"""Dock-panel controls for the cell-segmentation layer: what to draw (outline,
fill, or both), how opaque the fill is, and what colors the cells.

Colour is two choices, not one — a **color by** (which per-cell feature) and
its mapper: a **colormap** ramp for numeric features (and "Flat color"), a
qualitative **palette** for categorical ones (a clustering — see
`cytos.core.polygons.join_categories`).

A categorical color-by shows its categories as a checkable list that doubles
as the legend:

    [x] All
        [x] [swatch] 1  (12,345 cells)
        [x] [swatch] 2  (8,101 cells)
        [x] [swatch] Unassigned  (508 cells)

The checkbox shows or hides that category's cells; the swatch is a real
button — hovering highlights its border, clicking opens the color dialog
(preset swatches and free choice both). Plain widgets in a scroll area, not
a QTreeWidget: a tree draws its checkboxes inside the indented first
column, which turns "checkbox, then swatch, then label" into a fight with
style-dependent geometry. All of it — palette, color tweaks, hidden
categories — lives in the *session*, never in the slide's data: the slide
records what the cells are, the session records how you like them shown.
"""

from __future__ import annotations

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from cytos.render.points import CURATED_PALETTES, hex_to_rgba, palette_array
from cytos.render.polygons import DEFAULT_CATEGORY_PALETTE, _UNASSIGNED_RGBA
from cytos.ui.color_picker import CustomColors, open_color_popup
from cytos.ui.colormap_combo import make_colormap_combo

FLAT_COLOR_LABEL = "Flat color"
UNASSIGNED_KEY = "unassigned"

_LEGEND_HEIGHT = 170
_SWATCH_SIZE = 16  # square, like every color-picker swatch anyone has met
_CATEGORY_INDENT = 22


def _rgba_to_hex(rgba: np.ndarray) -> str:
    r, g, b = (int(round(float(c) * 255)) for c in rgba[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


class _IndicatorCheckBox(QtWidgets.QCheckBox):
    """A text-less checkbox whose width is exactly its drawn indicator.

    A plain QCheckBox reserves label padding right of the indicator even
    with no label, so in a row of widgets the gap after it reads wider than
    the layout's spacing. Nominal metrics (PM_IndicatorWidth) under-report
    what the native style actually draws, so this asks the style for the
    indicator's real rectangle and sizes itself to its right edge."""

    def sizeHint(self) -> QtCore.QSize:
        option = QtWidgets.QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(
            QtWidgets.QStyle.SubElement.SE_CheckBoxIndicator, option, self
        )
        return QtCore.QSize(rect.x() + rect.width(), super().sizeHint().height())

    def minimumSizeHint(self) -> QtCore.QSize:
        return self.sizeHint()


def _swatch_style(hex_color: str) -> str:
    # The hover border is what says "this is editable" before anyone clicks.
    return (
        f"QToolButton {{ background-color: {hex_color}; border: 1px solid #777777; "
        f"border-radius: 2px; }} "
        f"QToolButton:hover {{ border: 2px solid palette(highlight); }}"
    )


class SegmentRow(QtWidgets.QGroupBox):
    colormap_changed = QtCore.Signal(str)
    color_by_changed = QtCore.Signal(object)  # str feature name, or None for flat
    palette_changed = QtCore.Signal(str)
    # Whole mappings, feature -> {category key -> "#rrggbb"} / -> [hidden
    # keys]; the row owns the editing, listeners just display or save them.
    category_colors_changed = QtCore.Signal(object)
    hidden_categories_changed = QtCore.Signal(object)
    outline_changed = QtCore.Signal(bool)
    fill_changed = QtCore.Signal(bool)
    fill_opacity_changed = QtCore.Signal(float)
    visibility_changed = QtCore.Signal(bool)

    def __init__(
        self,
        title: str,
        feature_names: list[str],
        categorical: dict[str, list[tuple[str, int]]],
        colormap: str,
        color_by: str | None,
        show_outline: bool,
        show_fill: bool,
        fill_opacity: float,
        visible: bool = True,
        palette: str = DEFAULT_CATEGORY_PALETTE,
        category_colors: dict | None = None,
        hidden_categories: dict | None = None,
        custom_colors: CustomColors | None = None,
    ):
        super().__init__()
        # The window's shared pool of user-created colors (see
        # cytos.ui.color_picker); a private pool only in tests.
        self._custom_colors = custom_colors if custom_colors is not None else CustomColors()
        # feature -> [(category key, cell count)], categorical features only.
        # Keys are strings ("7", or "unassigned") -- the same JSON-safe form
        # the session stores. Public: describe() lists it so a remote caller
        # knows the legal categories.
        self.categorical = dict(categorical)
        self._category_colors: dict[str, dict[str, str]] = {
            f: dict(c) for f, c in (category_colors or {}).items()
        }
        self._hidden_categories: dict[str, list[str]] = {
            f: list(k) for f, k in (hidden_categories or {}).items()
        }
        # (key, checkbox, swatch button) per category row of the current
        # feature -- what the "All" checkbox and the hidden-set derivation
        # iterate over.
        self._category_rows: list[tuple[str, QtWidgets.QCheckBox, QtWidgets.QToolButton]] = []

        outer = QtWidgets.QVBoxLayout(self)

        # A slide can hold several segment layers -- cell and nucleus to
        # start with, and File > Add Segments... appends more -- so each row
        # folds like the sections do. The whole header is two things: the
        # fold arrow, and one checkbox whose label *is* the layer name --
        # "[x] Cell" both names the layer and switches it, the way napari's
        # layer list reads, instead of a title plus a separate "Visible"
        # row.
        header = QtWidgets.QWidget()
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        self._fold_button = QtWidgets.QToolButton()
        self._fold_button.setCheckable(True)
        self._fold_button.setChecked(True)
        self._fold_button.setArrowType(QtCore.Qt.ArrowType.DownArrow)
        self._fold_button.setStyleSheet("QToolButton { border: none; }")
        self._fold_button.toggled.connect(self._on_fold)
        self.visible_check = QtWidgets.QCheckBox(title)
        self.visible_check.setChecked(visible)
        self.visible_check.toggled.connect(self.visibility_changed)
        header_layout.addWidget(self._fold_button)
        header_layout.addWidget(self.visible_check)
        header_layout.addStretch()
        outer.addWidget(header)

        self._content = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(self._content)
        layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._content)

        # "Color by" leads: it decides whether the colour mapper below it is
        # a ramp or a palette, so it reads as cause before effect.
        self.color_by_combo = QtWidgets.QComboBox()
        self.color_by_combo.addItem(FLAT_COLOR_LABEL)
        for name in feature_names:
            self.color_by_combo.addItem(name)
        self.color_by_combo.setCurrentText(color_by if color_by is not None else FLAT_COLOR_LABEL)
        self.color_by_combo.currentTextChanged.connect(self._emit_color_by)
        # Nothing to spread a ramp over when the cache has no feature table —
        # leave the control visible but dead, so the panel's shape doesn't
        # depend on the dataset.
        self.color_by_combo.setEnabled(bool(feature_names))
        layout.addRow("Color by", self.color_by_combo)

        # Exactly one of the two mappers is shown at a time (see
        # `_sync_categorical`): the ramp for numeric features and flat
        # colour, the palette for categorical ones.
        self.cmap_combo = make_colormap_combo(colormap)
        self.cmap_combo.currentTextChanged.connect(self.colormap_changed)
        layout.addRow("Colormap", self.cmap_combo)

        self.palette_combo = QtWidgets.QComboBox()
        for name in CURATED_PALETTES:
            self.palette_combo.addItem(name)
        self.palette_combo.setCurrentText(palette)
        self.palette_combo.currentTextChanged.connect(self._on_palette)
        layout.addRow("Palette", self.palette_combo)

        # The legend, and the editor: an "All" master checkbox, then one row
        # per category -- checkbox (show/hide), swatch button (recolor),
        # label -- inside a scroll area.
        self.all_check = QtWidgets.QCheckBox("All")
        self.all_check.setTristate(True)
        self.all_check.clicked.connect(self._on_all_clicked)

        self._legend_rows = QtWidgets.QWidget()
        self._legend_rows_layout = QtWidgets.QVBoxLayout(self._legend_rows)
        self._legend_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._legend_rows_layout.setSpacing(2)

        legend_content = QtWidgets.QWidget()
        legend_layout = QtWidgets.QVBoxLayout(legend_content)
        legend_layout.setContentsMargins(2, 2, 2, 2)
        legend_layout.setSpacing(2)
        legend_layout.addWidget(self.all_check)
        legend_layout.addWidget(self._legend_rows)
        legend_layout.addStretch()

        self.legend = QtWidgets.QScrollArea()
        self.legend.setWidget(legend_content)
        self.legend.setWidgetResizable(True)
        self.legend.setFixedHeight(_LEGEND_HEIGHT)
        layout.addRow(self.legend)

        self.reset_colors_button = QtWidgets.QPushButton("Reset colors")
        self.reset_colors_button.clicked.connect(self._on_reset_colors)
        layout.addRow(self.reset_colors_button)

        self.outline_check = QtWidgets.QCheckBox("Outline")
        self.outline_check.setChecked(show_outline)
        self.outline_check.toggled.connect(self.outline_changed)
        layout.addRow(self.outline_check)

        self.fill_check = QtWidgets.QCheckBox("Fill")
        self.fill_check.setChecked(show_fill)
        self.fill_check.toggled.connect(self.fill_changed)
        self.fill_check.toggled.connect(self._sync_opacity_enabled)
        layout.addRow(self.fill_check)

        self.opacity_spin = QtWidgets.QDoubleSpinBox()
        self.opacity_spin.setRange(0.0, 1.0)
        self.opacity_spin.setSingleStep(0.05)
        self.opacity_spin.setDecimals(2)
        self.opacity_spin.setValue(fill_opacity)
        self.opacity_spin.valueChanged.connect(self.fill_opacity_changed)
        layout.addRow("Fill opacity", self.opacity_spin)
        self._sync_opacity_enabled(show_fill)
        self._sync_categorical()

    # -- fold --------------------------------------------------------------

    def _on_fold(self, expanded: bool) -> None:
        self._fold_button.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow
        )
        self._content.setVisible(expanded)

    # -- categorical legend ------------------------------------------------

    def _current_feature(self) -> str | None:
        text = self.color_by_combo.currentText()
        return None if text == FLAT_COLOR_LABEL else text

    def _on_palette(self, palette: str) -> None:
        # Rebuild the legend so the swatches show the new palette's colours.
        self._sync_categorical()
        self.palette_changed.emit(palette)

    def _category_color(self, feature: str, key: str) -> np.ndarray:
        override = self._category_colors.get(feature, {}).get(key)
        if override is not None:
            return hex_to_rgba(override)
        if key == UNASSIGNED_KEY:
            return np.asarray(_UNASSIGNED_RGBA, dtype=np.float32)
        pal = palette_array(self.palette_combo.currentText())
        return pal[int(key) % len(pal)]

    def _sync_categorical(self) -> None:
        """Palette and legend exist only while a categorical feature drives
        the colour; a numeric feature gets the ramp and no legend. Hidden,
        not disabled, so the row stays short in the common numeric case."""
        feature = self._current_feature()
        cats = self.categorical.get(feature) if feature is not None else None
        is_cat = bool(cats)
        self.palette_combo.setVisible(is_cat)
        self._content.layout().labelForField(self.palette_combo).setVisible(is_cat)
        self.legend.setVisible(is_cat)
        self.reset_colors_button.setVisible(is_cat)
        self.cmap_combo.setVisible(not is_cat)
        self._content.layout().labelForField(self.cmap_combo).setVisible(not is_cat)

        while self._legend_rows_layout.count():
            item = self._legend_rows_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._category_rows = []
        if not is_cat:
            return

        hidden = set(self._hidden_categories.get(feature, ()))
        for key, count in cats:
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(_CATEGORY_INDENT, 0, 0, 0)
            row_layout.setSpacing(10)
            check = _IndicatorCheckBox()
            check.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed
            )
            check.setChecked(key not in hidden)
            check.toggled.connect(self._on_category_toggled)
            swatch = QtWidgets.QToolButton()
            swatch.setFixedSize(_SWATCH_SIZE, _SWATCH_SIZE)
            swatch.setStyleSheet(_swatch_style(_rgba_to_hex(self._category_color(feature, key))))
            swatch.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            swatch.setToolTip("Click to change color")
            swatch.clicked.connect(lambda _=False, k=key: self._on_swatch_clicked(k))
            label = "Unassigned" if key == UNASSIGNED_KEY else str(key)
            row_layout.addWidget(check)
            row_layout.addWidget(swatch)
            row_layout.addWidget(QtWidgets.QLabel(f"{label}  ({count} cells)"))
            row_layout.addStretch()
            self._legend_rows_layout.addWidget(row)
            self._category_rows.append((key, check, swatch))
        self._sync_all_check()

    def _sync_all_check(self) -> None:
        states = [check.isChecked() for _k, check, _s in self._category_rows]
        blocked = self.all_check.blockSignals(True)
        if all(states):
            self.all_check.setCheckState(QtCore.Qt.CheckState.Checked)
        elif not any(states):
            self.all_check.setCheckState(QtCore.Qt.CheckState.Unchecked)
        else:
            self.all_check.setCheckState(QtCore.Qt.CheckState.PartiallyChecked)
        self.all_check.blockSignals(blocked)

    def _on_all_clicked(self) -> None:
        # A click on "All" means "everything on" unless everything already
        # was -- then it means "everything off". Never leave it partial by
        # clicking; partial only ever *reports* a mixed state.
        target = not all(check.isChecked() for _k, check, _s in self._category_rows)
        for _key, check, _swatch in self._category_rows:
            blocked = check.blockSignals(True)
            check.setChecked(target)
            check.blockSignals(blocked)
        self._on_category_toggled()

    def _on_category_toggled(self, _checked: bool = False) -> None:
        feature = self._current_feature()
        if feature is None:
            return
        self._sync_all_check()
        hidden = [key for key, check, _s in self._category_rows if not check.isChecked()]
        if hidden:
            self._hidden_categories[feature] = hidden
        else:
            self._hidden_categories.pop(feature, None)
        self.hidden_categories_changed.emit(
            {f: list(k) for f, k in self._hidden_categories.items()}
        )

    def _on_swatch_clicked(self, key: str) -> None:
        feature = self._current_feature()
        if feature is None:
            return
        swatch = next(s for k, _check, s in self._category_rows if k == key)
        current = _rgba_to_hex(self._category_color(feature, key))
        # The shared picker: same presets and the same session-saved custom
        # colors as the image channels — no ramps, a category is one color.
        open_color_popup(
            swatch, current, self._custom_colors, include_ramps=False,
            on_pick=lambda v, f=feature, k=key: self._set_category_color(f, k, v),
        )

    def _set_category_color(self, feature: str, key: str, hex_color: str) -> None:
        self._category_colors.setdefault(feature, {})[key] = hex_color
        for k, _check, swatch in self._category_rows:
            if k == key:
                swatch.setStyleSheet(_swatch_style(hex_color))
        self.category_colors_changed.emit({f: dict(c) for f, c in self._category_colors.items()})

    def _on_reset_colors(self) -> None:
        feature = self._current_feature()
        if feature is None or feature not in self._category_colors:
            return
        del self._category_colors[feature]
        self._sync_categorical()
        self.category_colors_changed.emit({f: dict(c) for f, c in self._category_colors.items()})

    # -- state -------------------------------------------------------------

    def state(self) -> dict:
        """What this row currently shows. Mirrors `apply`."""
        text = self.color_by_combo.currentText()
        return {
            "visible": self.visible_check.isChecked(),
            "colormap": self.cmap_combo.currentText(),
            "color_by": None if text == FLAT_COLOR_LABEL else text,
            "show_outline": self.outline_check.isChecked(),
            "show_fill": self.fill_check.isChecked(),
            "fill_opacity": self.opacity_spin.value(),
            "palette": self.palette_combo.currentText(),
            "category_colors": {f: dict(c) for f, c in self._category_colors.items() if c},
            "hidden_categories": {f: list(k) for f, k in self._hidden_categories.items() if k},
        }

    def apply(
        self,
        colormap: str,
        color_by: str | None,
        show_outline: bool,
        show_fill: bool,
        fill_opacity: float,
        visible: bool,
        palette: str = DEFAULT_CATEGORY_PALETTE,
        category_colors: dict | None = None,
        hidden_categories: dict | None = None,
    ) -> None:
        """Push a saved (or default) state back into the widgets; each setter
        re-emits this row's signal, which is how it reaches the tile cache."""
        self.visible_check.setChecked(visible)
        self.cmap_combo.setCurrentText(colormap)
        self._category_colors = {f: dict(c) for f, c in (category_colors or {}).items()}
        self._hidden_categories = {f: list(k) for f, k in (hidden_categories or {}).items()}
        self.palette_combo.setCurrentText(palette)
        self.color_by_combo.setCurrentText(color_by if color_by is not None else FLAT_COLOR_LABEL)
        self.outline_check.setChecked(show_outline)
        self.fill_check.setChecked(show_fill)
        self.opacity_spin.setValue(float(fill_opacity))
        self._sync_categorical()
        # setCurrentText emits nothing when the value didn't change, so push
        # both mappings through unconditionally.
        self.category_colors_changed.emit({f: dict(c) for f, c in self._category_colors.items()})
        self.hidden_categories_changed.emit(
            {f: list(k) for f, k in self._hidden_categories.items()}
        )

    def _sync_opacity_enabled(self, fill_on: bool) -> None:
        self.opacity_spin.setEnabled(fill_on)

    def _emit_color_by(self, text: str) -> None:
        self._sync_categorical()
        self.color_by_changed.emit(None if text == FLAT_COLOR_LABEL else text)
