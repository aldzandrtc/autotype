"""Shadow-buffer simulator.

A pure, in-memory model of a very simple text field.  It consumes the same
event sequence the scheduler would emit and reports the text that *would*
result.  Every plan is replayed through it before the application is allowed to
arm; if the replay does not match the requested text the plan is rejected and
nothing is typed.

This is a claim about the application's intended output only.  It says nothing
about what Google Docs, or any other application, actually received.
"""

from __future__ import annotations

from typing import Sequence

from typing_simulator.domain.events import (
    Delay,
    Event,
    KeyDown,
    KeyUp,
    NormalizedKey,
    SpecialKey,
    TypingPlan,
)
from typing_simulator.errors import PlanValidationError


class TextBuffer:
    """A minimal text buffer driven by key events.

    Rules:

    * a printable key inserts its character at the end of the buffer;
    * ``SPACE`` inserts ``" "``, ``ENTER`` inserts ``"\\n"``;
    * ``BACKSPACE`` removes the last character, and is a safe no-op when the
      buffer is empty;
    * ``SHIFT`` is tracked as a held modifier but inserts nothing (the
      generator emits final characters directly, so shift never affects the
      inserted text);
    * ``TAB`` is rejected - tab characters never reach a validated plan;
    * :class:`Delay` events are ignored;
    * insertion happens on key *down*; key *up* only releases the tracked key.
    """

    def __init__(self) -> None:
        self._chars: list[str] = []
        self._pressed: list[NormalizedKey] = []

    # -- state ------------------------------------------------------------
    @property
    def text(self) -> str:
        return "".join(self._chars)

    @property
    def pressed_keys(self) -> tuple[NormalizedKey, ...]:
        return tuple(self._pressed)

    def __len__(self) -> int:
        return len(self._chars)

    # -- event application ------------------------------------------------
    def apply(self, event: Event, *, strict: bool = True) -> None:
        if isinstance(event, Delay):
            return
        if isinstance(event, KeyDown):
            self._press(event.key, strict=strict)
            return
        if isinstance(event, KeyUp):
            self._release(event.key, strict=strict)
            return
        raise PlanValidationError(f"Unknown event type: {type(event).__name__}")

    def apply_all(self, events: Sequence[Event], *, strict: bool = True) -> None:
        for event in events:
            self.apply(event, strict=strict)
        if strict and self._pressed:
            held = ", ".join(k.describe() for k in self._pressed)
            raise PlanValidationError(f"Plan ends with keys still held down: {held}")

    # -- internals --------------------------------------------------------
    def _press(self, key: NormalizedKey, *, strict: bool) -> None:
        if strict and key in self._pressed:
            raise PlanValidationError(f"Key {key.describe()} pressed twice without release")
        self._pressed.append(key)

        if key.special is SpecialKey.BACKSPACE:
            if self._chars:  # backspace on an empty buffer is a safe no-op
                self._chars.pop()
            return
        if key.special is SpecialKey.SHIFT:
            return
        if key.special is SpecialKey.TAB:
            raise PlanValidationError("TAB is not supported by this prototype")
        inserted = key.inserted_text
        if inserted:
            self._chars.append(inserted)

    def _release(self, key: NormalizedKey, *, strict: bool) -> None:
        try:
            self._pressed.remove(key)
        except ValueError:
            if strict:
                raise PlanValidationError(
                    f"Key {key.describe()} released without a matching press"
                ) from None


def simulate(events: Sequence[Event], *, strict: bool = True) -> str:
    """Replay ``events`` and return the resulting text."""
    buffer = TextBuffer()
    buffer.apply_all(events, strict=strict)
    return buffer.text


def validate_plan(plan: TypingPlan) -> None:
    """Raise :class:`PlanValidationError` unless the plan reproduces its target.

    This is the gate every plan must pass before the application may arm.
    The error message deliberately reports only lengths and a position, never
    the pasted text itself.
    """
    produced = simulate(plan.events)
    expected = plan.expected_final_text
    if produced == expected:
        return

    divergence = next(
        (i for i, (a, b) in enumerate(zip(produced, expected)) if a != b),
        min(len(produced), len(expected)),
    )
    raise PlanValidationError(
        "Internal error: the generated plan does not reproduce the requested "
        f"text (produced {len(produced):,} characters, expected "
        f"{len(expected):,}; first difference at index {divergence:,}). "
        "Nothing was typed."
    )
