"""Wait for a TTL edge on a LabJack digital input before the trial schedule starts."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sniffler.config import TriggerSettings


class TriggerOutcome(StrEnum):
    RECEIVED = "received"
    STARTED_NOW = "started now"
    STOPPED = "stopped"
    ABORTED = "aborted"
    TIMED_OUT = "timed out"


@dataclass(frozen=True)
class TriggerResult:
    """How a wait ended, with the numbers that describe its resolution."""

    outcome: TriggerOutcome
    ended_seconds: float
    waited_seconds: float
    reads: int
    armed: bool

    @property
    def seconds_per_read(self) -> float:
        return self.waited_seconds / self.reads if self.reads else 0.0

    def describe(self) -> str:
        text = f"{self.outcome.value} after {self.waited_seconds:.3f} s and {self.reads} reads"
        if self.reads:
            text += f", {self.seconds_per_read * 1000:.1f} ms per read"
        if not self.armed:
            text += "; the line never showed the level before the edge"
        return text


def wait_for_trigger(
    read: Callable[[], bool],
    settings: TriggerSettings,
    *,
    clock: Callable[[], float],
    should_abort: Callable[[], bool],
    should_stop: Callable[[], bool],
    should_start_now: Callable[[], bool],
) -> TriggerResult:
    """Poll the input until the configured edge, or until the operator ends the wait.

    The edge counts only after the line was seen at the level before it, so an
    open input that floats high cannot start a rising-edge run by itself. Each
    read is one device round trip, which sets the detection resolution; the
    result records how many reads the wait took.
    """
    active = settings.edge == "rising"
    started = clock()
    reads = 0
    armed = False
    while True:
        if should_abort():
            outcome = TriggerOutcome.ABORTED
            break
        if should_stop():
            outcome = TriggerOutcome.STOPPED
            break
        if should_start_now():
            outcome = TriggerOutcome.STARTED_NOW
            break
        timeout = settings.timeout_seconds
        if timeout is not None and clock() - started >= timeout:
            outcome = TriggerOutcome.TIMED_OUT
            break
        level = read()
        reads += 1
        if not armed:
            armed = level != active
        elif level == active:
            outcome = TriggerOutcome.RECEIVED
            break
    ended = clock()
    return TriggerResult(outcome, ended, ended - started, reads, armed)
