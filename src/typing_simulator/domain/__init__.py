"""Immutable domain model: events, plans and the application state machine."""

from typing_simulator.domain.events import (
    Delay,
    Event,
    KeyDown,
    KeyUp,
    NormalizedKey,
    PlanStatistics,
    SpecialKey,
    TypingPlan,
    build_plan,
)
from typing_simulator.domain.state import ALLOWED_TRANSITIONS, AppState, StateMachine

__all__ = [
    "ALLOWED_TRANSITIONS",
    "AppState",
    "Delay",
    "Event",
    "KeyDown",
    "KeyUp",
    "NormalizedKey",
    "PlanStatistics",
    "SpecialKey",
    "StateMachine",
    "TypingPlan",
    "build_plan",
]
