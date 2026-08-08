"""Saved view state: what *you* changed, kept separately from what the bundle
says by default.

`cytos.json` holds a layer's defaults (colormap, contrast, which measurement
colours it). `session.json`, written next to it on close, holds only the
overrides on top — plus camera and window state, which have no default worth
recording. Keeping them in two files is what makes "reset" a real operation
instead of a hardcoded list of magic values: drop the session, re-read the
manifest.

It lives inside the bundle so it travels with the folder and is obvious where
to find (and delete) by hand. The trade is that a bundle is no longer strictly
read-only, and a copied bundle carries whoever-made-it's view state. Nothing
else writes into a bundle, and a session that's missing, unreadable, or from a
newer cytos is ignored rather than fatal — a broken session must never be the
reason a dataset won't open.
"""

from __future__ import annotations

import json
from pathlib import Path

SESSION_NAME = "session.json"
SESSION_FORMAT = 1


def session_path(bundle_root: Path) -> Path:
    return Path(bundle_root) / SESSION_NAME


def load_session(bundle_root: Path) -> dict:
    """The saved state, or `{}` if there isn't one (or it can't be read)."""
    path = session_path(bundle_root)
    if not path.exists():
        return {}
    try:
        session = json.loads(path.read_text())
    except (OSError, ValueError) as err:
        print(f"{path}: ignoring unreadable session ({err})")
        return {}
    if not isinstance(session, dict) or int(session.get("cytos_session", 0)) > SESSION_FORMAT:
        print(f"{path}: ignoring session written by a newer cytos")
        return {}
    return session


def save_session(bundle_root: Path, session: dict) -> None:
    path = session_path(bundle_root)
    try:
        path.write_text(json.dumps({"cytos_session": SESSION_FORMAT, **session}, indent=2) + "\n")
    except OSError as err:
        # A read-only or network bundle is a normal thing to be looking at;
        # losing the view state is not a reason to fail on the way out.
        print(f"{path}: could not save session ({err})")


def clear_session(bundle_root: Path) -> None:
    session_path(bundle_root).unlink(missing_ok=True)
