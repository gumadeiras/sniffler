"""Pulse detection on the LabJack hardware counter: the start gate and the sync record.

The step-timing thread owns the LabJack. It polls the counter only while it has
nothing else to do, so a read never delays a valve. The poll interval sets the
time resolution of every mark; a pulse shorter than one poll still counts,
because the counter saw it.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sniffler.config import TriggerSettings
from sniffler.hardware import DeviceError

POLL_SECONDS = 0.005
READ_FAILURE_LIMIT = 5


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
    count: int

    @property
    def seconds_per_read(self) -> float:
        return self.waited_seconds / self.reads if self.reads else 0.0

    def describe(self) -> str:
        text = f"{self.outcome.value} after {self.waited_seconds:.3f} s and {self.reads} reads"
        if self.reads:
            text += f", {self.seconds_per_read * 1000:.1f} ms per read"
        if self.count > 1:
            text += f"; {self.count} pulses arrived"
        return text


def wait_for_trigger(
    read_count: Callable[[], int],
    settings: TriggerSettings,
    *,
    clock: Callable[[], float],
    should_abort: Callable[[], bool],
    should_stop: Callable[[], bool],
    should_start_now: Callable[[], bool],
    poll_seconds: float = POLL_SECONDS,
) -> TriggerResult:
    """Poll the counter until it has counted a pulse, or until the operator ends the wait.

    The counter must have been reset when the wait was armed. The mark lands on
    the first read that sees the count, so its lag is at most one poll plus one
    device round trip.
    """
    started = clock()
    reads = 0
    count = 0
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
        count = read_count()
        reads += 1
        if count > 0:
            outcome = TriggerOutcome.RECEIVED
            break
        time.sleep(poll_seconds)
    ended = clock()
    return TriggerResult(outcome, ended, ended - started, reads, count)


class SyncRecorder:
    """Record every pulse the counter sees, from the step thread's idle time.

    ``poll`` runs only when the next deadline is far enough away that one read
    cannot delay it. A failed read is reported; after READ_FAILURE_LIMIT failures
    in a row the recorder stops and the run goes on without it.
    """

    def __init__(
        self,
        read_count: Callable[[], int],
        *,
        clock: Callable[[], float],
        on_pulse: Callable[[int, float, int], None],
        on_error: Callable[[str], None],
        on_stopped: Callable[[float], None],
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._read = read_count
        self._clock = clock
        self._on_pulse = on_pulse
        self._on_error = on_error
        self._on_stopped = on_stopped
        self._poll_seconds = poll_seconds
        self._next_poll = 0.0
        self._failures = 0
        self.count = 0
        self.pulses = 0
        self.stopped_seconds: float | None = None

    @property
    def active(self) -> bool:
        return self.stopped_seconds is None

    def start_from(self, count: int) -> None:
        """Take the count at the start gate as the baseline; that pulse is not a mark."""
        self.count = count

    def seconds_until_poll(self, now: float) -> float:
        return max(0.0, self._next_poll - now)

    def poll(self) -> None:
        """Read the counter once and report every pulse that arrived since the last read."""
        if not self.active:
            return
        now = self._clock()
        self._next_poll = now + self._poll_seconds
        try:
            count = self._read()
        except DeviceError as error:
            self._failures += 1
            self._on_error(str(error))
            if self._failures >= READ_FAILURE_LIMIT:
                self.stopped_seconds = self._clock()
                self._on_stopped(self.stopped_seconds)
            return
        self._failures = 0
        self.observe(count, self._clock())

    def observe(self, count: int, seen: float) -> None:
        """Take a count that another packet read, such as a valve write, as a poll result."""
        arrived = count - self.count
        for pulse in range(1, arrived + 1):
            self.pulses += 1
            self._on_pulse(self.count + pulse, seen, arrived)
        self.count = max(self.count, count)
