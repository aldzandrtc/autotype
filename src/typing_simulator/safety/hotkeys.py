"""Global hotkeys.

Two shortcuts work anywhere, whichever application is frontmost:

* ``Control + Option + P`` - pause or resume
* ``Control + Option + Escape`` - abort immediately

Both are *stop* controls, and that is the point: they have to work while the
user is in another application and typing is under way.  Starting is a
deliberate action taken from the overlay itself, so it has no shortcut.

The service **fails closed**: if the listener cannot be started and verified,
:meth:`start` raises and the application refuses to type.

Two implementations exist.  :class:`NSEventHotkeyService` is the default on
macOS and delivers on the **main thread** via AppKit event monitors.
:class:`PynputHotkeyService` is the portable fallback; on current macOS it must
not be used, because ``pynput``'s listener resolves key codes through
HIToolbox's Text Input Source APIs on its own tap thread, and those functions
now assert they are on the main dispatch queue - which terminates the process
with ``SIGTRAP`` the moment a key is pressed.  See
:mod:`typing_simulator.backends.quartz_backend` for the same problem on the
emission side.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Protocol, runtime_checkable

from typing_simulator import config
from typing_simulator.errors import HotkeyError, MissingPermissionError
from typing_simulator.safety.permissions import (
    accessibility_permission_granted,
    input_monitoring_permission_granted,
    permission_holder_name,
)

logger = logging.getLogger(__name__)

#: How long to wait for the listener thread to report that it is running.
_START_TIMEOUT_SECONDS = 3.0


@runtime_checkable
class HotkeyService(Protocol):
    #: Whether this implementation needs ``kTCCServiceListenEvent``.  Which
    #: permission actually gates a global hotkey depends entirely on how it is
    #: implemented, so each implementation says so rather than the caller
    #: guessing - guessing is what refused to type over a permission that was
    #: never needed.
    requires_input_monitoring: bool

    def start(self) -> None:
        """Start the listener, or raise if it cannot be verified as running."""
        ...

    def stop(self) -> None: ...

    @property
    def is_active(self) -> bool: ...


#: ``NSEventModifierFlag*`` bits, and the device-independent mask that filters
#: out the hardware-specific noise in ``modifierFlags()``.
_MODIFIER_CONTROL = 1 << 18
_MODIFIER_OPTION = 1 << 19
_MODIFIER_MASK = 0xFFFF0000

#: ``NSEventMaskKeyDown``
_MASK_KEY_DOWN = 1 << 10

#: ``kVK_*`` codes used to recognise the shortcuts layout-independently.
_VK_ESCAPE = 53
_VK_P = 35


def _require_accessibility_for_monitors() -> None:
    """The gate for ``NSEvent`` global monitors, which is *Accessibility*.

    This is what makes the fail-closed promise real.  Installing a global
    monitor *succeeds* whatever the permissions say -
    ``addGlobalMonitorForEventsMatchingMask_handler_`` hands back a perfectly
    valid token - and the handler is then simply never called.  Checking only
    the return value therefore reports working hotkeys while the abort shortcut
    is dead, which is the one failure this application must never have: the
    user would be mid-run with no way to stop it from another application.

    The permission to check is Accessibility, not Input Monitoring.  Apple
    documents key-event global monitors as requiring the process to be trusted
    for accessibility; ``kTCCServiceListenEvent`` gates ``CGEventTap``, which
    this implementation does not use.  Preflighting Input Monitoring here
    refuses to type over a permission the hotkeys never needed - and, because
    that preflight answers from process start-up and never updates, refuses
    even after the user has granted it.
    """
    if accessibility_permission_granted() is not False:
        return
    raise MissingPermissionError(
        "The global pause and abort hotkeys need Accessibility permission, "
        "which macOS has not granted, so they would never fire. Enable "
        f"{permission_holder_name()} under System Settings → Privacy & "
        "Security → Accessibility. Typing is refused until "
        f"{config.HOTKEY_ABORT_LABEL} can actually stop it."
    )


def _require_input_monitoring() -> None:
    """The gate for ``CGEventTap`` listeners, which is *Input Monitoring*.

    Used by the ``pynput`` fallback only.  Input Monitoring is its own switch
    in System Settings and Accessibility does not imply it, so the message
    names it specifically - telling someone to grant a permission they already
    granted is exactly how this gets diagnosed as "it says permission is
    missing but it isn't".
    """
    if input_monitoring_permission_granted() is not False:
        return
    raise MissingPermissionError(
        "The global pause and abort hotkeys need Input Monitoring permission, "
        "which macOS has not granted. This is a separate switch from "
        "Accessibility - having Accessibility on is not enough. Open System "
        "Settings → Privacy & Security → Input Monitoring, enable "
        f"{permission_holder_name()}, then fully quit it (Cmd+Q) and start it "
        "again. Typing is refused until "
        f"{config.HOTKEY_ABORT_LABEL} can actually stop it."
    )


class NSEventHotkeyService:
    """Global hotkeys via AppKit event monitors.

    The handlers run on the **main thread**, as part of the normal Cocoa event
    delivery that Qt already pumps.  Nothing touches a background thread, so
    the HIToolbox main-queue assertions that crash ``pynput``'s listener cannot
    be reached.

    Two monitors are installed: a *global* one for when another application is
    frontmost (the normal case for this overlay), and a *local* one for when
    the overlay itself has focus.  The local handler passes the event through
    unchanged so it does not swallow ordinary typing.

    :meth:`start` must be called from the main thread.
    """

    #: ``NSEvent`` monitors are gated by Accessibility, not Input Monitoring.
    requires_input_monitoring = False

    def __init__(
        self,
        on_pause_resume: Callable[[], None],
        on_abort: Callable[[], None],
    ) -> None:
        self._on_pause_resume = on_pause_resume
        self._on_abort = on_abort
        self._monitors: list[object] = []
        self._lock = threading.RLock()

    @property
    def is_active(self) -> bool:
        with self._lock:
            return bool(self._monitors)

    def start(self) -> None:
        with self._lock:
            if self._monitors:
                return
            _require_accessibility_for_monitors()
            try:
                from AppKit import NSEvent
            except Exception as exc:  # noqa: BLE001
                raise HotkeyError(
                    "AppKit could not be loaded, so the global hotkeys are "
                    "unavailable. Typing is disabled."
                ) from exc

            def global_handler(event) -> None:
                self._dispatch(event)

            def local_handler(event):
                self._dispatch(event)
                return event  # never swallow normal typing

            try:
                monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                    _MASK_KEY_DOWN, global_handler
                )
                local = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                    _MASK_KEY_DOWN, local_handler
                )
            except Exception as exc:  # noqa: BLE001
                raise HotkeyError(
                    "macOS refused to install the global hotkey monitor "
                    f"({type(exc).__name__}). Typing is disabled."
                ) from exc

            if monitor is None:
                raise MissingPermissionError(
                    "macOS refused to install the global hotkey monitor. Grant "
                    f"{permission_holder_name()} Input Monitoring in System "
                    "Settings → Privacy & Security, then fully quit and "
                    "restart the application."
                )
            self._monitors = [m for m in (monitor, local) if m is not None]
            logger.info("Global hotkeys active: %s, %s", *_labels())

    def stop(self) -> None:
        with self._lock:
            monitors, self._monitors = self._monitors, []
        if not monitors:
            return
        try:
            from AppKit import NSEvent

            for monitor in monitors:
                NSEvent.removeMonitor_(monitor)
        except Exception:  # noqa: BLE001 - shutdown must never raise
            logger.debug("Removing a hotkey monitor failed", exc_info=True)

    # -- dispatch ----------------------------------------------------------
    def _dispatch(self, event) -> None:
        """Match one key-down against the shortcuts.  Never raises."""
        try:
            flags = event.modifierFlags() & _MODIFIER_MASK
            if not (flags & _MODIFIER_CONTROL and flags & _MODIFIER_OPTION):
                return
            code = int(event.keyCode())
            if code == _VK_ESCAPE:
                self._invoke(self._on_abort, "abort")
            elif code == _VK_P or self._characters(event) == "p":
                self._invoke(self._on_pause_resume, "pause/resume")
        except Exception:  # noqa: BLE001 - a bad event must not break delivery
            logger.exception("Hotkey dispatch failed")

    @staticmethod
    def _characters(event) -> str:
        try:
            characters = event.charactersIgnoringModifiers()
        except Exception:  # noqa: BLE001
            return ""
        return str(characters).lower() if characters else ""

    @staticmethod
    def _invoke(callback: Callable[[], None] | None, label: str) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001
            logger.exception("The %s hotkey handler failed", label)


class PynputHotkeyService:
    """``pynput``-backed global hotkey listener (portable fallback).

    .. warning::
       Do not use this on current macOS.  Its listener thread resolves key
       codes through HIToolbox, which asserts it is on the main dispatch queue
       and terminates the process with ``SIGTRAP``.  Use
       :class:`NSEventHotkeyService` instead.
    """

    #: ``pynput`` installs a ``CGEventTap``, which Input Monitoring gates.
    requires_input_monitoring = True

    def __init__(
        self,
        on_pause_resume: Callable[[], None],
        on_abort: Callable[[], None],
    ) -> None:
        self._on_pause_resume = on_pause_resume
        self._on_abort = on_abort
        self._listener = None
        self._lock = threading.RLock()

    @property
    def is_active(self) -> bool:
        with self._lock:
            listener = self._listener
            return bool(listener is not None and listener.running)

    def start(self) -> None:
        with self._lock:
            if self.is_active:
                return
            _require_input_monitoring()
            try:
                from pynput import keyboard
            except Exception as exc:  # noqa: BLE001
                raise HotkeyError(
                    "The pynput keyboard library could not be loaded, so the "
                    "global hotkeys are unavailable. Typing is disabled."
                ) from exc

            bindings = {
                config.HOTKEY_PAUSE_RESUME: self._safely(self._on_pause_resume, "pause/resume"),
                config.HOTKEY_ABORT: self._safely(self._on_abort, "abort"),
            }
            try:
                listener = keyboard.GlobalHotKeys(bindings)
                listener.start()
                listener.wait()
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc) from exc

            deadline = threading.Event()
            waited = 0.0
            while not listener.running and waited < _START_TIMEOUT_SECONDS:
                deadline.wait(0.05)
                waited += 0.05
            if not listener.running:
                try:
                    listener.stop()
                except Exception:  # noqa: BLE001
                    logger.debug("Stopping a non-running listener failed", exc_info=True)
                raise HotkeyError(
                    "The global hotkey listener did not start within "
                    f"{_START_TIMEOUT_SECONDS:.0f} seconds. Typing is disabled."
                )
            self._listener = listener
            logger.info("Global hotkeys active: %s, %s", *_labels())

    def stop(self) -> None:
        with self._lock:
            listener, self._listener = self._listener, None
        if listener is None:
            return
        try:
            listener.stop()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            logger.debug("Stopping the hotkey listener failed", exc_info=True)

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _safely(callback: Callable[[], None], label: str) -> Callable[[], None]:
        """Wrap a hotkey callback so a failure cannot kill the listener thread."""

        def wrapped() -> None:
            try:
                callback()
            except Exception:  # noqa: BLE001
                logger.exception("The %s hotkey handler failed", label)

        return wrapped

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        message = str(exc).lower()
        if "not trusted" in message or "accessibility" in message:
            return MissingPermissionError(
                "macOS refused to install the global hotkey listener. Grant "
                "Accessibility (and Input Monitoring) permission in System "
                "Settings -> Privacy & Security, then restart the application."
            )
        return HotkeyError(
            "The global hotkey listener could not be started "
            f"({type(exc).__name__}). Typing is disabled until "
            f"{config.HOTKEY_PAUSE_RESUME_LABEL} and "
            f"{config.HOTKEY_ABORT_LABEL} are available."
        )


def _labels() -> tuple[str, str]:
    return config.HOTKEY_PAUSE_RESUME_LABEL, config.HOTKEY_ABORT_LABEL

