"""The interruptible event scheduler.

Runs a :class:`~typing_simulator.domain.events.TypingPlan` on a worker thread,
outside the Qt interface thread.  Delays are never one uninterruptible sleep:
they are waited on an event that pause and abort set, with a 20 ms tick as a
worst-case bound, so an abort normally takes effect in well under 100 ms.

The scheduler never touches Qt.  It reports progress through plain callbacks;
the interface layer turns those into signals.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from typing_simulator import config
from typing_simulator.backends.base import KeyboardBackend
from typing_simulator.domain.events import Delay, KeyDown, KeyUp, SpecialKey, TypingPlan
from typing_simulator.errors import AbortedError, SchedulerError
from typing_simulator.scheduler.clock import Clock, RealClock

logger = logging.getLogger(__name__)


class RunStatus(Enum):
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Progress:
    """A snapshot of an in-flight run."""

    event_index: int
    total_events: int
    characters_typed: int
    """Text-producing key presses emitted, including any later corrected.
    Monotonically increasing."""
    net_characters: int
    """Characters currently in the simulated destination buffer."""
    elapsed_seconds: float
    remaining_seconds: float

    @property
    def fraction(self) -> float:
        if self.total_events == 0:
            return 1.0
        return min(1.0, self.event_index / self.total_events)


@dataclass(frozen=True, slots=True)
class RunResult:
    status: RunStatus
    events_completed: int
    characters_typed: int
    net_characters: int
    error: Exception | None = None


ProgressCallback = Callable[[Progress], None]
FinishedCallback = Callable[[RunResult], None]
SimpleCallback = Callable[[], None]


class TypingScheduler:
    """Executes a plan's events, one at a time, with interruptible delays."""

    def __init__(
        self,
        backend: KeyboardBackend,
        clock: Clock | None = None,
        *,
        tick_seconds: float = config.SCHEDULER_TICK_SECONDS,
        progress_interval: float = config.PROGRESS_INTERVAL_SECONDS,
    ) -> None:
        self._backend = backend
        self._clock: Clock = clock if clock is not None else RealClock()
        self._tick = tick_seconds
        self._progress_interval = progress_interval

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._paused = threading.Event()

        # Control flags.  ``_wake`` cuts short any in-progress wait.
        self._pause_requested = threading.Event()
        self._abort_requested = threading.Event()
        self._wake = threading.Event()
        self._resume_signal = threading.Event()

        # Run state, only touched by the worker thread.
        self._event_index = 0
        self._characters_typed = 0
        self._net_characters = 0

    # -- introspection -----------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    @property
    def event_index(self) -> int:
        """Number of events completed so far."""
        return self._event_index

    @property
    def characters_typed(self) -> int:
        return self._characters_typed

    @property
    def net_characters(self) -> int:
        return self._net_characters

    # -- control -----------------------------------------------------------
    def start(
        self,
        plan: TypingPlan,
        *,
        on_progress: ProgressCallback | None = None,
        on_paused: SimpleCallback | None = None,
        on_resumed: SimpleCallback | None = None,
        on_finished: FinishedCallback | None = None,
    ) -> threading.Thread:
        """Run ``plan`` on a new worker thread.  Only one job may run at a time."""
        with self._lock:
            if self._running.is_set():
                raise SchedulerError("A typing job is already running.")
            self._reset_flags()
            self._running.set()

            def target() -> None:
                result = self._execute(
                    plan,
                    on_progress=on_progress,
                    on_paused=on_paused,
                    on_resumed=on_resumed,
                )
                if on_finished is not None:
                    on_finished(result)

            thread = threading.Thread(target=target, name="typing-scheduler", daemon=True)
            self._thread = thread
        thread.start()
        return thread

    def run(
        self,
        plan: TypingPlan,
        *,
        on_progress: ProgressCallback | None = None,
        on_paused: SimpleCallback | None = None,
        on_resumed: SimpleCallback | None = None,
    ) -> RunResult:
        """Run ``plan`` on the calling thread (used by tests)."""
        with self._lock:
            if self._running.is_set():
                raise SchedulerError("A typing job is already running.")
            self._reset_flags()
            self._running.set()
        return self._execute(
            plan, on_progress=on_progress, on_paused=on_paused, on_resumed=on_resumed
        )

    def pause(self) -> None:
        """Request a pause; takes effect at the next event or delay tick."""
        self._resume_signal.clear()
        self._pause_requested.set()
        self._wake.set()

    def resume(self) -> None:
        self._pause_requested.clear()
        self._resume_signal.set()
        self._wake.set()

    def abort(self) -> None:
        """Request an immediate abort; interrupts any in-progress delay."""
        self._abort_requested.set()
        self._pause_requested.clear()
        self._resume_signal.set()
        self._wake.set()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # -- internals ---------------------------------------------------------
    def _reset_flags(self) -> None:
        self._pause_requested.clear()
        self._abort_requested.clear()
        self._resume_signal.clear()
        self._wake.clear()
        self._paused.clear()
        self._event_index = 0
        self._characters_typed = 0
        self._net_characters = 0

    def _execute(
        self,
        plan: TypingPlan,
        *,
        on_progress: ProgressCallback | None,
        on_paused: SimpleCallback | None,
        on_resumed: SimpleCallback | None,
    ) -> RunResult:
        remaining = plan.remaining_durations()
        started = self._clock.monotonic()
        last_report = started
        status = RunStatus.COMPLETED
        error: Exception | None = None

        def report() -> None:
            if on_progress is None:
                return
            on_progress(
                Progress(
                    event_index=self._event_index,
                    total_events=plan.total_events,
                    characters_typed=self._characters_typed,
                    net_characters=self._net_characters,
                    elapsed_seconds=self._clock.monotonic() - started,
                    remaining_seconds=remaining[min(self._event_index, len(remaining) - 1)],
                )
            )

        try:
            report()
            for index, event in enumerate(plan.events):
                self._raise_if_aborted()
                if self._pause_requested.is_set():
                    self._enter_pause(on_paused, on_resumed)

                if isinstance(event, Delay):
                    self._interruptible_wait(event.seconds, on_paused, on_resumed)
                elif isinstance(event, KeyDown):
                    self._backend.key_down(event.key)
                    if event.key.special is SpecialKey.BACKSPACE:
                        self._net_characters = max(0, self._net_characters - 1)
                    elif event.key.produces_text:
                        self._characters_typed += 1
                        self._net_characters += 1
                elif isinstance(event, KeyUp):
                    self._backend.key_up(event.key)
                else:  # pragma: no cover - the event union is closed
                    raise SchedulerError(f"Unknown event type: {type(event).__name__}")

                self._event_index = index + 1
                now = self._clock.monotonic()
                if now - last_report >= self._progress_interval:
                    last_report = now
                    report()
        except AbortedError:
            status = RunStatus.ABORTED
        except Exception as exc:  # noqa: BLE001 - reported through RunResult
            logger.exception("Typing run failed")
            status = RunStatus.FAILED
            error = exc
        finally:
            # Every exit path - completion, abort, exception, interpreter
            # shutdown - releases whatever the backend still holds.
            try:
                self._backend.release_all()
            except Exception:  # noqa: BLE001 - release_all must never mask a result
                logger.exception("release_all() failed during cleanup")
            self._paused.clear()
            self._running.clear()

        report()
        return RunResult(
            status=status,
            events_completed=self._event_index,
            characters_typed=self._characters_typed,
            net_characters=self._net_characters,
            error=error,
        )

    def _raise_if_aborted(self) -> None:
        if self._abort_requested.is_set():
            raise AbortedError()

    def _interruptible_wait(
        self,
        seconds: float,
        on_paused: SimpleCallback | None,
        on_resumed: SimpleCallback | None,
    ) -> None:
        """Wait ``seconds``, checking pause and abort throughout.

        Time spent paused does not consume the delay: the remaining wait is
        resumed from where it left off.
        """
        deadline = self._clock.monotonic() + seconds
        while True:
            self._raise_if_aborted()
            if self._pause_requested.is_set():
                paused_at = self._clock.monotonic()
                self._enter_pause(on_paused, on_resumed)
                deadline += self._clock.monotonic() - paused_at
                continue
            now = self._clock.monotonic()
            if now >= deadline:
                return
            self._wake.clear()
            # Re-check after clearing, so a request that arrived in between is
            # not lost; the tick bounds any residual race to 20 ms.
            if self._abort_requested.is_set() or self._pause_requested.is_set():
                continue
            self._clock.sleep(min(deadline - now, self._tick), self._wake)

    def _enter_pause(
        self, on_paused: SimpleCallback | None, on_resumed: SimpleCallback | None
    ) -> None:
        """Release held keys and block until resume or abort.

        Paused time is real wall-clock time, never simulated: an injected clock
        controls the typing timeline, not how long a human takes to resume.
        """
        self._backend.release_all()
        self._paused.set()
        if on_paused is not None:
            on_paused()
        try:
            while self._pause_requested.is_set():
                self._raise_if_aborted()
                self._resume_signal.wait(self._tick)
            self._raise_if_aborted()
        finally:
            self._paused.clear()
        self._resume_signal.clear()
        if on_resumed is not None:
            on_resumed()
