"""macOS privacy permissions: probing them, and asking for them.

Three separate permissions matter here, and conflating them is the single
biggest source of "but I *did* grant it" confusion:

``Accessibility`` (``kTCCServiceAccessibility``)
    Read through ``AXIsProcessTrusted()``.  Gates the Accessibility API, which
    :mod:`typing_simulator.safety.caret_guard` uses to read the caret.

``Post events`` (``kTCCServicePostEvent``)
    Read through ``CGPreflightPostEventAccess()``.  This is the one that
    actually decides whether ``CGEventPost`` reaches another application.  It
    is granted by the same Accessibility toggle, but it is a *different* TCC
    service and the two can disagree - most often when a grant has gone stale.

``Input Monitoring`` (``kTCCServiceListenEvent``)
    Read through ``CGPreflightListenEventAccess()``.  Gates *observing* other
    applications' keystrokes, which is what a ``CGEventTap`` needs.  It lives
    under a separate switch in System Settings and is **not** implied by
    Accessibility.

Why the application must ask, not just look
-------------------------------------------
Reading ``AXIsProcessTrusted()`` never registers this process with TCC.  A user
who has to add the app by hand with the "+" button ends up with an entry whose
recorded code requirement is pinned to whatever the binary looked like at that
moment.  For a locally built, ad-hoc signed bundle that requirement includes
the ``cdhash``, so the next rebuild produces a binary the entry no longer
matches: System Settings still shows the row with the switch on, while
``AXIsProcessTrusted()`` keeps answering ``False``.

:func:`request_accessibility_permission` calls
``AXIsProcessTrustedWithOptions`` with the prompt option, which makes macOS
register the identity of the process that is *running right now*.  That is what
turns an enabled-but-ineffective switch back into a working grant.

Everything here is read-only with respect to the permission system: the
application asks macOS to prompt, and never tries to weaken, forge or bypass a
grant.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import plistlib
import shlex
import subprocess
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_ACCESSIBILITY_FRAMEWORK = (
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)

#: ``ps`` is only consulted once per call and never with user-supplied data.
_PS_TIMEOUT_SECONDS = 2.0

#: ``tccutil`` is only ever run with this application's own bundle identifier.
_TCCUTIL_TIMEOUT_SECONDS = 5.0

#: The TCC services this application depends on, under the names ``tccutil``
#: knows them by - which are not the ``kTCCService*`` constants.
TCC_SERVICES = ("Accessibility", "PostEvent", "ListenEvent")


@dataclass(frozen=True, slots=True)
class PermissionSubject:
    """What macOS actually attributes a permission grant to.

    TCC does not grant permissions to "the Python script".  It grants them to
    the *responsible process*: the app bundle when this is a bundle, and
    otherwise whichever application launched the interpreter.  Naming the wrong
    one is why toggling a switch so often changes nothing.
    """

    name: str
    """Human-readable name to look for in System Settings."""

    path: str | None
    """Exact path to add with the "+" button, when one can be determined.

    Always fully resolved.  TCC records the *real* path of an executable, so a
    symlink - which is exactly what ``.venv/bin/python`` is - names an entry
    that can never match the process asking for the permission.
    """

    bundled: bool
    """True when this process is a ``.app`` bundle and owns its own identity."""

    bundle_id: str | None = None
    """Bundle identifier TCC files the grants under, when there is one.

    Only a bundled build has one, and it is the only thing that makes
    :func:`reset_permissions` safe to offer: ``tccutil`` needs it to scope a
    reset to this application instead of every application on the machine.
    """

    def describe(self) -> str:
        if self.path and self.path != self.name:
            return f"{self.name} ({self.path})"
        return self.name


@dataclass(frozen=True, slots=True)
class PermissionStatus:
    """A single reading of every permission this application depends on.

    Each flag is ``True`` granted, ``False`` denied, ``None`` undeterminable -
    which off macOS is the normal answer, and on macOS means the probe itself
    failed and nothing should be trusted.
    """

    accessibility: bool | None
    post_events: bool | None
    input_monitoring: bool | None
    subject: PermissionSubject

    @property
    def can_type(self) -> bool | None:
        """Whether synthetic key events would actually reach another app.

        ``False`` as soon as either gate is denied.  ``None`` only when nothing
        could be determined at all, which is the off-macOS case.
        """
        if self.accessibility is False or self.post_events is False:
            return False
        if self.accessibility is None and self.post_events is None:
            return None
        return True

    @property
    def stale_grant_suspected(self) -> bool:
        """Two TCC services that one switch controls, disagreeing.

        The Accessibility switch grants ``kTCCServiceAccessibility`` and
        ``kTCCServicePostEvent`` together, so a split answer never means the
        user forgot to enable something.
        """
        return (
            self.accessibility is not None
            and self.post_events is not None
            and self.accessibility != self.post_events
        )

    @property
    def needs_restart_to_apply(self) -> bool:
        """Granted, but this process is still reading its start-up answers.

        The two kinds of probe do not age the same way, and the difference is
        the whole of "I granted it and only one of them went green":

        * ``AXIsProcessTrusted()`` asks macOS afresh every time, so it starts
          answering ``True`` the moment the user flips the switch.
        * ``CGPreflightPostEventAccess()`` and ``CGPreflightListenEventAccess()``
          answer from a decision the process was handed when it started, and
          keep answering it for as long as the process lives.

        So a run that was launched before the grant sees Accessibility go green
        while the other two stay red for ever, with nothing wrong in System
        Settings at all.  Only a restart can change their minds - and clearing
        the entry, the fix for a genuinely stale grant, would throw away the
        permission the user has just given.

        Keyed on Post Events alone.  One switch grants Accessibility and Post
        Events together, so those two disagreeing can only be a stale reading.
        Input Monitoring is a separate switch that is genuinely allowed to
        differ, and folding it in here would report "restart to apply" at
        someone who simply has not granted it.  It is refreshed by the same
        restart regardless.
        """
        return self.accessibility is True and self.post_events is False

    def checklist(self) -> list[tuple[str, bool | None]]:
        """One row per permission, in the order the user has to fix them.

        The banner shows this instead of merging everything into one sentence.
        Prose cannot say "Accessibility is on, Input Monitoring is off" without
        being read as "Accessibility is off" by someone who has just switched
        it on, and that misreading is the single most common way this
        application gets diagnosed as broken.
        """
        return [
            ("Accessibility", self.accessibility),
            ("Post events", self.post_events),
            ("Input Monitoring", self.input_monitoring),
        ]


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def accessibility_permission_granted() -> bool | None:
    """Whether this process is trusted for Accessibility.

    Returns ``None`` only off macOS.  A probe that fails *on* macOS answers
    ``False``: the caller's job is to refuse to type, and an unreadable
    permission is not evidence that typing would work.
    """
    if sys.platform != "darwin":
        return None
    try:
        framework = ctypes.cdll.LoadLibrary(_ACCESSIBILITY_FRAMEWORK)
        framework.AXIsProcessTrusted.restype = ctypes.c_bool
        framework.AXIsProcessTrusted.argtypes = []
        return bool(framework.AXIsProcessTrusted())
    except Exception:  # noqa: BLE001 - unreadable is treated as not granted
        logger.warning(
            "Could not probe Accessibility permission; treating it as not "
            "granted so nothing is typed into a void.",
            exc_info=True,
        )
        return False


def post_event_permission_granted() -> bool | None:
    """Whether ``CGEventPost`` would actually reach another application.

    This is the gate that matters for typing, and the reason the whole
    permission check exists: without it ``CGEventPost`` reports no error at all
    and every keystroke is discarded in silence.
    """
    return _quartz_preflight("CGPreflightPostEventAccess")


def input_monitoring_permission_granted() -> bool | None:
    """Whether this process may observe other applications' keystrokes.

    A ``CGEventTap`` needs this.  It is a separate switch from Accessibility,
    and granting one does not grant the other.
    """
    return _quartz_preflight("CGPreflightListenEventAccess")


def _quartz_preflight(function_name: str) -> bool | None:
    if sys.platform != "darwin":
        return None
    try:
        import Quartz

        preflight = getattr(Quartz, function_name, None)
        if preflight is None:
            # Older macOS without the preflight API: unknown, not denied.
            logger.debug("Quartz has no %s on this system", function_name)
            return None
        return bool(preflight())
    except Exception:  # noqa: BLE001
        logger.warning("Could not probe %s", function_name, exc_info=True)
        return False


def typing_permission_granted() -> bool | None:
    """Whether a key event emitted now would reach another application.

    Both gates are consulted, because either one being denied produces the same
    silent nothing-happens failure.  This is the default the safety controller
    checks before every run.
    """
    accessibility = accessibility_permission_granted()
    post_events = post_event_permission_granted()
    if accessibility is False or post_events is False:
        return False
    if accessibility is None and post_events is None:
        return None
    return True


def permission_status() -> PermissionStatus:
    """Read every permission at once, together with who they belong to."""
    return PermissionStatus(
        accessibility=accessibility_permission_granted(),
        post_events=post_event_permission_granted(),
        input_monitoring=input_monitoring_permission_granted(),
        subject=permission_subject(),
    )


#: Said whenever nothing more specific can be determined.  Every remedy this
#: module produces names System Settings, so callers can rely on that.
GENERIC_REMEDY = (
    "Open System Settings → Privacy & Security → Accessibility and enable "
    "this application."
)

#: The overlay's one permission button, named here so the advice and the button
#: it tells the user to press can never drift apart.
GRANT_BUTTON_LABEL = "Grant permission"

#: Set on a run this application started itself, to the *reason* it restarted.
#: The reason is the whole point: restarting to pick up a grant is only useful
#: once, so a second attempt has to be suppressed - but only when the previous
#: restart was that same attempt.  A plain "we restarted" flag conflates the
#: two restarts this application performs, and then a reset-and-restart eats
#: the one allowance the restart-to-apply needed, leaving the user looking at
#: "Accessibility ✓, Post events ✕" with nothing happening.
RESTARTED_MARKER = "TYPING_SIMULATOR_RESTARTED"

#: Restarted so a grant made since start-up is finally visible.  Nothing was
#: cleared; the permission is already there.
RESTART_TO_APPLY = "apply"

#: Restarted after clearing our own TCC entries, so macOS prompts again.  A
#: restart-to-apply is still owed after this one, because the user has yet to
#: grant anything.
RESTART_AFTER_RESET = "reset"


def restart_marker() -> str | None:
    """Why this run was restarted by us, or ``None`` if it was not."""
    marker = os.environ.get(RESTARTED_MARKER)
    return marker or None


def was_restarted_by_us() -> bool:
    """Whether this run is the result of our own restart, for any reason."""
    return restart_marker() is not None


def was_restarted_to_apply_permission() -> bool:
    """Whether this run is the result of a restart-to-apply that already ran.

    This is the only restart that must not be repeated: it exists to refresh
    the start-up permission answers, so if they are *still* stale afterwards
    they are not stale at all and restarting again would loop.

    An unrecognised marker - which is what an older build wrote - is read as
    this case, because suppressing one useful restart is a far smaller failure
    than a restart loop the user cannot interrupt.
    """
    marker = restart_marker()
    return marker is not None and marker != RESTART_AFTER_RESET


def clear_restart_marker() -> None:
    """Forget that this run was a restart, once the restart has done its job.

    Called when the permissions finally read as working.  Leaving the marker
    set would make the *next* grant in this same run - after the user revoked
    and re-granted, say - unable to restart to pick itself up, for no reason
    other than something that already succeeded.
    """
    os.environ.pop(RESTARTED_MARKER, None)


def describe_permission_remedy(
    status: PermissionStatus, *, restart_pending: bool = True
) -> str:
    """What the user should actually *do*, given ``status``.

    Deliberately the remedy only - never the diagnosis.  The banner and the
    refusal message describe the symptom differently but must never disagree
    about the fix, so the fix lives in one place and they both append it.

    ``restart_pending`` says whether the application is about to restart
    itself.  It only matters in the granted-but-not-yet-visible case, and there
    it decides between "sit tight" and "do this yourself": promising a restart
    that is not coming is how an already-confusing permission turns into an
    application the user is sure is broken.
    """
    subject = status.subject
    where = subject.describe()

    if status.needs_restart_to_apply:
        # Checked before the stale-grant case, and the order is load-bearing:
        # the two look identical from the outside, and the stale-grant remedy
        # clears the entry - which here would delete a permission that is
        # working and that the user has just granted.
        if restart_pending:
            return (
                "The permission is granted; this run of the application is "
                "still using the answer macOS gave it when it started, which "
                "is why only some of them went green. Restarting picks up the "
                "real one. Nothing needs changing in System Settings."
            )
        return (
            "The permission is granted; this run of the application is still "
            "using the answer macOS gave it when it started, which is why only "
            f"some of them went green. Press “{GRANT_BUTTON_LABEL}”, or quit "
            "this application and open it again - either way a new process "
            "gets the real answer. Nothing needs changing in System Settings."
        )

    if status.stale_grant_suspected:
        # The switch is on and still does nothing, so "enable it" is worse than
        # useless advice - it is the advice the user has already followed.
        return (
            f"macOS lists {subject.name} as allowed, but the grant no longer "
            "applies to this build: rebuilding an unsigned app changes its "
            "identity, which silently invalidates the entry while leaving the "
            f"switch on. Press “{GRANT_BUTTON_LABEL}”: it clears the stale "
            "entry and restarts, which is what makes macOS ask again. "
            "Failing that, in System Settings → Privacy & "
            f"Security → Accessibility select {subject.name}, remove it with "
            f"the “−” button, then use “{GRANT_BUTTON_LABEL}” to add it back."
        )

    if status.can_type is False:
        if not subject.bundled:
            # Naming a path here is close to useless: the "+" picker will not
            # offer a bare Unix executable, and macOS attributes the grant to
            # whatever launched the interpreter anyway. The honest answer is
            # that this configuration cannot hold a grant reliably.
            return (
                "This is running from source, so macOS attributes the "
                f"permission to {subject.name} - the application that launched "
                "the interpreter - rather than to this program, and an entry "
                "added for a virtualenv interpreter never matches the process "
                "asking. Build a bundle instead: `make dev` while working on "
                "this, or `make app` to use it. A bundle owns its own "
                "identity, so the grant is made to it and stays made. To grant "
                f"it here anyway, enable {where} in System Settings → Privacy "
                "& Security → Accessibility - which grants every program it "
                "launches the same access."
            )

        return (
            f"Enable {where} in System Settings → Privacy & Security → "
            "Accessibility. If that entry is already there and switched on, "
            f"press “{GRANT_BUTTON_LABEL}”, or remove it with “−” and add it "
            "again: an entry outlives the build it was granted to."
        )

    if status.input_monitoring is False:
        return (
            f"Enable {where} in System Settings → Privacy & Security → Input "
            "Monitoring. This is a separate switch from Accessibility, and "
            "having Accessibility on does not grant it."
        )

    return GENERIC_REMEDY


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


def request_accessibility_permission() -> bool | None:
    """Ask macOS to prompt for Accessibility, and report the answer.

    The prompt itself is the point.  Besides being the only way the user is
    offered a one-click "Open System Settings", it makes macOS record *this*
    binary as the subject of the request - which repairs the common case where
    an entry added by hand no longer matches the app after a rebuild.

    macOS shows the prompt at most once per process for a given identity; when
    it is suppressed this simply returns the current state.  Returns ``None``
    when the request could not be made at all.
    """
    if sys.platform != "darwin":
        return None
    try:
        import ApplicationServices

        options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
        return bool(ApplicationServices.AXIsProcessTrustedWithOptions(options))
    except Exception:  # noqa: BLE001 - fall back to the non-prompting probe
        logger.warning(
            "Could not ask macOS to prompt for Accessibility permission",
            exc_info=True,
        )
        return accessibility_permission_granted()


def request_post_event_permission() -> bool | None:
    """Ask macOS to prompt for the Post Events service specifically.

    Without this there is a dead end with no way out from inside the
    application.  ``kTCCServicePostEvent`` is a different service from
    ``kTCCServiceAccessibility``, and when only the former is denied -
    the stale-grant case, which is the common one for a rebuilt unsigned
    app - ``AXIsProcessTrustedWithOptions`` prompts for nothing at all: the
    process is *already* trusted for Accessibility, so it answers ``True``
    immediately and Post Events stays denied however many times the button is
    pressed.  ``CGRequestPostEventAccess`` is the only call that asks about the
    service that is actually refusing.
    """
    if sys.platform != "darwin":
        return None
    try:
        import Quartz

        request = getattr(Quartz, "CGRequestPostEventAccess", None)
        if request is None:
            logger.debug("Quartz has no CGRequestPostEventAccess on this system")
            return post_event_permission_granted()
        return bool(request())
    except Exception:  # noqa: BLE001
        logger.warning("Could not request Post Events permission", exc_info=True)
        return post_event_permission_granted()


def request_input_monitoring_permission() -> bool | None:
    """Ask macOS to prompt for Input Monitoring, and report the answer."""
    if sys.platform != "darwin":
        return None
    try:
        import Quartz

        request = getattr(Quartz, "CGRequestListenEventAccess", None)
        if request is None:
            return input_monitoring_permission_granted()
        return bool(request())
    except Exception:  # noqa: BLE001
        logger.warning("Could not request Input Monitoring permission", exc_info=True)
        return input_monitoring_permission_granted()


# ---------------------------------------------------------------------------
# Clearing this application's own grants
# ---------------------------------------------------------------------------


def own_bundle_identifier() -> str | None:
    """This application's bundle identifier, when it is a bundled build.

    Read from the bundle's own ``Info.plist`` rather than hard-coded, so it
    cannot drift away from what was actually built and what TCC therefore
    recorded.
    """
    if sys.platform != "darwin":
        return None
    bundle = _own_bundle_path()
    if bundle is None:
        return None
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as handle:
            identifier = plistlib.load(handle).get("CFBundleIdentifier")
    except Exception:  # noqa: BLE001 - a missing plist just means "no identifier"
        logger.debug("Could not read the bundle identifier", exc_info=True)
        return None
    return str(identifier) if identifier else None


def can_reset_permissions() -> bool:
    """Whether this build can clear its own grants safely.

    Only a bundled build can.  Run from source the grant belongs to whatever
    launched the interpreter, and clearing *that* would revoke a permission the
    user granted to their terminal or editor for everything else they do.
    """
    return own_bundle_identifier() is not None


def reset_permissions() -> bool:
    """Clear this application's TCC entries so macOS will prompt again.

    This is the escape hatch for the failure that otherwise has none: macOS
    shows each permission prompt **once per identity**, so a user who dismissed
    it - or whose entry went stale on a rebuild - can press "Grant permission"
    forever and see nothing happen.  Removing the entries makes the next
    request prompt properly.

    Every invocation is scoped to this application's own bundle identifier.
    ``tccutil reset <service>`` with no identifier revokes that permission for
    *every* application on the machine, so a missing identifier returns
    ``False`` rather than falling back to the unscoped form.
    """
    identifier = own_bundle_identifier()
    if not identifier:
        logger.info("Not a bundled build, so there is no entry of our own to clear.")
        return False

    return _reset_services(identifier)


def relaunch(reason: str = RESTART_TO_APPLY) -> bool:
    """Start this bundle again, so macOS re-decides what it is allowed to do.

    Clearing an entry is not enough on its own, and this is why both buttons
    looked broken: macOS settles a process's TCC answer when the process first
    asks and keeps that answer for as long as the process lives, and
    ``AXIsProcessTrustedWithOptions`` shows its prompt at most once per
    process.  So after a reset the *running* application keeps being told no,
    with no prompt, no matter how many times it asks.  Only a new process gets
    a fresh answer and a fresh prompt.

    The relaunch is deferred to a detached shell so it happens after this
    process has exited; otherwise the old instance is still around when the new
    one starts and macOS hands the request back to the old one.  ``-n`` forces
    a new instance rather than reactivating whatever LaunchServices still
    believes is running: this runs precisely when the last attempt appeared to
    do nothing, so "start something, definitely" beats being tidy.

    ``reason`` is handed to the new process so it knows which restart it is the
    result of; see :data:`RESTARTED_MARKER`.
    """
    bundle = _own_bundle_path()
    if bundle is None:
        logger.info("Not a bundled build, so there is nothing to relaunch.")
        return False
    marker = f"{RESTARTED_MARKER}={reason}"
    try:
        subprocess.Popen(  # noqa: S603 - fixed command, our own bundle path
            [
                "/bin/sh",
                "-c",
                f"sleep 1; exec /usr/bin/open -n --env {shlex.quote(marker)} "
                f"{shlex.quote(bundle)}",
            ],
            start_new_session=True,
        )
    except Exception:  # noqa: BLE001 - failing to relaunch must not crash
        logger.warning("Could not schedule a relaunch of %s", bundle, exc_info=True)
        return False
    logger.info("Scheduled a relaunch of %s (%s)", bundle, reason)
    return True


def _reset_services(identifier: str) -> bool:
    cleared = False
    for service in TCC_SERVICES:
        try:
            completed = subprocess.run(
                ["/usr/bin/tccutil", "reset", service, identifier],
                capture_output=True,
                text=True,
                timeout=_TCCUTIL_TIMEOUT_SECONDS,
                check=False,
            )
        except Exception:  # noqa: BLE001 - a missing tccutil is not a crash
            logger.warning("Could not run tccutil for %s", service, exc_info=True)
            continue
        if completed.returncode == 0:
            cleared = True
            logger.info("Cleared the %s grant for %s", service, identifier)
        else:
            # Routine: the service has no entry for us yet, which is fine.
            logger.info(
                "tccutil reset %s %s returned %d (%s)",
                service,
                identifier,
                completed.returncode,
                completed.stderr.strip() or "no output",
            )
    return cleared


# ---------------------------------------------------------------------------
# Who the permission belongs to
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def permission_subject() -> PermissionSubject:
    """Work out what the user has to enable in System Settings.

    Cached: the answer is fixed for the lifetime of the process, and this is
    called from a repeating interface timer that must not shell out every tick.

    A bundled build owns its identity, so the answer is the ``.app`` itself.
    Run from source there is no bundle, and macOS attributes the permission to
    the application that launched the interpreter - a terminal, an IDE, or
    whatever else.  Finding that application by name is what makes the
    instruction actionable instead of a guess.
    """
    if sys.platform != "darwin":
        return PermissionSubject(name="this application", path=None, bundled=False)

    bundle = _own_bundle_path()
    if bundle is not None:
        return PermissionSubject(
            name=_bundle_name(bundle),
            path=bundle,
            bundled=True,
            bundle_id=own_bundle_identifier(),
        )

    ancestor = _launching_application()
    if ancestor is not None:
        return PermissionSubject(name=_bundle_name(ancestor), path=ancestor, bundled=False)

    # No bundle anywhere in the ancestry - a bare `python` from a login shell,
    # a launchd job, or an SSH session.  The interpreter itself is the subject,
    # and it has to be named by its *real* path: a virtualenv's `bin/python` is
    # a symlink, TCC records what it points at, and an entry added for the
    # symlink is one the running process can never match.
    interpreter = _resolve(sys.executable)
    return PermissionSubject(
        name=os.path.basename(interpreter) or "the interpreter",
        path=interpreter,
        bundled=False,
    )


def _resolve(path: str | None) -> str:
    """The real path of ``path``, which is the one TCC records."""
    if not path:
        return ""
    try:
        return os.path.realpath(path)
    except OSError:  # pragma: no cover - realpath does not raise in practice
        logger.debug("Could not resolve %s", path, exc_info=True)
        return path


def _own_bundle_path() -> str | None:
    """The ``.app`` this process's executable lives in, if it lives in one.

    Deliberately keyed on where the executable *is* rather than on
    ``sys.frozen``.  macOS attributes a grant to the running executable, so any
    interpreter inside a bundle - the frozen build, and equally the development
    bundle from ``packaging/dev_bundle.py``, which is an ordinary interpreter -
    is one whose grant belongs to that bundle.  Asking about the location gets
    both right; asking about ``sys.frozen`` gets the second one wrong.
    """
    return _enclosing_bundle(_resolve(sys.executable))


def _enclosing_bundle(executable_path: str | None) -> str | None:
    """The nearest enclosing ``*.app`` directory of ``executable_path``."""
    if not executable_path:
        return None
    path = os.path.abspath(executable_path)
    while True:
        parent = os.path.dirname(path)
        if parent == path:  # reached the filesystem root
            return None
        if path.endswith(".app"):
            return path
        path = parent


def _bundle_name(path: str) -> str:
    name = os.path.basename(path)
    return name[: -len(".app")] if name.endswith(".app") else name


def _launching_application() -> str | None:
    """Walk up the process tree to the nearest ancestor that is an app bundle.

    ``ps`` is used rather than a private responsibility API: it needs no extra
    permission of its own, and getting the answer approximately right is far
    more useful than refusing to answer.
    """
    parents = _parent_map()
    if not parents:
        return None

    pid = os.getpid()
    seen: set[int] = set()
    while pid not in seen and pid > 1:
        seen.add(pid)
        entry = parents.get(pid)
        if entry is None:
            return None
        parent_pid, command = entry
        bundle = _enclosing_bundle(command)
        if bundle is not None and pid != os.getpid():
            return bundle
        pid = parent_pid
    return None


def _parent_map() -> dict[int, tuple[int, str]]:
    """``{pid: (ppid, executable_path)}`` for every visible process."""
    try:
        completed = subprocess.run(
            ["/bin/ps", "-Ao", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:  # noqa: BLE001 - diagnostics must never break the app
        logger.debug("Could not enumerate processes", exc_info=True)
        return {}

    table: dict[int, tuple[int, str]] = {}
    for line in completed.stdout.splitlines():
        # `comm` is a path and may contain spaces, so split only the two
        # leading numeric columns.
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), parts[2])
        except ValueError:
            continue
    return table


def permission_holder_name() -> str:
    """Short name of what the user must enable in System Settings."""
    return permission_subject().name
