"""Long jobs (reading an OBJ, writing an SLPK) in a thread, with progress and Cancel."""

from __future__ import annotations

import traceback
from typing import Callable

from .qt import QtCore, Signal

#: job(progress, log) -> result; progress(fraction, text) raises InterruptedError after Cancel.
Job = Callable[[Callable[[float, str], None], Callable[[str], None]], object]


class TaskThread(QtCore.QThread):
    progressed = Signal(float, str)
    logged = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str, str)      # message, traceback
    cancelled = Signal()

    def __init__(self, title: str, job: Job, parent=None):
        super().__init__(parent)
        self.title = title
        self._job = job
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def is_cancel_requested(self) -> bool:
        return self._cancel

    def _progress(self, fraction: float, text: str) -> None:
        if self._cancel:
            raise InterruptedError("Cancelled by the user.")
        self.progressed.emit(float(fraction), str(text))

    def _log(self, text: str) -> None:
        self.logged.emit(str(text))

    def run(self) -> None:  # worker thread: never touch widgets here
        try:
            result = self._job(self._progress, self._log)
        except InterruptedError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc) or exc.__class__.__name__, traceback.format_exc())
        else:
            self.succeeded.emit(result)
