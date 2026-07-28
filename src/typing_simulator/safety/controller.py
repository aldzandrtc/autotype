"""The safety controller: state machine plus orchestration.

This is where the rules that keep the prototype safe are enforced:

* a plan is generated and replayed through the shadow buffer before anything
  can be armed;
* the global hotkey listener must be verified active before typing may start
  (fail closed);
* macOS Accessibility permission is a hard precondition, because without it
  key events are discarded silently;
* typing does not begin until focus is *observed* to be on the application the
  overlay named, and the run is refused if it never gets there;
* focus loss pauses immediately and releases held keys, and typing resumes by
  itself once the original target is frontmost again;
* every exit path releases held keys.

The controller knows nothing about Qt.  It reports everything through the
callbacks in :class:`ControllerCallbacks`, which the interface layer turns into
signals.  Callbacks may be invoked from the scheduler or focus-monitor thread.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from typing_simulator import config
from typing_simulator.backends.base import KeyboardBackend
from typing_simulator.behavior.base import BehaviorGenerator
from typing_simulator.config import TypingSettings
from typing_simulator.domain.events import TypingPlan
from typing_simulator.domain.state import AppState, StateMachine
from typing_simulator.errors import (
    FocusTargetError,
    HotkeyError,
    InvalidStateTransitionError,
    TypingSimulatorError,
    ValidationError,
)
from typing_simulator.errors import MissingPermissionError
from typing_simulator.safety.caret_guard import CaretGuard, CaretSnapshot, create_caret_guard
from typing_simulator.safety.focus_guard import FocusGuard, FrontmostApp
from typing_simulator.safety.permissions import (
    PermissionStatus,
    describe_permission_remedy,
    permission_status,
    request_accessibility_permission,
    request_input_monitoring_permission,
    typing_permission_granted,
)
from typing_simulator.safety.hotkeys import HotkeyService
from typing_simulator.scheduler.clock import Clock
from typing_simulator.scheduler.scheduler import Progress, RunResult, RunStatus, TypingScheduler
from typing_simulator.simulation.text_buffer import validate_plan

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TargetApplication:
    """The application captured as the typing destination."""

    pid: int
    name: str
    bundle_id: str | None

    @classmethod
    def from_frontmost(cls, app: FrontmostApp) -> "TargetApplication":
        return cls(pid=app.pid, name=app.name, bundle_id=app.bundle_id)

    def describe(self) -> str:
        if self.bundle_id:
            return f"{self.name} ({self.bundle_id})"
        return self.name


@dataclass(slots=True)
class ControllerCallbacks:
    """Optional observers.  Any of these may be called from a worker thread."""

    on_state_changed: Callable[[AppState, AppState], None] | None = None
    on_progress: Callable[[Progress], None] | None = None
    on_status: Callable[[str], None] | None = None
    on_warning: Callable[[str], None] | None = None
    on_error: Callable[[TypingSimulatorError], None] | None = None
    on_target_captured: Callable[[TargetApplication], None] | None = None
    on_finished: Callable[[RunResult], None] | None = None


class SafetyController:
    """Owns the state machine and drives the scheduler."""

    def __init__(
        self,
        *,
        generator: BehaviorGenerator,
        backend: KeyboardBackend,
        focus_guard: FocusGuard,
        hotkeys: HotkeyService,
        caret_guard: CaretGuard | None = None,
        clock: Clock | None = None,
        scheduler: TypingScheduler | None = None,
        callbacks: ControllerCallbacks | None = None,
        focus_poll_interval: float = config.FOCUS_POLL_INTERVAL_SECONDS,
        focus_match_timeout: float = config.FOCUS_MATCH_TIMEOUT_SECONDS,
        permission_check: Callable[[], bool | None] | None = None,
    ) -> None:
        self._generator = generator
        self._backend = backend
        self._focus_guard = focus_guard
        self._hotkeys = hotkeys
        self._caret_guard = caret_guard or create_caret_guard()
        self._permission_check = permission_check or typing_permission_granted
        self._callbacks = callbacks or ControllerCallbacks()
        self._scheduler = scheduler or TypingScheduler(backend, clock)
        self._focus_poll_interval = focus_poll_interval
        self._focus_match_timeout = focus_match_timeout

        self._machine = StateMachine()
        self._machine.add_listener(self._notify_state)
        self._lock = threading.RLock()

        self._plan: TypingPlan | None = None
        self._target: TargetApplication | None = None
        self._last_external: FrontmostApp | None = None
        self._last_result: RunResult | None = None
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._focus_wait_thread: threading.Thread | None = None
        self._focus_wait_stop = threading.Event()
        self._auto_paused = False
        self._caret_at_pause: CaretSnapshot | None = None
        self._caret_moved_warned = False

    # -- introspection -----------------------------------------------------
    @property
    def state(self) -> AppState:
        return self._machine.state

    @property
    def plan(self) -> TypingPlan | None:
        return self._plan

    @property
    def target(self) -> TargetApplication | None:
        return self._target

    @property
    def last_result(self) -> RunResult | None:
        return self._last_result

    @property
    def scheduler(self) -> TypingScheduler:
        return self._scheduler

    # -- step 1: validate, plan, verify hotkeys ----------------------------
    def prepare(self, text: str, settings: TypingSettings) -> TypingPlan:
        """Validate input, build and verify the plan, then enter ARMING.

        Raises the specific validation error on bad input (state returns to
        IDLE) and refuses to continue if the global hotkeys are not active.
        """
        with self._lock:
            self._machine.transition_to(AppState.VALIDATING)
            self._target = None
            self._last_result = None
            try:
                plan = self._generator.generate(text, settings)
                validate_plan(plan)  # shadow-buffer gate: no plan runs unverified
                self._require_accessibility()
                self._ensure_hotkeys()
            except ValidationError:
                self._machine.transition_to(AppState.IDLE)
                raise
            except TypingSimulatorError as exc:
                self._fail(exc)
                raise
            except Exception as exc:  # noqa: BLE001
                wrapped = TypingSimulatorError(
                    f"Unexpected failure while preparing the plan: {type(exc).__name__}."
                )
                self._fail(wrapped)
                raise wrapped from exc

            self._plan = plan
            self._machine.transition_to(AppState.ARMING)
            self._status("Plan verified.")
            return plan

    def _require_accessibility(self) -> None:
        """Refuse to type unless macOS trusts this process.

        This gate exists because the failure is otherwise **silent**: without
        permission, ``CGEventPost`` returns no error and the keyboard listener
        still reports itself as running, so the application would happily
        "type" a whole document into nowhere.

        ``None`` means the answer could not be determined, which off macOS is
        the normal answer and is not treated as a refusal.  On macOS the probes
        answer ``False`` rather than ``None`` when they fail, so an unreadable
        permission does refuse - an unknown answer is not evidence that typing
        would work.
        """
        if self._permission_check() is not False:
            return
        raise MissingPermissionError(
            "macOS has not granted permission to send key events, so they "
            "would be silently discarded and nothing would be typed. "
            + describe_permission_remedy(self.permission_status())
        )

    def _ensure_hotkeys(self) -> None:
        if self._hotkeys.is_active:
            return
        self._hotkeys.start()  # raises HotkeyError / MissingPermissionError
        if not self._hotkeys.is_active:
            raise HotkeyError()

    # -- step 2: wait for focus to land on the target, then type -----------
    def begin_typing(self) -> TargetApplication:
        """Watch where focus actually goes, and type once it matches.

        There is no countdown.  The overlay has already told the user which
        application the text will go to - the last one that was genuinely in
        front - so the only correct moment to start emitting is when focus is
        observed to actually be there.

        This returns as soon as the watch begins; it does not block the caller.
        The run starts from the watcher thread once focus matches, or fails
        with a clear explanation if it never does.
        """
        with self._lock:
            self._machine.require(AppState.ARMING)
            plan = self._plan
            if plan is None:  # pragma: no cover - prepare() always sets it
                raise TypingSimulatorError("No plan is prepared.")

            expected = self._expected_target()
            self._target = TargetApplication.from_frontmost(expected)

            current = self._focus_guard.frontmost()
            holding = self._focus_guard.self_has_focus()
            logger.info(
                "Start requested. Expected target=%s (pid %d); frontmost now=%s "
                "(pid %s); overlay holds key window=%s",
                expected.name,
                expected.pid,
                current.name if current else "unknown",
                current.pid if current else "?",
                holding,
            )

            if holding or current is None or current.pid != expected.pid:
                # Hand focus back from the main thread; AppKit activation is
                # not something to call from the watcher.
                logger.info("Focus is not on the target; reactivating %s", expected.name)
                self._focus_guard.activate(expected)

            self._status(f"Waiting for focus to settle on {expected.name}…")
            self._focus_wait_stop.clear()
            thread = threading.Thread(
                target=self._await_focus_then_type,
                args=(expected, plan),
                name="focus-wait",
                daemon=True,
            )
            self._focus_wait_thread = thread
            thread.start()
            return self._target

    def _expected_target(self) -> FrontmostApp:
        """The application the overlay told the user it would type into."""
        expected = self._last_external
        if expected is None:
            expected = self._focus_guard.frontmost()
            if expected is not None and self._focus_guard.is_self(expected):
                expected = None
        if expected is None:
            error = FocusTargetError(
                "There is no application to type into yet. Click into the "
                "document where you want the text, wait for the overlay to "
                "name it, then press Start. Nothing was typed."
            )
            self._fail(error)
            raise error
        return expected

    def _await_focus_then_type(self, expected: FrontmostApp, plan: TypingPlan) -> None:
        """Poll until focus matches ``expected``, then start emitting.

        Runs on its own thread so the interface stays responsive.  Every change
        in what is observed is logged, which is what makes a failure here
        diagnosable rather than mysterious.
        """
        deadline = time.monotonic() + self._focus_match_timeout
        last_seen: tuple[int | None, bool] | None = None
        matched = False

        while time.monotonic() < deadline:
            if self._focus_wait_stop.is_set() or self.state is not AppState.ARMING:
                logger.info("Focus watch cancelled before typing began")
                return

            current = self._focus_guard.frontmost()
            holding = self._focus_guard.self_has_focus()
            observation = (current.pid if current else None, holding)
            if observation != last_seen:
                logger.info(
                    "Focus watch: frontmost=%s (pid %s), overlay holds key window=%s",
                    current.name if current else "unknown",
                    current.pid if current else "?",
                    holding,
                )
                last_seen = observation

            if current is not None and current.pid == expected.pid and not holding:
                matched = True
                break
            time.sleep(config.FOCUS_MATCH_POLL_SECONDS)

        if not matched:
            current = self._focus_guard.frontmost()
            logger.warning(
                "Focus never settled on %s within %.1fs (frontmost=%s, holding=%s)",
                expected.name,
                self._focus_match_timeout,
                current.name if current else "unknown",
                self._focus_guard.self_has_focus(),
            )
            self._fail(
                FocusTargetError(
                    f"Focus never settled on {expected.name}, so nothing was "
                    f"typed. It is currently on "
                    f"{current.name if current else 'an unknown application'}. "
                    "Click into the document where you want the text and press "
                    "Start again."
                )
            )
            return

        logger.info("Focus matched %s (pid %d); typing now", expected.name, expected.pid)
        self._start_run(TargetApplication.from_frontmost(expected), plan)

    def _start_run(self, target: TargetApplication, plan: TypingPlan) -> None:
        """Transition into RUNNING and hand the plan to the scheduler."""
        with self._lock:
            if self.state is not AppState.ARMING:  # aborted while we waited
                return
            self._machine.transition_to(AppState.ARMED)
            self._target = target
            self._notify(self._callbacks.on_target_captured, target)
            self._machine.transition_to(AppState.RUNNING)
            self._status(f"Typing into {target.name}.")

            self._scheduler.start(
                plan,
                on_progress=self._on_progress,
                on_paused=None,
                on_resumed=None,
                on_finished=self._on_finished,
            )
            self._start_focus_monitor()

    def start_immediately(self, text: str, settings: TypingSettings) -> TargetApplication:
        """Validate, plan, then wait for focus and type - the overlay's path.

        Every gate still applies: settings, shadow buffer, Accessibility
        permission, hotkeys, and the focus match itself.
        """
        self.prepare(text, settings)
        return self.begin_typing()

    def wait_for_start(self, timeout: float | None = None) -> None:
        """Block until the focus watch has finished (used by tests)."""
        thread = self._focus_wait_thread
        if thread is not None:
            thread.join(timeout)

    # -- focus queries used by the interface -------------------------------
    def check_permission(self) -> bool | None:
        """Re-probe whether key events would actually be delivered.

        ``True`` granted, ``False`` denied, ``None`` undeterminable.  The
        overlay calls this to decide whether Start may be offered at all, so
        both the gate and the interface read the same source.
        """
        return self._permission_check()

    def permission_status(self) -> PermissionStatus:
        """Every permission this application depends on, read fresh.

        Reads the real system state rather than the injected check: this is for
        explaining a problem to the user, and a test double has nothing useful
        to say about which switch in System Settings is off.
        """
        return permission_status()

    def request_permission(self) -> bool | None:
        """Ask macOS to prompt for whichever permission currently blocks a run.

        Distinct from :meth:`check_permission`, and the more important of the
        two.  Merely *reading* the permission never registers this process with
        macOS, so a user who added the application by hand can end up with an
        entry that no longer matches the running binary - switched on, and
        doing nothing.  Prompting re-registers the current identity, which is
        what makes enabling the switch take effect.

        Accessibility is requested first when key events cannot be delivered.
        Once that gate is open, Input Monitoring is requested if the global
        stop hotkeys would otherwise be unavailable.
        """
        status = self.permission_status()
        if status.can_type is not False and status.input_monitoring is False:
            granted = request_input_monitoring_permission()
            logger.info("Requested Input Monitoring permission; granted=%s", granted)
            return granted

        granted = request_accessibility_permission()
        logger.info("Requested Accessibility permission; granted=%s", granted)
        return granted

    def current_frontmost(self) -> FrontmostApp | None:
        """The frontmost application right now, for the overlay's live preview.

        Polling this also records where the text will be sent.  The check is on
        pid alone, deliberately: mixing in :meth:`self_has_focus` would mean
        that any period where we appear to hold the keyboard stops the target
        being recorded at all - and then there is nothing to type into, however
        hard the user tries.
        """
        app = self._focus_guard.frontmost()
        if app is not None and not self._focus_guard.is_self(app):
            self._last_external = app
        return app

    def frontmost_is_self(self, app: FrontmostApp | None) -> bool:
        """True when the overlay - not the user's document - holds the keyboard."""
        return self._focus_guard.is_self(app) or self._focus_guard.self_has_focus()

    @property
    def last_external_target(self) -> FrontmostApp | None:
        """The application the overlay says it will type into."""
        return self._last_external

    def cancel_arming(self) -> None:
        """Abort while waiting for focus, before any key is emitted."""
        with self._lock:
            self._machine.require(AppState.ARMING)
            self._focus_wait_stop.set()
            self._machine.transition_to(AppState.ABORTED)
            self._status("Cancelled before typing started. Nothing was typed.")

    # -- step 3: pause / resume / abort ------------------------------------
    def pause(self, reason: str | None = None, *, automatic: bool = False) -> None:
        """Pause the run.

        ``automatic`` marks a pause caused by focus moving away, which is the
        only kind that resumes by itself once focus comes back.  A pause the
        user asked for stays paused until the user says otherwise.
        """
        with self._lock:
            if self.state is AppState.PAUSED:
                return
            self._machine.require(AppState.RUNNING)
            self._scheduler.pause()
            self._machine.transition_to(AppState.PAUSED)
            self._auto_paused = automatic
            self._caret_at_pause = self._caret_guard.snapshot()
            logger.info("Paused with the cursor at %s", self._caret_at_pause.describe())
            if reason:
                # The reason goes to the warning channel only; duplicating it in
                # the status line just prints the same sentence twice.
                self._status("Paused.")
                self._notify(self._callbacks.on_warning, reason)
            else:
                self._status("Paused.")

    def resume(self) -> None:
        """Resume, but only once the original target is frontmost again."""
        with self._lock:
            self._machine.require(AppState.PAUSED)
            target = self._target
            current = self._focus_guard.frontmost()
            if target is None:  # pragma: no cover - RUNNING implies a target
                raise FocusTargetError("No target application was captured.")
            if current is None:
                raise FocusTargetError(
                    "The frontmost application could not be determined. Switch "
                    f"back to {target.describe()} and try again."
                )
            if self._focus_guard.self_has_focus():
                raise FocusTargetError(
                    "The overlay still has keyboard focus. Click back into "
                    f"{target.describe()}, place the cursor, and resume again."
                )
            if current.pid != target.pid:
                raise FocusTargetError(
                    f"{current.name} is frontmost, not the captured target "
                    f"{target.describe()}. Switch back to it, place the cursor, "
                    "and resume again."
                )
            moved = self.caret_moved_since_pause()
            self._machine.transition_to(AppState.RUNNING)
            self._auto_paused = False
            self._caret_moved_warned = False
            self._scheduler.resume()
            if moved is True:
                before = self._caret_at_pause
                self._status(f"Resumed typing into {target.name}.")
                self._notify(
                    self._callbacks.on_warning,
                    "Resumed, but the cursor is no longer where typing stopped "
                    f"({before.describe() if before else 'its previous position'}). "
                    "The rest of the text is going in at the current cursor.",
                )
            else:
                self._status(f"Resumed typing into {target.name}.")

    def toggle_pause(self) -> None:
        """Hotkey handler: pause when running, resume when paused."""
        state = self.state
        try:
            if state is AppState.RUNNING:
                self.pause("Paused with the global hotkey.", automatic=False)
            elif state is AppState.PAUSED:
                self.resume()
        except TypingSimulatorError as exc:
            self._notify(self._callbacks.on_error, exc)

    def abort(self, reason: str | None = None) -> None:
        """Stop scheduling, interrupt the delay, release keys, go to ABORTED."""
        with self._lock:
            state = self.state
            if state is AppState.ARMING:
                self.cancel_arming()
                return
            if state not in (AppState.ARMED, AppState.RUNNING, AppState.PAUSED):
                return
            self._auto_paused = False
            self._focus_wait_stop.set()
            self._scheduler.abort()
            self._stop_focus_monitor()
            self._machine.transition_to(AppState.ABORTED)
        # Outside the lock: the worker may still be finishing its last event.
        self._scheduler.join(timeout=1.0)
        self._backend.release_all()
        typed = self._scheduler.net_characters
        self._status(
            (reason or "Aborted.")
            + f" {typed:,} characters had been processed. Typing will not resume "
            "automatically."
        )

    def reset(self) -> None:
        with self._lock:
            self._machine.transition_to(AppState.IDLE)
            self._plan = None
            self._target = None
            self._auto_paused = False
            self._caret_at_pause = None
            self._caret_moved_warned = False
            self._status("Ready.")

    def shutdown(self) -> None:
        """Cleanup for application exit; safe to call from any state."""
        try:
            if self.state.is_active:
                self._focus_wait_stop.set()
                self._scheduler.abort()
                self._stop_focus_monitor()
                self._scheduler.join(timeout=1.0)
        finally:
            self._backend.release_all()
            self._hotkeys.stop()

    # -- focus monitoring --------------------------------------------------
    def _start_focus_monitor(self) -> None:
        self._monitor_stop.clear()
        thread = threading.Thread(
            target=self._focus_monitor_loop, name="focus-monitor", daemon=True
        )
        self._monitor_thread = thread
        thread.start()

    def _stop_focus_monitor(self) -> None:
        self._monitor_stop.set()

    def _focus_monitor_loop(self) -> None:
        while not self._monitor_stop.wait(self._focus_poll_interval):
            state = self.state
            if state not in (AppState.RUNNING, AppState.PAUSED):
                return
            try:
                if state is AppState.RUNNING:
                    self.check_focus()
                else:
                    self.try_auto_resume()
            except Exception:  # noqa: BLE001 - the monitor must never die
                logger.exception("Focus check failed")

    def try_auto_resume(self) -> None:
        """Resume by itself once focus is back on the target.

        Only a pause that this application caused (focus moved away) resumes
        automatically.  A pause the user asked for is left alone - coming back
        to the document is not a request to start typing again.
        """
        if self.state is not AppState.PAUSED or not self._auto_paused:
            return
        target = self._target
        if target is None:  # pragma: no cover - PAUSED implies a target
            return
        if self._focus_guard.self_has_focus():
            return
        current = self._focus_guard.frontmost()
        if current is None or current.pid != target.pid:
            return

        moved = self.caret_moved_since_pause()
        if moved is True:
            # The right application, but the cursor is somewhere else in it.
            # Resuming here would splice the rest of the text into the wrong
            # place, so wait for the user to put it back.
            self._warn_caret_moved_once()
            return

        with self._lock:
            # Re-check under the lock: the state may have moved on.
            if self.state is not AppState.PAUSED or not self._auto_paused:
                return
            self._machine.transition_to(AppState.RUNNING)
            self._auto_paused = False
            self._caret_moved_warned = False
            self._scheduler.resume()
        logger.info(
            "Focus returned to %s and the cursor is unchanged; resuming automatically",
            target.name,
        )
        if moved is None:
            self._status(
                f"Focus returned to {target.name}. Resumed automatically - this "
                "application does not report the cursor position, so check the result."
            )
        else:
            self._status(f"Focus returned to {target.name}. Resumed automatically.")

    def caret_moved_since_pause(self) -> bool | None:
        """Has the insertion point moved since the run was paused?

        ``True`` moved, ``False`` unchanged, ``None`` cannot be determined -
        which is the normal answer for applications that expose no caret.
        """
        before = self._caret_at_pause
        if before is None or not before.verifiable:
            return None
        now = self._caret_guard.snapshot()
        if not now.verifiable:
            return None
        return not before.matches(now)

    def _warn_caret_moved_once(self) -> None:
        if self._caret_moved_warned:
            return
        self._caret_moved_warned = True
        before = self._caret_at_pause
        where = before.describe() if before is not None else "where it was"
        logger.info("Cursor moved while paused; not resuming automatically")
        self._notify(
            self._callbacks.on_warning,
            "The cursor moved while this was paused, so typing has not resumed. "
            f"Click back to {where} and it will continue by itself, or press "
            "Resume to carry on from wherever the cursor is now.",
        )

    def check_focus(self) -> None:
        """Pause if the frontmost application is no longer the captured target."""
        if self.state is not AppState.RUNNING:
            return
        target = self._target
        if target is None:
            return
        if self._focus_guard.self_has_focus():
            who = "the overlay"
        else:
            current = self._focus_guard.frontmost()
            if current is not None and current.pid == target.pid:
                return
            who = current.name if current is not None else "an unknown application"
        try:
            self.pause(
                f"Focus moved to {who}; typing paused. It resumes by itself as "
                f"soon as {target.name} is frontmost again.",
                automatic=True,
            )
        except InvalidStateTransitionError:
            # The run finished or was aborted between the check and the pause.
            logger.debug("Focus pause raced with a state change", exc_info=True)

    # -- scheduler callbacks (worker thread) -------------------------------
    def _on_progress(self, progress: Progress) -> None:
        self._notify(self._callbacks.on_progress, progress)

    def _on_finished(self, result: RunResult) -> None:
        self._last_result = result
        self._stop_focus_monitor()
        with self._lock:
            state = self.state
            if result.status is RunStatus.COMPLETED and state in (
                AppState.RUNNING,
                AppState.PAUSED,
            ):
                self._machine.transition_to(AppState.COMPLETED)
                self._status(
                    f"Finished: {result.net_characters:,} characters were typed. "
                    "The application cannot confirm what the target application "
                    "actually received."
                )
            elif result.status is RunStatus.FAILED:
                error = result.error
                wrapped = (
                    error
                    if isinstance(error, TypingSimulatorError)
                    else TypingSimulatorError(
                        "The keyboard backend or scheduler failed while typing "
                        f"({type(error).__name__}). Typing stopped."
                    )
                )
                if state.is_active:
                    self._fail(wrapped)
            elif result.status is RunStatus.ABORTED and state.is_active:
                # abort() normally transitions first; this covers the race.
                self._machine.transition_to(AppState.ABORTED)
        self._notify(self._callbacks.on_finished, result)

    # -- helpers -----------------------------------------------------------
    def _fail(self, error: TypingSimulatorError) -> None:
        logger.error("Entering ERROR state: %s", error.user_message)
        try:
            self._machine.transition_to(AppState.ERROR)
        except InvalidStateTransitionError:  # pragma: no cover - defensive
            logger.debug("Could not enter ERROR state", exc_info=True)
        self._backend.release_all()
        self._notify(self._callbacks.on_error, error)

    def _status(self, message: str) -> None:
        self._notify(self._callbacks.on_status, message)

    def _notify_state(self, old: AppState, new: AppState) -> None:
        logger.info("State %s -> %s", old.name, new.name)
        self._notify(self._callbacks.on_state_changed, old, new)

    @staticmethod
    def _notify(callback: Callable | None, *args: object) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 - an observer must never break a run
            logger.exception("A controller callback failed")
