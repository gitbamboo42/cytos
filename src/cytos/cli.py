"""Console-script entry points — see pyproject.toml [project.scripts]. Kept as
a single dispatch file so "what commands does cytos install, and what do they
run" is answerable by reading one place, not by hunting through the package.

`cytos-import` builds a `.cytos` slide from a source dataset; that is what
this package is for, now that the viewer is the app in `web/`. The per-layer
prep steps are not commands of their own — a layer's world grid is the
slide's, so preparing one in isolation would put it on a grid nothing else
shares.

Each entry imports its module inside the function, not at the top: importing
this module must not cost more than the command being run.
"""


def import_slide() -> None:
    """`cytos-import`: build a `.cytos` slide from a source dataset."""
    from cytos.prep.slide import main

    main()


def convert_ome_zarr() -> None:
    """`cytos-convert-ome-zarr`: standalone OME-TIFF → OME-Zarr utility."""
    from cytos.prep.pyramid import main

    main()


__all__ = ["convert_ome_zarr", "import_slide"]
