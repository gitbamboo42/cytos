"""Named sessions: saved view state, several per bundle, one window at a time.

`cytos.json` holds a layer's defaults — colormap, contrast, which measurement
colours it — and is shared by everyone who opens the bundle. A *session* holds
only the overrides on top, plus camera and window state, which have no default
worth recording. Keeping them apart is what makes "reset" a real operation
rather than a hardcoded list of magic values: clear the session, re-read the
manifest.

Several sessions per bundle, because one view is not enough for one dataset —
two regions of the same slide are the ordinary case, and a single saved view
means the second window silently overwrites the first. So::

    sample.cytos/
      cytos.json                    shared defaults
      sessions/
        default.json  default.png   one view, plus a snapshot for the picker
        tumor-edge.json  .png

One file per session rather than one file holding a list, specifically so two
open windows never write the same file. That is what makes "a session belongs
to one window" enforceable instead of merely advisory.

Sessions live inside the bundle so they travel with the folder. The trade is
that a bundle is not strictly read-only; nothing else writes into one. A
session that's missing, unreadable, or from a newer cytos is ignored rather
than fatal — a broken session must never be why a dataset won't open.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SESSIONS_DIR = "sessions"
SESSION_FORMAT = 1
DEFAULT_SESSION_NAME = "default"

# What `cytos.core.session` used to write: a single file at the bundle root.
# Migrated on first open rather than dropped -- it holds real work.
_LEGACY_NAME = "session.json"


@dataclass
class SessionInfo:
    """One saved session, as the picker needs to show it."""

    name: str
    path: Path
    snapshot: Path | None
    modified: datetime | None

    @property
    def label(self) -> str:
        if self.modified is None:
            return self.name
        return f"{self.name}  ·  {self.modified.strftime('%Y-%m-%d %H:%M')}"


def sessions_dir(bundle_root: Path) -> Path:
    return Path(bundle_root) / SESSIONS_DIR


def slugify(name: str) -> str:
    """A filename that still reads like the name the user typed. The name
    itself is kept inside the JSON, so this only has to be unique and safe."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    return slug.lower() or "session"


def session_path(bundle_root: Path, name: str) -> Path:
    return sessions_dir(bundle_root) / f"{slugify(name)}.json"


def snapshot_path(bundle_root: Path, name: str) -> Path:
    return sessions_dir(bundle_root) / f"{slugify(name)}.png"


def migrate_legacy(bundle_root: Path) -> None:
    """Move a pre-named-sessions `session.json` into `sessions/default.json`.
    Runs before any listing, so old bundles just show one session called
    "default" instead of appearing to have lost their state."""
    legacy = Path(bundle_root) / _LEGACY_NAME
    if not legacy.exists():
        return
    target = session_path(bundle_root, DEFAULT_SESSION_NAME)
    if target.exists():
        return
    try:
        data = json.loads(legacy.read_text())
    except (OSError, ValueError):
        return
    data["name"] = DEFAULT_SESSION_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n")
    legacy.unlink(missing_ok=True)
    print(f"migrated {legacy} -> {target}")


def list_sessions(bundle_root: Path) -> list[SessionInfo]:
    """Every saved session, most recently used first."""
    migrate_legacy(bundle_root)
    directory = sessions_dir(bundle_root)
    if not directory.is_dir():
        return []

    found = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            print(f"{path}: ignoring unreadable session")
            continue
        if int(data.get("cytos_session", 0)) > SESSION_FORMAT:
            print(f"{path}: ignoring session written by a newer cytos")
            continue
        snapshot = path.with_suffix(".png")
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            modified = None
        found.append(
            SessionInfo(
                name=str(data.get("name", path.stem)),
                path=path,
                snapshot=snapshot if snapshot.exists() else None,
                modified=modified,
            )
        )
    found.sort(key=lambda s: s.modified or datetime.min, reverse=True)
    return found


def unique_session_name(bundle_root: Path, base: str = "Session") -> str:
    """`base`, or the first `base N` not already taken -- what a new session is
    offered as, so creating one is a single Enter rather than a naming task."""
    taken = {s.name.lower() for s in list_sessions(bundle_root)}
    if base.lower() not in taken:
        return base
    n = 2
    while f"{base} {n}".lower() in taken:
        n += 1
    return f"{base} {n}"


def load_session(bundle_root: Path, name: str) -> dict:
    """One session's saved state, or `{}` if it can't be read."""
    path = session_path(bundle_root, name)
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


def save_session(bundle_root: Path, name: str, session: dict, snapshot=None) -> None:
    """Write a session, and its picker thumbnail if one was captured.

    `snapshot` is an (H, W, 3|4) uint8 array — the actual rendered frame, so
    the picker shows the region the session is about rather than the whole
    slide.
    """
    path = session_path(bundle_root, name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"cytos_session": SESSION_FORMAT, "name": name, **session}, indent=2) + "\n"
        )
    except OSError as err:
        # A read-only or network bundle is a normal thing to be looking at;
        # losing the view state is not a reason to fail on the way out.
        print(f"{path}: could not save session ({err})")
        return

    if snapshot is not None:
        _write_snapshot(snapshot_path(bundle_root, name), snapshot)


def _write_snapshot(path: Path, rgb, max_side: int = 320) -> None:
    try:
        import numpy as np
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow rides along with imageio
        return
    try:
        array = np.asarray(rgb)
        if array.ndim != 3:
            return
        image = Image.fromarray(array[..., :3].astype("uint8"), mode="RGB")
        image.thumbnail((max_side, max_side))
        image.save(path)
    except (OSError, ValueError) as err:
        print(f"{path}: could not save snapshot ({err})")


def delete_session(bundle_root: Path, name: str) -> None:
    session_path(bundle_root, name).unlink(missing_ok=True)
    snapshot_path(bundle_root, name).unlink(missing_ok=True)
