"""The macOS keyboard backend, built directly on Quartz events.

Why not ``pynput`` here
-----------------------
``pynput``'s macOS backend reaches into HIToolbox's Text Input Source APIs
(``TISCopyCurrentKeyboardInputSource`` / ``TSMGetInputSourceProperty``) through
``ctypes`` to translate between key codes and characters.  On current macOS
those functions assert that they are called on the main dispatch queue, so
calling them from a worker thread kills the whole process with ``SIGTRAP``::

    _dispatch_assert_queue_fail
    dispatch_assert_queue
    HIToolbox  islGetInputSourceListWithAdditions
    HIToolbox  TSMGetInputSourceProperty
    libffi -> _ctypes -> ... -> thread_run

This backend avoids that entirely: it posts ``CGEvent`` keyboard events, which
are documented as safe to use from any thread, and never asks the system to
resolve a keyboard layout.  Each event carries both a US-QWERTY virtual key
code (so applications that inspect key codes behave normally) and an explicit
Unicode string (so the character is correct whatever layout is active).

:mod:`typing_simulator.backends.pynput_backend` is kept as a fallback for
platforms or macOS versions where this is not the better choice.
"""

from __future__ import annotations

import logging

from typing_simulator.backends.base import TrackingKeyboardBackend
from typing_simulator.domain.events import NormalizedKey, SpecialKey
from typing_simulator.errors import KeyboardBackendError, MissingPermissionError

logger = logging.getLogger(__name__)

#: ``kVK_ANSI_*`` virtual key codes for the unshifted US QWERTY layout.
US_VIRTUAL_KEYS: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33,
    "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
}

#: Characters produced by holding Shift, mapped to their unshifted key.
US_SHIFTED_KEYS: dict[str, str] = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]",
    "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
}

#: ``kVK_*`` codes for the non-printable keys this prototype emits.
SPECIAL_VIRTUAL_KEYS: dict[SpecialKey, int] = {
    SpecialKey.ENTER: 36,
    SpecialKey.TAB: 48,
    SpecialKey.SPACE: 49,
    SpecialKey.BACKSPACE: 51,
    SpecialKey.SHIFT: 56,
}

#: ``kCGEventFlagMaskShift``
SHIFT_FLAG = 0x00020000


def virtual_key_for(key: NormalizedKey) -> tuple[int, bool, str | None]:
    """Resolve ``key`` to ``(virtual_key_code, needs_shift, unicode_text)``.

    ``unicode_text`` is ``None`` for keys that insert nothing.  Pure function:
    it imports nothing from Quartz, so it is directly unit-testable.
    """
    if key.special is not None:
        code = SPECIAL_VIRTUAL_KEYS.get(key.special)
        if code is None:  # pragma: no cover - the enum is closed
            raise KeyboardBackendError(f"Unsupported special key: {key.special.name}")
        return code, False, None

    char = key.char
    assert char is not None

    if char in US_SHIFTED_KEYS:
        return US_VIRTUAL_KEYS[US_SHIFTED_KEYS[char]], True, char
    if char.isupper() and char.lower() in US_VIRTUAL_KEYS:
        return US_VIRTUAL_KEYS[char.lower()], True, char
    if char in US_VIRTUAL_KEYS:
        return US_VIRTUAL_KEYS[char], False, char
    raise KeyboardBackendError(
        f"No US QWERTY key produces {char!r}; it should have been rejected "
        "during validation."
    )


class QuartzKeyboardBackend(TrackingKeyboardBackend):
    """Posts individual key events through ``CGEventPost``.

    Thread-safe by design: ``CGEventPost`` may be called from any thread, so
    the scheduler keeps running on its worker thread.
    """

    def __init__(self) -> None:
        super().__init__()
        try:
            import Quartz
        except Exception as exc:  # noqa: BLE001
            raise KeyboardBackendError(
                "The Quartz framework could not be loaded, so no key events "
                "can be emitted. Install pyobjc-framework-Quartz."
            ) from exc

        required = (
            "CGEventCreateKeyboardEvent",
            "CGEventKeyboardSetUnicodeString",
            "CGEventSetFlags",
            "CGEventPost",
        )
        missing = [name for name in required if not hasattr(Quartz, name)]
        if missing:
            raise KeyboardBackendError(
                "The Quartz keyboard API is incomplete (missing "
                f"{', '.join(missing)})."
            )
        self._quartz = Quartz
        self._tap = getattr(Quartz, "kCGHIDEventTap", 0)
        self._source = self._create_event_source(Quartz)

    @staticmethod
    def _create_event_source(quartz):
        """Build a private event source that does not fight the real hardware.

        Two defaults make synthetic typing feel broken without this:

        * Posting an event **suppresses local hardware events for 0.25 s** by
          default.  While typing continuously that window never closes, so the
          user's own mouse and keyboard appear frozen or jumpy for the whole
          run.  Setting the interval to zero, and permitting all events during
          suppression, leaves real input alone.
        * A ``NULL`` source combines with the *current* hardware modifier
          state, so a modifier the user happens to be holding leaks into every
          synthetic keystroke.  A private-state source is independent of it.

        Returns ``None`` if the source cannot be created; the backend then
        falls back to the default behaviour rather than failing.
        """
        try:
            state = getattr(quartz, "kCGEventSourceStatePrivate", -1)
            source = quartz.CGEventSourceCreate(state)
            if source is None:
                return None
            if hasattr(quartz, "CGEventSourceSetLocalEventsSuppressionInterval"):
                quartz.CGEventSourceSetLocalEventsSuppressionInterval(source, 0.0)
            if hasattr(quartz, "CGEventSourceSetLocalEventsFilterDuringSuppressionState"):
                permit_all = getattr(quartz, "kCGEventFilterMaskPermitAllEvents", 0xFFFFFFFF)
                for suppression_state in (
                    getattr(quartz, "kCGEventSuppressionStateSuppressionInterval", 0),
                    getattr(quartz, "kCGEventSuppressionStateRemoteMouseDrag", 1),
                ):
                    quartz.CGEventSourceSetLocalEventsFilterDuringSuppressionState(
                        source, permit_all, suppression_state
                    )
            return source
        except Exception:  # noqa: BLE001 - a missing tunable is not fatal
            logger.warning(
                "Could not create a private event source; the pointer may feel "
                "unresponsive while typing.",
                exc_info=True,
            )
            return None

    # -- TrackingKeyboardBackend hooks ------------------------------------
    def _press(self, key: NormalizedKey) -> None:
        self._post(key, down=True)

    def _release(self, key: NormalizedKey) -> None:
        self._post(key, down=False)

    def _post(self, key: NormalizedKey, *, down: bool) -> None:
        quartz = self._quartz
        code, needs_shift, text = virtual_key_for(key)
        try:
            event = quartz.CGEventCreateKeyboardEvent(self._source, code, down)
            if event is None:
                raise KeyboardBackendError(
                    f"macOS refused to create a key event for {key.describe()}."
                )
            # Always set the flags explicitly, never inherit them.  If the user
            # is still holding Control+Option from the start hotkey, an
            # inherited modifier would corrupt every character - and a
            # synthetic "p" carrying Control+Option would re-trigger the pause
            # hotkey through our own global monitor.
            quartz.CGEventSetFlags(event, SHIFT_FLAG if needs_shift else 0)
            if text is not None and down:
                # Only on key-down: key-up inserts nothing, and attaching a
                # string to it makes some applications insert twice.
                quartz.CGEventKeyboardSetUnicodeString(event, len(text), text)
            quartz.CGEventPost(self._tap, event)
        except KeyboardBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, key) from exc

    # -- errors ------------------------------------------------------------
    @staticmethod
    def _translate(exc: Exception, key: NormalizedKey | None = None) -> Exception:
        message = str(exc).lower()
        if "not trusted" in message or "accessibility" in message or "denied" in message:
            return MissingPermissionError(
                "macOS refused to deliver a key event. Grant Accessibility "
                "permission in System Settings → Privacy & Security, then "
                "fully quit and restart the application."
            )
        where = f" while emitting {key.describe()}" if key is not None else ""
        return KeyboardBackendError(
            f"The keyboard backend failed{where} ({type(exc).__name__}). Typing stopped."
        )
