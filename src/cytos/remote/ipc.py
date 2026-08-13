"""The viewer's control socket: one JSON object per line, request in,
response out.

The socket already existed before any of this — it is how a second
`cytos-viewer` launch finds the first one and asks it to come to the front
(see `cytos.ui.main_window._claim_single_instance`). This module grows that
single hard-coded message into a small command protocol, so anything that can
write a line of JSON — `cytos-ctl`, a script, an AI agent — can drive the
running app: open a slide, move the camera, change layer settings, take a
screenshot.

Wire format, both directions, one line each:

    request:   {"cmd": "describe", "window": 2}\n
    response:  {"ok": true, "result": {...}}\n
               {"ok": false, "error": "why it failed"}\n

What the commands *do* lives elsewhere: the server here is handed a
`dispatch(payload) -> result` callable and knows nothing about windows or
slides. That keeps this file free of UI imports, which is what lets the
client half (`request`) be imported by `cytos-ctl` without dragging in
QtWidgets, wgpu, or the rest of the app.

A `QLocalServer` is a unix-domain socket on macOS/Linux and a named pipe on
Windows — local to the machine, owned by the user, no network exposure.
"""

from __future__ import annotations

import json
import traceback

from PySide6 import QtCore, QtNetwork

IPC_NAME = "cytos-viewer"

# Connecting to a socket nobody listens on fails in microseconds; this is
# only for the crossing-a-slow-machine case.
CONNECT_TIMEOUT_MS = 1000
DEFAULT_TIMEOUT_MS = 10_000


class NotRunning(Exception):
    """No viewer is listening on the socket."""


class CommandError(Exception):
    """A request that was understood but can't be done — wrong window id,
    unknown layer, bad value. Reported to the caller as the error string,
    without a server-side traceback: the request is wrong, not the app."""


class RemoteServer(QtCore.QObject):
    """The listening half, owned by the viewer process.

    Runs entirely on Qt's main loop — `QLocalSocket` delivers `readyRead` as
    an ordinary signal, so every command executes on the same thread as the
    widgets it pokes and no locking is needed anywhere.
    """

    def __init__(self, dispatch, parent=None):
        super().__init__(parent)
        self._dispatch = dispatch
        # A crashed instance leaves its socket file behind, and listen() then
        # fails to bind. Only called when a probe just failed to connect, so
        # removing it can't disconnect a live server.
        QtNetwork.QLocalServer.removeServer(IPC_NAME)
        self._server = QtNetwork.QLocalServer(self)
        if not self._server.listen(IPC_NAME):
            # Losing the bind isn't fatal: it only means remote control and
            # the "raise the running app" handshake won't work, not that the
            # window can't run.
            print(f"note: could not listen on {IPC_NAME} ({self._server.errorString()})")
        self._server.newConnection.connect(self._accept)
        self._buffers: dict[QtNetwork.QLocalSocket, bytes] = {}

    def _accept(self) -> None:
        while True:
            sock = self._server.nextPendingConnection()
            if sock is None:
                return
            sock.readyRead.connect(lambda s=sock: self._read(s))
            sock.disconnected.connect(lambda s=sock: self._drop(s))

    def _drop(self, sock) -> None:
        self._buffers.pop(sock, None)
        sock.deleteLater()

    def _read(self, sock) -> None:
        buffer = self._buffers.get(sock, b"") + bytes(sock.readAll())
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line.strip():
                self._answer(sock, line)
        self._buffers[sock] = buffer

    def _answer(self, sock, line: bytes) -> None:
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            reply = {"ok": False, "error": "request is not a JSON object"}
        else:
            try:
                reply = {"ok": True, "result": self._dispatch(payload)}
            except CommandError as err:
                reply = {"ok": False, "error": str(err)}
            except Exception as err:  # noqa: BLE001 - a command must never kill the app
                traceback.print_exc()
                reply = {"ok": False, "error": f"{type(err).__name__}: {err}"}
        sock.write(json.dumps(reply).encode() + b"\n")
        sock.flush()


def request(payload: dict, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict:
    """Send one command to the running viewer and return its reply
    (the `{"ok": ..., ...}` envelope, undecoded further).

    Raises `NotRunning` if nothing is listening, `TimeoutError` if the app is
    up but didn't answer in time — a modal dialog waiting on the user, or a
    command genuinely slower than the timeout.

    Needs a `QCoreApplication` to already exist (the `waitFor*` calls below
    don't need the event loop *running*, just the app object).
    """
    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(IPC_NAME)
    if not sock.waitForConnected(CONNECT_TIMEOUT_MS):
        raise NotRunning(f"no cytos-viewer is listening on '{IPC_NAME}'")

    sock.write(json.dumps(payload).encode() + b"\n")
    sock.waitForBytesWritten(timeout_ms)

    deadline = QtCore.QDeadlineTimer(timeout_ms)
    buffer = b""
    while b"\n" not in buffer:
        if not sock.waitForReadyRead(max(1, deadline.remainingTime())) or deadline.hasExpired():
            raise TimeoutError(f"cytos-viewer did not answer within {timeout_ms} ms")
        buffer += bytes(sock.readAll())
    sock.disconnectFromServer()
    return json.loads(buffer.split(b"\n", 1)[0])
