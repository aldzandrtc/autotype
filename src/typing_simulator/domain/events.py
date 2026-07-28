"""Immutable event types and the typing plan.

Unit convention
---------------
**Every duration in the core is expressed in seconds as a ``float``.**
Milliseconds appear only in user-facing strings and in the scheduler's tick
constant documentation.  Nothing inside the core converts units.

A plan is produced in full *before* any physical key is emitted.  That makes
the behavior reproducible (fixed seed), testable (the shadow buffer can replay
it) and directly comparable with a future non-probabilistic generator.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence


class SpecialKey(Enum):
    """Non-printable keys the prototype knows how to emit.

    ``TAB`` and ``SHIFT`` are part of the normalized vocabulary and are
    supported by the backends, but the first-prototype generator never emits
    them: tab characters are rejected during validation (see
    :mod:`typing_simulator.behavior.keyboard_map`) and uppercase / shifted
    characters are emitted as their final character rather than as an explicit
    modifier sequence, which is what the platform key controllers expect.
    """

    ENTER = "enter"
    BACKSPACE = "backspace"
    SPACE = "space"
    TAB = "tab"
    SHIFT = "shift"


@dataclass(frozen=True, slots=True)
class NormalizedKey:
    """A platform-independent key identity.

    Exactly one of ``char`` (a single printable character) or ``special``
    (a :class:`SpecialKey`) is set.
    """

    char: str | None = None
    special: SpecialKey | None = None

    def __post_init__(self) -> None:
        if (self.char is None) == (self.special is None):
            raise ValueError("NormalizedKey needs exactly one of char or special")
        if self.char is not None and len(self.char) != 1:
            raise ValueError("NormalizedKey.char must be exactly one character")

    # -- constructors ------------------------------------------------------
    @classmethod
    def printable(cls, char: str) -> "NormalizedKey":
        return cls(char=char)

    @classmethod
    def of(cls, special: SpecialKey) -> "NormalizedKey":
        return cls(special=special)

    # -- queries -----------------------------------------------------------
    @property
    def is_printable(self) -> bool:
        return self.char is not None

    @property
    def produces_text(self) -> bool:
        """True when pressing this key inserts a character into a document."""
        return self.char is not None or self.special in (
            SpecialKey.ENTER,
            SpecialKey.SPACE,
        )

    @property
    def inserted_text(self) -> str:
        """The text this key inserts, or ``""`` for non-inserting keys."""
        if self.char is not None:
            return self.char
        if self.special is SpecialKey.ENTER:
            return "\n"
        if self.special is SpecialKey.SPACE:
            return " "
        return ""

    def describe(self) -> str:
        if self.char is not None:
            return repr(self.char)
        assert self.special is not None
        return self.special.name


@dataclass(frozen=True, slots=True)
class KeyDown:
    """Press a key."""

    key: NormalizedKey


@dataclass(frozen=True, slots=True)
class KeyUp:
    """Release a key."""

    key: NormalizedKey


@dataclass(frozen=True, slots=True)
class Delay:
    """Wait for ``seconds`` before the next event."""

    seconds: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.seconds) or self.seconds < 0:
            raise ValueError("Delay.seconds must be a finite, non-negative float")


#: Union of every event kind the scheduler and shadow buffer understand.
Event = KeyDown | KeyUp | Delay


@dataclass(frozen=True, slots=True)
class PlanStatistics:
    """Summary statistics for a generated plan."""

    characters: int
    key_down_events: int
    delay_events: int
    deliberate_errors: int
    correction_events: int
    backspaces: int
    effective_wpm: float

    def summary(self) -> str:
        return (
            f"{self.characters:,} chars - {self.key_down_events:,} key presses - "
            f"{self.deliberate_errors} deliberate mistakes - "
            f"{self.correction_events} corrections "
            f"({self.backspaces} backspaces) - ~{self.effective_wpm:.0f} WPM"
        )


@dataclass(frozen=True, slots=True)
class TypingPlan:
    """A complete, reproducible description of one typing run."""

    target_text: str
    """The normalized text the user asked for."""

    events: tuple[Event, ...]
    """Ordered event sequence, generated before any key is emitted."""

    seed: int
    """The seed that produced this plan; replaying it reproduces the plan."""

    estimated_duration: float
    """Sum of every :class:`Delay` in ``events``, in seconds."""

    expected_final_text: str
    """What the shadow buffer must produce; equal to ``target_text``."""

    statistics: PlanStatistics = field(compare=False)

    @property
    def total_events(self) -> int:
        return len(self.events)

    def remaining_durations(self) -> tuple[float, ...]:
        """Suffix sums of delays, for "estimated time remaining" reporting.

        ``result[i]`` is the total delay contained in ``events[i:]``.
        """
        suffix: list[float] = [0.0] * (len(self.events) + 1)
        for index in range(len(self.events) - 1, -1, -1):
            event = self.events[index]
            extra = event.seconds if isinstance(event, Delay) else 0.0
            suffix[index] = suffix[index + 1] + extra
        return tuple(suffix)


def total_delay(events: Iterable[Event]) -> float:
    """Exact sum of every delay in ``events`` (seconds).

    ``math.fsum`` is used both here and in the tests so that
    ``plan.estimated_duration == total_delay(plan.events)`` holds exactly.
    """
    return math.fsum(e.seconds for e in events if isinstance(e, Delay))


def build_plan(
    *,
    target_text: str,
    events: Sequence[Event],
    seed: int,
    deliberate_errors: int,
    correction_events: int,
) -> TypingPlan:
    """Assemble a :class:`TypingPlan`, deriving duration and statistics."""
    events = tuple(events)
    duration = total_delay(events)
    key_downs = [e for e in events if isinstance(e, KeyDown)]
    backspaces = sum(1 for e in key_downs if e.key.special is SpecialKey.BACKSPACE)
    delay_events = sum(1 for e in events if isinstance(e, Delay))
    characters = len(target_text)
    effective_wpm = (characters / 5.0) / (duration / 60.0) if duration > 0 else 0.0
    statistics = PlanStatistics(
        characters=characters,
        key_down_events=len(key_downs),
        delay_events=delay_events,
        deliberate_errors=deliberate_errors,
        correction_events=correction_events,
        backspaces=backspaces,
        effective_wpm=effective_wpm,
    )
    return TypingPlan(
        target_text=target_text,
        events=events,
        seed=seed,
        estimated_duration=duration,
        expected_final_text=target_text,
        statistics=statistics,
    )
