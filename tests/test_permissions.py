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
from typing_simulator.safety.hotkeys import (
    NSEventHotkeyService,
    PynputHotkeyService,
    _require_accessibility_for_monitors,
    _require_input_monitoring,
)
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


def test_granted_but_not_yet_visible_is_a_restart_not_a_reset() -> None:
    """The two look identical and the wrong remedy deletes a working grant.

    Accessibility answers live, the Post Events preflight answers from
    start-up, so this pair means "granted a moment ago" - and clearing the
    entry would throw away exactly what the user just granted.
    """
    granted = status(accessibility=True, post_events=False)
    assert granted.needs_restart_to_apply

    remedy = describe_permission_remedy(granted)
    assert "Restarting" in remedy
    assert "remove it" not in remedy
    assert "−" not in remedy


def test_the_advice_does_not_promise_a_restart_that_is_not_coming() -> None:
    """The restart happens once; after that the user has to be told what to do.

    Repeating "restarting to pick it up" at someone watching nothing happen is
    how a permission that is fully granted gets diagnosed as a broken app.
    """
    remedy = describe_permission_remedy(
        status(accessibility=True, post_events=False), restart_pending=False
    )
    assert "Restarting picks up" not in remedy
    assert permissions.GRANT_BUTTON_LABEL in remedy
    # Still never the destructive advice: the grant is real and is working.
    assert "remove it" not in remedy


def test_a_stale_entry_still_gets_the_reset_advice() -> None:
    """The other direction is a genuinely stale grant, and does need clearing."""
    remedy = describe_permission_remedy(status(accessibility=False, post_events=True))
    assert "remove it" in remedy
    assert permissions.GRANT_BUTTON_LABEL in remedy


def test_input_monitoring_alone_is_not_a_restart() -> None:
    """It is a separate switch, so it is allowed to differ from the others."""
    assert not status(input_monitoring=False).needs_restart_to_apply


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


def test_subject_names_the_interpreter_by_its_real_path(monkeypatch, tmp_path) -> None:
    """A virtualenv's `bin/python` is a symlink, and TCC records the target.

    Telling the user to add the symlink names an entry the running process can
    never match, which is the whole "I granted it and nothing happened" story.
    """
    real = tmp_path / "python3.13"
    real.write_text("#!/bin/sh\n")
    link = tmp_path / "python"
    link.symlink_to(real)

    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    monkeypatch.setattr(permissions.sys, "frozen", False, raising=False)
    monkeypatch.setattr(permissions.sys, "executable", str(link))
    monkeypatch.setattr(permissions, "_launching_application", lambda: None)
    permissions.permission_subject.cache_clear()
    try:
        subject = permissions.permission_subject()
    finally:
        permissions.permission_subject.cache_clear()

    assert subject.path == str(real)
    assert subject.name == "python3.13"


# ---------------------------------------------------------------------------
# Clearing our own grants
# ---------------------------------------------------------------------------


def test_reset_refuses_without_a_bundle_identifier(monkeypatch) -> None:
    """An unscoped `tccutil reset` revokes the permission for *every* app."""
    ran: list[list[str]] = []
    monkeypatch.setattr(permissions, "own_bundle_identifier", lambda: None)
    monkeypatch.setattr(
        permissions.subprocess, "run", lambda argv, **_k: ran.append(argv)
    )
    assert permissions.reset_permissions() is False
    assert ran == []


def test_reset_scopes_every_service_to_our_own_identifier(monkeypatch) -> None:
    ran: list[list[str]] = []

    class Completed:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(permissions, "own_bundle_identifier", lambda: "local.example")
    monkeypatch.setattr(
        permissions.subprocess,
        "run",
        lambda argv, **_k: ran.append(argv) or Completed(),
    )

    assert permissions.reset_permissions() is True
    assert [argv[2] for argv in ran] == list(permissions.TCC_SERVICES)
    # Never the unscoped form: every invocation must name the application.
    assert all(argv[3] == "local.example" for argv in ran)


def test_reset_survives_tccutil_being_unavailable(monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise OSError("no tccutil")

    monkeypatch.setattr(permissions, "own_bundle_identifier", lambda: "local.example")
    monkeypatch.setattr(permissions.subprocess, "run", explode)
    assert permissions.reset_permissions() is False


def test_relaunch_refuses_when_there_is_no_bundle(monkeypatch) -> None:
    """Run from source there is nothing to reopen; `open` on a script is not it."""
    started: list[list[str]] = []
    monkeypatch.setattr(permissions, "_own_bundle_path", lambda: None)
    monkeypatch.setattr(
        permissions.subprocess, "Popen", lambda argv, **_k: started.append(argv)
    )
    assert permissions.relaunch() is False
    assert started == []


def test_relaunch_reopens_our_own_bundle_after_we_exit(monkeypatch) -> None:
    """The delay is the point: a new process only gets a fresh answer from
    macOS once the old one is gone."""
    started: list[list[str]] = []
    monkeypatch.setattr(
        permissions, "_own_bundle_path", lambda: "/Applications/Typing Simulator.app"
    )
    monkeypatch.setattr(
        permissions.subprocess, "Popen", lambda argv, **_k: started.append(argv)
    )

    assert permissions.relaunch() is True
    command = started[0][-1]
    assert "sleep" in command
    assert "/usr/bin/open -n" in command
    # Quoted, because the bundle name contains a space.
    assert "'/Applications/Typing Simulator.app'" in command


def test_relaunch_tells_the_new_process_why_it_was_restarted(monkeypatch) -> None:
    """Which restart it was decides whether the next one is still allowed."""
    started: list[list[str]] = []
    monkeypatch.setattr(permissions, "_own_bundle_path", lambda: "/Applications/T.app")
    monkeypatch.setattr(
        permissions.subprocess, "Popen", lambda argv, **_k: started.append(argv)
    )

    permissions.relaunch(permissions.RESTART_AFTER_RESET)
    assert (
        f"{permissions.RESTARTED_MARKER}={permissions.RESTART_AFTER_RESET}"
        in started[0][-1]
    )


def test_relaunch_survives_being_unable_to_start_anything(monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise OSError("no sh")

    monkeypatch.setattr(permissions, "_own_bundle_path", lambda: "/Applications/T.app")
    monkeypatch.setattr(permissions.subprocess, "Popen", explode)
    assert permissions.relaunch() is False


def test_reset_is_only_offered_to_a_bundled_build(monkeypatch) -> None:
    monkeypatch.setattr(permissions, "own_bundle_identifier", lambda: None)
    assert permissions.can_reset_permissions() is False
    monkeypatch.setattr(permissions, "own_bundle_identifier", lambda: "local.example")
    assert permissions.can_reset_permissions() is True


def test_bundle_identifier_is_read_from_the_bundle(monkeypatch, tmp_path) -> None:
    import plistlib

    contents = tmp_path / "Typing Simulator.app" / "Contents"
    contents.mkdir(parents=True)
    with open(contents / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleIdentifier": "local.typing-simulator"}, handle)

    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    monkeypatch.setattr(
        permissions, "_own_bundle_path", lambda: str(tmp_path / "Typing Simulator.app")
    )
    assert permissions.own_bundle_identifier() == "local.typing-simulator"


def test_bundle_identifier_is_none_without_a_plist(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(permissions.sys, "platform", "darwin")
    monkeypatch.setattr(permissions, "_own_bundle_path", lambda: str(tmp_path / "Gone.app"))
    assert permissions.own_bundle_identifier() is None


# ---------------------------------------------------------------------------
# The restart marker
#
# One switch grants Accessibility and Post Events, but the process only learns
# about Post Events when it starts - so a grant made while running needs a
# restart to become visible.  That restart must happen at most once, and the
# marker is how a new process finds out whether it already has.
# ---------------------------------------------------------------------------


def test_no_marker_means_this_run_was_not_restarted(monkeypatch) -> None:
    monkeypatch.delenv(permissions.RESTARTED_MARKER, raising=False)
    assert permissions.was_restarted_by_us() is False
    assert permissions.was_restarted_to_apply_permission() is False


def test_a_reset_restart_still_owes_a_restart_to_apply(monkeypatch) -> None:
    """The bug this exists to prevent.

    Pressing "Grant permission" clears the entries and restarts, and macOS then
    asks the user to grant the permission.  Treating that restart as the one
    allowance meant the grant they made a second later was never picked up, and
    the banner sat on "Accessibility ✓, Post events ✕" until the button was
    pressed a second time.
    """
    monkeypatch.setenv(permissions.RESTARTED_MARKER, permissions.RESTART_AFTER_RESET)
    assert permissions.was_restarted_by_us() is True
    assert permissions.was_restarted_to_apply_permission() is False


def test_an_apply_restart_is_never_repeated(monkeypatch) -> None:
    """If refreshing the answers did not change them, they were never stale."""
    monkeypatch.setenv(permissions.RESTARTED_MARKER, permissions.RESTART_TO_APPLY)
    assert permissions.was_restarted_to_apply_permission() is True


def test_an_unrecognised_marker_is_read_as_an_apply_restart(monkeypatch) -> None:
    """What an older build wrote.  Suppressing one restart beats looping."""
    monkeypatch.setenv(permissions.RESTARTED_MARKER, "1")
    assert permissions.was_restarted_to_apply_permission() is True


def test_the_marker_is_cleared_once_the_restart_has_worked(monkeypatch) -> None:
    """Otherwise a later grant in the same run could never be picked up."""
    monkeypatch.setenv(permissions.RESTARTED_MARKER, permissions.RESTART_TO_APPLY)
    permissions.clear_restart_marker()
    assert permissions.was_restarted_by_us() is False
    permissions.clear_restart_marker()  # idempotent


# ---------------------------------------------------------------------------
# The checklist the banner shows
# ---------------------------------------------------------------------------


def test_checklist_reports_each_permission_separately() -> None:
    rows = status(accessibility=True, post_events=False, input_monitoring=None).checklist()
    assert rows == [
        ("Accessibility", True),
        ("Post events", False),
        ("Input Monitoring", None),
    ]


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


# ---------------------------------------------------------------------------
# Which permission actually gates the hotkeys
# ---------------------------------------------------------------------------


def test_nsevent_monitors_are_gated_by_accessibility_not_input_monitoring() -> None:
    """Apple gates key-event global monitors on accessibility trust.

    ``kTCCServiceListenEvent`` gates ``CGEventTap``, which this implementation
    does not use - so preflighting it refused to type over a permission the
    hotkeys never needed.
    """
    assert NSEventHotkeyService.requires_input_monitoring is False
    assert PynputHotkeyService.requires_input_monitoring is True


def test_the_nsevent_gate_refuses_without_accessibility(monkeypatch) -> None:
    monkeypatch.setattr(
        "typing_simulator.safety.hotkeys.accessibility_permission_granted",
        lambda: False,
    )
    with pytest.raises(MissingPermissionError) as excinfo:
        _require_accessibility_for_monitors()
    assert "Accessibility" in excinfo.value.user_message


def test_the_nsevent_gate_ignores_input_monitoring(monkeypatch) -> None:
    """The permission that has no bearing on it must not be able to block it."""
    monkeypatch.setattr(
        "typing_simulator.safety.hotkeys.accessibility_permission_granted",
        lambda: True,
    )
    monkeypatch.setattr(
        "typing_simulator.safety.hotkeys.input_monitoring_permission_granted",
        lambda: False,
    )
    _require_accessibility_for_monitors()  # must not raise
