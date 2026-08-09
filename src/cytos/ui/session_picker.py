"""The dialog shown every time a slide is opened: which saved view do you
want to come back to, or start a new one.

A window is always bound to exactly one session (see `cytos.core.session`), so
this is not an optional extra step -- it's how that binding gets chosen. The
snapshot beside each name is the actual rendered frame from when the session
was last saved, which is what makes "the tumour edge one" recognisable without
opening it.

Sessions already open in another window are listed but not selectable. One
window per session is what keeps two windows from writing the same file, and
showing them greyed out explains the rule better than hiding them would.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from cytos.core.session import (
    SessionInfo,
    delete_session,
    list_sessions,
    save_session,
    unique_session_name,
)

_THUMB = QtCore.QSize(128, 96)
# Saved frames come in whatever shape the window was, so the preview is padded
# out to exactly _THUMB rather than shrunk to fit. Dark grey reads as empty
# padding next to a rendered frame, not as part of it.
_THUMB_PADDING = QtGui.QColor(38, 38, 38)


class SessionPicker(QtWidgets.QDialog):
    """Returns the chosen session name via `chosen_name`, or None if cancelled."""

    def __init__(self, slide_root: Path, slide_name: str, in_use: set[str], parent=None):
        super().__init__(parent)
        self.slide_root = Path(slide_root)
        self.in_use = {name.lower() for name in in_use}
        self.chosen_name: str | None = None

        self.setWindowTitle(f"Open session — {slide_name}")
        self.resize(520, 460)
        layout = QtWidgets.QVBoxLayout(self)

        self.list = QtWidgets.QListWidget()
        self.list.setIconSize(_THUMB)
        self.list.setSpacing(2)
        self.list.itemDoubleClicked.connect(self._accept_selected)
        self.list.itemSelectionChanged.connect(self._sync_buttons)
        layout.addWidget(self.list)

        buttons = QtWidgets.QHBoxLayout()
        self.new_button = QtWidgets.QPushButton("New Session")
        self.new_button.clicked.connect(self._new_session)
        self.delete_button = QtWidgets.QPushButton("Delete")
        self.delete_button.clicked.connect(self._delete_selected)
        self.open_button = QtWidgets.QPushButton("Open")
        self.open_button.setDefault(True)
        self.open_button.clicked.connect(self._accept_selected)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)

        buttons.addWidget(self.new_button)
        buttons.addWidget(self.delete_button)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(self.open_button)
        layout.addLayout(buttons)

        self._reload()

    # -- list ---------------------------------------------------------------

    def _reload(self) -> None:
        self.list.clear()
        for info in list_sessions(self.slide_root):
            self.list.addItem(self._make_item(info))
        if self.list.count():
            # Most recent first (list_sessions sorts that way), so the top row
            # is almost always the one wanted -- but skip past any that are
            # already open, since those can't be chosen.
            for i in range(self.list.count()):
                if self.list.item(i).flags() & QtCore.Qt.ItemFlag.ItemIsSelectable:
                    self.list.setCurrentRow(i)
                    break
        self._sync_buttons()

    def _make_item(self, info: SessionInfo) -> QtWidgets.QListWidgetItem:
        busy = info.name.lower() in self.in_use
        item = QtWidgets.QListWidgetItem(f"{info.label}{'   (open in another window)' if busy else ''}")
        item.setData(QtCore.Qt.ItemDataRole.UserRole, info.name)
        item.setSizeHint(QtCore.QSize(0, _THUMB.height() + 12))
        item.setIcon(self._thumb_icon(info.snapshot))
        if busy:
            item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable & ~QtCore.Qt.ItemFlag.ItemIsEnabled)
        return item

    def _thumb_icon(self, snapshot: Path | None) -> QtGui.QIcon:
        """A preview card of exactly `_THUMB`, whatever shape the frame was.

        Handing the file straight to `QIcon` lets Qt scale it, which gives every
        row a differently sized icon -- a wide frame comes back short, a tall
        one narrow -- so the names beside them never line up. Painting the frame
        centred on a fixed grey card keeps one clean column edge down the whole
        list, and a session with no snapshot yet gets the bare card, so it lines
        up too.
        """
        ratio = self.devicePixelRatioF()
        card = QtGui.QPixmap(_THUMB * ratio)
        card.setDevicePixelRatio(ratio)
        card.fill(_THUMB_PADDING)

        frame = QtGui.QPixmap(str(snapshot)) if snapshot is not None else QtGui.QPixmap()
        if not frame.isNull():
            frame = frame.scaled(
                _THUMB * ratio,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
            frame.setDevicePixelRatio(ratio)
            painter = QtGui.QPainter(card)
            painter.drawPixmap(
                QtCore.QPointF(
                    (_THUMB.width() - frame.width() / ratio) / 2,
                    (_THUMB.height() - frame.height() / ratio) / 2,
                ),
                frame,
            )
            painter.end()
        return QtGui.QIcon(card)

    def _select(self, name: str) -> None:
        """Put the cursor on `name` — after creating a session, so Open is one
        keystroke away rather than a hunt through the list."""
        for i in range(self.list.count()):
            if self.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == name:
                self.list.setCurrentRow(i)
                return

    def _selected_name(self) -> str | None:
        item = self.list.currentItem()
        if item is None or not (item.flags() & QtCore.Qt.ItemFlag.ItemIsSelectable):
            return None
        return item.data(QtCore.Qt.ItemDataRole.UserRole)

    def _sync_buttons(self) -> None:
        has = self._selected_name() is not None
        self.open_button.setEnabled(has)
        self.delete_button.setEnabled(has)

    # -- actions ------------------------------------------------------------

    def _new_session(self) -> None:
        suggested = unique_session_name(self.slide_root)
        name, ok = QtWidgets.QInputDialog.getText(
            self, "New session", "Name:", QtWidgets.QLineEdit.EchoMode.Normal, suggested
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        if name.lower() in {s.name.lower() for s in list_sessions(self.slide_root)}:
            QtWidgets.QMessageBox.warning(self, "Name taken", f"A session called {name!r} already exists.")
            return
        # Written to disk now, holding nothing but the slide's own defaults,
        # and the dialog stays open. Setting up three or four named sessions
        # before looking at any of them is a normal way to start, and that only
        # works if creating one is complete in itself rather than a side effect
        # of opening a window.
        save_session(self.slide_root, name, {})
        self._reload()
        self._select(name)

    def _delete_selected(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Delete session",
            f"Delete the session {name!r}? The slide's data and defaults are untouched.",
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        delete_session(self.slide_root, name)
        self._reload()

    def _accept_selected(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        self.chosen_name = name
        self.accept()


def choose_session(slide_root: Path, slide_name: str, in_use: set[str], parent=None) -> str | None:
    """Pick a session for a slide about to be opened, or None to not open it.

    With no saved sessions there is nothing to choose between, so this skips
    the dialog and returns the default name -- a window still ends up bound to
    a session, which is the rule; it just doesn't ask a question with one
    possible answer.
    """
    sessions = list_sessions(slide_root)
    if not sessions:
        return unique_session_name(slide_root, "default")

    dialog = SessionPicker(slide_root, slide_name, in_use, parent)
    if not dialog.exec():
        return None
    return dialog.chosen_name
