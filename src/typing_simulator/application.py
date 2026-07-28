"""Application wiring.

Builds the concrete implementations, hands them to the safety controller, and
opens the window.  This is the only module that knows which concrete backend,
focus guard and behavior generator are in use - swapping any of them happens
here and nowhere else.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass

from typing_simulator import config
from typing_simulator.backends.base import KeyboardBackend
from typing_simulator.backends.recording_backend import RecordingKeyboardBackend
from typing_simulator.behavior.probabilistic import ProbabilisticBehaviorGenerator
from typing_simulator.errors import TypingSimulatorError
from typing_simulator.safety.controller import SafetyController
from typing_simulator.safety.focus_guard import create_focus_guard

logger = logging.getLogger(__name__)


#: Where a bundled build writes its log.  Only used when frozen; from source
#: the launching terminal already shows everything.
LOG_DIRECTORY = "~/Library/Logs/Typing Simulator"
LOG_FILE_NAME = "typing-simulator.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 2

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """Developer diagnostics only.

    The pasted text is never passed to the logging system anywhere in this
    application, so no log line can contain it.

    ``faulthandler`` is enabled here as well: this application drives native
    frameworks from worker threads, and a native assertion would otherwise kill
    the process with a bare "trace trap" and no indication of where. With it
    on, the offending Python stack is printed first.
    """
    logging.basicConfig(level=level, format=LOG_FORMAT)
    _add_file_handler_when_bundled(level)
    try:
        faulthandler.enable()
    except Exception:  # noqa: BLE001 - diagnostics must never block startup
        logger.debug("Could not enable faulthandler", exc_info=True)


def _add_file_handler_when_bundled(level: int) -> None:
    """Give a Finder-launched build somewhere to put its diagnostics.

    An app opened from Finder has no terminal attached, so everything written
    to stderr is discarded.  That is exactly backwards for this application:
    the failures worth diagnosing - a permission that is granted but not in
    effect, focus never settling on the target - are the silent ones, and the
    log is the only place they are visible at all.
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        from logging.handlers import RotatingFileHandler

        directory = os.path.expanduser(LOG_DIRECTORY)
        os.makedirs(directory, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(directory, LOG_FILE_NAME),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.setLevel(level)
        logging.getLogger().addHandler(handler)
        logger.info("Logging to %s", os.path.join(directory, LOG_FILE_NAME))
    except Exception:  # noqa: BLE001 - a missing log must never block startup
        logger.debug("Could not open the log file", exc_info=True)


@dataclass(slots=True)
class BuiltApplication:
    controller: SafetyController
    backend: KeyboardBackend
    backend_error: str | None
    permission_granted: bool | None


def _create_backend() -> tuple[KeyboardBackend, str | None]:
    """Pick the best available keyboard backend for this platform.

    On macOS this is the Quartz backend: ``pynput``'s macOS implementation
    calls HIToolbox from worker threads, which current macOS turns into a
    ``SIGTRAP``.  Elsewhere ``pynput`` is still the portable choice.
    """
    errors: list[str] = []

    if sys.platform == "darwin":
        try:
            from typing_simulator.backends.quartz_backend import QuartzKeyboardBackend

            return QuartzKeyboardBackend(), None
        except TypingSimulatorError as exc:
            logger.error("Quartz backend unavailable: %s", exc.user_message)
            errors.append(exc.user_message)

    try:
        from typing_simulator.backends.pynput_backend import PynputKeyboardBackend

        return PynputKeyboardBackend(), None
    except TypingSimulatorError as exc:
        logger.error("pynput backend unavailable: %s", exc.user_message)
        errors.append(exc.user_message)

    # A recording backend keeps the window usable, but Start stays disabled:
    # the interface must never look like it is typing when it cannot.
    return RecordingKeyboardBackend(), " ".join(errors)


def _create_hotkey_service(pause_resume, abort):
    if sys.platform == "darwin":
        from typing_simulator.safety.hotkeys import NSEventHotkeyService

        return NSEventHotkeyService(on_pause_resume=pause_resume, on_abort=abort)
    from typing_simulator.safety.hotkeys import PynputHotkeyService

    return PynputHotkeyService(on_pause_resume=pause_resume, on_abort=abort)


def build_controller(callbacks) -> BuiltApplication:
    """Assemble the controller with the real backend, focus guard and hotkeys."""
    backend, backend_error = _create_backend()

    holder: dict[str, SafetyController] = {}

    def pause_resume() -> None:
        controller = holder.get("controller")
        if controller is not None:
            controller.toggle_pause()

    def abort() -> None:
        controller = holder.get("controller")
        if controller is not None:
            controller.abort(
                f"Aborted with {config.HOTKEY_ABORT_LABEL}."
            )

    controller = SafetyController(
        generator=ProbabilisticBehaviorGenerator(),
        backend=backend,
        focus_guard=create_focus_guard(),
        hotkeys=_create_hotkey_service(pause_resume, abort),
        callbacks=callbacks,
    )
    holder["controller"] = controller

    # The first thing anyone diagnosing this application needs to know, and the
    # thing that is invisible without it: which permissions are actually in
    # effect, and who macOS thinks they belong to.
    status = controller.permission_status()
    logger.info(
        "Permissions: accessibility=%s post_events=%s input_monitoring=%s; "
        "granted to %s (bundled=%s)%s",
        status.accessibility,
        status.post_events,
        status.input_monitoring,
        status.subject.describe(),
        status.subject.bundled,
        "; the grant looks stale" if status.stale_grant_suspected else "",
    )

    return BuiltApplication(
        controller=controller,
        backend=backend,
        backend_error=backend_error,
        permission_granted=controller.check_permission(),
    )


def run(argv: list[str] | None = None) -> int:
    """Open the overlay and run the Qt event loop."""
    configure_logging()

    from PySide6.QtWidgets import QApplication

    from typing_simulator.ui.macos_overlay import (
        configure_overlay_window,
        use_accessory_activation_policy,
    )
    from typing_simulator.ui.overlay_window import OverlayWindow
    from typing_simulator.ui.worker import ControllerBridge

    if sys.platform != "darwin":
        logger.warning(
            "This prototype is macOS-first; focus capture, the global hotkeys "
            "and the non-activating overlay are not supported on %s.",
            sys.platform,
        )

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Local Typing Input Simulator")
    app.setOrganizationName("Local Typing Input Simulator")
    # No Dock icon, and the overlay never activates this app on its own.
    use_accessory_activation_policy()

    bridge = ControllerBridge()
    built = build_controller(bridge.callbacks())

    window = OverlayWindow(
        built.controller,
        bridge,
        backend_error=built.backend_error,
        permission_granted=built.permission_granted,
    )
    window.show()
    # Must happen after show(): the native window only exists from then on.
    if not configure_overlay_window(window):
        logger.info(
            "The overlay is running without native panel behavior; clicking "
            "Start may move focus. The controller hands focus back to the "
            "previous application before typing."
        )

    _install_interrupt_handler(app)

    try:
        return app.exec()
    finally:
        built.controller.shutdown()


def _install_interrupt_handler(app) -> None:
    """Make Ctrl+C in the launching terminal actually quit.

    Two things are needed, and both matter:

    * Qt's event loop blocks inside C++, so Python never reaches a bytecode
      boundary and the SIGINT handler never runs at all.  A repeating timer
      hands control back to the interpreter often enough for it to fire.
    * The handler itself must do **nothing but set a flag**.  Python runs a
      signal handler at whatever bytecode boundary it happens to reach, which
      is routinely in the middle of a Qt callback; quitting or closing windows
      from there re-enters Qt's event dispatch and segfaults the process.  The
      actual shutdown happens from the timer slot, which the event loop only
      calls at a safe point.
    """
    from PySide6.QtCore import QTimer

    interrupted = threading.Event()

    def handle_interrupt(_signum, _frame) -> None:
        interrupted.set()

    def poll() -> None:
        if interrupted.is_set():
            logger.info("Interrupted; shutting down.")
            app.quit()  # run()'s `finally` performs the cleanup

    signal.signal(signal.SIGINT, handle_interrupt)
    signal.signal(signal.SIGTERM, handle_interrupt)

    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(120)
    app._interrupt_timer = timer  # keep a reference alive  # noqa: SLF001
