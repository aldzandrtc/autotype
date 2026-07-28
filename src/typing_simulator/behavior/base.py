"""The behavior-generator boundary.

Everything above this line - the scheduler, the safety controller, the
keyboard backends and the interface - depends only on
:class:`BehaviorGenerator`.  Replacing
:class:`~typing_simulator.behavior.probabilistic.ProbabilisticBehaviorGenerator`
with, say, a model-driven generator therefore requires no change anywhere else:
the replacement only has to return a :class:`~typing_simulator.domain.events.TypingPlan`
whose events replay to the normalized target text.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from typing_simulator.config import TypingSettings
from typing_simulator.domain.events import TypingPlan


@runtime_checkable
class BehaviorGenerator(Protocol):
    """Turns text plus settings into a complete, reproducible event plan."""

    def generate(self, text: str, settings: TypingSettings) -> TypingPlan:
        """Return a plan whose events reproduce the normalized ``text``.

        Implementations must:

        * validate ``settings`` before doing any work;
        * normalize line endings and reject unsupported characters rather than
          removing them;
        * record the seed actually used on the returned plan;
        * emit only delays inside the configured clamps;
        * guarantee that replaying the events through the shadow buffer yields
          ``plan.expected_final_text`` exactly.
        """
        ...
