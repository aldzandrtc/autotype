"""Local Typing Input Simulator.

A macOS-first prototype that accepts pasted text and emits it as individual
keyboard events into another, manually focused application.

The package is deliberately layered so that the probabilistic behavior
generator can later be swapped for a different implementation without
touching the user interface, scheduler, safety controller or keyboard
backend.  Every layer depends on the abstractions in
:mod:`typing_simulator.behavior.base` and
:mod:`typing_simulator.backends.base`, never on a concrete implementation.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
