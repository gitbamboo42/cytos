"""The loading half of a slide window: what fills it while `cytos-import` is
still building the slide it was opened for.

A slide that doesn't exist yet still opens a window. It shows the import
running, and when that finishes the same window becomes the viewer -- "a view
that had to load first", rather than a progress dialog that vanishes and is
replaced by something else. Closing the window is how you cancel.

The import runs as a subprocess rather than in a thread. `cytos-import` already
exists, already narrates every layer it writes, and is the documented way to
build a slide; running it means no second code path to drift out of step, no
CPU-heavy work sharing a process with a live render loop, and a cancel that is
just killing a pid. It is invoked through `sys.executable` rather than the
console-script name, so it does not depend on how the user's PATH is set up.

Imports nothing from `cytos.ui.main_window` on purpose: that module builds the
menus that start an import, so the dependency has to run one way only.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

# Enough scrollback to see what happened, bounded so a chatty import can't grow
# without limit.
_MAX_LOG_LINES = 5000


class ImportPanel(QtWidgets.QWidget):
    """Runs one `cytos-import` and shows its output.

    Emits `finished(True)` when the slide is built and ready to open, and
    `finished(False)` on failure or cancellation -- in which case the panel
    stays on screen with the log still readable, because a window that
    disappears on failure leaves you with no idea what went wrong.
    """

    finished = QtCore.Signal(bool)

    def __init__(self, source: Path, out: Path, parent=None):
        super().__init__(parent)
        self.source = Path(source)
        self.out = Path(out)
        self._cancelled = False
        self._done = False
        # The importer prints one "error: ..." line for the ordinary mistakes.
        # Kept so a failure can say what went wrong without making anyone open
        # the log to find out.
        self._last_error = ""

        # Sized to its contents and nothing more, so the window that holds it
        # can be too (see `adjust_window`). For most of an import there is one
        # thing worth knowing -- that it is still going, and roughly where -- and
        # the log is for when that isn't enough.
        self.setMinimumWidth(360)
        card_layout = QtWidgets.QVBoxLayout(self)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(8)

        self.status = QtWidgets.QLabel(f"Building {self.out.stem}")
        title_font = self.status.font()
        title_font.setPointSize(title_font.pointSize() + 5)
        self.status.setFont(title_font)
        card_layout.addWidget(self.status)

        self.step = QtWidgets.QLabel(f"from {self.source.name}")
        self.step.setStyleSheet("color: #8a8a8a;")
        self.step.setWordWrap(True)
        card_layout.addWidget(self.step)

        # Indeterminate: the importer reports which layer it is on, never how
        # far through it is, and a bar that invents a percentage is a lie.
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        card_layout.addWidget(self.bar)

        buttons = QtWidgets.QHBoxLayout()
        self.details_button = QtWidgets.QToolButton()
        self.details_button.setText("Details")
        self.details_button.setCheckable(True)
        self.details_button.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self.details_button.toggled.connect(self._toggle_details)
        buttons.addWidget(self.details_button)
        buttons.addStretch()
        self.action_button = QtWidgets.QPushButton("Cancel")
        self.action_button.clicked.connect(self._on_action)
        buttons.addWidget(self.action_button)
        card_layout.addLayout(buttons)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(_MAX_LOG_LINES)
        self.log.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self.log.setFixedHeight(200)
        self.log.hide()
        card_layout.addWidget(self.log)

        self.process = QtCore.QProcess(self)
        # One stream: the ordering between what it printed and what went wrong
        # is the most useful thing in the log when an import fails.
        self.process.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_error)

    def start(self) -> None:
        # -u because Python block-buffers stdout when it isn't a terminal, and
        # a log that arrives in one lump at the end is not progress.
        args = ["-u", "-m", "cytos.prep.slide", str(self.source), "--out", str(self.out)]
        self._append(f"$ {Path(sys.executable).name} {' '.join(args)}\n")
        self.process.start(sys.executable, args)
        # Closing the window is the cancel gesture, and the window is not ours
        # to subclass; watching it for a close is how that reaches the process.
        window = self.window()
        if window is not None:
            window.installEventFilter(self)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.cancel)

    def cancel(self) -> None:
        """Stop the import. Safe to call when it already finished."""
        if self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self._cancelled = True
            self.process.kill()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt's own naming
        if watched is self.window() and event.type() == QtCore.QEvent.Type.Close:
            self.cancel()
        return False

    # -- process output -----------------------------------------------------

    def adjust_window(self) -> None:
        """Shrink the window onto this panel. The window is a viewer window that
        happens to be loading, so its default size is a viewer's -- far bigger
        than a few lines of status, and mostly empty until the slide arrives."""
        window = self.window()
        if window is None:
            return
        # Activate first: showing or hiding the log queues a layout change, and
        # asking for the size hint before it is applied returns the old size --
        # which is why closing the log used to leave the window tall.
        if self.layout() is not None:
            self.layout().invalidate()
            self.layout().activate()
        window.adjustSize()

    def _toggle_details(self, shown: bool) -> None:
        self.log.setVisible(shown)
        self.details_button.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if shown else QtCore.Qt.ArrowType.RightArrow
        )
        # Opening the log makes the panel taller and closing it makes it shorter;
        # without this the window keeps the taller size and leaves a gap.
        self.adjust_window()

    def _on_action(self) -> None:
        # One button, two jobs: it stops the import while one is running, and
        # closes the window afterwards -- which is the only thing left to do
        # once a failed import has been read.
        if self._done:
            self.window().close()
        else:
            self.cancel()

    def _append(self, text: str) -> None:
        self.log.appendPlainText(text.rstrip("\n"))
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _read_output(self) -> None:
        data = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if not data:
            return
        self._append(data)
        # The importer narrates itself a line at a time; the last thing it said
        # is the best available answer to "what is it doing now".
        for line in reversed(data.splitlines()):
            if line.strip():
                self.step.setText(line.strip())
                break
        for line in data.splitlines():
            if line.startswith("error: "):
                self._last_error = line[len("error: ") :]

    def _on_error(self, error) -> None:
        # Covers the import never starting at all -- a wrong interpreter, say --
        # which produces no output and would otherwise look like a hang.
        if error == QtCore.QProcess.ProcessError.FailedToStart:
            self._settle(False, f"Could not start the importer: {self.process.errorString()}")

    def _on_finished(self, exit_code: int, exit_status) -> None:
        if self._cancelled:
            self._settle(False, "Cancelled. The partly-written folder is not a slide and won't open.")
        elif exit_status != QtCore.QProcess.ExitStatus.NormalExit:
            self._settle(False, self._last_error or "The importer stopped unexpectedly.")
        elif exit_code != 0:
            self._settle(False, self._last_error or f"The importer exited with code {exit_code}.")
        else:
            self._settle(True, f"Imported {self.out.name}.")

    def _settle(self, ok: bool, message: str) -> None:
        if self._done:
            return
        self._done = True
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self.bar.setVisible(not ok)
        if ok:
            self.status.setText(f"Built {self.out.stem}")
            self.step.setText("Opening…")
        else:
            self.status.setText("Import cancelled" if self._cancelled else "Import failed")
            self.step.setText(message)
            self.action_button.setText("Close")
            # Failures are the case the log exists for, so stop making them ask.
            self.details_button.setChecked(True)
        self.finished.emit(ok)
