"""Native macOS configuration for the floating overlay.

Qt alone cannot express what this overlay needs.  Two AppKit settings do the
real work:

* **Accessory activation policy** - the process gets no Dock icon and no menu
  bar, and does not become the active application just because one of its
  windows appears.
* **Non-activating panel style** - clicking the overlay's buttons does *not*
  activate this application, so the document the user was editing keeps
  keyboard focus and the caret stays exactly where they put it.

Without the second setting, clicking Start would steal focus and the frontmost
application would be the overlay itself.  That case is still caught by the
controller's self-target refusal, so a failure here is safe: it degrades to a
clear error instead of typing into the wrong place.

Every function fails soft and returns whether it worked.
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

#: ``NSApplicationActivationPolicyAccessory``
_ACTIVATION_POLICY_ACCESSORY = 1

#: ``NSWindowStyleMaskNonactivatingPanel``
_STYLE_MASK_NONACTIVATING_PANEL = 1 << 7

#: ``NSFloatingWindowLevel``
_FLOATING_WINDOW_LEVEL = 3

#: ``NSWindowCollectionBehaviorCanJoinAllSpaces``
_BEHAVIOR_CAN_JOIN_ALL_SPACES = 1 << 0

#: ``NSWindowCollectionBehaviorFullScreenAuxiliary`` - lets the overlay appear
#: over another application's full-screen window.
_BEHAVIOR_FULL_SCREEN_AUXILIARY = 1 << 8


def use_accessory_activation_policy() -> bool:
    """Hide the Dock icon and stop the app from activating on its own."""
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSApplication

        NSApplication.sharedApplication().setActivationPolicy_(
            _ACTIVATION_POLICY_ACCESSORY
        )
        return True
    except Exception:  # noqa: BLE001 - a cosmetic failure must not stop startup
        logger.warning("Could not set the accessory activation policy", exc_info=True)
        return False


#: ``NSViewWidthSizable | NSViewHeightSizable``
_AUTORESIZE_BOTH = 2 | 16


def _native_view(window):
    """The window's ``NSView``, or ``None`` when there is not one.

    ``winId()`` is only an ``NSView`` pointer under the Cocoa platform plugin.
    Under any other plugin - ``offscreen`` in tests, or ``minimal`` - it is a
    different handle entirely, and handing it to ``objc`` segfaults the
    process.  Every native call here goes through this check first.
    """
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None or app.platformName() != "cocoa":
            return None
        import objc

        handle = int(window.winId())
        if handle == 0:
            return None
        return objc.objc_object(c_void_p=ctypes.c_void_p(handle))
    except Exception:  # noqa: BLE001
        logger.debug("No native view available for this window", exc_info=True)
        return None


# A note on background blur
# -------------------------
# An ``NSVisualEffectView`` behind the panel would give a true system blur, but
# there is no safe place to put one.  Qt's own ``NSView`` *is* the window's
# content view, so adding the effect view to it makes it a child - and children
# draw above their parent, covering the whole interface with a blank rectangle.
# Re-parenting Qt's view under a container so the effect can sit beneath it does
# render, but it takes the view out from under Qt's ownership: the window then
# logs "Already setting window visible!" and segfaults on close.
#
# The glass look is therefore done entirely in the stylesheet - a translucent
# gradient, a bright rim and frosted inner surfaces (see
# :mod:`typing_simulator.ui.theme`).  That is stable, testable, and renders the
# same everywhere.


def configure_overlay_window(window) -> bool:
    """Turn a shown Qt window into a floating, non-activating panel.

    The window must already be shown: ``winId()`` only resolves to a real
    ``NSView`` once the native window exists.
    """
    view = _native_view(window)
    if view is None:
        return False
    try:
        ns_window = view.window()
        if ns_window is None:
            return False

        ns_window.setLevel_(_FLOATING_WINDOW_LEVEL)
        ns_window.setCollectionBehavior_(
            _BEHAVIOR_CAN_JOIN_ALL_SPACES | _BEHAVIOR_FULL_SCREEN_AUXILIARY
        )

        if hasattr(ns_window, "setStyleMask_"):
            ns_window.setStyleMask_(
                ns_window.styleMask() | _STYLE_MASK_NONACTIVATING_PANEL
            )
        if hasattr(ns_window, "setFloatingPanel_"):
            ns_window.setFloatingPanel_(True)
        if hasattr(ns_window, "setBecomesKeyOnlyIfNeeded_"):
            # Clicking a button does not take focus; clicking the text editor
            # does, so the user can still paste into the overlay.
            ns_window.setBecomesKeyOnlyIfNeeded_(True)
        if hasattr(ns_window, "setHidesOnDeactivate_"):
            ns_window.setHidesOnDeactivate_(False)
        return True
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not configure the native overlay panel; clicking Start may "
            "steal focus. The self-target check will catch that safely.",
            exc_info=True,
        )
        return False
