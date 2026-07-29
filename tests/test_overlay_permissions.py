"""What the overlay does when a permission is granted while it is running.

This is the layer the "I granted it and only Accessibility went green" bug
lived in, and it had no tests: the controller and the probes were both correct,
and the overlay simply declined to act on them.  Every test here drives the
real :class:`OverlayWindow` against a fake controller, off screen, and never
touches TCC or launches anything.
"""

from __future__ import annotations

import pytest

from typing_simulator.domain.state import AppState
from typing_simulator.safety import permissions
from typing_simulator.safety.permissions import PermissionStatus, PermissionSubject

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

from typing_simulator.ui.overlay_window import OverlayWindow  # noqa: E402
from typing_simulator.ui.worker import ControllerBridge  # noqa: E402

SUBJECT = PermissionSubject(
    name="Typing Simulator", path="/Applications/Typing Simulator.app", bundled=True
)


def status(
    *,
    accessibility: bool | None = True,
    post_events: bool | None = True,
    input_monitoring: bool | None = True,
) -> PermissionStatus:
    return PermissionStatus(
        accessibility=accessibility,
        post_events=post_events,
        input_monitoring=input_monitoring,
        subject=SUBJECT,
    )


#: Granted a moment ago: Accessibility answers live, the Post Events preflight
#: still answers from start-up.  Only a new process can see the real answer.
JUST_GRANTED = status(accessibility=True, post_events=False, input_monitoring=False)
NOTHING_GRANTED = status(
    accessibility=False, post_events=False, input_monitoring=False
)


class FakeController:
    """The overlay's whole view of the controller, and nothing more."""

    hotkeys_require_input_monitoring = False

    def __init__(self, permission_status: PermissionStatus) -> None:
        self._status = permission_status
        self.state = AppState.IDLE
        self.restarts: list[str] = []
        self.requests = 0
        self.restart_succeeds = True

    # -- permissions -------------------------------------------------------
    def permission_status(self) -> PermissionStatus:
        return self._status

    def set_status(self, permission_status: PermissionStatus) -> None:
        self._status = permission_status

    def check_permission(self) -> bool | None:
        return self._status.can_type

    def request_permission(self) -> bool | None:
        self.requests += 1
        return self._status.can_type

    @staticmethod
    def can_reset_permissions() -> bool:
        return True

    def restart_to_apply_permission(self) -> bool:
        self.restarts.append("apply")
        return self.restart_succeeds

    def restart_for_permission(self) -> bool:
        self.restarts.append("reset")
        return self.restart_succeeds

    # -- everything else the overlay touches -------------------------------
    def current_frontmost(self):
        return None

    def frontmost_is_self(self, _app) -> bool:
        return False

    def shutdown(self) -> None:
        return None


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def overlay(qt_app, monkeypatch, request):
    """A real overlay over a fake controller, with no restart marker set."""
    monkeypatch.delenv(permissions.RESTARTED_MARKER, raising=False)

    def build(controller: FakeController) -> OverlayWindow:
        window = OverlayWindow(controller, ControllerBridge())
        request.addfinalizer(window.deleteLater)
        return window

    return build


# ---------------------------------------------------------------------------
# Picking up a grant made while the application was already running
# ---------------------------------------------------------------------------


def test_a_grant_made_while_running_restarts_by_itself(overlay) -> None:
    controller = FakeController(NOTHING_GRANTED)
    window = overlay(controller)
    assert controller.restarts == []

    # The user enables the switch in System Settings; the poll notices.
    controller.set_status(JUST_GRANTED)
    window._refresh_permission_banner()

    assert controller.restarts == ["apply"]


def test_a_grant_made_after_a_reset_restart_still_restarts_by_itself(
    overlay, monkeypatch
) -> None:
    """The reported bug, in one test.

    Pressing "Grant permission" clears this application's entries and restarts
    it so macOS will prompt again.  The user then grants the permission macOS
    has just asked for - and the restart that would make it visible has to
    still be available, because the previous one was spent on something else
    entirely.
    """
    monkeypatch.setenv(
        permissions.RESTARTED_MARKER, permissions.RESTART_AFTER_RESET
    )
    controller = FakeController(NOTHING_GRANTED)
    window = overlay(controller)

    controller.set_status(JUST_GRANTED)
    window._refresh_permission_banner()

    assert controller.restarts == ["apply"]


def test_a_second_restart_to_apply_is_never_attempted(overlay, monkeypatch) -> None:
    """If refreshing the start-up answers did not change them, they were real."""
    monkeypatch.setenv(permissions.RESTARTED_MARKER, permissions.RESTART_TO_APPLY)
    controller = FakeController(JUST_GRANTED)
    window = overlay(controller)
    window._refresh_permission_banner()

    assert controller.restarts == []


def test_the_restart_is_attempted_only_once_per_run(overlay) -> None:
    controller = FakeController(JUST_GRANTED)
    window = overlay(controller)
    window._refresh_permission_banner()
    window._refresh_permission_banner()

    assert controller.restarts == ["apply"]


def test_working_permissions_release_the_marker(overlay, monkeypatch) -> None:
    """A restart that succeeded must not forbid the next one for ever."""
    monkeypatch.setenv(permissions.RESTARTED_MARKER, permissions.RESTART_TO_APPLY)
    controller = FakeController(status())
    overlay(controller)

    assert permissions.was_restarted_by_us() is False


# ---------------------------------------------------------------------------
# What the banner says
# ---------------------------------------------------------------------------


def test_the_banner_only_promises_a_restart_that_is_happening(
    overlay, monkeypatch
) -> None:
    monkeypatch.setenv(permissions.RESTARTED_MARKER, permissions.RESTART_TO_APPLY)
    controller = FakeController(JUST_GRANTED)
    window = overlay(controller)
    window._refresh_permission_banner()

    message = window.banner_label.text()
    assert "restarting to pick it up" not in message.lower()
    assert permissions.GRANT_BUTTON_LABEL in message


def test_the_banner_says_it_is_restarting_when_it_is(overlay) -> None:
    controller = FakeController(JUST_GRANTED)
    window = overlay(controller)

    assert "restarting to pick it up" in window.banner_label.text().lower()


def test_a_failed_restart_does_not_leave_the_banner_promising_one(overlay) -> None:
    controller = FakeController(JUST_GRANTED)
    controller.restart_succeeds = False
    window = overlay(controller)

    assert "restarting to pick it up" not in window.banner_label.text().lower()


# ---------------------------------------------------------------------------
# The checklist
# ---------------------------------------------------------------------------


def test_input_monitoring_is_not_shown_as_a_failure_when_it_is_not_needed(
    overlay,
) -> None:
    """A red ✕ next to a permission this build never uses reads as "broken"."""
    controller = FakeController(status(input_monitoring=False))
    window = overlay(controller)

    row = window._permission_marks[2].text()
    assert "Input Monitoring" in row
    assert "not needed" in row
    assert "✕" not in row


def test_input_monitoring_is_shown_as_a_failure_when_the_hotkeys_need_it(
    overlay,
) -> None:
    controller = FakeController(status(input_monitoring=False))
    controller.hotkeys_require_input_monitoring = True
    window = overlay(controller)

    assert "✕" in window._permission_marks[2].text()


# ---------------------------------------------------------------------------
# The button
# ---------------------------------------------------------------------------


def test_the_button_restarts_rather_than_clearing_a_grant_it_can_see(
    overlay, monkeypatch
) -> None:
    """Pressing it here must never reach the destructive path.

    The permission is granted and working; clearing the entry would delete
    exactly what the user came to the button to obtain.
    """
    monkeypatch.setenv(permissions.RESTARTED_MARKER, permissions.RESTART_TO_APPLY)
    controller = FakeController(JUST_GRANTED)
    window = overlay(controller)
    controller.restarts.clear()

    window._on_request_permission()
    assert controller.restarts == ["apply"]


def test_the_button_asks_macos_before_it_resets_anything(qt_app, overlay) -> None:
    controller = FakeController(NOTHING_GRANTED)
    window = overlay(controller)
    # The launch-time ask is deferred a turn, so the overlay is on screen
    # before a system dialog appears in front of it.
    qt_app.processEvents()
    assert controller.requests == 1
    # That spent the one prompt macOS grants per process, so the next press is
    # the one that has to escalate.
    window._on_request_permission()
    assert controller.restarts == ["reset"]


def test_the_first_press_asks_rather_than_escalating(overlay) -> None:
    """Nothing has asked yet, so asking is the whole remedy."""
    controller = FakeController(NOTHING_GRANTED)
    window = overlay(controller)

    window._on_request_permission()
    assert controller.requests == 1
    assert controller.restarts == []


# ---------------------------------------------------------------------------
# Collapsing
# ---------------------------------------------------------------------------


def test_the_collapse_button_collapses(overlay) -> None:
    """`clicked` carries a `checked` argument, and a slot with an optional
    parameter receives it - so wiring it straight through asked the panel to
    expand every time and it could never be collapsed."""
    window = overlay(FakeController(status()))
    # `is_collapsed` asks whether the body is visible, which it cannot be while
    # the window itself is hidden.
    window.show()
    assert not window.is_collapsed

    window.collapse_button.click()
    assert window.is_collapsed

    window.collapse_button.click()
    assert not window.is_collapsed
