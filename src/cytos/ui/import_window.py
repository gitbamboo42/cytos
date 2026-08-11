"""Running one of cytos's own prep commands from inside the app, and showing
what it is doing.

Two of them exist. `ImportPanel` builds a whole slide: a slide that doesn't
exist yet still opens a window, that window shows the import running, and when
it finishes the same window becomes the viewer -- "a view that had to load
first", rather than a progress dialog that vanishes and is replaced by
something else. `AddSegmentsPanel` adds one segmentation layer to a slide that
is already open, and shows in a dialog over it.

Both run as a subprocess rather than a thread, which is the reason they share
a base class here. The commands already exist, already narrate every layer they
write, and are the documented way to do this work; running them means no second
code path to drift out of step, no CPU-heavy work sharing a process with a live
render loop, and a cancel that is just killing a pid. They are invoked through
`sys.executable` rather than the console-script names, so nothing depends on
how the user's PATH is set up.

Imports nothing from `cytos.ui.main_window` on purpose: that module builds the
menus that start this work, so the dependency has to run one way only.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

# Enough scrollback to see what happened, bounded so a chatty import can't grow
# without limit.
_MAX_LOG_LINES = 5000


class SubprocessPanel(QtWidgets.QWidget):
    """Runs one `python -m cytos.prep.…` and shows its output.

    Emits `finished(True)` when the work is done, and `finished(False)` on
    failure or cancellation -- in which case the panel stays on screen with the
    log still readable, because something that disappears on failure leaves you
    with no idea what went wrong.

    Subclasses supply the command and the wording; everything below is the same
    either way.
    """

    finished = QtCore.Signal(bool)

    # Wording, overridden per command.
    failure_title = "Failed"
    cancel_title = "Cancelled"
    cancel_message = "Cancelled."
    unexpected_message = "The command stopped unexpectedly."

    def __init__(self, args: list[str], title: str, subtitle: str, parent=None):
        super().__init__(parent)
        self.args = args
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

        self.status = QtWidgets.QLabel(title)
        title_font = self.status.font()
        title_font.setPointSize(title_font.pointSize() + 5)
        self.status.setFont(title_font)
        card_layout.addWidget(self.status)

        self.step = QtWidgets.QLabel(subtitle)
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
        args = ["-u", *self.args]
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
        # One button, two jobs: it stops the command while one is running,
        # and closes the window afterwards -- which is the only thing left to
        # do once a failure has been read.
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
        # The command narrates itself a line at a time; the last thing it said
        # is the best available answer to "what is it doing now".
        for line in reversed(data.splitlines()):
            if line.strip():
                self.step.setText(line.strip())
                break
        for line in data.splitlines():
            if line.startswith("error: "):
                self._last_error = line[len("error: ") :]

    def _on_error(self, error) -> None:
        # Covers the command never starting at all -- a wrong interpreter, say
        # -- which produces no output and would otherwise look like a hang.
        if error == QtCore.QProcess.ProcessError.FailedToStart:
            self._settle(False, f"Could not start it: {self.process.errorString()}")

    def _on_finished(self, exit_code: int, exit_status) -> None:
        if self._cancelled:
            self._settle(False, self.cancel_message)
        elif exit_status != QtCore.QProcess.ExitStatus.NormalExit:
            self._settle(False, self._last_error or self.unexpected_message)
        elif exit_code != 0:
            self._settle(False, self._last_error or f"It exited with code {exit_code}.")
        else:
            self._settle(True, "")

    def _success_wording(self) -> tuple[str, str]:
        """(status, step) shown when the command succeeds."""
        return "Done", ""

    def _settle(self, ok: bool, message: str) -> None:
        if self._done:
            return
        self._done = True
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self.bar.setVisible(not ok)
        if ok:
            status, step = self._success_wording()
            self.status.setText(status)
            self.step.setText(step)
        else:
            self.status.setText(self.cancel_title if self._cancelled else self.failure_title)
            self.step.setText(message)
            self.action_button.setText("Close")
            # Failures are the case the log exists for, so stop making them ask.
            self.details_button.setChecked(True)
        self.finished.emit(ok)


class ImportPanel(SubprocessPanel):
    """Builds a whole slide with `cytos-import`, into the window that will
    become its viewer."""

    failure_title = "Import failed"
    cancel_title = "Import cancelled"
    cancel_message = "Cancelled. The partly-written folder is not a slide and won't open."
    unexpected_message = "The importer stopped unexpectedly."

    def __init__(self, source: Path, out: Path, parent=None):
        # Locals first: PySide6 objects to their attributes being set before
        # the base class's __init__ has run.
        source, out = Path(source), Path(out)
        super().__init__(
            ["-m", "cytos.prep.slide", str(source), "--out", str(out)],
            f"Building {out.stem}",
            f"from {source.name}",
            parent,
        )
        self.source = source
        self.out = out

    def _success_wording(self) -> tuple[str, str]:
        return f"Built {self.out.stem}", "Opening…"


class AddSegmentsPanel(SubprocessPanel):
    """Adds one segmentation layer to a slide that is already open.

    Unlike an import this writes *into* an existing slide, so the cancel
    wording says what that leaves behind: the manifest is written last and in
    one step (see `cytos.core.slide.write_manifest`), so a killed run cannot
    leave a slide that half-knows about a layer -- only some files under
    `segments/` that nothing refers to.
    """

    failure_title = "Could not add segments"
    cancel_title = "Cancelled"
    cancel_message = "Cancelled. The slide still opens exactly as it did before."
    unexpected_message = "It stopped unexpectedly."

    def __init__(self, slide_root: Path, source: Path, name: str | None = None, parent=None):
        source = Path(source)
        args = ["-m", "cytos.prep.segments", str(slide_root), str(source)]
        if name:
            args += ["--name", name]
        super().__init__(args, f"Adding {name or source.name}", f"to {Path(slide_root).name}", parent)
        self.source = source

    def _success_wording(self) -> tuple[str, str]:
        return f"Added {self.source.name}", ""
