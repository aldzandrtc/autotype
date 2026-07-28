"""Shadow-buffer tests."""

from __future__ import annotations

import pytest

from typing_simulator.behavior.keyboard_map import BACKSPACE_KEY, key_for_character
from typing_simulator.domain.events import Delay, KeyDown, KeyUp, NormalizedKey, SpecialKey
from typing_simulator.errors import PlanValidationError
from typing_simulator.simulation.text_buffer import TextBuffer, simulate


def press(char: str) -> list:
    key = key_for_character(char)
    return [KeyDown(key), KeyUp(key)]


def type_text(text: str) -> list:
    events = []
    for char in text:
        events.extend(press(char))
    return events


def backspace(count: int = 1) -> list:
    events = []
    for _ in range(count):
        events.extend([KeyDown(BACKSPACE_KEY), KeyUp(BACKSPACE_KEY)])
    return events


def test_printable_characters_are_inserted() -> None:
    assert simulate(type_text("Hello, world! 42")) == "Hello, world! 42"


def test_space_key_inserts_a_space() -> None:
    assert simulate(type_text("a b")) == "a b"


def test_enter_creates_a_newline() -> None:
    assert simulate(type_text("a\nb")) == "a\nb"


def test_backspace_removes_one_character() -> None:
    assert simulate(type_text("abc") + backspace()) == "ab"


def test_backspace_on_empty_buffer_is_safe() -> None:
    assert simulate(backspace(3)) == ""
    assert simulate(backspace(3) + type_text("ok")) == "ok"


def test_delay_events_are_ignored() -> None:
    events = [Delay(0.5), *press("a"), Delay(2.0), *press("b")]
    assert simulate(events) == "ab"


def test_substitution_is_corrected() -> None:
    # "teh" typed for "the": wrong char, backspace, right char.
    events = type_text("t") + type_text("r") + backspace() + type_text("he")
    assert simulate(events) == "the"


def test_duplicate_is_corrected() -> None:
    events = type_text("hee") + backspace() + type_text("llo")
    assert simulate(events) == "hello"


def test_transposition_is_corrected() -> None:
    # "ht" typed for "th": both wrong letters deleted, then retyped in order.
    events = type_text("ht") + backspace(2) + type_text("the")
    assert simulate(events) == "the"


def test_shift_is_tracked_but_inserts_nothing() -> None:
    shift = NormalizedKey.of(SpecialKey.SHIFT)
    events = [KeyDown(shift), *press("A"), KeyUp(shift)]
    assert simulate(events) == "A"


def test_tab_is_rejected() -> None:
    tab = NormalizedKey.of(SpecialKey.TAB)
    with pytest.raises(PlanValidationError):
        simulate([KeyDown(tab), KeyUp(tab)])


def test_unbalanced_key_up_is_rejected_in_strict_mode() -> None:
    key = key_for_character("a")
    with pytest.raises(PlanValidationError):
        simulate([KeyUp(key)])


def test_keys_left_held_are_rejected() -> None:
    key = key_for_character("a")
    with pytest.raises(PlanValidationError):
        simulate([KeyDown(key)])


def test_buffer_tracks_pressed_keys() -> None:
    buffer = TextBuffer()
    key = key_for_character("a")
    buffer.apply(KeyDown(key))
    assert buffer.pressed_keys == (key,)
    buffer.apply(KeyUp(key))
    assert buffer.pressed_keys == ()
    assert buffer.text == "a"
    assert len(buffer) == 1
