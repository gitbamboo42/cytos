"""Console-script entry points — see pyproject.toml [project.scripts]. Kept as
a single dispatch file so "what commands does cytos install, and what do they
run" is answerable by reading one place, not by hunting through the package.

Two commands do the real work: `cytos-import` builds a `.cytos` bundle from a
source dataset, `cytos-viewer` opens one. The per-layer prep steps are no
longer commands of their own — a layer's world grid is the bundle's, so
preparing one in isolation would put it on a grid nothing else shares.
"""

from cytos.prep.bundle import main as import_bundle
from cytos.prep.pyramid import main as convert_ome_zarr
from cytos.ui.main_window import main as viewer

__all__ = ["convert_ome_zarr", "import_bundle", "viewer"]
