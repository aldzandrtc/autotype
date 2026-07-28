"""A backend that records calls instead of touching the real keyboard.

Used by the whole automated test suite: no test ever emits a real key event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from typing_simulator.backends.base import TrackingKeyboardBackend
from typing_simulator.domain.events import Event, KeyDown, KeyUp, NormalizedKey


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One recorded backend call: ``kind`` is ``"down"``, ``"up"`` or ``"release_all"``."""

    kind: str
    key: NormalizedKey | None = None


class RecordingKeyboardBackend(TrackingKeyboardBackend):
    """Records every call.  Optionally fails on demand, to exercise cleanup.

    ``fail_on`` receives each key about to be pressed or released and may raise
    to simulate a backend failure.
    """

    def __init__(self, fail_on: Callable[[str, NormalizedKey], None] | None = None) -> None:
        super().__init__()
        self.calls: list[RecordedCall] = []
        self.release_all_count = 0
        self._fail_on = fail_on

    # -- TrackingKeyboardBackend hooks ------------------------------------
    def _press(self, key: NormalizedKey) -> None:
        if self._fail_on is not None:
            self._fail_on("down", key)
        self.calls.append(RecordedCall("down", key))

    def _release(self, key: NormalizedKey) -> None:
        if self._fail_on is not None:
            self._fail_on("up", key)
        self.calls.append(RecordedCall("up", key))

    def release_all(self) -> None:
        self.release_all_count += 1
        super().release_all()
        self.calls.append(RecordedCall("release_all"))

    # -- helpers for tests -------------------------------------------------
    @property
    def key_events(self) -> list[Event]:
        """The recorded presses and releases as domain events."""
        events: list[Event] = []
        for call in self.calls:
            if call.key is None:
                continue
            events.append(KeyDown(call.key) if call.kind == "down" else KeyUp(call.key))
        return events

    def reset(self) -> None:
        self.calls.clear()
        self.release_all_count = 0
