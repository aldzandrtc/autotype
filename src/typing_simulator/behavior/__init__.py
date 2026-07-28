"""Behavior generation: the pluggable "how does this get typed" layer."""

from typing_simulator.behavior.base import BehaviorGenerator
from typing_simulator.behavior.probabilistic import ProbabilisticBehaviorGenerator

__all__ = ["BehaviorGenerator", "ProbabilisticBehaviorGenerator"]
