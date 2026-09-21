"""The run status types that the executor publishes and the GUI and bench read."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Phase(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    WAITING = "waiting"
    RUNNING = "running"
    FINISHING = "finishing"
    DONE = "done"
    STOPPED = "stopped"
    ABORTED = "aborted"
    FAILED = "failed"

    @property
    def is_final(self) -> bool:
        return self in {Phase.DONE, Phase.STOPPED, Phase.ABORTED, Phase.FAILED}


@dataclass(frozen=True)
class Event:
    """One events.csv row, as the step-timing thread recorded it.

    Delivered to ``on_event`` after the row is written. The MFC worker writes its
    ``mfc_command`` and ``error`` rows itself and does not deliver them here.
    """

    event: str
    returned_run_seconds: float
    returned_wall_time: str
    scheduled_run_seconds: float | None = None
    commanded_run_seconds: float | None = None
    trial_index: int | None = None
    trial_name: str = ""
    step_index: int | None = None
    device: str = ""
    value: object = ""
    detail: str = ""
    sync_count: int | None = None


@dataclass(frozen=True)
class Status:
    """A snapshot of the run that is safe to read from any thread."""

    phase: Phase = Phase.IDLE
    message: str = ""
    run_directory: Path | None = None
    order: tuple[str, ...] = ()
    planned_seconds: float = 0.0
    trial_index: int | None = None
    step_index: int | None = None
    trial_started_seconds: float | None = None
    step_started_seconds: float | None = None
    step_duration_seconds: float | None = None
    valves: dict[str, bool] = field(default_factory=dict)
    setpoints: dict[str, float] = field(default_factory=dict)
    stop_requested: bool = False
    # Run seconds at which the trial schedule started: 0.0 for a run that did not
    # wait, the trigger time for one that did, None until the trials start.
    trigger_seconds: float | None = None
    # Sync pulses recorded so far; None until the record starts at the schedule
    # start, and always None when the rig has no trigger line.
    sync_pulses: int | None = None
    sync_stopped_seconds: float | None = None

    @property
    def trial_name(self) -> str | None:
        if self.trial_index is None or self.trial_index >= len(self.order):
            return None
        return self.order[self.trial_index]
