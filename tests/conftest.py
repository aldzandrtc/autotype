"""Shared test fixtures.

Automated tests never emit real key events: every scheduler and safety test
uses :class:`~typing_simulator.backends.recording_backend.RecordingKeyboardBackend`
and fake focus / hotkey services.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Qt must never open a real window during the suite, and the native overlay
# configuration is deliberately inert off the Cocoa plugin.  Set before any
# test module imports PySide6, which is why it lives here.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Allow running the suite straight from a checkout, without installing.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from typing_simulator.backends.recording_backend import RecordingKeyboardBackend  # noqa: E402
from typing_simulator.behavior.probabilistic import (  # noqa: E402
    ProbabilisticBehaviorGenerator,
)
from typing_simulator.config import TypingSettings, VariationLevel  # noqa: E402


@pytest.fixture
def generator() -> ProbabilisticBehaviorGenerator:
    return ProbabilisticBehaviorGenerator()


@pytest.fixture
def backend() -> RecordingKeyboardBackend:
    return RecordingKeyboardBackend()


@pytest.fixture
def settings() -> TypingSettings:
    """Deterministic default settings: fixed seed, no deliberate mistakes."""
    return TypingSettings(
        wpm=60,
        variation=VariationLevel.MEDIUM,
        typo_rate=0.0,
        corrections_enabled=True,
        seed=1234,
    )
