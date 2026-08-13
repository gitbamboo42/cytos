"""cytos-ctl — drive a running cytos-viewer from the command line.

The viewer listens on a local socket (see `cytos.remote.ipc`); this client sends it
one JSON command per invocation and prints the JSON reply to stdout. That
makes the whole app scriptable from a shell — by a person, a script, or an
AI agent: open a slide, move the camera, change what a layer shows, write a
screenshot to a file, and read back everything the dock panel knows.

A typical exchange::

    cytos-ctl status
    cytos-ctl open data/breast_rep1.cytos --session default
    cytos-ctl describe                 # layer keys + every legal value
    cytos-ctl camera --center 3800 2600 --width 500
    cytos-ctl set '{"layers": {"segments:cells": {"show_fill": true}}}'
    cytos-ctl snapshot /tmp/view.png
    cytos-ctl quit

Exit codes: 0 success, 1 the viewer refused the command, 2 no viewer is
running (start it with `cytos-viewer`; this tool never starts one itself).

Every world coordinate is in the slide's own units (see `describe`'s
`world_units`), with Y increasing downward — the same convention as the
data files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PySide6 import QtCore

from cytos.remote import ipc

# A slide open can page a whole pyramid level in; a snapshot may load every
# tile the view needs first. Both are seconds, not the default ten.
_SLOW_TIMEOUT_MS = 120_000
_TIMEOUT_MS = 15_000


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cytos-ctl",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def window_arg(p):
        p.add_argument(
            "-w", "--window", type=int, default=None,
            help="which window, when several are open (ids from `windows`)",
        )

    p = sub.add_parser(
        "skill",
        help="print an AI onboarding guide — read this first if cytos is new to you",
    )
    p.add_argument(
        "which", nargs="?", default="user", choices=["user", "developer"],
        help="'user' (default): operating the viewer; 'developer': working on the codebase",
    )
    sub.add_parser("status", help="is the viewer running, and what windows are open")
    sub.add_parser("raise", help="bring the viewer's windows to the front")
    sub.add_parser("windows", help="list open slide windows and their ids")
    sub.add_parser("quit", help="save every session and quit the viewer")

    p = sub.add_parser("open", help="open a slide in a new window, no dialogs")
    p.add_argument("slide", help="path to a .cytos slide directory")
    p.add_argument(
        "--session", default=None,
        help="session name to open under (default: 'default'; created if new; "
        "errors if another window already has it)",
    )

    p = sub.add_parser("sessions", help="list a slide's saved sessions (reads the slide, not the app)")
    p.add_argument("slide", help="path to a .cytos slide directory")

    p = sub.add_parser("describe", help="everything about a window: layers, state, and every legal value for `set`")
    window_arg(p)

    p = sub.add_parser("state", help="current view state, in saved-session form")
    window_arg(p)

    p = sub.add_parser(
        "set",
        help="apply a partial, session-shaped state change",
        description="Apply a partial state change, shaped like a saved session:\n"
        '  {"camera": {...}, "sections": {...}, "layers": {"<key>": {...}}}\n'
        "Layer entries are partial too — only the fields given change.\n"
        "`describe` lists the layer keys and the legal values for every field.\n"
        'Example: \'{"layers": {"image:dapi": {"colormap": "green", "clim": [0, 40]}}}\'',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("changes", help="the change as a JSON object, or '-' to read it from stdin")
    window_arg(p)

    p = sub.add_parser(
        "camera",
        help="move the camera",
        description="Move the camera. World units, Y increasing downward. "
        "--width/--height alone keep the current aspect ratio.",
    )
    p.add_argument("--fit", action="store_true", help="fit the whole slide")
    p.add_argument("--center", nargs=2, type=float, metavar=("X", "Y"))
    p.add_argument("--width", type=float, help="how much world width to show")
    p.add_argument("--height", type=float, help="how much world height to show")
    p.add_argument(
        "--rect", nargs=4, type=float, metavar=("MINX", "MINY", "MAXX", "MAXY"),
        help="show exactly this world rectangle",
    )
    window_arg(p)

    p = sub.add_parser("snapshot", help="render the current view and write it as a PNG")
    p.add_argument("out", help="where to write the PNG")
    window_arg(p)

    p = sub.add_parser("reset", help="reset a window to the slide's defaults")
    window_arg(p)

    p = sub.add_parser("save", help="save the window's session to disk now")
    window_arg(p)

    return parser


def _payload(args: argparse.Namespace) -> tuple[dict, int]:
    """The request for the viewer, plus how long to wait for the answer."""
    cmd = args.command
    if cmd == "status":
        return {"cmd": "ping"}, _TIMEOUT_MS
    if cmd == "open":
        return {
            "cmd": "open",
            "path": str(Path(args.slide).expanduser().resolve()),
            "session": args.session,
        }, _SLOW_TIMEOUT_MS
    if cmd == "set":
        raw = sys.stdin.read() if args.changes == "-" else args.changes
        try:
            changes = json.loads(raw)
        except ValueError as err:
            sys.exit(f"cytos-ctl: changes are not valid JSON: {err}")
        return {"cmd": "set", "window": args.window, "changes": changes}, _TIMEOUT_MS
    if cmd == "camera":
        spec = {}
        if args.fit:
            spec["fit"] = True
        if args.center is not None:
            spec["center"] = args.center
        if args.width is not None:
            spec["width"] = args.width
        if args.height is not None:
            spec["height"] = args.height
        if args.rect is not None:
            spec["rect"] = args.rect
        if not spec:
            sys.exit("cytos-ctl: camera needs at least one of --fit/--center/--width/--height/--rect")
        return {"cmd": "set", "window": args.window, "changes": {"camera": spec}}, _TIMEOUT_MS
    if cmd == "snapshot":
        # The viewer writes the file, so the path must mean the same thing in
        # its working directory as in this one.
        return {
            "cmd": "snapshot",
            "window": args.window,
            "path": str(Path(args.out).expanduser().resolve()),
        }, _SLOW_TIMEOUT_MS
    payload = {"cmd": cmd}
    if getattr(args, "window", None) is not None:
        payload["window"] = args.window
    return payload, _TIMEOUT_MS


def _list_sessions(slide: str) -> None:
    """Answered from the slide directory itself — works whether or not the
    viewer is running, which is exactly when "what can I open?" comes up."""
    from cytos.core.session import list_sessions

    root = Path(slide).expanduser()
    if not root.is_dir():
        sys.exit(f"cytos-ctl: no slide at {root}")
    found = [
        {
            "name": info.name,
            "modified": info.modified.isoformat(timespec="seconds") if info.modified else None,
        }
        for info in list_sessions(root)
    ]
    print(json.dumps(found, indent=2))


def main() -> None:
    args = _build_parser().parse_args()
    if args.command == "skill":
        from cytos.skills import guide_text

        print(guide_text(args.which))
        return
    if args.command == "sessions":
        _list_sessions(args.slide)
        return

    # QLocalSocket wants a Qt application object to exist (no event loop
    # needed); QCoreApplication, so no GUI ever flashes up for a CLI call.
    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication(sys.argv[:1])
    assert app is not None

    payload, timeout_ms = _payload(args)
    try:
        reply = ipc.request(payload, timeout_ms=timeout_ms)
    except ipc.NotRunning:
        print("cytos-ctl: cytos-viewer is not running — start it first", file=sys.stderr)
        sys.exit(2)
    except TimeoutError as err:
        sys.exit(f"cytos-ctl: {err}")

    if not reply.get("ok"):
        sys.exit(f"cytos-ctl: {reply.get('error', 'unknown error')}")
    print(json.dumps(reply.get("result"), indent=2))


if __name__ == "__main__":
    main()
