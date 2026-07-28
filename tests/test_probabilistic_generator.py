"""Behavior-generator tests."""

from __future__ import annotations

import dataclasses
import math

import pytest

from typing_simulator import config
from typing_simulator.behavior.base import BehaviorGenerator
from typing_simulator.behavior.keyboard_map import QWERTY_ADJACENCY, adjacent_letters
from typing_simulator.behavior.probabilistic import ProbabilisticBehaviorGenerator
from typing_simulator.config import TypingSettings, VariationLevel
from typing_simulator.domain.events import Delay, KeyDown, SpecialKey, total_delay
from typing_simulator.errors import (
    EmptyInputError,
    InputTooLongError,
    InvalidSettingsError,
    UnsupportedCharacterError,
)
from typing_simulator.simulation.text_buffer import simulate


def delays(plan) -> list[float]:
    return [e.seconds for e in plan.events if isinstance(e, Delay)]


def test_generator_satisfies_the_protocol(generator) -> None:
    assert isinstance(generator, BehaviorGenerator)


def test_empty_text_is_rejected(generator, settings) -> None:
    with pytest.raises(EmptyInputError):
        generator.generate("", settings)
    with pytest.raises(EmptyInputError):
        generator.generate("   \n  ", settings)


def test_text_over_the_cap_is_rejected(generator, settings) -> None:
    capped = dataclasses.replace(settings, max_text_length=10)
    with pytest.raises(InputTooLongError):
        generator.generate("a" * 11, capped)
    # Exactly at the cap is fine.
    assert generator.generate("a" * 10, capped).target_text == "a" * 10


def test_default_cap_is_ten_thousand_characters() -> None:
    assert config.MAX_TEXT_LENGTH == 10_000
    assert TypingSettings().max_text_length == 10_000


def test_crlf_and_cr_are_normalized(generator, settings) -> None:
    plan = generator.generate("one\r\ntwo\rthree", settings)
    assert plan.target_text == "one\ntwo\nthree"
    assert "\r" not in plan.target_text
    assert simulate(plan.events) == "one\ntwo\nthree"


def test_unsupported_characters_are_reported_not_removed(generator, settings) -> None:
    with pytest.raises(UnsupportedCharacterError) as excinfo:
        generator.generate("smart ’quotes’ here", settings)
    assert "U+2019" in excinfo.value.user_message


def test_tab_is_rejected_with_a_useful_message(generator, settings) -> None:
    with pytest.raises(UnsupportedCharacterError) as excinfo:
        generator.generate("a\tb", settings)
    assert "tab" in excinfo.value.user_message.lower()


def test_invalid_settings_are_rejected(generator) -> None:
    for bad in (
        TypingSettings(wpm=5),
        TypingSettings(wpm=500),
        TypingSettings(typo_rate=0.9),
        TypingSettings(typo_rate=-0.1),
    ):
        with pytest.raises(InvalidSettingsError):
            generator.generate("hello", bad)


def test_there_is_no_countdown_setting() -> None:
    """Typing starts when focus is seen on the target, not after a delay."""
    assert not hasattr(TypingSettings(), "countdown_seconds")
    assert not hasattr(config, "COUNTDOWN_DEFAULT_SECONDS")


def test_same_seed_and_settings_produce_the_same_plan(generator, settings) -> None:
    text = "The quick brown fox jumps over the lazy dog."
    first = generator.generate(text, settings)
    second = generator.generate(text, settings)
    assert first.events == second.events
    assert first.seed == second.seed
    assert first.estimated_duration == second.estimated_duration


def test_a_missing_seed_is_chosen_and_recorded(generator) -> None:
    settings = TypingSettings(seed=None)
    plan = generator.generate("hello there", settings)
    assert isinstance(plan.seed, int)
    replay = generator.generate("hello there", settings.with_seed(plan.seed))
    assert replay.events == plan.events


def test_different_seeds_usually_produce_different_timing(generator) -> None:
    text = "The quick brown fox jumps over the lazy dog."
    base = TypingSettings(typo_rate=0.0)
    plans = [generator.generate(text, base.with_seed(seed)) for seed in range(12)]
    signatures = {tuple(delays(plan)) for plan in plans}
    # Allow for an unlikely collision, but they must not all be identical.
    assert len(signatures) >= 11


def test_zero_typo_rate_introduces_no_mistakes(generator) -> None:
    settings = TypingSettings(typo_rate=0.0, seed=7)
    text = "The quick brown fox jumps over the lazy dog. " * 20
    plan = generator.generate(text, settings)
    assert plan.statistics.deliberate_errors == 0
    assert plan.statistics.correction_events == 0
    assert plan.statistics.backspaces == 0
    assert not any(
        isinstance(e, KeyDown) and e.key.special is SpecialKey.BACKSPACE for e in plan.events
    )


def test_corrections_disabled_introduces_no_mistakes(generator) -> None:
    settings = TypingSettings(typo_rate=0.05, corrections_enabled=False, seed=11)
    text = "The quick brown fox jumps over the lazy dog. " * 20
    plan = generator.generate(text, settings)
    assert plan.statistics.deliberate_errors == 0
    assert plan.statistics.backspaces == 0


def test_nonzero_typo_rate_produces_and_corrects_mistakes(generator) -> None:
    settings = TypingSettings(typo_rate=0.05, seed=3)
    text = "The quick brown fox jumps over the lazy dog. " * 30
    plan = generator.generate(text, settings)
    assert plan.statistics.deliberate_errors > 0
    assert plan.statistics.correction_events == plan.statistics.deliberate_errors
    assert plan.statistics.backspaces >= plan.statistics.correction_events
    # Every mistake is corrected: the simulated buffer still matches.
    assert simulate(plan.events) == plan.target_text


def test_mistakes_are_never_adjacent(generator) -> None:
    """The cooldown must stop errors clustering at one position."""
    settings = TypingSettings(typo_rate=0.05, seed=17)
    text = "the quick brown fox jumps over the lazy dog " * 40
    plan = generator.generate(text, settings)

    # Count the characters typed between successive backspace groups.
    gaps: list[int] = []
    since = 0
    in_group = False
    for event in plan.events:
        if not isinstance(event, KeyDown):
            continue
        if event.key.special is SpecialKey.BACKSPACE:
            if not in_group:
                gaps.append(since)
                since = 0
                in_group = True
        else:
            in_group = False
            since += 1
    assert plan.statistics.deliberate_errors > 1
    assert all(gap >= config.TYPO_COOLDOWN_CHARACTERS for gap in gaps[1:])


def test_all_delays_are_within_configured_bounds(generator) -> None:
    text = "Hello, world!\n\nSecond paragraph with numbers 1234567890 and symbols #$%^&*().\n"
    for level in VariationLevel:
        for wpm in (config.WPM_MIN, config.WPM_DEFAULT, config.WPM_MAX):
            for seed in range(5):
                settings = TypingSettings(wpm=wpm, variation=level, typo_rate=0.05, seed=seed)
                plan = generator.generate(text, settings)
                for value in delays(plan):
                    assert config.MIN_DELAY_SECONDS <= value <= config.MAX_DELAY_SECONDS


def test_estimated_duration_equals_the_sum_of_delays(generator, settings) -> None:
    plan = generator.generate("The quick brown fox.\nA second line!", settings)
    assert plan.estimated_duration == math.fsum(delays(plan))
    assert plan.estimated_duration == total_delay(plan.events)
    assert plan.estimated_duration > 0


def test_faster_wpm_yields_a_shorter_estimate(generator) -> None:
    text = "The quick brown fox jumps over the lazy dog. " * 5
    slow = generator.generate(text, TypingSettings(wpm=20, typo_rate=0.0, seed=5))
    fast = generator.generate(text, TypingSettings(wpm=120, typo_rate=0.0, seed=5))
    assert fast.estimated_duration < slow.estimated_duration


def test_higher_variation_widens_the_delay_spread(generator) -> None:
    text = "the quick brown fox jumps over the lazy dog " * 10
    spreads = {}
    for level in VariationLevel:
        plan = generator.generate(
            text, TypingSettings(variation=level, typo_rate=0.0, seed=42)
        )
        values = delays(plan)
        mean = sum(values) / len(values)
        spreads[level] = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    assert spreads[VariationLevel.LOW] < spreads[VariationLevel.HIGH]


def test_plan_statistics_are_consistent(generator) -> None:
    settings = TypingSettings(typo_rate=0.05, seed=99)
    text = "The quick brown fox jumps over the lazy dog. " * 10
    plan = generator.generate(text, settings)
    stats = plan.statistics
    assert stats.characters == len(plan.target_text)
    assert stats.key_down_events >= stats.characters
    assert stats.delay_events == sum(1 for e in plan.events if isinstance(e, Delay))
    assert plan.expected_final_text == plan.target_text
    assert stats.effective_wpm > 0
    assert stats.summary()


def test_remaining_durations_are_suffix_sums(generator, settings) -> None:
    plan = generator.generate("hello world", settings)
    remaining = plan.remaining_durations()
    assert len(remaining) == len(plan.events) + 1
    assert remaining[0] == pytest.approx(plan.estimated_duration)
    assert remaining[-1] == 0.0
    assert all(remaining[i] >= remaining[i + 1] for i in range(len(plan.events)))


def test_qwerty_adjacency_is_sane() -> None:
    assert "s" in QWERTY_ADJACENCY["a"]
    assert "w" in QWERTY_ADJACENCY["s"]
    assert "z" not in QWERTY_ADJACENCY["p"]
    assert adjacent_letters("A") == tuple(c.upper() for c in adjacent_letters("a"))
    assert all(c.isalpha() for c in adjacent_letters("k"))


def test_generator_never_emits_tab_or_shift(generator) -> None:
    plan = generator.generate(
        "Mixed CASE text, with punctuation! And numbers 42.\n", TypingSettings(seed=2)
    )
    specials = {
        e.key.special for e in plan.events if isinstance(e, KeyDown) and e.key.special
    }
    assert SpecialKey.TAB not in specials
    assert SpecialKey.SHIFT not in specials


def test_probabilistic_generator_is_the_default_implementation() -> None:
    assert isinstance(ProbabilisticBehaviorGenerator(), BehaviorGenerator)
