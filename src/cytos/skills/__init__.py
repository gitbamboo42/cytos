"""AI onboarding guides, shipped inside the package. The guides are the
`.md` files in this directory; this module is just the code that serves
them, living with its data.

They ship inside the package so an AI assistant can read them straight from
an install — no repo clone, no copy-paste, and no drifting from the code
they ship with. `guide_text` is how a command would serve them; the one that
did (`cytos-ctl skill`) was removed with the rest of that surface, so today
the files are simply read.
"""

from pathlib import Path

_GUIDES = {
    "user": "users.md",
    "developer": "developers.md",
}


def guide_text(which="user") -> str:
    """One guide's full text.

    ``"user"`` (default) — for AI assistants *operating the viewer*: mental
    model, the command loop, the state vocabulary, coordinates, snapshots,
    session etiquette.
    ``"developer"`` — for AI assistants *working on cytos itself*:
    architecture, the data model, verified gotchas (assumes a repo clone;
    contributors symlink it as their tool's config file).
    """
    try:
        name = _GUIDES[which]
    except KeyError:
        options = ", ".join(repr(k) for k in _GUIDES)
        raise ValueError(f"unknown guide {which!r}; expected one of {options}") from None
    return (Path(__file__).parent / name).read_text(encoding="utf-8")
