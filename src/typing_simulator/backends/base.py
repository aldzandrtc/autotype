"""The keyboard-backend boundary.

The scheduler only ever calls :meth:`key_down`, :meth:`key_up` and
:meth:`release_all`.  There is deliberately no "type this whole string"
convenience method: the scheduler must retain control over every individual
key event and every delay between them.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol, runtime_checkable

from typing_simulator.domain.events import NormalizedKey

logger = logging.getLogger(__name__)


@runtime_checkable
class KeyboardBackend(Protocol):
    """Emits individual key events."""

    def key_down(self, key: NormalizedKey) -> None: ...

    def key_up(self, key: NormalizedKey) -> None: ...

    def release_all(self) -> None:
        """Release every key this backend believes is still held.

        Must attempt to release *all* tracked keys even if an individual
        release raises, and must never raise itself.
        """
        ...


class TrackingKeyboardBackend:
    """Base class that tracks held keys and implements a safe ``release_all``.

    Subclasses implement :meth:`_press` and :meth:`_release`, which may raise;
    the tracking bookkeeping and the "release everything, swallow individual
    failures" contract live here so both backends behave identically.
    """

    def __init__(self) -> None:
        self._pressed: list[NormalizedKey] = []
        self._lock = threading.RLock()

    # -- subclass hooks ---------------------------------------------------
    def _press(self, key: NormalizedKey) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _release(self, key: NormalizedKey) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- public API -------------------------------------------------------
    @property
    def pressed_keys(self) -> tuple[NormalizedKey, ...]:
        with self._lock:
            return tuple(self._pressed)

    def key_down(self, key: NormalizedKey) -> None:
        with self._lock:
            # Track before pressing: if the press raises half-way through, the
            # key may still be physically down and must be released.
            self._pressed.append(key)
        self._press(key)

    def key_up(self, key: NormalizedKey) -> None:
        try:
            self._release(key)
        finally:
            with self._lock:
                if key in self._pressed:
                    self._pressed.remove(key)

    def release_all(self) -> None:
        """Release every tracked key; never raises."""
        with self._lock:
            outstanding = list(self._pressed)
            self._pressed.clear()
        for key in reversed(outstanding):
            try:
                self._release(key)
            except Exception:  # noqa: BLE001 - one failure must not stop the rest
                logger.exception("Failed to release key %s during release_all", key.describe())
