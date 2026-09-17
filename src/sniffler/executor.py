"""Run a recipe on the rig from a worker thread and publish its progress.

Step timing runs on the executor thread. The MFC serial traffic runs on its
own thread with its own event loop, so a slow MFC read never delays a valve.
"""

import asyncio
import queue
import threading
import time
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field, replace
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import Any

from sniffler import hardware
from sniffler.hardware import DeviceError
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

SPIN_SECONDS = 0.015
MFC_READ_FAILURE_LIMIT = 5
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
class Sample:
    """One MFC reading next to the setpoint that was commanded at that time."""

    mfc: str
    run_seconds: float
    commanded_setpoint: float | None
    device_setpoint: float | None
    mass_flow: float | None
    wall_time: str


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

    @property
    def trial_name(self) -> str | None:
        if self.trial_index is None or self.trial_index >= len(self.order):
            return None
        return self.order[self.trial_index]


class _Aborted(Exception):
    """Abort now was requested."""


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


class _MfcWorker(threading.Thread):
    """Own the MFC connections in one event loop, off the step-timing thread."""

    def __init__(
        self,
        rig: RigMap,
        open_alicat: Callable[..., Any],
        clock: Callable[[], float],
        log: RunLog,
        on_sample: Callable[[Sample], None] | None,
        interval_seconds: float,
    ) -> None:
        super().__init__(name="sniffler-mfc", daemon=True)
        self._rig = rig
        self._open_alicat = open_alicat
        self._clock = clock
        self._log = log
        self._on_sample = on_sample
        self._interval = interval_seconds
        self._commands: queue.Queue[tuple[str, float, float, dict[str, Any]]] = queue.Queue()
        self._finished = threading.Event()
        self._final: tuple[dict[str, float], str] | None = None
        self._commanded: dict[str, float | None] = dict.fromkeys(rig.mfcs)
        self.ready = threading.Event()
        self.error: str | None = None
        self.full_scales: dict[str, tuple[float, str]] = {}

    @property
    def failed(self) -> bool:
        return self.error is not None

    def command(self, name: str, value: float, context: dict[str, Any]) -> None:
        self._commands.put((name, value, self._clock(), context))

    def finish(self, setpoints: dict[str, float], detail: str) -> None:
        if self._final is None:
            self._final = (dict(setpoints), detail)
        self._finished.set()

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as error:
            self.error = self.error or f"The MFC worker failed: {error}"
        finally:
            self.ready.set()

    async def _main(self) -> None:
        sessions: dict[str, Any] = {}
        async with AsyncExitStack() as stack:
            for name, mfc in self._rig.mfcs.items():
                try:
                    session = await stack.enter_async_context(
                        self._open_alicat(mfc.port, mfc.unit, mfc.baud_rate, mfc.timeout_seconds)
                    )
                    self.full_scales[name] = await session.prepare_setpoints()
                except DeviceError as error:
                    self.error = f"MFC {name}: {error}"
                    return
                sessions[name] = session
            self.ready.set()
            try:
                await self._serve(sessions)
            except DeviceError as error:
                self.error = str(error)
            finally:
                await self._apply_final(sessions)

    async def _serve(self, sessions: dict[str, Any]) -> None:
        due = dict.fromkeys(sessions, self._clock())
        failures = dict.fromkeys(sessions, 0)
        while not self._finished.is_set():
            if await self._apply_commands(sessions):
                continue
            if not due:
                await asyncio.sleep(0.01)
                continue
            name = min(due, key=due.__getitem__)
            now = self._clock()
            if due[name] > now:
                await asyncio.sleep(min(due[name] - now, 0.005))
                continue
            due[name] = max(due[name] + self._interval, now)
            try:
                state = await sessions[name].read()
            except DeviceError as error:
                failures[name] += 1
                self._log.event(
                    "error", returned_run_seconds=self._clock(), device=name, detail=str(error)
                )
                if failures[name] >= MFC_READ_FAILURE_LIMIT:
                    raise DeviceError(
                        f"MFC {name} did not answer {failures[name]} reads in a row: {error}"
                    ) from error
                continue
            failures[name] = 0
            run_seconds = self._clock()
            wall_time = wall_time_now()
            self._log.sample(
                name,
                run_seconds=run_seconds,
                commanded_setpoint=self._commanded[name],
                state=state,
                wall_time=wall_time,
            )
            if self._on_sample is not None:
                self._on_sample(
                    Sample(
                        mfc=name,
                        run_seconds=run_seconds,
                        commanded_setpoint=self._commanded[name],
                        device_setpoint=_number(state.get("setpoint")),
                        mass_flow=_number(state.get("mass_flow")),
                        wall_time=wall_time,
                    )
                )

    async def _apply_commands(self, sessions: dict[str, Any]) -> bool:
        applied = False
        while True:
            try:
                name, value, commanded_seconds, context = self._commands.get_nowait()
            except queue.Empty:
                return applied
            applied = True
            await self._write(sessions, name, value, commanded_seconds, context)

    async def _write(
        self,
        sessions: dict[str, Any],
        name: str,
        value: float,
        commanded_seconds: float,
        context: dict[str, Any],
    ) -> None:
        try:
            applied = await sessions[name].write_setpoint(value)
        except DeviceError as error:
            self._log.event(
                "error",
                returned_run_seconds=self._clock(),
                commanded_run_seconds=commanded_seconds,
                device=name,
                value=value,
                detail=str(error),
                **context,
            )
            raise DeviceError(f"MFC {name}: {error}") from error
        returned = self._clock()
        self._commanded[name] = applied
        self._log.event(
            "mfc_command",
            returned_run_seconds=returned,
            commanded_run_seconds=commanded_seconds,
            device=name,
            value=applied,
            **context,
        )

    async def _apply_final(self, sessions: dict[str, Any]) -> None:
        setpoints, detail = self._final or (dict.fromkeys(sessions, 0.0), "safe state")
        problems: list[str] = []
        for name, session in sessions.items():
            commanded = self._clock()
            try:
                applied = await session.write_setpoint(setpoints.get(name, 0.0))
            except DeviceError as error:
                problems.append(f"MFC {name}: {error}")
                self._log.event(
                    "error",
                    returned_run_seconds=self._clock(),
                    commanded_run_seconds=commanded,
                    device=name,
                    value=setpoints.get(name, 0.0),
                    detail=f"{detail}: {error}",
                )
                continue
            self._commanded[name] = applied
            self._log.event(
                "mfc_command",
                returned_run_seconds=self._clock(),
                commanded_run_seconds=commanded,
                device=name,
                value=applied,
                detail=detail,
            )
        if problems:
            self.error = " ".join([*([self.error] if self.error else []), *problems])


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
        self._thread: threading.Thread | None = None
        self._log: RunLog | None = None
        self._started_at: float | None = None
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

    def run(self) -> Status:
        """Run the recipe on the calling thread and return the final status."""
        try:
            validate_recipe(self._recipe, self._rig)
            order = resolve_trial_order(self._recipe.schedule, self._seed)
        except RecipeError as error:
            return self._finish(
                Phase.FAILED, f"The recipe is not valid. No hardware was used.\n{error}"
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
        worker = _MfcWorker(
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
            self._publish(phase=Phase.RUNNING, message="Running.")
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

    def _run_trials(self, order: list[str], labjack: Any, worker: _MfcWorker) -> bool:
        cumulative = 0.0
        for trial_index, name in enumerate(order):
            if self._stop.is_set():
                return True
            trial = self._recipe.trial(name)
            self._publish(
                trial_index=trial_index, trial_started_seconds=cumulative, step_index=None
            )
            self._record(
                "trial_start",
                scheduled_run_seconds=cumulative,
                trial_index=trial_index,
                trial_name=name,
            )
            for step_index, step in enumerate(trial.steps):
                self._wait_until(cumulative, worker)
                context = {
                    "scheduled_run_seconds": cumulative,
                    "trial_index": trial_index,
                    "trial_name": name,
                    "step_index": step_index,
                }
                self._apply_step(step, labjack, worker, context)
                self._publish(
                    step_index=step_index,
                    step_started_seconds=cumulative,
                    step_duration_seconds=step.duration_seconds,
                    valves=dict(step.valves),
                    setpoints=dict(step.setpoints),
                )
                cumulative += step.duration_seconds or 0.0
            self._wait_until(cumulative, worker)
            self._record(
                "trial_end",
                scheduled_run_seconds=cumulative,
                trial_index=trial_index,
                trial_name=name,
            )
        return False

    def _wait_until(self, deadline_seconds: float, worker: _MfcWorker) -> None:
        while True:
            if self._abort.is_set():
                raise _Aborted
            if worker.failed:
                raise DeviceError(worker.error or "The MFC worker failed.")
            remaining = deadline_seconds - self.elapsed_seconds()
            if remaining <= 0:
                return
            if remaining > SPIN_SECONDS:
                self._abort.wait(min(remaining - SPIN_SECONDS, 0.05))

    def _apply_step(
        self, step: Step, labjack: Any, worker: _MfcWorker, context: dict[str, Any]
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
        commanded = self.elapsed_seconds()
        labjack.write_digital_lines(channels)
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
                **context,
            )

    def _apply_final_state(
        self, labjack: Any, worker: _MfcWorker, state: Step, event: str
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
