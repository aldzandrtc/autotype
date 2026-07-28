"""Exact-output tests.

The single most important invariant of the prototype::

    simulate(generate(text, settings).events) == normalized_text

is checked across a representative set of passages, every variation level, the
full WPM range and many random seeds.
"""

from __future__ import annotations

import pytest

from typing_simulator.behavior.keyboard_map import normalize_line_endings
from typing_simulator.behavior.probabilistic import ProbabilisticBehaviorGenerator
from typing_simulator.config import TypingSettings, VariationLevel
from typing_simulator.simulation.text_buffer import simulate, validate_plan

GENERATOR = ProbabilisticBehaviorGenerator()

PASSAGES: dict[str, str] = {
    "one_sentence": "The quick brown fox jumps over the lazy dog.",
    "punctuation": "Wait - really? Yes; truly! (Well, mostly.) \"Quoted,\" he said: 100% sure.",
    "capitalization": "MacOS And PySide6 Are Proper Nouns; ALL CAPS SHOUTING too.",
    "numbers": "Order 66 shipped 1,024 units at $3.50 each on 2026-07-27 (up 12.5%).",
    "paragraphs": (
        "First paragraph, with a comma.\n"
        "Second line of the same paragraph.\n"
        "\n"
        "A new paragraph starts here. It has two sentences.\n"
    ),
    "repeated_letters": "aaa bookkeeper committee successfully mississippi lloyd.",
    "empty_lines": "one\n\n\ntwo\n\n\n\nthree\n",
    "symbols": "`~!@#$%^&*()_+-=[]{}\\|;:'\",.<>/?",
    "crlf": "windows\r\nline\r\nendings\rand a lone carriage return",
    "single_char": "x",
    "long_text": (
        "Prototype validation passage. " * 120
    ),
}

SETTINGS_MATRIX = [
    TypingSettings(wpm=20, variation=VariationLevel.LOW, typo_rate=0.0),
    TypingSettings(wpm=50, variation=VariationLevel.MEDIUM, typo_rate=0.01),
    TypingSettings(wpm=120, variation=VariationLevel.HIGH, typo_rate=0.05),
    TypingSettings(wpm=75, variation=VariationLevel.HIGH, typo_rate=0.05, corrections_enabled=True),
]


@pytest.mark.parametrize("name", sorted(PASSAGES))
@pytest.mark.parametrize("seed", range(8))
def test_plan_replays_to_the_normalized_text(name: str, seed: int) -> None:
    text = PASSAGES[name]
    expected = normalize_line_endings(text)
    for base in SETTINGS_MATRIX:
        plan = GENERATOR.generate(text, base.with_seed(seed))
        assert plan.target_text == expected
        assert simulate(plan.events) == expected
        validate_plan(plan)


@pytest.mark.parametrize("seed", range(40))
def test_high_typo_rate_still_reproduces_the_text(seed: int) -> None:
    text = (
        "The prototype must reproduce this text exactly, even when it makes "
        "deliberate mistakes and corrects them.\n"
        "Second line, with punctuation; and a question? Yes.\n"
    )
    settings = TypingSettings(
        wpm=90, variation=VariationLevel.HIGH, typo_rate=0.05, seed=seed
    )
    plan = GENERATOR.generate(text, settings)
    assert simulate(plan.events) == normalize_line_endings(text)


@pytest.mark.parametrize("seed", range(6))
def test_long_text_at_the_cap_reproduces_exactly(seed: int) -> None:
    text = ("Sentence number one, with a comma. " * 300)[:10_000]
    settings = TypingSettings(wpm=110, variation=VariationLevel.MEDIUM, typo_rate=0.05, seed=seed)
    plan = GENERATOR.generate(text, settings)
    assert len(plan.target_text) == 10_000
    assert simulate(plan.events) == text


def test_validate_plan_rejects_a_tampered_plan() -> None:
    from dataclasses import replace

    from typing_simulator.errors import PlanValidationError

    plan = GENERATOR.generate("hello world", TypingSettings(seed=1))
    broken = replace(plan, events=plan.events[:-4])
    with pytest.raises(PlanValidationError):
        validate_plan(broken)
