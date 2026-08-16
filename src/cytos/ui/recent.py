"""The recently-opened-slides list behind File > Open Recent.

Plain JSON in the app's own data directory — Qt's AppDataLocation, which is
`~/Library/Application Support/cytos` on macOS, `~/.local/share/cytos` on
Linux, `%APPDATA%/cytos` on Windows. This is the first (and so far only)
app-level file cytos keeps: everything else persistent lives inside a slide
directory, but "which slides have I opened" belongs to the user, not to any
one slide. The path ends in "cytos" only because `_ensure_app` names the
application; without that Qt falls back to the executable's basename, and the
file would move around depending on how the app was launched.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QStandardPaths

_MAX_RECENT = 10
_STORE_NAME = "recent_slides.json"


def _store_path() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    return Path(base) / _STORE_NAME


def recent_slides() -> list[Path]:
    """Every remembered slide, most recently opened first.

    Paths that don't currently exist are kept, not dropped: a slide on an
    unmounted drive isn't gone, and forgetting it because the drive was out
    once would be a surprise. The menu skips missing ones at display time.
    """
    try:
        entries = json.loads(_store_path().read_text())
    except (OSError, ValueError):
        # No file yet, or an unreadable one. Either way the list is empty and
        # the next remember_slide() rewrites the store whole, so a corrupted
        # file heals itself rather than needing hand-repair.
        return []
    if not isinstance(entries, list):
        return []
    return [Path(p) for p in entries if isinstance(p, str)]


def remember_slide(path: Path) -> None:
    """Put a slide at the front of the list, dropping the oldest past the cap.

    Resolved first, so the same slide reached via different relative paths or
    symlinks stays one entry instead of crowding the menu with itself.
    """
    resolved = Path(path).resolve()
    entries = [p for p in recent_slides() if p != resolved]
    entries.insert(0, resolved)
    store = _store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    # A plain write, unlike write_manifest's temp-and-replace: only this
    # single-instance app writes the file, and a half-written one merely
    # empties the menu until the next open (see recent_slides above).
    store.write_text(json.dumps([str(p) for p in entries[:_MAX_RECENT]], indent=2))


def forget_all() -> None:
    """Clear Menu. Removing the file is the whole job — an absent store and an
    empty list are already the same thing to recent_slides()."""
    _store_path().unlink(missing_ok=True)
