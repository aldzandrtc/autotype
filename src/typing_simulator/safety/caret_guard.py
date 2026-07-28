"""Checking that the text caret has not moved while a run was paused.

The focus guard only sees *applications*.  That is not enough before resuming:
the user may come back to the right application but click somewhere else in the
document first, and resuming would then splice the rest of the text into the
wrong place.

macOS can answer the finer question through the Accessibility API.  For the
system-wide focused UI element it exposes a role, a title, and - for text
controls - the selected text range, whose location is the caret offset.
Snapshotting that when the run pauses and comparing it before resuming catches
a moved cursor.

This is best-effort by design.  Plenty of applications expose no caret at all
(canvas-based editors such as Google Docs are the obvious case), and the API is
unavailable without Accessibility permission.  When the caret cannot be read
the snapshot reports itself as unverifiable, and the caller falls back to the
application-level check rather than refusing to work.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CaretSnapshot:
    """Where the insertion point was, as far as macOS would tell us."""

    pid: int | None = None
    role: str | None = None
    title: str | None = None
    identifier: str | None = None
    caret: int | None = None
    selection_length: int | None = None
    reason: str | None = None
    """Why the caret is unknown, when it is."""

    @property
    def verifiable(self) -> bool:
        """True when this snapshot is precise enough to compare."""
        return self.caret is not None

    def same_field_as(self, other: "CaretSnapshot") -> bool:
        return (
            self.pid == other.pid
            and self.role == other.role
            and self.title == other.title
            and self.identifier == other.identifier
        )

    def matches(self, other: "CaretSnapshot") -> bool:
        """True when the caret is in the same place, in the same field."""
        if not (self.verifiable and other.verifiable):
            return False
        return self.same_field_as(other) and self.caret == other.caret

    def describe(self) -> str:
        if not self.verifiable:
            return self.reason or "the cursor position is not reported by this application"
        where = self.title or self.identifier or self.role or "the text field"
        return f"character {self.caret:,} of {where}"


@runtime_checkable
class CaretGuard(Protocol):
    def snapshot(self) -> CaretSnapshot:
        """Read the current caret position, or say why it cannot be read."""
        ...


class MacCaretGuard:
    """Accessibility-backed caret reader."""

    def __init__(self) -> None:
        import ApplicationServices  # imported lazily: macOS only

        self._ax = ApplicationServices

    def snapshot(self) -> CaretSnapshot:
        ax = self._ax
        try:
            system = ax.AXUIElementCreateSystemWide()
            error, focused = ax.AXUIElementCopyAttributeValue(
                system, ax.kAXFocusedUIElementAttribute, None
            )
            if error != 0 or focused is None:
                return CaretSnapshot(
                    reason=self._explain(error),
                )

            caret, length = self._selected_range(focused)
            return CaretSnapshot(
                pid=self._pid(focused),
                role=self._string(focused, "kAXRoleAttribute"),
                title=self._string(focused, "kAXTitleAttribute"),
                identifier=self._string(focused, "kAXIdentifierAttribute"),
                caret=caret,
                selection_length=length,
                reason=None
                if caret is not None
                else "this application does not report a cursor position",
            )
        except Exception:  # noqa: BLE001 - never let a probe break a run
            logger.debug("Caret snapshot failed", exc_info=True)
            return CaretSnapshot(reason="the cursor position could not be read")

    # -- helpers -----------------------------------------------------------
    def _pid(self, element) -> int | None:
        try:
            error, pid = self._ax.AXUIElementGetPid(element, None)
            return int(pid) if error == 0 else None
        except Exception:  # noqa: BLE001
            return None

    def _string(self, element, attribute_name: str) -> str | None:
        attribute = getattr(self._ax, attribute_name, None)
        if attribute is None:
            return None
        try:
            error, value = self._ax.AXUIElementCopyAttributeValue(element, attribute, None)
        except Exception:  # noqa: BLE001
            return None
        if error != 0 or value is None:
            return None
        return str(value)

    def _selected_range(self, element) -> tuple[int | None, int | None]:
        ax = self._ax
        try:
            error, value = ax.AXUIElementCopyAttributeValue(
                element, ax.kAXSelectedTextRangeAttribute, None
            )
            if error != 0 or value is None:
                return None, None
            range_type = getattr(ax, "kAXValueTypeCFRange", None)
            if range_type is None:  # pragma: no cover - older bindings
                range_type = getattr(ax, "kAXValueCFRangeType", None)
            ok, decoded = ax.AXValueGetValue(value, range_type, None)
            if not ok or decoded is None:
                return None, None
            return int(decoded.location), int(decoded.length)
        except Exception:  # noqa: BLE001
            logger.debug("Could not read the selected text range", exc_info=True)
            return None, None

    @staticmethod
    def _explain(error: int) -> str:
        if error == -25204:  # kAXErrorAPIDisabled
            return "Accessibility is not enabled for this process"
        if error == -25212:  # kAXErrorNoValue
            return "no text field is focused"
        return "the focused element could not be read"


class NullCaretGuard:
    """Used off macOS, and whenever the Accessibility bindings are missing."""

    def snapshot(self) -> CaretSnapshot:
        return CaretSnapshot(reason="cursor tracking is only available on macOS")


def create_caret_guard() -> CaretGuard:
    """Return the best caret guard available on this platform."""
    if sys.platform != "darwin":
        return NullCaretGuard()
    try:
        return MacCaretGuard()
    except Exception:  # noqa: BLE001 - missing bindings must not break startup
        logger.warning("Accessibility caret tracking is unavailable", exc_info=True)
        return NullCaretGuard()
