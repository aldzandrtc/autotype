"""PySide6 interface layer.

Importing this package pulls in PySide6; the rest of the application does not
depend on Qt at all.
"""

__all__ = ["ControllerBridge", "OverlayWindow"]


def __getattr__(name: str):  # pragma: no cover - thin lazy re-export
    if name == "ControllerBridge":
        from typing_simulator.ui.worker import ControllerBridge

        return ControllerBridge
    if name == "OverlayWindow":
        from typing_simulator.ui.overlay_window import OverlayWindow

        return OverlayWindow
    raise AttributeError(name)
