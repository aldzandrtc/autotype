"""Keyboard backends: the only place that talks to a real keyboard."""

from typing_simulator.backends.base import KeyboardBackend, TrackingKeyboardBackend
from typing_simulator.backends.pynput_backend import PynputKeyboardBackend
from typing_simulator.backends.quartz_backend import QuartzKeyboardBackend
from typing_simulator.backends.recording_backend import RecordedCall, RecordingKeyboardBackend

__all__ = [
    "KeyboardBackend",
    "PynputKeyboardBackend",
    "QuartzKeyboardBackend",
    "RecordedCall",
    "RecordingKeyboardBackend",
    "TrackingKeyboardBackend",
]
