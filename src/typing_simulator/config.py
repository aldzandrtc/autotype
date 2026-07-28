"""Typing settings and every tunable constant, in one documented place.

All durations are **seconds**.  The timing numbers below are provisional: they
were chosen to be readable and testable rather than to model any particular
person.  Adjust them here, not in the generator.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from typing_simulator.errors import InvalidSettingsError

# ---------------------------------------------------------------------------
# Input limits
# ---------------------------------------------------------------------------

#: Maximum number of characters accepted in one run.  Configurable in code.
MAX_TEXT_LENGTH = 10_000

# ---------------------------------------------------------------------------
# Speed
# ---------------------------------------------------------------------------

WPM_MIN = 20
WPM_MAX = 120
WPM_DEFAULT = 50

#: The conventional "a word is five characters" approximation.
CHARS_PER_WORD = 5.0

#: Hard clamps applied to every generated delay.  No delay may fall outside
#: this range, whatever the sampled multipliers happen to be.
MIN_DELAY_SECONDS = 0.010
MAX_DELAY_SECONDS = 3.000

# ---------------------------------------------------------------------------
# Timing variation
# ---------------------------------------------------------------------------


class VariationLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class VariationProfile:
    """Parameters of the timing model for one variation level.

    The per-character speed multiplier follows an AR(1) process in log space::

        log m_next = burst_persistence * log m + N(0, burst_sigma)

    which makes several consecutive characters share a coherent fast or slow
    burst instead of each delay being drawn independently.  ``jitter_sigma``
    then adds independent per-character noise on top of that.
    """

    jitter_sigma: float
    burst_sigma: float
    burst_persistence: float
    burst_multiplier_min: float
    burst_multiplier_max: float
    hesitation_probability: float
    hesitation_min: float
    hesitation_max: float


VARIATION_PROFILES: dict[VariationLevel, VariationProfile] = {
    VariationLevel.LOW: VariationProfile(
        jitter_sigma=0.12,
        burst_sigma=0.05,
        burst_persistence=0.85,
        burst_multiplier_min=0.75,
        burst_multiplier_max=1.35,
        hesitation_probability=0.004,
        hesitation_min=0.10,
        hesitation_max=0.35,
    ),
    VariationLevel.MEDIUM: VariationProfile(
        jitter_sigma=0.22,
        burst_sigma=0.10,
        burst_persistence=0.90,
        burst_multiplier_min=0.60,
        burst_multiplier_max=1.70,
        hesitation_probability=0.015,
        hesitation_min=0.15,
        hesitation_max=0.60,
    ),
    VariationLevel.HIGH: VariationProfile(
        jitter_sigma=0.35,
        burst_sigma=0.16,
        burst_persistence=0.92,
        burst_multiplier_min=0.50,
        burst_multiplier_max=2.10,
        hesitation_probability=0.035,
        hesitation_min=0.20,
        hesitation_max=0.90,
    ),
}

# ---------------------------------------------------------------------------
# Context pauses
#
# Multipliers applied to the baseline interval for the delay that *precedes*
# the next keystroke, based on the character that was just typed.
# ---------------------------------------------------------------------------

WORD_BOUNDARY_MULTIPLIER = 1.35  # after a space
COMMA_MULTIPLIER = 2.20  # after , ; :
SENTENCE_MULTIPLIER = 3.40  # after . ! ?
BEFORE_NEWLINE_MULTIPLIER = 1.80  # just before pressing Enter
AFTER_NEWLINE_MULTIPLIER = 2.60  # just after pressing Enter

COMMA_CHARACTERS = ",;:"
SENTENCE_CHARACTERS = ".!?"

# ---------------------------------------------------------------------------
# Typos and corrections
# ---------------------------------------------------------------------------

TYPO_RATE_MIN = 0.0
TYPO_RATE_MAX = 0.05
TYPO_RATE_DEFAULT = 0.01

#: Minimum number of characters between two deliberate mistakes.  Prevents the
#: pathological "stuttering" behavior of repeated errors in one place.
TYPO_COOLDOWN_CHARACTERS = 8

#: Relative weights for the three supported mistake kinds.
TYPO_WEIGHT_SUBSTITUTION = 0.55
TYPO_WEIGHT_DUPLICATE = 0.25
TYPO_WEIGHT_TRANSPOSITION = 0.20

#: Pause after the mistake is "noticed", before the first backspace.
CORRECTION_REALIZATION_MIN = 0.18
CORRECTION_REALIZATION_MAX = 0.55

#: Interval between successive backspaces, as a fraction of the baseline.
CORRECTION_BACKSPACE_MULTIPLIER = 0.65

#: Pause after the last backspace, before retyping.
CORRECTION_RESUME_MIN = 0.10
CORRECTION_RESUME_MAX = 0.30

# ---------------------------------------------------------------------------
# Focus matching (replaces the old countdown)
# ---------------------------------------------------------------------------

#: There is no countdown.  Instead, after Start the application watches where
#: focus actually is, and begins emitting the moment it matches the target it
#: told the user about.  These bound that wait.
FOCUS_MATCH_TIMEOUT_SECONDS = 5.0
FOCUS_MATCH_POLL_SECONDS = 0.05

# ---------------------------------------------------------------------------
# Scheduler / safety
# ---------------------------------------------------------------------------

#: Longest slice of a delay the scheduler waits before re-checking pause and
#: abort flags.  Waits are additionally woken by an event, so abort latency is
#: normally far below this; the tick only bounds the worst case (20 ms).
SCHEDULER_TICK_SECONDS = 0.020

#: How often the focus guard samples the frontmost application while typing.
FOCUS_POLL_INTERVAL_SECONDS = 0.25

#: How often progress is reported back to the interface.
PROGRESS_INTERVAL_SECONDS = 0.10

#: How often the overlay refreshes the "will type into ..." preview while idle.
FRONTMOST_PREVIEW_INTERVAL_MS = 500

#: Global hotkeys.  There is deliberately no "start" hotkey: starting is a
#: deliberate action taken from the overlay, while these two are stop controls
#: that must work from anywhere.
HOTKEY_PAUSE_RESUME = "<ctrl>+<alt>+p"
HOTKEY_ABORT = "<ctrl>+<alt>+<esc>"
HOTKEY_PAUSE_RESUME_LABEL = "Control + Option + P"
HOTKEY_ABORT_LABEL = "Control + Option + Escape"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TypingSettings:
    """User-configurable settings for one typing run.

    ``seed`` of ``None`` means "choose a fresh random seed"; the chosen seed is
    always recorded on the generated plan so a run can be reproduced.
    """

    wpm: int = WPM_DEFAULT
    variation: VariationLevel = VariationLevel.MEDIUM
    typo_rate: float = TYPO_RATE_DEFAULT
    corrections_enabled: bool = True
    seed: int | None = None
    max_text_length: int = MAX_TEXT_LENGTH

    # -- derived ----------------------------------------------------------
    @property
    def baseline_interval(self) -> float:
        """Baseline keydown-to-keydown interval in seconds."""
        return 60.0 / (self.wpm * CHARS_PER_WORD)

    @property
    def profile(self) -> VariationProfile:
        return VARIATION_PROFILES[self.variation]

    def with_seed(self, seed: int) -> "TypingSettings":
        return replace(self, seed=seed)

    # -- validation -------------------------------------------------------
    def validate(self) -> None:
        """Raise :class:`InvalidSettingsError` if anything is out of range."""
        if not isinstance(self.wpm, int) or not WPM_MIN <= self.wpm <= WPM_MAX:
            raise InvalidSettingsError(
                f"Typing speed must be a whole number between {WPM_MIN} and "
                f"{WPM_MAX} WPM (got {self.wpm})."
            )
        if not isinstance(self.variation, VariationLevel):
            raise InvalidSettingsError("Timing variation must be low, medium or high.")
        if not TYPO_RATE_MIN <= self.typo_rate <= TYPO_RATE_MAX:
            raise InvalidSettingsError(
                f"Typo rate must be between {TYPO_RATE_MIN:.0%} and "
                f"{TYPO_RATE_MAX:.0%} (got {self.typo_rate:.2%})."
            )
        if not isinstance(self.corrections_enabled, bool):
            raise InvalidSettingsError("Enable-corrections must be true or false.")
        if self.seed is not None and not isinstance(self.seed, int):
            raise InvalidSettingsError("The random seed must be a whole number.")
        if self.seed is not None and not 0 <= self.seed <= 2**63 - 1:
            raise InvalidSettingsError("The random seed must be between 0 and 2^63 - 1.")
        if not isinstance(self.max_text_length, int) or self.max_text_length <= 0:
            raise InvalidSettingsError("The maximum text length must be a positive integer.")
