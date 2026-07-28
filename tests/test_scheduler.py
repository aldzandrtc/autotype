"""Scheduler tests.

No test emits a real key event: every run uses the recording backend, and an
injectable clock is used wherever determinism matters.
"""

from __future__ import annotations

import threading
import time

import pytest

from typing_simulator.backends.recording_backend import RecordingKeyboardBackend
from typing_simulator.behavior.keyboard_map import key_for_character
from typing_simulator.domain.events import Delay, KeyDown, KeyUp, NormalizedKey, build_plan
from typing_simulator.errors import SchedulerError
from typing_simulator.scheduler.clock import FakeClock, RealClock
from typing_simulator.scheduler.scheduler import Progress, RunStatus, TypingScheduler
from typing_simulator.simulation.text_buffer import simulate


def make_plan(text: str, delay: float = 0.01):
    """A plain plan: one fixed delay, press and release, per character."""
    events = []
    for char in text:
        key = key_for_character(char)
        events.extend([Delay(delay), KeyDown(key), KeyUp(key)])
    return build_plan(
        target_text=text, events=events, seed=0, deliberate_errors=0, correction_events=0
    )


def wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ---------------------------------------------------------------------------
# Ordering and completion
# ---------------------------------------------------------------------------


def test_events_run_in_order(backend) -> None:
    plan = make_plan("hello world", delay=0.5)
    scheduler = TypingScheduler(backend, FakeClock())
    result = scheduler.run(plan)

    assert result.status is RunStatus.COMPLETED
    assert simulate(backend.key_events) == "hello world"
    assert result.events_completed == plan.total_events
    assert result.characters_typed == len("hello world")


def test_fake_clock_consumes_the_planned_duration(backend) -> None:
    plan = make_plan("abc", delay=0.25)
    clock = FakeClock()
    TypingScheduler(backend, clock).run(plan)
    assert clock.monotonic() == pytest.approx(0.75, abs=1e-9)


def test_completion_releases_all_keys(backend) -> None:
    plan = make_plan("done", delay=0.0)
    result = TypingScheduler(backend, FakeClock()).run(plan)
    assert result.status is RunStatus.COMPLETED
    assert backend.release_all_count >= 1
    assert backend.pressed_keys == ()


def test_progress_updates_are_monotonic(backend) -> None:
    plan = make_plan("monotonic progress", delay=0.05)
    updates: list[Progress] = []
    scheduler = TypingScheduler(backend, FakeClock(), progress_interval=0.0)
    scheduler.run(plan, on_progress=updates.append)

    assert len(updates) > 5
    indexes = [u.event_index for u in updates]
    typed = [u.characters_typed for u in updates]
    elapsed = [u.elapsed_seconds for u in updates]
    remaining = [u.remaining_seconds for u in updates]
    assert indexes == sorted(indexes)
    assert typed == sorted(typed)
    assert elapsed == sorted(elapsed)
    assert remaining == sorted(remaining, reverse=True)
    assert updates[-1].event_index == plan.total_events
    assert updates[-1].fraction == 1.0


def test_estimated_remaining_starts_at_the_plan_duration(backend) -> None:
    plan = make_plan("abcdef", delay=0.1)
    updates: list[Progress] = []
    TypingScheduler(backend, FakeClock(), progress_interval=0.0).run(
        plan, on_progress=updates.append
    )
    assert updates[0].remaining_seconds == pytest.approx(plan.estimated_duration)
    assert updates[-1].remaining_seconds == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------


def test_pause_stops_progress_and_resume_continues(backend) -> None:
    plan = make_plan("p" * 120, delay=0.008)
    scheduler = TypingScheduler(backend, RealClock(), progress_interval=0.0)
    paused = threading.Event()
    resumed = threading.Event()
    scheduler.start(plan, on_paused=paused.set, on_resumed=resumed.set)

    assert wait_until(lambda: scheduler.event_index > 3)
    scheduler.pause()
    assert wait_until(paused.is_set), "the scheduler never reported a pause"

    frozen = scheduler.event_index
    time.sleep(0.15)
    assert scheduler.event_index == frozen, "progress continued while paused"
    assert scheduler.is_paused

    scheduler.resume()
    assert wait_until(resumed.is_set)
    scheduler.join(timeout=5.0)
    assert not scheduler.is_running
    assert scheduler.event_index == plan.total_events
    assert simulate(backend.key_events) == "p" * 120


def test_pause_releases_held_keys(backend) -> None:
    plan = make_plan("hold", delay=0.05)
    scheduler = TypingScheduler(backend, RealClock())
    paused = threading.Event()
    scheduler.start(plan, on_paused=paused.set)
    scheduler.pause()
    assert wait_until(paused.is_set)
    assert backend.release_all_count >= 1
    assert backend.pressed_keys == ()
    scheduler.abort()
    scheduler.join(timeout=2.0)


def test_paused_time_does_not_consume_the_delay(backend) -> None:
    """Resuming continues the delay from where it stopped."""
    plan = make_plan("ab", delay=0.3)
    scheduler = TypingScheduler(backend, RealClock())
    paused = threading.Event()
    scheduler.start(plan, on_paused=paused.set)
    scheduler.pause()
    assert wait_until(paused.is_set)
    time.sleep(0.35)
    assert scheduler.event_index < plan.total_events
    scheduler.resume()
    scheduler.join(timeout=3.0)
    assert scheduler.event_index == plan.total_events


# ---------------------------------------------------------------------------
# Abort
# ---------------------------------------------------------------------------


def test_abort_stops_execution_promptly(backend) -> None:
    """Abort during a long delay must take effect in well under 100 ms."""
    plan = make_plan("xyz", delay=5.0)
    scheduler = TypingScheduler(backend, RealClock())
    finished = threading.Event()
    scheduler.start(plan, on_finished=lambda result: finished.set())

    assert wait_until(lambda: scheduler.is_running)
    time.sleep(0.05)
    started = time.monotonic()
    scheduler.abort()
    assert finished.wait(1.0), "the scheduler did not stop after abort"
    latency = time.monotonic() - started
    assert latency < 0.1, f"abort took {latency * 1000:.0f} ms"
    assert backend.release_all_count >= 1
    assert backend.pressed_keys == ()


def test_abort_from_paused_stops_the_run(backend) -> None:
    plan = make_plan("abcdef", delay=0.2)
    scheduler = TypingScheduler(backend, RealClock())
    paused = threading.Event()
    finished = threading.Event()
    results = []
    scheduler.start(
        plan,
        on_paused=paused.set,
        on_finished=lambda result: (results.append(result), finished.set()),
    )
    scheduler.pause()
    assert wait_until(paused.is_set)
    scheduler.abort()
    assert finished.wait(1.0)
    assert results[0].status is RunStatus.ABORTED
    assert not scheduler.is_running


def test_abort_reports_how_far_it_got(backend) -> None:
    plan = make_plan("abcdefghij", delay=0.02)
    scheduler = TypingScheduler(backend, RealClock())
    finished = threading.Event()
    results = []
    scheduler.start(plan, on_finished=lambda r: (results.append(r), finished.set()))
    assert wait_until(lambda: scheduler.characters_typed >= 2)
    scheduler.abort()
    assert finished.wait(2.0)
    result = results[0]
    assert result.status is RunStatus.ABORTED
    assert 0 < result.characters_typed < 10
    assert result.events_completed < plan.total_events


def test_abort_during_the_first_delay_emits_nothing(backend) -> None:
    """Aborting inside the very first delay must emit no key at all."""
    plan = make_plan("never typed", delay=0.5)
    scheduler = TypingScheduler(backend, FakeClock())

    def abort_on_first_sleep(clock: FakeClock, seconds: float) -> None:
        scheduler.abort()

    scheduler._clock.on_sleep = abort_on_first_sleep  # type: ignore[attr-defined]
    result = scheduler.run(plan)

    assert result.status is RunStatus.ABORTED
    assert result.characters_typed == 0
    assert simulate(backend.key_events) == ""
    assert backend.release_all_count >= 1


def test_a_stale_abort_does_not_kill_the_next_run(backend) -> None:
    """Control flags are reset when a run starts."""
    scheduler = TypingScheduler(backend, FakeClock())
    scheduler.abort()
    result = scheduler.run(make_plan("fresh", delay=0.0))
    assert result.status is RunStatus.COMPLETED
    assert simulate(backend.key_events) == "fresh"


# ---------------------------------------------------------------------------
# Failures and mutual exclusion
# ---------------------------------------------------------------------------


def test_backend_exceptions_stop_the_run_and_release_keys() -> None:
    def fail_on_third(kind: str, key: NormalizedKey) -> None:
        if kind == "down" and key.char == "c":
            raise RuntimeError("simulated backend failure")

    backend = RecordingKeyboardBackend(fail_on=fail_on_third)
    plan = make_plan("abcdef", delay=0.0)
    result = TypingScheduler(backend, FakeClock()).run(plan)

    assert result.status is RunStatus.FAILED
    assert isinstance(result.error, RuntimeError)
    assert backend.release_all_count >= 1
    assert backend.pressed_keys == ()
    assert not backend.calls or backend.calls[-1].kind == "release_all"


def test_only_one_job_can_run(backend) -> None:
    plan = make_plan("concurrent", delay=0.05)
    scheduler = TypingScheduler(backend, RealClock())
    scheduler.start(plan)
    with pytest.raises(SchedulerError):
        scheduler.start(plan)
    with pytest.raises(SchedulerError):
        scheduler.run(plan)
    scheduler.abort()
    scheduler.join(timeout=2.0)


def test_scheduler_can_be_reused_after_a_run(backend) -> None:
    scheduler = TypingScheduler(backend, FakeClock())
    assert scheduler.run(make_plan("one", delay=0.0)).status is RunStatus.COMPLETED
    backend.reset()
    assert scheduler.run(make_plan("two", delay=0.0)).status is RunStatus.COMPLETED
    assert simulate(backend.key_events) == "two"


def test_unknown_event_type_fails_cleanly(backend) -> None:
    class Weird:
        pass

    plan = build_plan(
        target_text="",
        events=[Weird()],  # type: ignore[list-item]
        seed=0,
        deliberate_errors=0,
        correction_events=0,
    )
    result = TypingScheduler(backend, FakeClock()).run(plan)
    assert result.status is RunStatus.FAILED
    assert isinstance(result.error, SchedulerError)
    assert backend.release_all_count >= 1


def test_key_up_is_emitted_for_every_key_down(backend) -> None:
    plan = make_plan("pairs", delay=0.0)
    TypingScheduler(backend, FakeClock()).run(plan)
    downs = [c for c in backend.calls if c.kind == "down"]
    ups = [c for c in backend.calls if c.kind == "up"]
    assert len(downs) == len(ups) == len("pairs")
    assert isinstance(backend.key_events[0], KeyDown)
    assert isinstance(backend.key_events[1], KeyUp)
