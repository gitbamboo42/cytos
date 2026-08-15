"""Programmatic control of one slide window — the object behind the control
socket's per-window commands (see `cytos.remote.ipc` for the wire, and
`cytos.ui.main_window` for the dispatcher that routes to a window).

Built by `build_window` after the widgets exist, and given the same closures
and row lists the menus use. Every change goes *through the dock-panel
widgets* (each row's `apply` re-emits its own signals, which is how a change
reaches the tile caches), so a remote command and a mouse click are the same
code path and the panel always shows the truth. Nothing here is a second way
to mutate render state — it's a second way to press the existing controls.

The vocabulary is deliberately the saved-session vocabulary: `state()`
returns what `collect_session` writes to disk, and `apply_state` accepts a
partial dict of the same shape. Anyone who can read a `session.json` can
already write a valid command, and `describe()` lists every legal value
(layer keys, colormap names, features, genes) so a caller — human or AI —
can form one without guessing.
"""

from __future__ import annotations

import numpy as np

from cytos.remote.ipc import CommandError
from cytos.render.camera import effective_camera_view_size
from cytos.render.image import CHANNEL_COLOR_PRESETS, CHANNEL_RAMP_CHOICES
from cytos.render.points import COLOR_MODE_FLAT, COLOR_MODE_GENE
from cytos.ui.segment_panel import FLAT_COLOR_LABEL

# No "visible" for points: a points row has no per-row checkbox, only the
# section master switch (`sections.points.checked`) — see cytos.ui.points_panel.
_LAYER_FIELDS = {
    "image": {"visible", "colormap", "clim"},
    "segments": {"visible", "colormap", "color_by", "show_outline", "show_fill", "fill_opacity", "palette", "category_colors", "hidden_categories"},
    "points": {"color_mode", "palette", "colormap", "size", "opacity", "genes"},
}

_CAMERA_FIELDS = {"fit", "rect", "center", "size", "width", "height"}


def _combo_items(combo) -> list[str]:
    return [combo.itemText(i) for i in range(combo.count())]


def _check_choice(value: str, combo, what: str) -> None:
    if combo.findText(value) < 0:
        raise CommandError(f"unknown {what} '{value}' — one of: {', '.join(_combo_items(combo))}")


def _check_fields(partial: dict, allowed: set[str], key: str) -> None:
    unknown = set(partial) - allowed
    if unknown:
        raise CommandError(
            f"'{key}' has no field(s) {sorted(unknown)} — settable: {sorted(allowed)}"
        )


def _check_hex_color(value) -> None:
    v = str(value)
    if not (len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:])):
        raise CommandError(f"'{value}' is not a color — use \"#rrggbb\"")


def _check_channel_color(value) -> None:
    """An image channel's `colormap` is a color: any "#rrggbb", or a legacy
    colormap name a saved session may still carry."""
    v = str(value)
    if len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:]):
        return
    import plotlet

    if v in plotlet.list_colormaps():
        return
    raise CommandError(
        f"'{value}' is not a channel color — use \"#rrggbb\" "
        f"(presets: {', '.join(h for _n, h in CHANNEL_COLOR_PRESETS)})"
    )


def _check_categorical_feature(feature: str, categorical: dict) -> None:
    if feature not in categorical:
        raise CommandError(
            f"'{feature}' is not a categorical feature — one of: {', '.join(categorical) or '(none)'}"
        )


def _check_category_key(category, feature: str, categorical: dict) -> str:
    legal = [k for k, _n in categorical[feature]]
    if str(category) not in legal:
        raise CommandError(f"'{feature}' has no category '{category}' — one of: {', '.join(legal)}")
    return str(category)


def _check_category_colors(value, categorical: dict) -> dict:
    """Validate a feature -> {category -> "#rrggbb"} mapping against the
    layer's actual categorical features, normalising category keys to the
    strings the session stores ("7", or "unassigned")."""
    if not isinstance(value, dict):
        raise CommandError('category_colors must be an object like {"cluster": {"7": "#e41a1c"}}')
    checked: dict[str, dict[str, str]] = {}
    for feature, colors in value.items():
        _check_categorical_feature(feature, categorical)
        if not isinstance(colors, dict):
            raise CommandError(f"category_colors['{feature}'] must map categories to colors")
        checked[feature] = {}
        for category, hex_color in colors.items():
            key = _check_category_key(category, feature, categorical)
            _check_hex_color(hex_color)
            checked[feature][key] = str(hex_color).lower()
    return checked


def _check_hidden_categories(value, categorical: dict) -> dict:
    """Validate a feature -> [category, ...] mapping the same way."""
    if not isinstance(value, dict):
        raise CommandError('hidden_categories must be an object like {"cluster": ["3", "unassigned"]}')
    checked: dict[str, list[str]] = {}
    for feature, keys in value.items():
        _check_categorical_feature(feature, categorical)
        if not isinstance(keys, (list, tuple)):
            raise CommandError(f"hidden_categories['{feature}'] must be a list of categories")
        checked[feature] = [_check_category_key(k, feature, categorical) for k in keys]
    return checked


class WindowController:
    def __init__(
        self,
        *,
        win,
        slide,
        session_name,
        camera,
        renderer,
        render_widget,
        collect_session,
        reset_to_defaults,
        fit_camera_to_slide,
        render_offscreen,
        save_now,
        pick_world,
        image_rows,
        segment_rows,
        point_rows,
        sections,
        layer_meta,
        panel=None,
        custom_colors=None,
    ):
        self.win = win
        self.slide = slide
        self.session_name = session_name
        self.camera = camera
        self.renderer = renderer
        self.render_widget = render_widget
        self.collect_session = collect_session
        self.reset_to_defaults = reset_to_defaults
        self.fit_camera_to_slide = fit_camera_to_slide
        self.render_offscreen = render_offscreen
        self.save_now = save_now
        self.pick_world = pick_world
        # Live references to build_window's own lists, so layers added later
        # (File > Add Image…/Add Segments…) appear here without bookkeeping.
        self.image_rows = image_rows
        self.segment_rows = segment_rows
        self.point_rows = point_rows
        self.sections = sections
        self.layer_meta = layer_meta
        # The dock panel widget, for snapshot(target="panel").
        self.panel = panel
        # The window's shared pool of user-created colors (session field
        # `custom_colors`), settable like everything else session-shaped.
        self.custom_colors = custom_colors

    # -- reading -----------------------------------------------------------

    def summary(self) -> dict:
        """One line per window, for `windows` listings."""
        return {
            "window": self.win.window_id,
            "slide": self.slide.name,
            "session": self.session_name,
            "title": self.win.windowTitle(),
        }

    def state(self) -> dict:
        """Exactly what a saved session holds, minus the base64 window
        geometry blobs, which no caller can do anything with. Session-shaped
        all the way down, so the output round-trips through `apply_state`
        untouched — the computed `view_rect` lives in `describe()`, not here."""
        state = dict(self.collect_session())
        state.pop("window", None)
        return state

    def describe(self) -> dict:
        """Everything needed to form a valid `set` command: the layer keys,
        each layer's current state, and every legal value for its fields."""
        layers = {}
        for layer, _ch, row in self.image_rows:
            key = f"image:{layer.id}"
            layers[key] = {
                "kind": "image",
                "state": row.state(),
                # `colormap` takes any "#rrggbb" (the panel's presets below)
                # or one of the ramp colormap names.
                "colors": [h for _n, h in CHANNEL_COLOR_PRESETS],
                "colormaps": CHANNEL_RAMP_CHOICES,
                **self.layer_meta.get(key, {}),
            }
        for layer, feature_names, row in self.segment_rows:
            key = f"segments:{layer.id}"
            layers[key] = {
                "kind": "segments",
                "state": row.state(),
                "colormaps": _combo_items(row.cmap_combo),
                "color_by_choices": [None, *feature_names],
                "palettes": _combo_items(row.palette_combo),
                # Which features are categorical, and their legal category
                # keys ("7", "unassigned") -- what category_colors and
                # hidden_categories may name.
                "categories": {f: [k for k, _n in cats] for f, cats in row.categorical.items()},
                **self.layer_meta.get(key, {}),
            }
        for layer, gene_names, row in self.point_rows:
            key = f"points:{layer.id}"
            layers[key] = {
                "kind": "points",
                "state": row.state(),
                "color_modes": [COLOR_MODE_GENE, COLOR_MODE_FLAT],
                "palettes": _combo_items(row.palette_combo),
                "colormaps": _combo_items(row.cmap_combo),
                # Index = gene id, which is what `state()["genes"]` holds; a
                # `set` may use either ids or these names.
                "genes": list(gene_names),
                **self.layer_meta.get(key, {}),
            }
        minx, miny, maxx, maxy = self.slide.world_bounds
        return {
            **self.summary(),
            "root": str(self.slide.root),
            # World Y increases downward, so miny is the top edge on screen.
            "world_bounds": [float(minx), float(miny), float(maxx), float(maxy)],
            "world_units": self.slide.world_units,
            "camera": self._camera_state(),
            "sections": {
                name: {"expanded": s.is_expanded(), "checked": s.is_checked()}
                for name, s in self.sections.items()
            },
            "layers": layers,
        }

    def _camera_state(self) -> dict:
        """The camera for `describe()` only: the session fields plus the
        computed, read-only `view_rect`. `state()` must not use this —
        `view_rect` is not settable, and `state` promises a dict that feeds
        back into `apply_state` as-is."""
        cx = float(self.camera.local.position[0])
        cy = float(self.camera.local.position[1])
        w, h = float(self.camera.width), float(self.camera.height)
        # What is actually on screen, which is wider than the camera's own
        # rect whenever the viewport aspect differs — see cytos.render.camera.
        logical_w, logical_h = self.render_widget.get_logical_size()
        eff_w, eff_h = effective_camera_view_size(w, h, logical_w, logical_h)
        return {
            "center": [cx, cy],
            "size": [w, h],
            "view_rect": [cx - eff_w / 2, cy - eff_h / 2, cx + eff_w / 2, cy + eff_h / 2],
        }

    # -- writing -----------------------------------------------------------

    def apply_state(self, changes: dict) -> dict:
        """Apply a partial, session-shaped dict: any of `camera`, `sections`,
        `layers` — each layer entry itself partial, merged over what the row
        currently shows. Returns the resulting state."""
        _check_fields(changes, {"camera", "sections", "layers", "custom_colors"}, "set")
        if "camera" in changes:
            self.set_camera(changes["camera"])
        if "custom_colors" in changes:
            value = changes["custom_colors"]
            if not isinstance(value, (list, tuple)):
                raise CommandError('custom_colors must be a list like ["#ff8000"]')
            for c in value:
                _check_hex_color(c)
            self.custom_colors.set([str(c) for c in value])
        for name, partial in changes.get("sections", {}).items():
            section = self.sections.get(name)
            if section is None:
                raise CommandError(f"unknown section '{name}' — one of: {', '.join(self.sections)}")
            _check_fields(partial, {"expanded", "checked"}, name)
            section.apply(
                bool(partial.get("expanded", section.is_expanded())),
                bool(partial.get("checked", section.is_checked())),
            )
        for key, partial in changes.get("layers", {}).items():
            self._apply_layer(key, partial)
        return self.state()

    def set_camera(self, spec: dict) -> None:
        _check_fields(spec, _CAMERA_FIELDS, "camera")
        if spec.get("fit"):
            self.fit_camera_to_slide()
            return
        if "rect" in spec:
            minx, miny, maxx, maxy = (float(v) for v in spec["rect"])
            self.camera.show_rect(minx, maxx, miny, maxy)
            return
        cx = float(self.camera.local.position[0])
        cy = float(self.camera.local.position[1])
        w, h = float(self.camera.width), float(self.camera.height)
        if "center" in spec:
            cx, cy = (float(v) for v in spec["center"])
        if "size" in spec:
            w, h = (float(v) for v in spec["size"])
        elif "width" in spec:
            # Width alone keeps the current aspect — "zoom to 500 µm across"
            # shouldn't need the matching height worked out by the caller.
            new_w = float(spec["width"])
            w, h = new_w, h * (new_w / w)
        elif "height" in spec:
            new_h = float(spec["height"])
            w, h = w * (new_h / h), new_h
        self.camera.show_rect(cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2)

    def _apply_layer(self, key: str, partial: dict) -> None:
        kind = key.split(":", 1)[0]
        if kind not in _LAYER_FIELDS:
            raise CommandError(f"unknown layer '{key}' — layer keys: {self._layer_keys()}")
        _check_fields(partial, _LAYER_FIELDS[kind], key)

        if kind == "image":
            for layer, _ch, row in self.image_rows:
                if f"image:{layer.id}" == key:
                    s = {**row.state(), **partial}
                    if "colormap" in partial:
                        _check_channel_color(s["colormap"])
                    row.apply(s["colormap"], bool(s["visible"]), tuple(s["clim"]))
                    return
        elif kind == "segments":
            for layer, feature_names, row in self.segment_rows:
                if f"segments:{layer.id}" == key:
                    s = {**row.state(), **partial}
                    if "colormap" in partial:
                        _check_choice(s["colormap"], row.cmap_combo, "colormap")
                    if "palette" in partial:
                        _check_choice(s["palette"], row.palette_combo, "palette")
                    if s["color_by"] is not None and s["color_by"] not in feature_names:
                        raise CommandError(
                            f"unknown color_by '{s['color_by']}' — null ({FLAT_COLOR_LABEL.lower()}) "
                            f"or one of: {', '.join(feature_names)}"
                        )
                    if "category_colors" in partial:
                        s["category_colors"] = _check_category_colors(
                            partial["category_colors"], row.categorical
                        )
                    if "hidden_categories" in partial:
                        s["hidden_categories"] = _check_hidden_categories(
                            partial["hidden_categories"], row.categorical
                        )
                    row.apply(
                        s["colormap"], s["color_by"], bool(s["show_outline"]),
                        bool(s["show_fill"]), float(s["fill_opacity"]), bool(s["visible"]),
                        s["palette"], s["category_colors"], s["hidden_categories"],
                    )
                    return
        else:
            for layer, gene_names, row in self.point_rows:
                if f"points:{layer.id}" == key:
                    s = {**row.state(), **partial}
                    if "palette" in partial:
                        _check_choice(s["palette"], row.palette_combo, "palette")
                    if "colormap" in partial:
                        _check_choice(s["colormap"], row.cmap_combo, "colormap")
                    if s["color_mode"] not in (COLOR_MODE_GENE, COLOR_MODE_FLAT):
                        raise CommandError(
                            f"color_mode must be '{COLOR_MODE_GENE}' or '{COLOR_MODE_FLAT}'"
                        )
                    genes = self._gene_ids(s["genes"], gene_names)
                    row.apply(
                        s["color_mode"], s["palette"], s["colormap"],
                        float(s["size"]), float(s["opacity"]), genes,
                    )
                    return
        raise CommandError(f"unknown layer '{key}' — layer keys: {self._layer_keys()}")

    def _layer_keys(self) -> str:
        keys = [f"image:{layer.id}" for layer, _ch, _row in self.image_rows]
        keys += [f"segments:{layer.id}" for layer, _f, _row in self.segment_rows]
        keys += [f"points:{layer.id}" for layer, _g, _row in self.point_rows]
        return ", ".join(keys) or "(none)"

    @staticmethod
    def _gene_ids(genes, gene_names) -> set[int] | None:
        """A session's `genes` field, as the row wants it: None means all,
        entries may be ids (ints) or names (strs) — names are what a caller
        actually has in hand."""
        if genes is None:
            return None
        by_name = {name: i for i, name in enumerate(gene_names)}
        ids = set()
        for g in genes:
            if isinstance(g, str):
                if g not in by_name:
                    raise CommandError(f"unknown gene '{g}'")
                ids.add(by_name[g])
            else:
                gene_id = int(g)
                if not 0 <= gene_id < len(gene_names):
                    raise CommandError(f"gene id {gene_id} out of range (0..{len(gene_names) - 1})")
                ids.add(gene_id)
        return ids

    # -- actions -----------------------------------------------------------

    def snapshot(self, path: str, target: str = "canvas") -> dict:
        """Render the current state and write it to `path` as a PNG.

        `target="canvas"` (the default) is the slide view: `render_offscreen`
        draws a fresh frame right now — tile loads are synchronous, and no
        window repaint is involved — so the PNG reflects the command that
        just ran even if the window is hidden, occluded, or hasn't had a
        paint event since the last change.

        `target="panel"` grabs the dock panel widget instead — the controls,
        not the picture. It exists so whoever is *building* the UI (an AI
        assistant, usually) can look at their own work instead of asking the
        human to referee pixels; `QWidget.grab` renders the widget tree
        directly, so it works while the window is hidden too.
        """
        from PIL import Image

        if target == "panel":
            if self.panel is None:
                raise CommandError("this window has no panel to snapshot")
            pixmap = self.panel.grab()
            if not pixmap.save(path, "PNG"):
                raise CommandError(f"could not write {path}")
            return {"path": path, "width": pixmap.width(), "height": pixmap.height()}
        if target != "canvas":
            raise CommandError(f"unknown snapshot target '{target}' — 'canvas' or 'panel'")

        self.render_offscreen()
        array = np.asarray(self.renderer.snapshot())
        if array.ndim != 3:
            raise CommandError(f"renderer returned no frame (shape {array.shape})")
        Image.fromarray(array[..., :3].astype("uint8"), mode="RGB").save(path)
        height, width = array.shape[:2]
        return {"path": path, "width": int(width), "height": int(height)}

    def pick(self, x, y) -> dict:
        """Which cell is at world point (x, y): the layer key, the cell's
        dense index, and its whole feature row -- or hit: false. The point
        must be inside the current view (picking reads the rendered frame)."""
        return self.pick_world(float(x), float(y))

    def reset(self) -> dict:
        self.reset_to_defaults()
        return self.state()

    def save(self) -> dict:
        self.save_now()
        return {"session": self.session_name, "saved": True}
