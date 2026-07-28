"""Permission probing, subject identification and the advice they produce.

These tests never read the real TCC database.  Every probe is monkeypatched, so
the suite gives the same answer on a machine where the permissions happen to be
granted as on one where they are not - which matters, because the behaviour
under test is precisely what the application does when they disagree.
"""

from __future__ import annotations

import pytest

from typing_simulator.errors import MissingPermissionError
from typing_simulator.safety import permissions
from typing_simulator.safety.hotkeys import _require_input_monitoring
from typing_simulator.safety.permissions import (
    PermissionStatus,
    PermissionSubject,
    describe_permission_remedy,
)

BUNDLED = PermissionSubject(
    name="Typing Simulator", path="/Applications/Typing Simulator.app", bundled=True
)
FROM_SOURCE = PermissionSubject(name="Terminal", path="/Applications/Utilities/Terminal.app", bundled=False)


def status(
    *,
    accessibility: bool | None = True,
    post_events: bool | None = True,
    input_monitoring: bool | None = True,
    subject: PermissionSubject = BUNDLED,
) -> PermissionStatus:
    return PermissionStatus(
        accessibility=accessibility,
        post_events=post_events,
        input_monitoring=input_monitoring,
        subject=subject,
    )


# ---------------------------------------------------------------------------
# can_type
# ---------------------------------------------------------------------------


def test_either_gate_denied_means_typing_is_denied() -> None:
    """Accessibility and PostEvent fail identically: silently."""
    assert status(accessibility=False).can_type is False
    assert status(post_events=False).can_type is False
    assert status().can_type is True


def test_unknown_everywhere_stays_unknown() -> None:
    """Off macOS nothing can be probed, and that is not a refusal."""
    assert status(accessibility=None, post_events=None).can_type is None


def test_one_known_gate_is_enough_to_answer() -> None:
    assert status(accessibility=None, post_events=True).can_type is True


# ---------------------------------------------------------------------------
# The stale-grant case: the switch is on and nothing works
# ---------------------------------------------------------------------------


def test_disagreeing_services_are_reported_as_a_stale_grant() -> None:
    """One switch grants both services, so a split answer is not user error."""
    assert status(accessibility=True, post_events=False).stale_grant_suspected
    assert status(accessibility=False, post_events=True).stale_grant_suspected


def test_agreeing_services_are_not_a_stale_grant() -> None:
    assert not status(accessibility=False, post_events=False).stale_grant_suspected
    assert not status().stale_grant_suspected
    assert not status(accessibility=None, post_events=False).stale_grant_suspected


def test_stale_grant_advice_does_not_tell_the_user_to_enable_it_again() -> None:
    """They already did; that is what makes this case so confusing."""
    remedy = describe_permission_remedy(status(accessibility=True, post_events=False))
    assert "remove it" in remedy
    assert "Request permission" in remedy


def test_denied_advice_names_the_subject_and_the_stale_entry_escape_hatch() -> None:
    remedy = describe_permission_remedy(status(accessibility=False, post_events=False))
    assert "Typing Simulator" in remedy
    assert "Accessibility" in remedy
    assert "remove it" in remedy


def test_running_from_source_is_called_out() -> None:
    """The permission belongs to the launching app, not to the script."""
    remedy = describe_permission_remedy(
        status(accessibility=False, post_events=False, subject=FROM_SOURCE)
    )
    assert "Terminal" in remedy
    assert "running from source" in remedy


def test_bundled_advice_does_not_mention_running_from_source() -> None:
    remedy = describe_permission_remedy(status(accessibility=False, post_events=False))
    assert "running from source" not in remedy


def test_input_monitoring_advice_is_kept_separate_from_accessibility() -> None:
    remedy = describe_permission_remedy(status(input_monitoring=False))
    assert "Input Monitoring" in remedy
    assert "separate switch" in remedy


def test_every_remedy_names_system_settings() -> None:
    """The interface and the refusal message both rely on this."""
    for candidate in (
        status(accessibility=False, post_events=False),
        status(accessibility=True, post_events=False),
        status(input_monitoring=False),
        status(),
    ):
        assert "System Settings" in describe_permission_remedy(candidate)


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def test_a_failed_probe_on_macos_answers_denied_not_unknown(monkeypatch) -> None:
    """An unreadable permission is not evidence that typing would work."""
    monkeypatch.setattr(permissions.sys, "platform", "darwin")

    def explode(_path):
        raise OSError("framework missing")

    monkeypatch.setattr(permissions.ctypes.cdll, "LoadLibrary", explode)
    assert permissions.accessibility_permission_granted() is False


def test_probes_return_unknown_off_macos(monkeypatch) -> None:
    monkeypatch.setattr(permissions.sys, "platform", "linux")
    assert permissions.accessibility_permission_granted() is None
    assert permissions.post_event_permission_granted() is None
    assert permissions.input_monitoring_permission_granted() is None
    assert permissions.typing_permission_granted() is None


def test_typing_permission_combines_both_gates(monkeypatch) -> None:
    monkeypatch.setattr(permissions, "accessibility_permission_granted", lambda: True)
    monkeypatch.setattr(permissions, "post_event_permission_granted", lambda: False)
    assert permissions.typing_permission_granted() is False


# ---------------------------------------------------------------------------
# Subject identification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/Applications/Foo.app/Contents/MacOS/Foo", "/Applications/Foo.app"),
        ("/Applications/Foo.app", "/Applications/Foo.app"),
        ("/usr/bin/python3", None),
        ("", None),
        (None, None),
    ],
)
def test_enclosing_bundle(path, expected) -> None:
    assert permissions._enclosing_bundle(path) == expected


def test_bundle_name_strips_the_suffix() -> None:
    assert permissions._bundle_name("/Applications/Typing Simulator.app") == "Typing Simulator"
    assert permissions._bundle_name("/usr/bin/python3") == "python3"


def test_subject_is_the_app_bundle_when_frozen(monkeypatch) -> None:
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    monkeypatch.setattr(permissions.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        permissions.sys,
        "executable",
        "/Applications/Typing Simulator.app/Contents/MacOS/TypingSimulator",
    )
    permissions.permission_subject.cache_clear()
    try:
        subject = permissions.permission_subject()
    finally:
        permissions.permission_subject.cache_clear()

    assert subject.bundled is True
    assert subject.name == "Typing Simulator"
    assert subject.path == "/Applications/Typing Simulator.app"


def test_subject_falls_back_to_the_launching_application(monkeypatch) -> None:
    """Run from source, the grant belongs to whatever started the interpreter."""
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    monkeypatch.setattr(permissions.sys, "frozen", False, raising=False)
    monkeypatch.setattr(
        permissions,
        "_launching_application",
        lambda: "/Applications/Utilities/Terminal.app",
    )
    permissions.permission_subject.cache_clear()
    try:
        subject = permissions.permission_subject()
    finally:
        permissions.permission_subject.cache_clear()

    assert subject.bundled is False
    assert subject.name == "Terminal"


def test_parent_map_tolerates_paths_containing_spaces(monkeypatch) -> None:
    class Completed:
        stdout = (
            "  501     1 /Applications/My App.app/Contents/MacOS/My App\n"
            "  502   501 /usr/bin/python3\n"
            "garbage line\n"
        )

    monkeypatch.setattr(permissions.subprocess, "run", lambda *a, **k: Completed())
    table = permissions._parent_map()
    assert table[501] == (1, "/Applications/My App.app/Contents/MacOS/My App")
    assert table[502] == (501, "/usr/bin/python3")


def test_parent_map_survives_ps_failing(monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise OSError("no ps")

    monkeypatch.setattr(permissions.subprocess, "run", explode)
    assert permissions._parent_map() == {}


# ---------------------------------------------------------------------------
# The Input Monitoring gate on the hotkeys
# ---------------------------------------------------------------------------


def test_hotkeys_refuse_without_input_monitoring(monkeypatch) -> None:
    """A global monitor installs fine without it and then never fires."""
    monkeypatch.setattr(
        "typing_simulator.safety.hotkeys.input_monitoring_permission_granted",
        lambda: False,
    )
    with pytest.raises(MissingPermissionError) as excinfo:
        _require_input_monitoring()

    message = excinfo.value.user_message
    assert "Input Monitoring" in message
    # Naming the wrong permission is the whole bug this guards against.
    assert "separate switch from" in message


@pytest.mark.parametrize("granted", [True, None])
def test_hotkeys_proceed_when_input_monitoring_is_granted_or_unknown(
    monkeypatch, granted
) -> None:
    monkeypatch.setattr(
        "typing_simulator.safety.hotkeys.input_monitoring_permission_granted",
        lambda: granted,
    )
    _require_input_monitoring()  # must not raise
