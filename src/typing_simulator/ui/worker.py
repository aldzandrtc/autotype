"""Bridge between the worker threads and the Qt interface thread.

The scheduler and the focus monitor call plain Python callbacks from their own
threads.  Those callbacks must never touch a widget, so each one only emits a
Qt signal.  Because the emitting thread differs from the receiving one, Qt
queues the delivery automatically and the slots run on the interface thread.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from typing_simulator.domain.state import AppState
from typing_simulator.errors import TypingSimulatorError
from typing_simulator.safety.controller import ControllerCallbacks, TargetApplication
from typing_simulator.scheduler.scheduler import Progress, RunResult


class ControllerBridge(QObject):
    """Turns :class:`ControllerCallbacks` into thread-safe Qt signals."""

    stateChanged = Signal(object, object)  # AppState, AppState
    progressChanged = Signal(object)  # Progress
    statusChanged = Signal(str)
    warningRaised = Signal(str)
    errorRaised = Signal(str)
    targetCaptured = Signal(object)  # TargetApplication
    runFinished = Signal(object)  # RunResult

    def callbacks(self) -> ControllerCallbacks:
        return ControllerCallbacks(
            on_state_changed=self._on_state_changed,
            on_progress=self._on_progress,
            on_status=self.statusChanged.emit,
            on_warning=self.warningRaised.emit,
            on_error=self._on_error,
            on_target_captured=self._on_target,
            on_finished=self._on_finished,
        )

    # -- callback adapters (may run on a worker thread) --------------------
    def _on_state_changed(self, old: AppState, new: AppState) -> None:
        self.stateChanged.emit(old, new)

    def _on_progress(self, progress: Progress) -> None:
        self.progressChanged.emit(progress)

    def _on_error(self, error: TypingSimulatorError) -> None:
        self.errorRaised.emit(error.user_message)

    def _on_target(self, target: TargetApplication) -> None:
        self.targetCaptured.emit(target)

    def _on_finished(self, result: RunResult) -> None:
        self.runFinished.emit(result)
