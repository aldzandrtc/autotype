"""The physical keyboard backend.

Emits one press and one release at a time through ``pynput``.  There is
deliberately no call to ``Controller.type()`` or any other "type the whole
string" helper: the scheduler owns every key event and every delay between
them.
"""

from __future__ import annotations

import logging

from typing_simulator.backends.base import TrackingKeyboardBackend
from typing_simulator.domain.events import NormalizedKey, SpecialKey
from typing_simulator.errors import KeyboardBackendError, MissingPermissionError

logger = logging.getLogger(__name__)


class PynputKeyboardBackend(TrackingKeyboardBackend):
    """Sends real key events to whichever application is frontmost."""

    def __init__(self) -> None:
        super().__init__()
        try:
            from pynput.keyboard import Controller, Key
        except Exception as exc:  # noqa: BLE001
            raise KeyboardBackendError(
                "The pynput keyboard library could not be loaded, so no keys "
                "can be emitted."
            ) from exc

        try:
            self._controller = Controller()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc) from exc

        self._special_keys = {
            SpecialKey.ENTER: Key.enter,
            SpecialKey.BACKSPACE: Key.backspace,
            SpecialKey.SPACE: Key.space,
            SpecialKey.TAB: Key.tab,
            SpecialKey.SHIFT: Key.shift,
        }

    # -- mapping -----------------------------------------------------------
    def _resolve(self, key: NormalizedKey) -> object:
        if key.special is not None:
            resolved = self._special_keys.get(key.special)
            if resolved is None:  # pragma: no cover - the enum is closed
                raise KeyboardBackendError(f"Unsupported special key: {key.special.name}")
            return resolved
        assert key.char is not None
        return key.char

    # -- TrackingKeyboardBackend hooks ------------------------------------
    def _press(self, key: NormalizedKey) -> None:
        try:
            self._controller.press(self._resolve(key))
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, key) from exc

    def _release(self, key: NormalizedKey) -> None:
        try:
            self._controller.release(self._resolve(key))
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, key) from exc

    # -- errors ------------------------------------------------------------
    @staticmethod
    def _translate(exc: Exception, key: NormalizedKey | None = None) -> Exception:
        message = str(exc).lower()
        if "not trusted" in message or "accessibility" in message or "denied" in message:
            return MissingPermissionError(
                "macOS refused to deliver a key event. Grant Accessibility "
                "(and, if prompted, Input Monitoring) permission in System "
                "Settings -> Privacy & Security, then restart the application."
            )
        where = f" while emitting {key.describe()}" if key is not None else ""
        return KeyboardBackendError(
            f"The keyboard backend failed{where} ({type(exc).__name__}). Typing stopped."
        )
