"""Safety tests: state machine, hotkey gating, focus guard, abort paths.

The focus guard and the hotkey service are replaced by fakes, and the keyboard
backend is the recording one, so no real key event is ever emitted.
"""

from __future__ import annotations

import threading
import time

import pytest

from typing_simulator.behavior.probabilistic import ProbabilisticBehaviorGenerator
from typing_simulator.config import TypingSettings, VariationLevel
from typing_simulator.domain.state import ALLOWED_TRANSITIONS, AppState, StateMachine
from typing_simulator.errors import (
    EmptyInputError,
    FocusTargetError,
    HotkeyError,
    InvalidStateTransitionError,
    MissingPermissionError,
    PlanValidationError,
    UnsupportedCharacterError,
)
from typing_simulator.safety import controller as controller_module
from typing_simulator.safety.caret_guard import CaretSnapshot
from typing_simulator.safety.controller import ControllerCallbacks, SafetyController
from typing_simulator.safety.focus_guard import FrontmostApp
from typing_simulator.safety.permissions import PermissionStatus, PermissionSubject
from typing_simulator.scheduler.clock import FakeClock, RealClock
from typing_simulator.scheduler.scheduler import RunStatus, TypingScheduler
from typing_simulator.simulation.text_buffer import simulate

SELF_APP = FrontmostApp(pid=1000, name="Local Typing Input Simulator", bundle_id="local.sim")
EDITOR = FrontmostApp(pid=2000, name="TextEdit", bundle_id="com.apple.TextEdit")
BROWSER = FrontmostApp(pid=3000, name="Safari", bundle_id="com.apple.Safari")

SHORT_TEXT = "hello there"
LONG_TEXT = "the quick brown fox jumps over the lazy dog " * 6


class FakeFocusGuard:
    """Reports whatever the test tells it to report.

    ``has_focus`` models the awkward macOS case the controller exists to
    handle: the overlay holds keyboard focus while ``frontmost()`` still names
    the user's document application.
    """

    def __init__(self, app: FrontmostApp | None = EDITOR, has_focus: bool = False) -> None:
        self.app = app
        self.has_focus = has_focus
        self.activated: list[FrontmostApp] = []
        #: When True, activate() actually hands focus back, as macOS would.
        self.activation_works = True

    def frontmost(self) -> FrontmostApp | None:
        return self.app

    def is_self(self, app: FrontmostApp | None) -> bool:
        return app is not None and app.pid == SELF_APP.pid

    def self_has_focus(self) -> bool:
        return self.has_focus

    def activate(self, app: FrontmostApp) -> bool:
        self.activated.append(app)
        if self.activation_works:
            self.app = app
            self.has_focus = False
        return True


class FakeCaretGuard:
    """Reports whatever caret the test wants, or none at all."""

    def __init__(self, caret: int | None = 100, verifiable: bool = True) -> None:
        self.caret = caret if verifiable else None
        self.verifiable = verifiable

    def snapshot(self) -> CaretSnapshot:
        if not self.verifiable:
            return CaretSnapshot(reason="no cursor reported")
        return CaretSnapshot(
            pid=EDITOR.pid, role="AXTextArea", title="Untitled", caret=self.caret
        )


class FakeHotkeyService:
    """Records start/stop; can fail to start, to exercise fail-closed behavior."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.started = False
        self.stopped = False

    def start(self) -> None:
        if self.failure is not None:
            raise self.failure
        self.started = True

    def stop(self) -> None:
        self.stopped = True
        self.started = False

    @property
    def is_active(self) -> bool:
        return self.started


def build_controller(
    backend,
    *,
    focus_guard: FakeFocusGuard | None = None,
    hotkeys: FakeHotkeyService | None = None,
    clock=None,
    focus_poll_interval: float = 60.0,
    callbacks: ControllerCallbacks | None = None,
    permission_check=lambda: True,
    focus_match_timeout: float = 1.0,
    caret_guard=None,
) -> SafetyController:
    scheduler = TypingScheduler(
        backend, clock if clock is not None else FakeClock(), progress_interval=0.0
    )
    return SafetyController(
        generator=ProbabilisticBehaviorGenerator(),
        backend=backend,
        focus_guard=focus_guard or FakeFocusGuard(),
        hotkeys=hotkeys or FakeHotkeyService(),
        caret_guard=caret_guard or FakeCaretGuard(),
        scheduler=scheduler,
        callbacks=callbacks,
        focus_poll_interval=focus_poll_interval,
        focus_match_timeout=focus_match_timeout,
        permission_check=permission_check,
    )


def settings_for(wpm: int = 120, seed: int = 5) -> TypingSettings:
    return TypingSettings(
        wpm=wpm, variation=VariationLevel.LOW, typo_rate=0.0, seed=seed
    )


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_every_state_has_a_documented_transition_set() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(AppState)


def test_invalid_state_transitions_are_rejected() -> None:
    machine = StateMachine()
    with pytest.raises(InvalidStateTransitionError):
        machine.transition_to(AppState.RUNNING)
    assert machine.state is AppState.IDLE

    machine.transition_to(AppState.VALIDATING)
    with pytest.raises(InvalidStateTransitionError):
        machine.transition_to(AppState.RUNNING)
    assert machine.state is AppState.VALIDATING


def test_the_documented_happy_path_is_allowed() -> None:
    machine = StateMachine()
    for target in (
        AppState.VALIDATING,
        AppState.ARMING,
        AppState.ARMED,
        AppState.RUNNING,
        AppState.PAUSED,
        AppState.RUNNING,
        AppState.COMPLETED,
        AppState.IDLE,
    ):
        machine.transition_to(target)
    assert machine.state is AppState.IDLE


def test_state_listeners_see_every_change() -> None:
    machine = StateMachine()
    seen: list[tuple[AppState, AppState]] = []
    machine.add_listener(lambda old, new: seen.append((old, new)))
    machine.transition_to(AppState.VALIDATING)
    machine.transition_to(AppState.IDLE)
    assert seen == [
        (AppState.IDLE, AppState.VALIDATING),
        (AppState.VALIDATING, AppState.IDLE),
    ]


# ---------------------------------------------------------------------------
# Hotkey gating: fail closed
# ---------------------------------------------------------------------------


def test_typing_cannot_begin_without_global_hotkeys(backend) -> None:
    hotkeys = FakeHotkeyService(failure=HotkeyError())
    controller = build_controller(backend, hotkeys=hotkeys)

    with pytest.raises(HotkeyError):
        controller.prepare(SHORT_TEXT, settings_for())

    assert controller.state is AppState.ERROR
    assert backend.calls == [] or all(c.kind == "release_all" for c in backend.calls)
    with pytest.raises(InvalidStateTransitionError):
        controller.begin_typing()


def test_missing_permission_during_hotkey_start_is_reported(backend) -> None:
    hotkeys = FakeHotkeyService(failure=MissingPermissionError())
    errors: list[Exception] = []
    controller = build_controller(
        backend, hotkeys=hotkeys, callbacks=ControllerCallbacks(on_error=errors.append)
    )
    with pytest.raises(MissingPermissionError):
        controller.prepare(SHORT_TEXT, settings_for())
    assert controller.state is AppState.ERROR
    assert "System Settings" in errors[0].user_message


def test_typing_is_refused_without_accessibility_permission(backend) -> None:
    """Without permission macOS discards events silently - fail loudly instead."""
    hotkeys = FakeHotkeyService()
    controller = build_controller(backend, hotkeys=hotkeys, permission_check=lambda: False)

    with pytest.raises(MissingPermissionError) as excinfo:
        controller.prepare(SHORT_TEXT, settings_for())

    assert "silently discarded" in excinfo.value.user_message
    assert "System Settings" in excinfo.value.user_message
    assert controller.state is AppState.ERROR
    assert backend.key_events == []
    # The permission gate runs before the hotkey listener is even started.
    assert not hotkeys.started


def test_start_immediately_is_refused_without_permission(backend) -> None:
    controller = build_controller(backend, permission_check=lambda: False)
    with pytest.raises(MissingPermissionError):
        controller.start_immediately(SHORT_TEXT, settings_for())
    assert controller.state is AppState.ERROR
    assert backend.key_events == []


def test_unknown_permission_state_does_not_block(backend) -> None:
    """Off macOS the probe returns None; that is not a refusal."""
    controller = build_controller(backend, permission_check=lambda: None)
    controller.prepare(SHORT_TEXT, settings_for())
    assert controller.state is AppState.ARMING


def _record_permission_requests(monkeypatch, status: PermissionStatus) -> list[str]:
    """Replace every request call so the suite never prompts the real system."""
    calls: list[str] = []
    monkeypatch.setattr(controller_module, "permission_status", lambda: status)
    for name, label, answer in (
        ("request_accessibility_permission", "accessibility", status.accessibility),
        ("request_post_event_permission", "post-events", status.post_events),
        (
            "request_input_monitoring_permission",
            "input-monitoring",
            status.input_monitoring,
        ),
    ):
        monkeypatch.setattr(
            controller_module,
            name,
            (lambda label=label, answer=answer: calls.append(label) or answer),
        )
    return calls


def _status(
    *,
    accessibility: bool | None = True,
    post_events: bool | None = True,
    input_monitoring: bool | None = True,
) -> PermissionStatus:
    return PermissionStatus(
        accessibility=accessibility,
        post_events=post_events,
        input_monitoring=input_monitoring,
        subject=PermissionSubject("Typing Simulator", None, True),
    )


DENIED = _status(accessibility=False, post_events=False, input_monitoring=False)


def test_permission_request_asks_for_accessibility_first(backend, monkeypatch) -> None:
    """Input Monitoring is irrelevant until key events can be delivered."""
    calls = _record_permission_requests(monkeypatch, DENIED)

    controller = build_controller(backend)
    assert controller.request_permission() is False
    assert calls[0] == "accessibility"
    assert "input-monitoring" not in calls


def test_permission_request_asks_about_post_events_when_that_is_what_is_denied(
    backend, monkeypatch
) -> None:
    """The stale-grant dead end: already trusted, so nothing else prompts.

    ``AXIsProcessTrustedWithOptions`` answers True without showing anything
    when Accessibility is already granted, so unless Post Events is requested
    by name this state can never be repaired from inside the application.
    """
    calls = _record_permission_requests(
        monkeypatch, _status(accessibility=True, post_events=False)
    )

    controller = build_controller(backend)
    controller.request_permission()
    assert "post-events" in calls


def test_permission_request_leaves_post_events_alone_when_it_is_granted(
    backend, monkeypatch
) -> None:
    calls = _record_permission_requests(
        monkeypatch, _status(accessibility=False, post_events=True)
    )

    controller = build_controller(backend)
    controller.request_permission()
    assert calls == ["accessibility"]


def test_permission_request_asks_for_input_monitoring_when_it_blocks_hotkeys(
    backend, monkeypatch
) -> None:
    """The banner button must request the permission named in the banner."""
    calls = _record_permission_requests(monkeypatch, _status(input_monitoring=False))

    controller = build_controller(backend)
    assert controller.request_permission() is False
    assert calls == ["input-monitoring"]


def test_input_monitoring_is_not_requested_when_the_hotkeys_do_not_need_it(
    backend, monkeypatch
) -> None:
    """macOS runs the hotkeys on NSEvent monitors, which Accessibility gates.

    Prompting for Input Monitoring anyway puts a dialog for a permission this
    build never uses in front of the user, and leaves a red row behind when
    they decline it.
    """
    calls = _record_permission_requests(monkeypatch, _status(input_monitoring=False))

    hotkeys = FakeHotkeyService()
    hotkeys.requires_input_monitoring = False
    controller = build_controller(backend, hotkeys=hotkeys)
    controller.request_permission()
    assert "input-monitoring" not in calls


def _record_restarts(monkeypatch, status: PermissionStatus | None = None) -> list[str]:
    """Record what a restart did, without touching TCC or launching anything."""
    calls: list[str] = []
    if status is not None:
        monkeypatch.setattr(controller_module, "permission_status", lambda: status)
    monkeypatch.setattr(
        controller_module, "reset_permissions", lambda: calls.append("reset") or True
    )
    monkeypatch.setattr(
        controller_module,
        "relaunch",
        lambda reason=controller_module.RESTART_TO_APPLY: (
            calls.append(f"relaunch:{reason}") or True
        ),
    )
    return calls


def test_restarting_to_apply_a_grant_never_clears_it(backend, monkeypatch) -> None:
    """The destructive and non-destructive restarts must not be confused.

    This one runs when the user has just granted the permission and the
    process is still quoting its start-up answer.  Clearing the entry here
    would delete exactly what they granted.
    """
    calls = _record_restarts(monkeypatch)

    controller = build_controller(backend)
    assert controller.restart_to_apply_permission() is True
    assert calls == [f"relaunch:{controller_module.RESTART_TO_APPLY}"]


def test_restarting_for_permission_clears_the_entries_first(backend, monkeypatch) -> None:
    """Order matters and neither half works alone.

    Restarting without clearing leaves the same stale entry in place; clearing
    without restarting changes nothing the running process can see, because
    macOS settles a process's answer once and keeps it.
    """
    order = _record_restarts(monkeypatch, DENIED)

    controller = build_controller(backend)
    assert controller.restart_for_permission() is True
    assert order == ["reset", f"relaunch:{controller_module.RESTART_AFTER_RESET}"]


def test_the_reset_restart_says_it_was_a_reset(backend, monkeypatch) -> None:
    """The next process has to know a restart-to-apply is still owed.

    Marking every restart the same way is what made the automatic one stop
    happening: the reset spent the single allowance, and the grant the user
    made a moment later then sat there unread.
    """
    order = _record_restarts(monkeypatch, DENIED)

    controller = build_controller(backend)
    controller.restart_for_permission()
    assert order[-1] == f"relaunch:{controller_module.RESTART_AFTER_RESET}"


@pytest.mark.parametrize(
    "granted",
    [
        {"accessibility": True},
        {"post_events": True},
        {"input_monitoring": True},
    ],
)
def test_a_live_grant_is_never_cleared_to_ask_for_it_again(
    backend, monkeypatch, granted
) -> None:
    """Clearing exists to get a prompt back, not to destroy a working grant."""
    fields = {
        "accessibility": False,
        "post_events": False,
        "input_monitoring": False,
        **granted,
    }
    calls = _record_restarts(monkeypatch, _status(**fields))

    controller = build_controller(backend)
    assert controller.restart_for_permission() is True
    assert "reset" not in calls
    assert calls == [f"relaunch:{controller_module.RESTART_TO_APPLY}"]


def test_restarting_reports_failure_when_nothing_can_be_reopened(
    backend, monkeypatch
) -> None:
    """The overlay must not close itself when nothing is coming back."""
    monkeypatch.setattr(controller_module, "permission_status", lambda: DENIED)
    monkeypatch.setattr(controller_module, "reset_permissions", lambda: False)
    monkeypatch.setattr(controller_module, "relaunch", lambda reason=None: False)

    controller = build_controller(backend)
    assert controller.restart_for_permission() is False


def test_hotkeys_are_started_exactly_once(backend) -> None:
    hotkeys = FakeHotkeyService()
    controller = build_controller(backend, hotkeys=hotkeys)
    controller.prepare(SHORT_TEXT, settings_for())
    assert hotkeys.is_active
    controller.cancel_arming()
    controller.reset()
    controller.prepare(SHORT_TEXT, settings_for())
    assert hotkeys.is_active


# ---------------------------------------------------------------------------
# Validation gating
# ---------------------------------------------------------------------------


def test_validation_failure_returns_to_idle(backend) -> None:
    controller = build_controller(backend)
    with pytest.raises(EmptyInputError):
        controller.prepare("   ", settings_for())
    assert controller.state is AppState.IDLE

    with pytest.raises(UnsupportedCharacterError):
        controller.prepare("emoji \U0001f600 here", settings_for())
    assert controller.state is AppState.IDLE


def test_a_plan_that_fails_the_shadow_buffer_never_arms(backend) -> None:
    class BrokenGenerator:
        def generate(self, text, settings):
            from typing_simulator.domain.events import build_plan

            return build_plan(
                target_text=text,
                events=[],  # produces "" instead of the requested text
                seed=1,
                deliberate_errors=0,
                correction_events=0,
            )

    controller = SafetyController(
        generator=BrokenGenerator(),
        backend=backend,
        focus_guard=FakeFocusGuard(),
        hotkeys=FakeHotkeyService(),
        scheduler=TypingScheduler(backend, FakeClock()),
    )
    with pytest.raises(PlanValidationError):
        controller.prepare(SHORT_TEXT, settings_for())
    assert controller.state is AppState.ERROR
    assert backend.key_events == []


# ---------------------------------------------------------------------------
# Target capture
# ---------------------------------------------------------------------------


def test_the_app_refuses_to_target_itself(backend) -> None:
    """With only the overlay ever in front, there is nowhere to type."""
    guard = FakeFocusGuard(app=SELF_APP)
    controller = build_controller(backend, focus_guard=guard)
    controller.current_frontmost()  # polling never records the overlay
    controller.prepare(SHORT_TEXT, settings_for())

    with pytest.raises(FocusTargetError) as excinfo:
        controller.begin_typing()

    assert "no application to type into" in excinfo.value.user_message
    assert controller.state is AppState.ERROR
    assert backend.key_events == []


def test_focus_stolen_by_the_overlay_is_handed_back_before_typing(backend) -> None:
    """Clicking Start can pull focus onto the overlay; it must be given back."""
    guard = FakeFocusGuard()
    controller = build_controller(backend, focus_guard=guard)

    # The overlay polls the frontmost app while idle; that is what it later
    # restores focus to.
    assert controller.current_frontmost() == EDITOR
    assert controller.last_external_target == EDITOR

    # Now the click steals focus: we hold it, but NSWorkspace still says TextEdit.
    guard.has_focus = True

    finished = threading.Event()
    controller._callbacks.on_finished = lambda r: finished.set()
    target = controller.start_immediately(SHORT_TEXT, settings_for())

    assert guard.activated == [EDITOR], "the previous application was not reactivated"
    assert target.pid == EDITOR.pid
    controller.wait_for_start(timeout=3.0)
    assert finished.wait(5.0)
    assert simulate(backend.key_events) == SHORT_TEXT


def test_typing_is_refused_when_focus_never_settles(backend) -> None:
    """If focus never lands on the named target, nothing is typed at all."""
    guard = FakeFocusGuard()
    guard.activation_works = False
    errors: list[Exception] = []
    controller = build_controller(
        backend,
        focus_guard=guard,
        callbacks=ControllerCallbacks(on_error=errors.append),
        focus_match_timeout=0.3,
    )
    controller.current_frontmost()
    guard.has_focus = True  # we keep the keyboard, whatever we do

    controller.start_immediately(SHORT_TEXT, settings_for())
    controller.wait_for_start(timeout=3.0)

    assert controller.state is AppState.ERROR
    assert backend.key_events == []
    assert "never settled" in errors[-1].user_message


def test_typing_is_refused_when_no_target_was_ever_seen(backend) -> None:
    guard = FakeFocusGuard(app=SELF_APP, has_focus=True)
    controller = build_controller(backend, focus_guard=guard)

    with pytest.raises(FocusTargetError) as excinfo:
        controller.start_immediately(SHORT_TEXT, settings_for())

    assert "no application to type into" in excinfo.value.user_message
    assert controller.state is AppState.ERROR
    assert backend.key_events == []


def test_the_overlay_is_never_recorded_as_the_last_external_target(backend) -> None:
    guard = FakeFocusGuard(app=SELF_APP)
    controller = build_controller(backend, focus_guard=guard)
    controller.current_frontmost()
    assert controller.last_external_target is None


def test_self_focus_is_detected_even_when_frontmost_says_otherwise(backend) -> None:
    """The overlay can hold the keyboard while NSWorkspace names another app."""
    guard = FakeFocusGuard(has_focus=True)
    controller = build_controller(backend, focus_guard=guard)
    assert controller.frontmost_is_self(EDITOR)
    guard.has_focus = False
    assert not controller.frontmost_is_self(EDITOR)


def test_focus_loss_to_the_overlay_pauses_the_run(backend) -> None:
    guard = FakeFocusGuard()
    controller = start_long_run(backend, guard)
    try:
        guard.has_focus = True  # user clicked the overlay
        controller.check_focus()
        assert controller.state is AppState.PAUSED

        # Resume must refuse while we still hold the keyboard.
        with pytest.raises(FocusTargetError) as excinfo:
            controller.resume()
        assert "overlay still has keyboard focus" in excinfo.value.user_message

        guard.has_focus = False
        controller.resume()
        assert controller.state is AppState.RUNNING
    finally:
        controller.abort()


def test_an_unknown_frontmost_application_is_refused(backend) -> None:
    controller = build_controller(backend, focus_guard=FakeFocusGuard(app=None))
    controller.prepare(SHORT_TEXT, settings_for())
    with pytest.raises(FocusTargetError):
        controller.begin_typing()
    assert controller.state is AppState.ERROR
    assert backend.key_events == []


def test_the_captured_target_is_reported(backend) -> None:
    captured = []
    controller = build_controller(
        backend, callbacks=ControllerCallbacks(on_target_captured=captured.append)
    )
    controller.prepare(SHORT_TEXT, settings_for())
    target = controller.begin_typing()
    controller.wait_for_start(timeout=3.0)
    controller.scheduler.join(timeout=3.0)

    assert target.name == "TextEdit"
    assert target.pid == EDITOR.pid
    assert captured == [target]
    assert "TextEdit" in target.describe()


def test_a_complete_run_reaches_completed_and_types_the_text(backend) -> None:
    finished = threading.Event()
    controller = build_controller(
        backend, callbacks=ControllerCallbacks(on_finished=lambda r: finished.set())
    )
    controller.prepare(SHORT_TEXT, settings_for())
    controller.begin_typing()

    assert finished.wait(5.0)
    assert controller.state is AppState.COMPLETED
    assert simulate(backend.key_events) == SHORT_TEXT
    assert controller.last_result.status is RunStatus.COMPLETED
    controller.reset()
    assert controller.state is AppState.IDLE


# ---------------------------------------------------------------------------
# Focus loss
# ---------------------------------------------------------------------------


def start_long_run(backend, guard: FakeFocusGuard, **kwargs) -> SafetyController:
    controller = build_controller(backend, focus_guard=guard, clock=RealClock(), **kwargs)
    controller.prepare(LONG_TEXT, settings_for(wpm=20))
    controller.begin_typing()
    assert wait_until(lambda: controller.scheduler.characters_typed >= 1)
    return controller


def test_focus_loss_pauses_execution(backend) -> None:
    guard = FakeFocusGuard()
    warnings: list[str] = []
    controller = start_long_run(
        backend, guard, callbacks=ControllerCallbacks(on_warning=warnings.append)
    )
    try:
        guard.app = BROWSER
        controller.check_focus()

        assert controller.state is AppState.PAUSED
        assert wait_until(lambda: controller.scheduler.is_paused)
        assert warnings and "Safari" in warnings[0]
        assert backend.pressed_keys == ()
    finally:
        controller.abort()


def test_the_focus_monitor_pauses_automatically(backend) -> None:
    guard = FakeFocusGuard()
    controller = start_long_run(backend, guard, focus_poll_interval=0.02)
    try:
        guard.app = BROWSER
        assert wait_until(lambda: controller.state is AppState.PAUSED)
    finally:
        controller.abort()


def test_focus_loss_resumes_automatically_when_the_target_returns(backend) -> None:
    """Switching away pauses; coming back resumes on its own."""
    guard = FakeFocusGuard()
    statuses: list[str] = []
    controller = start_long_run(
        backend,
        guard,
        focus_poll_interval=0.02,
        callbacks=ControllerCallbacks(on_status=statuses.append),
    )
    try:
        guard.app = BROWSER
        assert wait_until(lambda: controller.state is AppState.PAUSED)
        assert wait_until(lambda: controller.scheduler.is_paused)

        guard.app = EDITOR  # the user comes back
        assert wait_until(lambda: controller.state is AppState.RUNNING), (
            "typing did not resume by itself once the target was frontmost again"
        )
        # The state changes before the worker wakes, deliberately: resuming the
        # scheduler first would emit keys while the state still said PAUSED,
        # and the focus monitor ignores every state but RUNNING.  So this has
        # to be waited for rather than asserted outright.
        assert wait_until(lambda: not controller.scheduler.is_paused)
        assert wait_until(lambda: any("Resumed automatically" in m for m in statuses))
    finally:
        controller.abort()


def test_auto_resume_waits_until_the_overlay_releases_the_keyboard(backend) -> None:
    guard = FakeFocusGuard()
    controller = start_long_run(backend, guard, focus_poll_interval=0.02)
    try:
        guard.app = BROWSER
        assert wait_until(lambda: controller.state is AppState.PAUSED)

        # Back on the target, but the overlay still holds the keyboard.
        guard.app = EDITOR
        guard.has_focus = True
        time.sleep(0.15)
        assert controller.state is AppState.PAUSED

        guard.has_focus = False
        assert wait_until(lambda: controller.state is AppState.RUNNING)
    finally:
        controller.abort()


def test_a_manual_pause_does_not_resume_by_itself(backend) -> None:
    """Only a focus-loss pause is automatic; a deliberate one stays put."""
    guard = FakeFocusGuard()
    controller = start_long_run(backend, guard, focus_poll_interval=0.02)
    try:
        controller.pause("Paused from the overlay.")
        assert controller.state is AppState.PAUSED
        time.sleep(0.2)  # the target is frontmost the whole time
        assert controller.state is AppState.PAUSED, "a manual pause resumed itself"

        controller.resume()
        assert controller.state is AppState.RUNNING
    finally:
        controller.abort()


def test_the_pause_hotkey_never_arms_auto_resume(backend) -> None:
    guard = FakeFocusGuard()
    controller = start_long_run(backend, guard, focus_poll_interval=0.02)
    try:
        controller.toggle_pause()
        assert controller.state is AppState.PAUSED
        time.sleep(0.2)
        assert controller.state is AppState.PAUSED
    finally:
        controller.abort()


def test_resume_fails_when_the_original_target_is_not_focused(backend) -> None:
    guard = FakeFocusGuard()
    controller = start_long_run(backend, guard)
    try:
        guard.app = BROWSER
        controller.check_focus()
        assert controller.state is AppState.PAUSED

        with pytest.raises(FocusTargetError) as excinfo:
            controller.resume()
        assert "Safari" in excinfo.value.user_message
        assert controller.state is AppState.PAUSED

        guard.app = EDITOR
        controller.resume()
        assert controller.state is AppState.RUNNING
    finally:
        controller.abort()


def test_resume_fails_when_focus_cannot_be_determined(backend) -> None:
    guard = FakeFocusGuard()
    controller = start_long_run(backend, guard)
    try:
        controller.pause("test pause")
        guard.app = None
        with pytest.raises(FocusTargetError):
            controller.resume()
        assert controller.state is AppState.PAUSED
    finally:
        controller.abort()


def test_the_pause_hotkey_toggles_and_reports_refusals(backend) -> None:
    guard = FakeFocusGuard()
    errors: list[Exception] = []
    controller = start_long_run(
        backend, guard, callbacks=ControllerCallbacks(on_error=errors.append)
    )
    try:
        controller.toggle_pause()
        assert controller.state is AppState.PAUSED

        guard.app = BROWSER
        controller.toggle_pause()  # refused: wrong application in front
        assert controller.state is AppState.PAUSED
        assert isinstance(errors[-1], FocusTargetError)

        guard.app = EDITOR
        controller.toggle_pause()
        assert controller.state is AppState.RUNNING
    finally:
        controller.abort()


# ---------------------------------------------------------------------------
# Abort
# ---------------------------------------------------------------------------


def test_abort_works_from_running(backend) -> None:
    statuses: list[str] = []
    controller = start_long_run(
        backend, FakeFocusGuard(), callbacks=ControllerCallbacks(on_status=statuses.append)
    )
    controller.abort("Aborted with the global hotkey.")

    assert controller.state is AppState.ABORTED
    assert wait_until(lambda: not controller.scheduler.is_running)
    assert backend.pressed_keys == ()
    assert backend.release_all_count >= 1
    assert "characters had been processed" in statuses[-1]
    assert "will not resume automatically" in statuses[-1]


def test_abort_works_from_paused(backend) -> None:
    controller = start_long_run(backend, FakeFocusGuard())
    controller.pause("test pause")
    assert controller.state is AppState.PAUSED
    controller.abort()
    assert controller.state is AppState.ABORTED
    assert wait_until(lambda: not controller.scheduler.is_running)
    assert backend.pressed_keys == ()


def test_start_immediately_types_at_the_current_target(backend) -> None:
    """The overlay path: no countdown, every gate still applied."""
    finished = threading.Event()
    controller = build_controller(
        backend, callbacks=ControllerCallbacks(on_finished=lambda r: finished.set())
    )
    target = controller.start_immediately(SHORT_TEXT, settings_for())

    assert target.pid == EDITOR.pid
    assert finished.wait(5.0)
    assert controller.state is AppState.COMPLETED
    assert simulate(backend.key_events) == SHORT_TEXT


def test_start_immediately_still_refuses_to_target_itself(backend) -> None:
    controller = build_controller(backend, focus_guard=FakeFocusGuard(app=SELF_APP))
    with pytest.raises(FocusTargetError) as excinfo:
        controller.start_immediately(SHORT_TEXT, settings_for())
    assert "Click into the document" in excinfo.value.user_message
    assert controller.state is AppState.ERROR
    assert backend.key_events == []


def test_start_immediately_still_requires_hotkeys(backend) -> None:
    controller = build_controller(backend, hotkeys=FakeHotkeyService(failure=HotkeyError()))
    with pytest.raises(HotkeyError):
        controller.start_immediately(SHORT_TEXT, settings_for())
    assert controller.state is AppState.ERROR
    assert backend.key_events == []


def test_the_frontmost_preview_is_exposed_for_the_overlay(backend) -> None:
    guard = FakeFocusGuard()
    controller = build_controller(backend, focus_guard=guard)
    assert controller.current_frontmost() == EDITOR
    assert not controller.frontmost_is_self(EDITOR)

    guard.app = SELF_APP
    assert controller.frontmost_is_self(controller.current_frontmost())

    guard.app = None
    assert controller.current_frontmost() is None


def test_abort_during_the_countdown_types_nothing(backend) -> None:
    controller = build_controller(backend)
    controller.prepare(SHORT_TEXT, settings_for())
    controller.abort()
    assert controller.state is AppState.ABORTED
    assert backend.key_events == []
    controller.reset()
    assert controller.state is AppState.IDLE


def test_abort_is_a_no_op_from_idle(backend) -> None:
    controller = build_controller(backend)
    controller.abort()
    assert controller.state is AppState.IDLE


def test_shutdown_aborts_and_releases_everything(backend) -> None:
    hotkeys = FakeHotkeyService()
    guard = FakeFocusGuard()
    controller = build_controller(backend, focus_guard=guard, hotkeys=hotkeys, clock=RealClock())
    controller.prepare(LONG_TEXT, settings_for(wpm=20))
    controller.begin_typing()
    assert wait_until(lambda: controller.scheduler.characters_typed >= 1)

    controller.shutdown()

    assert wait_until(lambda: not controller.scheduler.is_running)
    assert backend.pressed_keys == ()
    assert backend.release_all_count >= 1
    assert hotkeys.stopped


def test_shutdown_from_idle_is_safe(backend) -> None:
    controller = build_controller(backend)
    controller.shutdown()
    assert controller.state is AppState.IDLE


# ---------------------------------------------------------------------------
# Invalid operations
# ---------------------------------------------------------------------------


def test_operations_are_rejected_in_the_wrong_state(backend) -> None:
    controller = build_controller(backend)
    with pytest.raises(InvalidStateTransitionError):
        controller.begin_typing()
    with pytest.raises(InvalidStateTransitionError):
        controller.resume()
    with pytest.raises(InvalidStateTransitionError):
        controller.pause()
    with pytest.raises(InvalidStateTransitionError):
        controller.cancel_arming()
    with pytest.raises(InvalidStateTransitionError):
        controller.reset()
    assert controller.state is AppState.IDLE


# ---------------------------------------------------------------------------
# macOS key mapping
#
# These cover the pure mapping used by the Quartz backend. No test ever posts a
# real event; only the resolution table is exercised.
# ---------------------------------------------------------------------------


def test_us_virtual_key_mapping_covers_every_supported_character() -> None:
    from typing_simulator.backends.quartz_backend import virtual_key_for
    from typing_simulator.behavior.keyboard_map import SUPPORTED_CHARACTERS, key_for_character

    for char in sorted(SUPPORTED_CHARACTERS):
        code, needs_shift, text = virtual_key_for(key_for_character(char))
        assert isinstance(code, int) and code >= 0
        if char in (" ", "\n"):
            assert text is None  # special keys insert via their key code
        else:
            assert text == char
            assert needs_shift == (char.isupper() or char in "!@#$%^&*()_+{}|:\"<>?~")


def test_special_keys_map_to_their_mac_virtual_key_codes() -> None:
    from typing_simulator.backends.quartz_backend import virtual_key_for
    from typing_simulator.domain.events import NormalizedKey, SpecialKey

    expected = {
        SpecialKey.ENTER: 36,
        SpecialKey.TAB: 48,
        SpecialKey.SPACE: 49,
        SpecialKey.BACKSPACE: 51,
        SpecialKey.SHIFT: 56,
    }
    for special, code in expected.items():
        assert virtual_key_for(NormalizedKey.of(special)) == (code, False, None)


def test_shifted_characters_share_the_unshifted_key_code() -> None:
    from typing_simulator.backends.quartz_backend import virtual_key_for
    from typing_simulator.behavior.keyboard_map import key_for_character

    for shifted, base in (("!", "1"), ("?", "/"), ("~", "`"), ("A", "a"), (":", ";")):
        shifted_code, shifted_needs, _ = virtual_key_for(key_for_character(shifted))
        base_code, base_needs, _ = virtual_key_for(key_for_character(base))
        assert shifted_code == base_code
        assert shifted_needs and not base_needs


# ---------------------------------------------------------------------------
# Cursor position across a pause
# ---------------------------------------------------------------------------


def test_auto_resume_waits_until_the_cursor_is_back_where_it_was(backend) -> None:
    """The right app is not enough - the caret must be where typing stopped."""
    guard = FakeFocusGuard()
    caret = FakeCaretGuard(caret=100)
    warnings: list[str] = []
    controller = start_long_run(
        backend,
        guard,
        focus_poll_interval=0.02,
        caret_guard=caret,
        callbacks=ControllerCallbacks(on_warning=warnings.append),
    )
    try:
        guard.app = BROWSER
        assert wait_until(lambda: controller.state is AppState.PAUSED)

        # Back in the target application, but the user clicked elsewhere.
        caret.caret = 4_200
        guard.app = EDITOR
        time.sleep(0.2)
        assert controller.state is AppState.PAUSED, "resumed with the cursor moved"
        assert any("cursor moved" in w for w in warnings)

        # Cursor put back where it was.
        caret.caret = 100
        assert wait_until(lambda: controller.state is AppState.RUNNING)
    finally:
        controller.abort()


def test_caret_moved_since_pause_reports_the_three_cases(backend) -> None:
    guard = FakeFocusGuard()
    caret = FakeCaretGuard(caret=10)
    controller = start_long_run(backend, guard, caret_guard=caret)
    try:
        assert controller.caret_moved_since_pause() is None  # not paused yet
        controller.pause("test pause")
        assert controller.caret_moved_since_pause() is False
        caret.caret = 11
        assert controller.caret_moved_since_pause() is True
    finally:
        controller.abort()


def test_an_unreadable_cursor_does_not_block_auto_resume(backend) -> None:
    """Applications that expose no caret must still resume."""
    guard = FakeFocusGuard()
    controller = start_long_run(
        backend,
        guard,
        focus_poll_interval=0.02,
        caret_guard=FakeCaretGuard(verifiable=False),
    )
    try:
        guard.app = BROWSER
        assert wait_until(lambda: controller.state is AppState.PAUSED)
        assert controller.caret_moved_since_pause() is None
        guard.app = EDITOR
        assert wait_until(lambda: controller.state is AppState.RUNNING)
    finally:
        controller.abort()


def test_manual_resume_proceeds_but_warns_when_the_cursor_moved(backend) -> None:
    """A person clicking Resume is making a decision; warn, do not block."""
    guard = FakeFocusGuard()
    caret = FakeCaretGuard(caret=10)
    warnings: list[str] = []
    controller = start_long_run(
        backend,
        guard,
        caret_guard=caret,
        callbacks=ControllerCallbacks(on_warning=warnings.append),
    )
    try:
        controller.pause("test pause")
        caret.caret = 999
        controller.resume()
        assert controller.state is AppState.RUNNING
        assert any("no longer where typing stopped" in w for w in warnings)
    finally:
        controller.abort()


def test_caret_snapshot_comparisons() -> None:
    a = CaretSnapshot(pid=1, role="AXTextArea", title="Doc", caret=42)
    same = CaretSnapshot(pid=1, role="AXTextArea", title="Doc", caret=42)
    moved = CaretSnapshot(pid=1, role="AXTextArea", title="Doc", caret=43)
    other_field = CaretSnapshot(pid=1, role="AXTextField", title="Search", caret=42)
    unknown = CaretSnapshot(reason="nothing focused")

    assert a.verifiable and not unknown.verifiable
    assert a.matches(same)
    assert not a.matches(moved)
    assert not a.matches(other_field)
    assert not a.matches(unknown)
    assert "42" in a.describe()
    assert "nothing focused" in unknown.describe()
