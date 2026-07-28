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
    applications' keystrokes, which is what the global abort hotkey needs.  It
    lives under a separate switch in System Settings and is **not** implied by
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
import subprocess
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_ACCESSIBILITY_FRAMEWORK = (
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)

#: ``ps`` is only consulted once per call and never with user-supplied data.
_PS_TIMEOUT_SECONDS = 2.0


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
    """Exact path to add with the "+" button, when one can be determined."""

    bundled: bool
    """True when this process is a ``.app`` bundle and owns its own identity."""

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
        ``kTCCServicePostEvent`` together, so a split answer means the recorded
        grant no longer matches this binary rather than that the user forgot to
        enable something.
        """
        return (
            self.accessibility is not None
            and self.post_events is not None
            and self.accessibility != self.post_events
        )


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

    The global pause and abort hotkeys need this.  It is a separate switch from
    Accessibility, and granting one does not grant the other.
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


def describe_permission_remedy(status: PermissionStatus) -> str:
    """What the user should actually *do*, given ``status``.

    Deliberately the remedy only - never the diagnosis.  The banner and the
    refusal message describe the symptom differently but must never disagree
    about the fix, so the fix lives in one place and they both append it.
    """
    subject = status.subject
    where = subject.describe()

    if status.stale_grant_suspected:
        # The switch is on and still does nothing, so "enable it" is worse than
        # useless advice - it is the advice the user has already followed.
        return (
            f"macOS lists {subject.name} as allowed, but the grant no longer "
            "applies to this build: rebuilding an unsigned app changes its "
            "identity, which silently invalidates the entry while leaving the "
            "switch on. In System Settings → Privacy & Security → "
            f"Accessibility, select {subject.name}, remove it with the “−” "
            "button, then use “Request permission” to add it back."
        )

    if status.can_type is False:
        parts = [
            f"Enable {where} in System Settings → Privacy & Security → "
            "Accessibility."
        ]
        if not subject.bundled:
            parts.append(
                "This is running from source, so macOS attributes the "
                "permission to the application that launched it rather than to "
                "the script - which is why enabling anything else changes "
                "nothing. Building the app bundle avoids this entirely."
            )
        parts.append(
            "If that entry is already there and switched on, remove it with "
            "“−” and add it again: an entry outlives the build it was granted "
            "to."
        )
        return " ".join(parts)

    if status.input_monitoring is False:
        return (
            "Enable {} in System Settings → Privacy & Security → Input "
            "Monitoring. This is a separate switch from Accessibility, and "
            "having Accessibility on does not grant it.".format(where)
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
        return PermissionSubject(name=_bundle_name(bundle), path=bundle, bundled=True)

    ancestor = _launching_application()
    if ancestor is not None:
        return PermissionSubject(name=_bundle_name(ancestor), path=ancestor, bundled=False)

    # No bundle anywhere in the ancestry - a bare `python` from a login shell,
    # a launchd job, or an SSH session.  The interpreter itself is the subject.
    return PermissionSubject(
        name=os.path.basename(sys.executable) or "the interpreter",
        path=sys.executable,
        bundled=False,
    )


def _own_bundle_path() -> str | None:
    """The ``.app`` this process is running from, if it is running from one."""
    if not getattr(sys, "frozen", False):
        return None
    return _enclosing_bundle(sys.executable)


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
