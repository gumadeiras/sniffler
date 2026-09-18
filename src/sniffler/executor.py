"""Run a recipe on the rig from a worker thread and publish its progress.

Step timing runs on the executor thread. The MFC serial traffic runs on its
own thread (see ``mfc_worker``), so a slow MFC read never delays a valve.
"""

import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import Any

from sniffler import hardware
from sniffler.hardware import DeviceError
from sniffler.mfc_worker import MfcWorker, Sample
from sniffler.recipe import (
    Recipe,
    RecipeError,
    RigMap,
    Step,
    resolve_trial_order,
    resolved_duration_seconds,
    safe_state,
    validate_recipe,
)
from sniffler.runlog import (
    RunLock,
    RunLockError,
    RunLog,
    RunLogError,
    new_run_directory,
    wall_time_now,
)
from sniffler.trigger import READ_FAILURE_LIMIT, SyncRecorder, TriggerOutcome, wait_for_trigger

SPIN_SECONDS = 0.015
# No counter poll starts closer than this to a deadline: spin window plus one round trip.
SYNC_MARGIN_SECONDS = 0.03
MFC_WORKER_TIMEOUT_SECONDS = 15.0
DEFAULT_SAMPLE_INTERVAL_SECONDS = 0.1


def software_version() -> str:
    try:
        return metadata.version("sniffler")
    except metadata.PackageNotFoundError:
        return "unknown"


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
    # Sync pulses recorded so far; None when the rig has no trigger line.
    sync_pulses: int | None = None
    sync_stopped_seconds: float | None = None

    @property
    def trial_name(self) -> str | None:
        if self.trial_index is None or self.trial_index >= len(self.order):
            return None
        return self.order[self.trial_index]


class _Aborted(Exception):
    """Abort now was requested."""


class Executor:
    """Run one recipe. Create one executor for each run."""

    def __init__(
        self,
        recipe: Recipe,
        rig: RigMap,
        seed: int,
        runs_directory: Path,
        *,
        operator_notes: str = "",
        open_labjack: Callable[..., Any] = hardware.open_labjack,
        open_alicat: Callable[..., Any] = hardware.open_alicat,
        on_status: Callable[[Status], None] | None = None,
        on_sample: Callable[[Sample], None] | None = None,
        on_event: Callable[[Event], None] | None = None,
        sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.perf_counter,
        wait_for_trigger: bool = False,
    ) -> None:
        self._recipe = recipe
        self._rig = rig
        self._seed = seed
        self._runs_directory = runs_directory
        self._operator_notes = operator_notes
        self._open_labjack = open_labjack
        self._open_alicat = open_alicat
        self._on_status = on_status
        self._on_sample = on_sample
        self._on_event = on_event
        self._sample_interval = sample_interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._status = Status()
        self._stop = threading.Event()
        self._abort = threading.Event()
        self._start_now = threading.Event()
        self._wait_for_trigger = wait_for_trigger
        self._thread: threading.Thread | None = None
        self._log: RunLog | None = None
        self._started_at: float | None = None
        self._schedule_offset = 0.0
        self._sync: SyncRecorder | None = None
        self._last_valves: dict[str, bool] | None = None
        self._last_setpoints: dict[str, float] = {}

    @property
    def status(self) -> Status:
        with self._lock:
            return self._status

    def elapsed_seconds(self) -> float:
        started = self._started_at
        return 0.0 if started is None else self._clock() - started

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("This executor has already started.")
        self._thread = threading.Thread(target=self.run, name="sniffler-executor", daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def request_stop(self) -> None:
        """Finish the current trial, then apply the recipe shutdown state."""
        self._stop.set()
        self._publish(stop_requested=True)
        self._record("stop_requested")

    def abort(self) -> None:
        """Stop now and force the safe state. The recipe shutdown state is ignored."""
        self._abort.set()
        self._record("abort_requested")

    def start_now(self) -> None:
        """End a trigger wait and start the trials now. No effect at any other time."""
        self._start_now.set()

    def run(self) -> Status:
        """Run the recipe on the calling thread and return the final status."""
        try:
            validate_recipe(self._recipe, self._rig)
            order = resolve_trial_order(self._recipe.schedule, self._seed)
        except RecipeError as error:
            return self._finish(
                Phase.FAILED, f"The recipe is not valid. No hardware was used.\n{error}"
            )
        if self._wait_for_trigger and self._rig.trigger is None:
            return self._finish(
                Phase.FAILED,
                "lab.toml has no [trigger] table, so the run cannot wait for a TTL trigger. "
                "No hardware was used.",
            )

        directory = new_run_directory(self._runs_directory, self._recipe.name)
        lock = RunLock(self._runs_directory, directory)
        try:
            lock.acquire()
        except RunLockError as error:
            return self._finish(Phase.FAILED, f"{error} No hardware was used.")
        log = RunLog(directory)
        try:
            log.open(
                {
                    "run_id": directory.name,
                    "started_at": wall_time_now(),
                    "software_version": software_version(),
                    "operator_notes": self._operator_notes,
                    "seed": self._seed,
                    "resolved_trial_order": order,
                    "planned_duration_seconds": resolved_duration_seconds(self._recipe, order),
                    "sample_interval_seconds": self._sample_interval,
                    "wait_for_trigger": self._wait_for_trigger,
                    "recipe": self._recipe.to_dict(),
                    "rig_map": self._rig.to_dict(),
                }
            )
        except RunLogError as error:
            lock.release()
            return self._finish(Phase.FAILED, f"{error} No hardware was used.")

        self._log = log
        self._publish(
            phase=Phase.STARTING,
            message="Opening the LabJack and the MFCs.",
            run_directory=directory,
            order=tuple(order),
            planned_seconds=resolved_duration_seconds(self._recipe, order),
            valves={},
            setpoints={},
        )
        phase, message, full_scales = self._run_hardware(order)
        self._record("run_end", detail=f"{phase.value}: {message}")
        log.close(
            ended_at=wall_time_now(),
            outcome=phase.value,
            message=message,
            trigger_seconds=self.status.trigger_seconds,
            sync_pulses=None if self._sync is None else self._sync.pulses,
            sync_recording_stopped_seconds=None
            if self._sync is None
            else self._sync.stopped_seconds,
            mfc_full_scales={name: list(scale) for name, scale in full_scales.items()},
        )
        self._log = None
        lock.release()
        return self._finish(phase, message)

    def _run_hardware(self, order: list[str]) -> tuple[Phase, str, dict[str, tuple[float, str]]]:
        try:
            with self._open_labjack(self._rig.labjack_serial) as labjack:
                return self._run_on(labjack, order)
        except DeviceError as error:
            return Phase.FAILED, f"{error} No hardware command was sent.", {}

    def _run_on(self, labjack: Any, order: list[str]) -> tuple[Phase, str, dict[str, Any]]:
        assert self._log is not None
        worker = MfcWorker(
            self._rig,
            self._open_alicat,
            self.elapsed_seconds,
            self._log,
            self._on_sample,
            self._sample_interval,
        )
        worker.start()
        try:
            if not worker.ready.wait(MFC_WORKER_TIMEOUT_SECONDS):
                raise DeviceError(f"The MFCs did not answer in {MFC_WORKER_TIMEOUT_SECONDS:g} s.")
            if worker.failed:
                raise DeviceError(worker.error or "The MFCs are not ready.")
            self._started_at = self._clock()
            self._record("run_start", detail=f"planned order: {', '.join(order)}")
            if self._rig.trigger is not None:
                self._arm_counter(labjack)
            if self._wait_for_trigger:
                self._await_trigger(labjack, worker)
            self._publish(
                phase=Phase.RUNNING, message="Running.", trigger_seconds=self._schedule_offset
            )
            stopped = self._run_trials(order, labjack, worker)
            self._publish(phase=Phase.FINISHING, message="Applying the end state.")
            problems = self._apply_final_state(
                labjack, worker, self._recipe.shutdown, "shutdown_state"
            )
            if problems:
                return Phase.FAILED, " ".join(problems), worker.full_scales
            if stopped:
                done = self._status.trial_index
                count = 0 if done is None else done + 1
                message = f"Stopped after trial {count} of {len(order)}. End state applied."
            else:
                message = f"Done. {len(order)} trials ran. End state applied."
            return Phase.STOPPED if stopped else Phase.DONE, message, worker.full_scales
        except _Aborted:
            self._publish(
                phase=Phase.FINISHING, message="Aborting. Closing all valves, every flow to zero."
            )
            problems = self._apply_final_state(labjack, worker, safe_state(self._rig), "safe_state")
            if problems:
                return Phase.FAILED, "Aborted. " + " ".join(problems), worker.full_scales
            message = "Aborted. All valves closed, every flow zero."
            return Phase.ABORTED, message, worker.full_scales
        except Exception as error:
            self._publish(
                phase=Phase.FINISHING, message="Error. Closing all valves, every flow to zero."
            )
            problems = self._apply_final_state(labjack, worker, safe_state(self._rig), "safe_state")
            problems = [problem for problem in problems if problem != str(error)]
            message = f"{error} " + (" ".join(problems) or "All valves closed, every flow zero.")
            return Phase.FAILED, message, worker.full_scales

    def _trigger_line(self) -> str:
        assert self._rig.trigger is not None
        return hardware.digital_channel_name(self._rig.trigger.channel)

    def _arm_counter(self, labjack: Any) -> None:
        """Count pulses on the trigger line from now on, and record them from idle time."""
        assert self._rig.trigger is not None
        line = self._trigger_line()
        labjack.enable_counter(self._rig.trigger.channel)
        before = labjack.read_counter(reset=True)
        _is_input, level = labjack.read_digital(self._rig.trigger.channel)
        self._record(
            "counter_enabled",
            device=line,
            value=before,
            detail=f"pulse counter on {line}; line {'high' if level else 'low'}; "
            f"count before reset {before}",
        )
        self._sync = SyncRecorder(
            labjack.read_counter,
            clock=self.elapsed_seconds,
            on_pulse=self._on_sync_pulse,
            on_error=self._on_sync_error,
            on_stopped=self._on_sync_stopped,
        )
        self._publish(sync_pulses=0)

    def _on_sync_pulse(self, count: int, seconds: float, arrived: int) -> None:
        status = self.status
        assert self._sync is not None
        self._record(
            "sync_pulse",
            returned_run_seconds=seconds,
            device=self._trigger_line(),
            value=count,
            trial_index=status.trial_index,
            trial_name=status.trial_name or "",
            step_index=status.step_index,
            detail=f"{arrived} pulses since the last read" if arrived > 1 else "",
            sync_count=count,
        )
        self._publish(sync_pulses=self._sync.pulses)

    def _on_sync_error(self, message: str) -> None:
        self._record("error", device=self._trigger_line(), detail=f"pulse counter: {message}")

    def _on_sync_stopped(self, seconds: float) -> None:
        self._record(
            "sync_recording_stopped",
            returned_run_seconds=seconds,
            device=self._trigger_line(),
            detail=f"{READ_FAILURE_LIMIT} failed counter reads in a row; the run goes on",
        )
        self._publish(sync_stopped_seconds=seconds)

    def _await_trigger(self, labjack: Any, worker: MfcWorker) -> None:
        """Hold the recipe end state and poll the counter until the schedule may start.

        A stop request ends the wait and lets ``_run_trials`` end the run with no
        trial. Abort and timeout end in the safe state through the callers.
        """
        trigger = self._rig.trigger
        assert trigger is not None and self._sync is not None
        line = self._trigger_line()
        timeout = trigger.timeout_seconds
        self._publish(
            phase=Phase.WAITING,
            message=f"Waiting for the TTL pulse on {line}."
            + (f" Timeout {timeout:g} s." if timeout is not None else ""),
        )
        self._record("trigger_wait", device=line, detail="counter armed")
        rest = self._recipe.shutdown
        self._apply_step(rest, labjack, worker, {"detail": "rest state while waiting"})
        self._publish(valves=dict(rest.valves), setpoints=dict(rest.setpoints))

        def read_count() -> int:
            if worker.failed:
                raise DeviceError(worker.error or "The MFC worker failed.")
            return labjack.read_counter()

        result = wait_for_trigger(
            read_count,
            trigger,
            clock=self.elapsed_seconds,
            should_abort=self._abort.is_set,
            should_stop=self._stop.is_set,
            should_start_now=self._start_now.is_set,
        )
        if result.outcome in {TriggerOutcome.RECEIVED, TriggerOutcome.STARTED_NOW}:
            self._schedule_offset = result.ended_seconds
            self._sync.start_from(result.count)
            self._record(
                "trigger_received",
                returned_run_seconds=result.ended_seconds,
                device=line,
                value=result.outcome.value,
                detail=result.describe(),
            )
            return
        self._record(
            "trigger_end", device=line, value=result.outcome.value, detail=result.describe()
        )
        if result.outcome is TriggerOutcome.ABORTED:
            raise _Aborted
        if result.outcome is TriggerOutcome.TIMED_OUT:
            raise DeviceError(f"No TTL pulse on {line} within {timeout:g} s.")

    def _run_trials(self, order: list[str], labjack: Any, worker: MfcWorker) -> bool:
        origin = self._schedule_offset
        cumulative = 0.0
        for trial_index, name in enumerate(order):
            if self._stop.is_set():
                return True
            trial = self._recipe.trial(name)
            self._publish(
                trial_index=trial_index, trial_started_seconds=origin + cumulative, step_index=None
            )
            self._record(
                "trial_start",
                scheduled_run_seconds=origin + cumulative,
                trial_index=trial_index,
                trial_name=name,
            )
            for step_index, step in enumerate(trial.steps):
                self._wait_until(origin + cumulative, worker)
                context = {
                    "scheduled_run_seconds": origin + cumulative,
                    "trial_index": trial_index,
                    "trial_name": name,
                    "step_index": step_index,
                }
                self._apply_step(step, labjack, worker, context)
                self._publish(
                    step_index=step_index,
                    step_started_seconds=origin + cumulative,
                    step_duration_seconds=step.duration_seconds,
                    valves=dict(step.valves),
                    setpoints=dict(step.setpoints),
                )
                cumulative += step.duration_seconds or 0.0
            self._wait_until(origin + cumulative, worker)
            self._record(
                "trial_end",
                scheduled_run_seconds=origin + cumulative,
                trial_index=trial_index,
                trial_name=name,
            )
        return False

    def _wait_until(self, deadline_seconds: float, worker: MfcWorker) -> None:
        """Wait for a step deadline; poll the pulse counter while the deadline is far."""
        while True:
            if self._abort.is_set():
                raise _Aborted
            if worker.failed:
                raise DeviceError(worker.error or "The MFC worker failed.")
            now = self.elapsed_seconds()
            remaining = deadline_seconds - now
            if remaining <= 0:
                return
            if remaining <= SPIN_SECONDS:
                continue
            wait = min(remaining - SPIN_SECONDS, 0.05)
            sync = self._sync
            if sync is not None and sync.active and remaining > SYNC_MARGIN_SECONDS:
                due = sync.seconds_until_poll(now)
                if due <= 0:
                    sync.poll()
                    continue
                wait = min(wait, due)
            self._abort.wait(wait)

    def _apply_step(
        self, step: Step, labjack: Any, worker: MfcWorker, context: dict[str, Any]
    ) -> None:
        for name, value in step.setpoints.items():
            if self._last_setpoints.get(name) != value:
                worker.command(name, value, context)
        self._last_setpoints = dict(step.setpoints)
        if step.valves != self._last_valves:
            self._write_valves(labjack, step.valves, context)

    def _write_valves(
        self,
        labjack: Any,
        valves: dict[str, bool],
        context: dict[str, Any],
        *,
        record_all: bool = False,
    ) -> None:
        """Write every valve in one transaction and record each changed valve."""
        channels = {self._rig.valves[name]: state for name, state in valves.items()}
        sync = self._sync if self._sync is not None and self._sync.active else None
        commanded = self.elapsed_seconds()
        count = labjack.write_digital_lines(channels, read_counter=sync is not None)
        returned = self.elapsed_seconds()
        wall_time = wall_time_now()
        previous = self._last_valves
        self._last_valves = dict(valves)
        for name, state in valves.items():
            if not record_all and previous is not None and previous.get(name) == state:
                continue
            self._record(
                "valve_command",
                returned_run_seconds=returned,
                commanded_run_seconds=commanded,
                device=name,
                value="open" if state else "closed",
                returned_wall_time=wall_time,
                sync_count=count,
                **context,
            )
        if sync is not None and count is not None:
            sync.observe(count, returned)

    def _apply_final_state(
        self, labjack: Any, worker: MfcWorker, state: Step, event: str
    ) -> list[str]:
        """Apply a final state to every device. Return the problems that remain."""
        problems: list[str] = []
        try:
            self._write_valves(labjack, state.valves, {"detail": event}, record_all=True)
        except DeviceError as error:
            problems.append(str(error))
        worker.finish(state.setpoints, event)
        worker.join(MFC_WORKER_TIMEOUT_SECONDS)
        if worker.is_alive():
            problems.append(
                f"The MFCs did not finish in {MFC_WORKER_TIMEOUT_SECONDS:g} s; "
                "the MFC setpoints might have changed."
            )
        elif worker.error:
            problems.append(worker.error)
        if self._rig.trigger is not None:
            # Also when arming failed halfway: the session restores only what it enabled.
            try:
                labjack.disable_counter()
                self._record("counter_restored", device=self._trigger_line())
            except DeviceError as error:
                problems.append(str(error))
        self._publish(valves=dict(state.valves), setpoints=dict(state.setpoints))
        self._record(event, detail="; ".join(problems) or "applied")
        return problems

    def _record(self, event: str, **fields: Any) -> None:
        """Write one event row, then hand the same fields to ``on_event``.

        The callback must not block: the GUI side only puts into a queue.
        """
        log = self._log
        if log is None:
            return
        fields.setdefault("returned_run_seconds", self.elapsed_seconds())
        fields.setdefault("returned_wall_time", wall_time_now())
        with suppress(RunLogError):
            log.event(event, **fields)
        if self._on_event is not None:
            self._on_event(Event(event, **fields))

    def _publish(self, **changes: Any) -> None:
        with self._lock:
            self._status = replace(self._status, **changes)
            status = self._status
        if self._on_status is not None:
            self._on_status(status)

    def _finish(self, phase: Phase, message: str) -> Status:
        self._publish(phase=phase, message=message)
        return self.status
