"""Injectable clocks.

The scheduler never calls :func:`time.sleep` directly.  It asks a
:class:`Clock` to wait, and always hands over an :class:`threading.Event` that
can cut the wait short - this is what makes pause and abort responsive.
Tests substitute :class:`FakeClock` to get deterministic, instant runs.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; only differences are meaningful."""
        ...

    def sleep(self, seconds: float, wake: threading.Event) -> None:
        """Wait up to ``seconds``, returning early as soon as ``wake`` is set."""
        ...


class RealClock:
    """Wall-clock implementation used by the application."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float, wake: threading.Event) -> None:
        if seconds <= 0:
            return
        wake.wait(seconds)


class FakeClock:
    """Deterministic virtual clock for tests.

    Time only moves when :meth:`sleep` is called, and it moves instantly.
    ``on_sleep`` is invoked *before* each wait with ``(clock, seconds)``, which
    lets a test request a pause or an abort at a precise point in the run.
    """

    def __init__(
        self,
        start: float = 0.0,
        on_sleep: Callable[["FakeClock", float], None] | None = None,
    ) -> None:
        self._now = start
        self.on_sleep = on_sleep
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def sleep(self, seconds: float, wake: threading.Event) -> None:
        if seconds <= 0:
            return
        if self.on_sleep is not None:
            self.on_sleep(self, seconds)
        if wake.is_set():
            # Woken immediately: no virtual time passes, exactly like a real
            # wait that returns straight away.
            return
        self.sleeps.append(seconds)
        self._now += seconds
