"""The first-prototype behavior generator.

Produces a complete event plan from text plus settings using a seeded
random-number generator, so the same seed and settings always yield exactly
the same plan.

Timing model
------------
The baseline keydown-to-keydown interval comes from the target speed::

    baseline = 60 / (WPM * 5)

Each keystroke's delay is then::

    delay = baseline * burst_multiplier * jitter * context_multiplier (+ hesitation)

* ``burst_multiplier`` follows an AR(1) process in log space, so consecutive
  characters share a coherent fast or slow burst rather than each delay being
  drawn independently from one distribution.
* ``jitter`` is independent log-normal noise, scaled by the variation level.
* ``context_multiplier`` lengthens the pause after spaces, after commas and
  semicolons, after sentence-ending punctuation, and around newlines.
* ``hesitation`` is an occasional extra pause.

Every result is clamped into ``[MIN_DELAY_SECONDS, MAX_DELAY_SECONDS]``, so no
delay can be negative or unreasonable regardless of what was sampled.

Mistakes
--------
Only three kinds of mistake are produced - adjacent-key substitution,
accidental duplicate, and transposed adjacent letters - and each one is always
followed by a correction sequence, so the simulated buffer still ends up equal
to the requested text.  Mistake *selection* and correction *planning* happen
here; nothing in this module touches a real keyboard.
"""

from __future__ import annotations

import math
import secrets
from dataclasses import dataclass
from enum import Enum
from random import Random

from typing_simulator import config
from typing_simulator.behavior.keyboard_map import (
    BACKSPACE_KEY,
    adjacent_letters,
    key_for_character,
    normalize_line_endings,
    normalize_text,
)
from typing_simulator.config import TypingSettings
from typing_simulator.domain.events import Delay, Event, KeyDown, KeyUp, TypingPlan, build_plan
from typing_simulator.errors import EmptyInputError, InputTooLongError


class TypoKind(Enum):
    SUBSTITUTION = "substitution"
    DUPLICATE = "duplicate"
    TRANSPOSITION = "transposition"


@dataclass(frozen=True, slots=True)
class _Typo:
    """A mistake selected for one position, before it is turned into events."""

    kind: TypoKind
    #: Characters of the target text this mistake consumes (1, or 2 for a swap).
    consumed: int
    #: The wrong character, for substitutions.
    wrong_char: str | None = None


def _clamp(value: float) -> float:
    return min(max(value, config.MIN_DELAY_SECONDS), config.MAX_DELAY_SECONDS)


class ProbabilisticBehaviorGenerator:
    """Reference implementation of
    :class:`~typing_simulator.behavior.base.BehaviorGenerator`."""

    def generate(self, text: str, settings: TypingSettings) -> TypingPlan:
        settings.validate()

        normalized = normalize_line_endings(text)
        if not normalized.strip():
            raise EmptyInputError()
        if len(normalized) > settings.max_text_length:
            raise InputTooLongError(len(normalized), settings.max_text_length)

        # Rejects unsupported characters; never removes or substitutes any.
        normalized = normalize_text(normalized)

        seed = settings.seed if settings.seed is not None else secrets.randbits(63)
        run = _PlanBuilder(normalized, settings, Random(seed))
        events, errors, corrections = run.build()

        return build_plan(
            target_text=normalized,
            events=events,
            seed=seed,
            deliberate_errors=errors,
            correction_events=corrections,
        )


def rough_duration_estimate(text: str, settings: TypingSettings) -> float:
    """A cheap duration estimate for the interface, without building a plan.

    Uses the same baseline and context multipliers as the real model but skips
    the random components, so it is deterministic and O(n).  The plan's own
    ``estimated_duration`` replaces it as soon as the user arms.
    """
    normalized = normalize_line_endings(text)
    if not normalized:
        return 0.0

    baseline = settings.baseline_interval
    total = 0.0
    previous: str | None = None
    for char in normalized:
        multiplier = 1.0
        if previous == "\n":
            multiplier = config.AFTER_NEWLINE_MULTIPLIER
        elif previous == " ":
            multiplier = config.WORD_BOUNDARY_MULTIPLIER
        elif previous is not None and previous in config.SENTENCE_CHARACTERS:
            multiplier = config.SENTENCE_MULTIPLIER
        elif previous is not None and previous in config.COMMA_CHARACTERS:
            multiplier = config.COMMA_MULTIPLIER
        if char == "\n":
            multiplier *= config.BEFORE_NEWLINE_MULTIPLIER
        total += _clamp(baseline * multiplier)
        previous = char

    if settings.corrections_enabled and settings.typo_rate > 0:
        # Mistakes are limited by the cooldown as well as by the rate.
        expected = min(
            len(normalized) * settings.typo_rate,
            len(normalized) / config.TYPO_COOLDOWN_CHARACTERS,
        )
        realization = (config.CORRECTION_REALIZATION_MIN + config.CORRECTION_REALIZATION_MAX) / 2
        resume = (config.CORRECTION_RESUME_MIN + config.CORRECTION_RESUME_MAX) / 2
        per_mistake = (
            realization
            + resume
            + baseline * config.CORRECTION_BACKSPACE_MULTIPLIER
            + baseline  # retyping the corrected character
        )
        total += expected * per_mistake
    return total


class _PlanBuilder:
    """Single-use helper that walks the text and accumulates events."""

    def __init__(self, text: str, settings: TypingSettings, rng: Random) -> None:
        self._text = text
        self._settings = settings
        self._rng = rng
        self._profile = settings.profile
        self._baseline = settings.baseline_interval
        self._events: list[Event] = []
        self._burst = 1.0
        self._prev_char: str | None = None
        self._deliberate_errors = 0
        self._correction_events = 0
        self._last_typo_end = -config.TYPO_COOLDOWN_CHARACTERS - 1

    # -- entry point -------------------------------------------------------
    def build(self) -> tuple[list[Event], int, int]:
        index = 0
        length = len(self._text)
        while index < length:
            typo = self._select_typo(index)
            if typo is None:
                self._type_character(self._text[index])
                index += 1
                continue

            self._emit_typo(index, typo)
            self._deliberate_errors += 1
            self._correction_events += 1
            index += typo.consumed
            self._last_typo_end = index
        return self._events, self._deliberate_errors, self._correction_events

    # -- timing ------------------------------------------------------------
    def _advance_burst(self) -> None:
        """Evolve the short-lived speed multiplier (AR(1) in log space)."""
        log_burst = math.log(self._burst)
        log_burst = (
            self._profile.burst_persistence * log_burst
            + self._rng.gauss(0.0, self._profile.burst_sigma)
        )
        self._burst = min(
            max(math.exp(log_burst), self._profile.burst_multiplier_min),
            self._profile.burst_multiplier_max,
        )

    def _context_multiplier(self, next_char: str | None) -> float:
        multiplier = 1.0
        previous = self._prev_char
        if previous == "\n":
            multiplier *= config.AFTER_NEWLINE_MULTIPLIER
        elif previous == " ":
            multiplier *= config.WORD_BOUNDARY_MULTIPLIER
        elif previous is not None and previous in config.SENTENCE_CHARACTERS:
            multiplier *= config.SENTENCE_MULTIPLIER
        elif previous is not None and previous in config.COMMA_CHARACTERS:
            multiplier *= config.COMMA_MULTIPLIER
        if next_char == "\n":
            multiplier *= config.BEFORE_NEWLINE_MULTIPLIER
        return multiplier

    def _keystroke_delay(self, next_char: str | None) -> float:
        self._advance_burst()
        jitter = math.exp(self._rng.gauss(0.0, self._profile.jitter_sigma))
        delay = self._baseline * self._burst * jitter * self._context_multiplier(next_char)
        if self._rng.random() < self._profile.hesitation_probability:
            delay += self._rng.uniform(
                self._profile.hesitation_min, self._profile.hesitation_max
            )
        return _clamp(delay)

    def _short_delay(self, low: float, high: float) -> float:
        return _clamp(self._rng.uniform(low, high))

    # -- emission ----------------------------------------------------------
    def _emit_delay(self, seconds: float) -> None:
        self._events.append(Delay(seconds))

    def _emit_key(self, char: str) -> None:
        key = key_for_character(char)
        self._events.append(KeyDown(key))
        self._events.append(KeyUp(key))

    def _emit_backspace(self) -> None:
        self._events.append(KeyDown(BACKSPACE_KEY))
        self._events.append(KeyUp(BACKSPACE_KEY))

    def _type_character(self, char: str, *, context_char: str | None = None) -> None:
        """Emit the delay preceding ``char`` and then the key press/release."""
        self._emit_delay(self._keystroke_delay(context_char if context_char else char))
        self._emit_key(char)
        self._prev_char = char

    def _emit_backspaces(self, count: int) -> None:
        interval = _clamp(self._baseline * config.CORRECTION_BACKSPACE_MULTIPLIER)
        for _ in range(count):
            self._emit_delay(interval)
            self._emit_backspace()

    def _emit_correction(self, backspaces: int) -> None:
        """Pause, delete ``backspaces`` characters, pause again."""
        self._emit_delay(
            self._short_delay(
                config.CORRECTION_REALIZATION_MIN, config.CORRECTION_REALIZATION_MAX
            )
        )
        self._emit_backspaces(backspaces)
        self._emit_delay(
            self._short_delay(config.CORRECTION_RESUME_MIN, config.CORRECTION_RESUME_MAX)
        )

    # -- typo selection ----------------------------------------------------
    def _select_typo(self, index: int) -> _Typo | None:
        """Decide whether to make a mistake at ``index``.

        Mistakes are only made on plain ASCII letters that are not adjacent to
        a newline, and never within ``TYPO_COOLDOWN_CHARACTERS`` of the last
        one, so corrections stay unambiguous and errors cannot cluster.
        """
        settings = self._settings
        if not settings.corrections_enabled or settings.typo_rate <= 0.0:
            return None
        if index - self._last_typo_end < config.TYPO_COOLDOWN_CHARACTERS:
            return None
        if not self._is_typo_eligible(index):
            return None
        if self._rng.random() >= settings.typo_rate:
            return None

        kind = self._rng.choices(
            (TypoKind.SUBSTITUTION, TypoKind.DUPLICATE, TypoKind.TRANSPOSITION),
            weights=(
                config.TYPO_WEIGHT_SUBSTITUTION,
                config.TYPO_WEIGHT_DUPLICATE,
                config.TYPO_WEIGHT_TRANSPOSITION,
            ),
        )[0]

        char = self._text[index]
        if kind is TypoKind.TRANSPOSITION and not self._can_transpose(index):
            kind = TypoKind.SUBSTITUTION
        if kind is TypoKind.SUBSTITUTION:
            neighbours = adjacent_letters(char)
            if not neighbours:
                return _Typo(TypoKind.DUPLICATE, consumed=1)
            return _Typo(
                TypoKind.SUBSTITUTION, consumed=1, wrong_char=self._rng.choice(neighbours)
            )
        if kind is TypoKind.TRANSPOSITION:
            return _Typo(TypoKind.TRANSPOSITION, consumed=2)
        return _Typo(TypoKind.DUPLICATE, consumed=1)

    def _is_typo_eligible(self, index: int) -> bool:
        char = self._text[index]
        if not (char.isascii() and char.isalpha()):
            return False
        if index > 0 and self._text[index - 1] == "\n":
            return False
        if index + 1 < len(self._text) and self._text[index + 1] == "\n":
            return False
        return True

    def _can_transpose(self, index: int) -> bool:
        """A swap needs two distinct letters with no newline immediately after."""
        following = index + 1
        if following >= len(self._text):
            return False
        first, second = self._text[index], self._text[following]
        if first == second:  # swapping identical letters would not be a mistake
            return False
        if not (second.isascii() and second.isalpha()):
            return False
        if following + 1 < len(self._text) and self._text[following + 1] == "\n":
            return False
        return True

    # -- typo emission -----------------------------------------------------
    def _emit_typo(self, index: int, typo: _Typo) -> None:
        if typo.kind is TypoKind.SUBSTITUTION:
            self._emit_substitution(index, typo)
        elif typo.kind is TypoKind.DUPLICATE:
            self._emit_duplicate(index)
        else:
            self._emit_transposition(index)

    def _emit_substitution(self, index: int, typo: _Typo) -> None:
        correct = self._text[index]
        assert typo.wrong_char is not None
        # Type the wrong character where the correct one belonged...
        self._type_character(typo.wrong_char, context_char=correct)
        # ...notice, delete it, and type the right one.
        self._emit_correction(backspaces=1)
        self._type_character(correct)

    def _emit_duplicate(self, index: int) -> None:
        char = self._text[index]
        self._type_character(char)
        # The accidental repeat lands quickly after the first press.
        self._emit_delay(_clamp(self._baseline * 0.45))
        self._emit_key(char)
        self._emit_correction(backspaces=1)
        self._prev_char = char

    def _emit_transposition(self, index: int) -> None:
        first, second = self._text[index], self._text[index + 1]
        # The two letters come out in the wrong order...
        self._type_character(second, context_char=first)
        self._type_character(first, context_char=second)
        # ...both are deleted and retyped correctly.
        self._emit_correction(backspaces=2)
        self._type_character(first)
        self._type_character(second)
