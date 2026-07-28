"""The explicit application state machine.

Allowed transitions
-------------------
=============  ==========================================================
From           To
=============  ==========================================================
IDLE           VALIDATING
VALIDATING     ARMING (plan accepted), IDLE (recoverable validation
               failure), ERROR (internal failure)
ARMING         ARMED (focus settled on the target), ABORTED (cancelled while
               waiting), ERROR (focus never matched)
ARMED          RUNNING, ABORTED, ERROR
RUNNING        PAUSED, COMPLETED, ABORTED, ERROR
PAUSED         RUNNING, ABORTED, ERROR
COMPLETED      IDLE (reset)
ABORTED        IDLE (reset)
ERROR          IDLE (reset)
=============  ==========================================================

Any transition not listed above is rejected with
:class:`~typing_simulator.errors.InvalidStateTransitionError`; the state is
never changed silently.
"""

from __future__ import annotations

import threading
from enum import Enum
from typing import Callable

from typing_simulator.errors import InvalidStateTransitionError


class AppState(Enum):
    IDLE = "idle"
    VALIDATING = "validating"
    ARMING = "arming"
    ARMED = "armed"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"
    ERROR = "error"

    @property
    def is_active(self) -> bool:
        """True while a job is in flight and cleanup would be required."""
        return self in _ACTIVE_STATES

    @property
    def is_terminal(self) -> bool:
        return self in (AppState.COMPLETED, AppState.ABORTED, AppState.ERROR)


_ACTIVE_STATES = frozenset(
    {AppState.VALIDATING, AppState.ARMING, AppState.ARMED, AppState.RUNNING, AppState.PAUSED}
)


ALLOWED_TRANSITIONS: dict[AppState, frozenset[AppState]] = {
    AppState.IDLE: frozenset({AppState.VALIDATING}),
    AppState.VALIDATING: frozenset({AppState.ARMING, AppState.IDLE, AppState.ERROR}),
    AppState.ARMING: frozenset({AppState.ARMED, AppState.ABORTED, AppState.ERROR}),
    AppState.ARMED: frozenset({AppState.RUNNING, AppState.ABORTED, AppState.ERROR}),
    AppState.RUNNING: frozenset(
        {AppState.PAUSED, AppState.COMPLETED, AppState.ABORTED, AppState.ERROR}
    ),
    AppState.PAUSED: frozenset({AppState.RUNNING, AppState.ABORTED, AppState.ERROR}),
    AppState.COMPLETED: frozenset({AppState.IDLE}),
    AppState.ABORTED: frozenset({AppState.IDLE}),
    AppState.ERROR: frozenset({AppState.IDLE}),
}


StateListener = Callable[[AppState, AppState], None]


class StateMachine:
    """Thread-safe holder of the current :class:`AppState`.

    Listeners are invoked outside the lock so a listener may query the machine
    without deadlocking.
    """

    def __init__(self, initial: AppState = AppState.IDLE) -> None:
        self._state = initial
        self._lock = threading.RLock()
        self._listeners: list[StateListener] = []

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    def add_listener(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    def can_transition_to(self, target: AppState) -> bool:
        with self._lock:
            return target in ALLOWED_TRANSITIONS[self._state]

    def transition_to(self, target: AppState) -> AppState:
        """Move to ``target`` or raise :class:`InvalidStateTransitionError`."""
        with self._lock:
            current = self._state
            if target not in ALLOWED_TRANSITIONS[current]:
                raise InvalidStateTransitionError(current, target)
            self._state = target
        for listener in list(self._listeners):
            listener(current, target)
        return target

    def require(self, *states: AppState) -> AppState:
        """Assert the machine is in one of ``states``; return the state."""
        with self._lock:
            if self._state not in states:
                expected = " or ".join(s.name for s in states)
                raise InvalidStateTransitionError(self._state, expected)
            return self._state
