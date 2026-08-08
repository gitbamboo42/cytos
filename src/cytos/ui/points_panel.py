"""Dock-panel controls for the transcript-point layer: how the dots look
(size, opacity), what colours them (one colour, or one colour per gene), and
which genes are shown at all.

The gene list is the part that matters. A real panel is a few hundred genes and
the interesting view is almost never "all of them" -- it's two or three genes
at once, each in its own colour. So the list is checkable, sorted by abundance
rather than alphabetically (the genes worth looking at first are the ones
actually detected), and filterable by typing.

Checking and unchecking only rewrites a colour LUT of one row per gene (see
`cytos.render.points`), so this stays instant no matter how many transcripts
are on screen.
"""

from __future__ import annotations

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from cytos.render.points import (
    COLOR_MODE_FLAT,
    COLOR_MODE_GENE,
    CURATED_PALETTES,
    gene_colors,
)
from cytos.ui.colormap_combo import make_colormap_combo

_LIST_HEIGHT = 180
_SWATCH = 12

_MODE_LABELS = {"Per gene": COLOR_MODE_GENE, "One color": COLOR_MODE_FLAT}


def _color_icon(rgba: np.ndarray) -> QtGui.QIcon:
    pixmap = QtGui.QPixmap(_SWATCH, _SWATCH)
    r, g, b = (int(round(float(c) * 255)) for c in rgba[:3])
    pixmap.fill(QtGui.QColor(r, g, b))
    return QtGui.QIcon(pixmap)


class PointsRow(QtWidgets.QGroupBox):
    color_mode_changed = QtCore.Signal(str)
    palette_changed = QtCore.Signal(str)
    colormap_changed = QtCore.Signal(str)
    size_changed = QtCore.Signal(float)
    opacity_changed = QtCore.Signal(float)
    visible_genes_changed = QtCore.Signal(object)  # set[int], or None for "all"

    def __init__(
        self,
        title: str,
        gene_names: list[str],
        gene_counts: list[int],
        color_mode: str,
        palette: str,
        colormap: str,
        size: float,
        opacity: float,
        visible_genes: set[int] | None = None,
    ):
        super().__init__(title)
        self._gene_names = gene_names
        layout = QtWidgets.QFormLayout(self)

        self.mode_combo = QtWidgets.QComboBox()
        for label in _MODE_LABELS:
            self.mode_combo.addItem(label)
        self.mode_combo.setCurrentText(
            next(label for label, mode in _MODE_LABELS.items() if mode == color_mode)
        )
        self.mode_combo.currentTextChanged.connect(self._on_mode)
        layout.addRow("Color", self.mode_combo)

        self.palette_combo = QtWidgets.QComboBox()
        for name in CURATED_PALETTES:
            self.palette_combo.addItem(name)
        self.palette_combo.setCurrentText(palette)
        self.palette_combo.currentTextChanged.connect(self._on_palette)
        layout.addRow("Palette", self.palette_combo)

        self.cmap_combo = make_colormap_combo(colormap)
        self.cmap_combo.currentTextChanged.connect(self.colormap_changed)
        layout.addRow("One color", self.cmap_combo)

        self.size_spin = QtWidgets.QDoubleSpinBox()
        self.size_spin.setRange(0.5, 20.0)
        self.size_spin.setSingleStep(0.5)
        self.size_spin.setValue(size)
        self.size_spin.valueChanged.connect(self.size_changed)
        layout.addRow("Point size", self.size_spin)

        self.opacity_spin = QtWidgets.QDoubleSpinBox()
        self.opacity_spin.setRange(0.0, 1.0)
        self.opacity_spin.setSingleStep(0.05)
        self.opacity_spin.setDecimals(2)
        self.opacity_spin.setValue(opacity)
        self.opacity_spin.valueChanged.connect(self.opacity_changed)
        layout.addRow("Opacity", self.opacity_spin)

        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText(f"Filter {len(gene_names)} genes…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._on_filter)
        layout.addRow(self.filter_edit)

        self.gene_list = QtWidgets.QListWidget()
        self.gene_list.setFixedHeight(_LIST_HEIGHT)
        self.gene_list.setUniformItemSizes(True)
        self.gene_list.setIconSize(QtCore.QSize(_SWATCH, _SWATCH))
        # Most-detected first: with a few hundred genes, alphabetical order
        # buries everything actually worth looking at.
        order = sorted(range(len(gene_names)), key=lambda i: (-gene_counts[i], gene_names[i]))
        for gene_id in order:
            item = QtWidgets.QListWidgetItem(f"{gene_names[gene_id]}  ({gene_counts[gene_id]})")
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            checked = visible_genes is None or gene_id in visible_genes
            item.setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, gene_id)
            self.gene_list.addItem(item)
        self.gene_list.itemChanged.connect(self._on_item_changed)
        layout.addRow(self.gene_list)

        buttons = QtWidgets.QWidget()
        button_layout = QtWidgets.QHBoxLayout(buttons)
        button_layout.setContentsMargins(0, 0, 0, 0)
        all_button = QtWidgets.QPushButton("All")
        none_button = QtWidgets.QPushButton("None")
        all_button.clicked.connect(lambda: self._set_all(True))
        none_button.clicked.connect(lambda: self._set_all(False))
        button_layout.addWidget(all_button)
        button_layout.addWidget(none_button)
        layout.addRow(buttons)

        self._sync_mode_enabled(color_mode)
        self._refresh_swatches()

    # -- color mode --------------------------------------------------------

    def _on_mode(self, label: str) -> None:
        mode = _MODE_LABELS[label]
        self._sync_mode_enabled(mode)
        self._refresh_swatches()
        self.color_mode_changed.emit(mode)

    def _on_palette(self, palette: str) -> None:
        self._refresh_swatches()
        self.palette_changed.emit(palette)

    def _sync_mode_enabled(self, mode: str) -> None:
        self.palette_combo.setEnabled(mode == COLOR_MODE_GENE)
        self.cmap_combo.setEnabled(mode == COLOR_MODE_FLAT)

    def _refresh_swatches(self) -> None:
        """Each row's swatch is the colour that gene actually renders in, so
        the list doubles as the legend -- which means it has to be recomputed
        whenever the *selection* changes too, not just the palette: a selected
        gene's colour depends on its rank among the selected ones (see
        `cytos.render.points.gene_colors`). Cleared in flat mode, where 400
        identical swatches would say nothing."""
        gene_mode = _MODE_LABELS[self.mode_combo.currentText()] == COLOR_MODE_GENE
        colors = (
            gene_colors(len(self._gene_names), self.palette_combo.currentText(), self._visible_genes())
            if gene_mode
            else None
        )
        blank = QtGui.QIcon()
        # QListWidget emits itemChanged for *any* item change, setIcon
        # included -- and this method is itself called from that signal, so
        # without blocking it here the first swatch update recurses until the
        # interpreter runs out of stack.
        blocked = self.gene_list.blockSignals(True)
        try:
            for i in range(self.gene_list.count()):
                item = self.gene_list.item(i)
                if colors is None:
                    item.setIcon(blank)
                    continue
                rgba = colors[item.data(QtCore.Qt.ItemDataRole.UserRole)]
                # Alpha 0 means "hidden", and a black swatch would read as a
                # real colour rather than as "not drawn".
                item.setIcon(_color_icon(rgba) if rgba[3] > 0 else blank)
        finally:
            self.gene_list.blockSignals(blocked)

    # -- gene selection ----------------------------------------------------

    def _on_filter(self, text: str) -> None:
        needle = text.strip().upper()
        for i in range(self.gene_list.count()):
            item = self.gene_list.item(i)
            name = self._gene_names[item.data(QtCore.Qt.ItemDataRole.UserRole)]
            item.setHidden(bool(needle) and needle not in name.upper())

    def _set_all(self, checked: bool) -> None:
        # One signal, not one per gene: itemChanged fires per row, and a few
        # hundred LUT rebuilds in a row would visibly stall the click.
        state = QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked
        blocked = self.gene_list.blockSignals(True)
        for i in range(self.gene_list.count()):
            self.gene_list.item(i).setCheckState(state)
        self.gene_list.blockSignals(blocked)
        self._emit_visible()

    def _on_item_changed(self, _item: QtWidgets.QListWidgetItem) -> None:
        self._emit_visible()

    def _visible_genes(self) -> set[int] | None:
        """The checked gene ids, or None when everything is checked -- the
        renderer then skips the hide mask entirely and cycles the palette over
        all genes instead of ranking a selection."""
        visible = {
            self.gene_list.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
            for i in range(self.gene_list.count())
            if self.gene_list.item(i).checkState() == QtCore.Qt.CheckState.Checked
        }
        return None if len(visible) == self.gene_list.count() else visible

    def state(self) -> dict:
        """What this row currently shows. Mirrors `apply`. `genes` is None for
        "all of them", matching `visible_genes_changed`."""
        genes = self._visible_genes()
        return {
            "color_mode": _MODE_LABELS[self.mode_combo.currentText()],
            "palette": self.palette_combo.currentText(),
            "colormap": self.cmap_combo.currentText(),
            "size": self.size_spin.value(),
            "opacity": self.opacity_spin.value(),
            "genes": None if genes is None else sorted(genes),
        }

    def apply(
        self,
        color_mode: str,
        palette: str,
        colormap: str,
        size: float,
        opacity: float,
        visible_genes: set[int] | None,
    ) -> None:
        """Push a saved (or default) state back into the widgets; each setter
        re-emits this row's signal, which is how it reaches the tile cache."""
        self.mode_combo.setCurrentText(next(label for label, m in _MODE_LABELS.items() if m == color_mode))
        self.palette_combo.setCurrentText(palette)
        self.cmap_combo.setCurrentText(colormap)
        self.size_spin.setValue(float(size))
        self.opacity_spin.setValue(float(opacity))

        # One signal for the whole list, as in _set_all: a few hundred
        # per-item emissions would each rebuild the colour LUT.
        blocked = self.gene_list.blockSignals(True)
        for i in range(self.gene_list.count()):
            item = self.gene_list.item(i)
            gene_id = item.data(QtCore.Qt.ItemDataRole.UserRole)
            checked = visible_genes is None or gene_id in visible_genes
            item.setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
        self.gene_list.blockSignals(blocked)
        self._emit_visible()

    def _emit_visible(self) -> None:
        visible = self._visible_genes()
        # Swatches first: the selection just changed, so the colours the list
        # is claiming are stale until this runs.
        self._refresh_swatches()
        self.visible_genes_changed.emit(visible)
