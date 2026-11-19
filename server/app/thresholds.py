"""Pure continuous-threshold decisions; no database, threads or wall-clock reads."""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PendingThreshold:
    since_monotonic: float
    since_utc: float
    phase: Literal['trigger', 'recovery']


@dataclass(frozen=True)
class ThresholdDecision:
    action: Literal['hold', 'raise', 'recover', 'reset']
    pending: PendingThreshold | None = None
    started_at: float | None = None


def sampling_interrupted(previous_received_at, received_at, offline_after_seconds):
    """A missing interval cannot count as continuously observed evidence."""
    return previous_received_at is not None and received_at - previous_received_at >= offline_after_seconds


def evaluate_threshold(*, value, direction, limits, active, pending, sample_fresh,
                       now_monotonic, now_utc, trigger_seconds, recovery_seconds,
                       sample_gap=False):
    if not sample_fresh or value is None:
        return ThresholdDecision('reset')

    phase = 'recovery' if active else 'trigger'
    if active:
        qualifies = (value >= limits['min'] + limits['hysteresis'] if direction == 'low'
                     else value <= limits['max'] - limits['hysteresis'])
    else:
        qualifies = value < limits['min'] if direction == 'low' else value > limits['max']
    if not qualifies:
        return ThresholdDecision('reset')

    if sample_gap or pending is None or pending.phase != phase:
        pending = PendingThreshold(now_monotonic, now_utc, phase)
    duration = recovery_seconds if active else trigger_seconds
    if now_monotonic - pending.since_monotonic < duration:
        return ThresholdDecision('hold', pending)
    if active:
        return ThresholdDecision('recover')
    return ThresholdDecision('raise', started_at=pending.since_utc)
