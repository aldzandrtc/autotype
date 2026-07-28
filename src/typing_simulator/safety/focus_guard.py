"""Reading the frontmost macOS application.

Known limitation
----------------
This guard sees *applications*, nothing finer.  It cannot tell that a
different text field, window, browser tab or document became active inside the
same application.  If the user switches from one Google Doc to another in the
same browser, the guard sees no change and typing continues.  The prototype
deliberately does not inspect browser contents and never tries to work out
whether Google Docs is open.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Permission probing lives in its own module; these are re-exported because
# every caller of the focus guard also needs to ask about permissions, and the
# two were a single module before the distinct TCC services were separated out.
from typing_simulator.safety.permissions import (  # noqa: F401
    PermissionStatus,
    PermissionSubject,
    accessibility_permission_granted,
    permission_holder_name,
    permission_status,
    permission_subject,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FrontmostApp:
    """Identity of the application that currently owns the foreground."""

    pid: int
    name: str
    bundle_id: str | None = None

    def same_as(self, other: "FrontmostApp | None") -> bool:
        if other is None:
            return False
        if self.pid == other.pid:
            return True
        # A relaunched target keeps its bundle id but gets a new pid; treating
        # that as the same application would be wrong, so bundle id alone is
        # never enough. pid is the identity.
        return False

    def describe(self) -> str:
        if self.bundle_id:
            return f"{self.name} ({self.bundle_id})"
        return self.name


@runtime_checkable
class FocusGuard(Protocol):
    def frontmost(self) -> FrontmostApp | None:
        """The frontmost application, or ``None`` if it cannot be determined."""
        ...

    def is_self(self, app: FrontmostApp | None) -> bool:
        """True when ``app`` is this typing simulator."""
        ...

    def self_has_focus(self) -> bool:
        """True when *this process* currently holds keyboard focus.

        This is a different question from :meth:`frontmost`, and asking it
        matters.  The overlay runs as an accessory application, so macOS can
        hand it the key window - and therefore every keystroke - while
        ``NSWorkspace`` still reports the user's document application as
        frontmost.  Relying on :meth:`frontmost` alone would let the app type
        into its own window believing it was typing into the target.
        """
        ...

    def activate(self, app: FrontmostApp) -> bool:
        """Bring ``app`` back to the front.  Returns whether the call was made."""
        ...


class MacFocusGuard:
    """AppKit-backed focus guard."""

    def __init__(self) -> None:
        from AppKit import NSWorkspace  # imported lazily: macOS only

        self._workspace = NSWorkspace.sharedWorkspace()
        self._own_pid = os.getpid()

    def frontmost(self) -> FrontmostApp | None:
        try:
            app = self._workspace.frontmostApplication()
        except Exception:  # noqa: BLE001 - never let a lookup failure crash a run
            logger.exception("Could not read the frontmost application")
            return None
        if app is None:
            return None
        name = app.localizedName() or "Unknown application"
        bundle_id = app.bundleIdentifier()
        return FrontmostApp(
            pid=int(app.processIdentifier()),
            name=str(name),
            bundle_id=str(bundle_id) if bundle_id else None,
        )

    def is_self(self, app: FrontmostApp | None) -> bool:
        return app is not None and app.pid == self._own_pid

    def self_has_focus(self) -> bool:
        """Whether *we* are actually receiving keystrokes.

        Being "active" is not enough on its own: an accessory application can
        report ``isActive`` while having no window that accepts key events, in
        which case keystrokes still go to the previously focused document.
        The precise question is whether we hold a **key window**, so that is
        what decides here.
        """
        try:
            from AppKit import NSApplication

            app = NSApplication.sharedApplication()
            return bool(app.isActive() and app.keyWindow() is not None)
        except Exception:  # noqa: BLE001 - fall back to "cannot tell"
            logger.debug("Could not read our own key-window state", exc_info=True)
            return False

    def activate(self, app: FrontmostApp) -> bool:
        """Return keyboard focus to ``app``.

        Used only to undo focus the overlay took from the user a moment
        earlier; it never activates an application the user was not already
        working in.
        """
        try:
            from AppKit import NSRunningApplication

            running = NSRunningApplication.runningApplicationWithProcessIdentifier_(app.pid)
            if running is None:
                return False
            running.activateWithOptions_(0)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("Could not reactivate %s", app.name)
            return False


class UnavailableFocusGuard:
    """Fallback used off macOS: reports that focus cannot be determined.

    The controller treats "unknown frontmost application" as a hard failure, so
    this guard fails closed rather than allowing an unverified run.
    """

    def frontmost(self) -> FrontmostApp | None:
        return None

    def is_self(self, app: FrontmostApp | None) -> bool:
        return False

    def self_has_focus(self) -> bool:
        return False

    def activate(self, app: FrontmostApp) -> bool:
        return False


def create_focus_guard() -> FocusGuard:
    """Return the best focus guard available on this platform."""
    if sys.platform != "darwin":
        logger.warning("Focus guard is macOS-only; focus checks will fail closed.")
        return UnavailableFocusGuard()
    try:
        return MacFocusGuard()
    except Exception:  # noqa: BLE001 - missing PyObjC must not crash startup
        logger.exception("AppKit is unavailable; focus checks will fail closed.")
        return UnavailableFocusGuard()


