"""AI onboarding guides, shipped inside the package. The guides are the
`.md` files in this directory; this module is just the code that serves
them, living with its data.

`cytos-ctl skill` prints the users guide (and `cytos-ctl skill developer`
the developer one) so an AI assistant can read it straight from the
installed package — no repo clone, no copy-paste, and it can't drift from
the code it ships with. The same text backs the MCP server's `usage_guide`
tool.
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
