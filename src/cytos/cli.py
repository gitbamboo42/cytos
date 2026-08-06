"""Console-script entry points — see pyproject.toml [project.scripts]. Kept as
a single dispatch file so "what commands does cytos install, and what do they
run" is answerable by reading one place, not by hunting through the package."""

from cytos.prep.polygons import main as prep_polygons
from cytos.prep.pyramid import main as convert_ome_zarr
from cytos.ui.main_window import main as viewer

__all__ = ["convert_ome_zarr", "prep_polygons", "viewer"]
