"""Run a recipe on the rig from a worker thread and publish its progress.

Step timing runs on the executor thread. The MFC serial traffic runs on its
own thread (see ``mfc_worker``), so a slow MFC read never delays a valve.
``apply_safe_state`` puts the rig in the safe state outside a run.
"""

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import replace
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
    start_pulse_problem,
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
from sniffler.status import Event, Phase, Status
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


class _Aborted(Exception):
    """Abort now was requested."""


def apply_safe_state(
    rig: RigMap,
    *,
    open_labjack: Callable[..., Any] = hardware.open_labjack,
    open_alicat: Callable[..., Any] = hardware.open_alicat,
) -> list[str]:
    """Close every valve, drive the TTL output low, and zero every MFC, outside a run.

    Every device is commanded even when another one fails. Each returned problem
    says whether the hardware might have changed. Nothing is logged: this is not
    a run, and the caller refuses it while a run is marked active.
    """
    problems: list[str] = []
    lines = dict.fromkeys(rig.valves.values(), False)
    if rig.ttl_output is not None:
        lines[rig.ttl_output.channel] = False
    try:
        with open_labjack(rig.labjack_serial) as labjack:
            labjack.write_digital_lines(lines)
    except DeviceError as error:
        problems.append(str(error))
    return problems + asyncio.run(_zero_setpoints(rig, open_alicat))


async def _zero_setpoints(rig: RigMap, open_alicat: Callable[..., Any]) -> list[str]:
    problems: list[str] = []
    for name, mfc in rig.mfcs.items():
        try:
            async with open_alicat(
                mfc.port, mfc.unit, mfc.baud_rate, mfc.timeout_seconds
            ) as session:
                await session.prepare_setpoints()  # the mode and source guard
                await session.write_setpoint(0.0)
        except DeviceError as error:
            problems.append(f"MFC {name}: {error}")
    return problems


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
        send_ttl: bool = False,
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
        # Raise the TTL output with the first step; lab.toml says for how long.
        self._send_ttl = send_ttl
        self._thread: threading.Thread | None = None
        self._log: RunLog | None = None
        self._started_at: float | None = None
        self._schedule_offset = 0.0
        self._sync: SyncRecorder | None = None
        self._log_failure: str | None = None
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
        if self._send_ttl:
            output = self._rig.ttl_output
            problem = (
                "lab.toml has no [ttl_output] table, so the run cannot send a TTL at start."
                if output is None
                else None
                if output.holds_high
                else start_pulse_problem(self._recipe, output.pulse_seconds)
            )
            if problem is not None:
                return self._finish(Phase.FAILED, f"{problem} No hardware was used.")

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
                    "send_ttl": self._send_ttl,
                    "recipe": self._recipe.to_dict(),
                    "rig_map": self._rig.to_dict(),
                },
                mfcs=self._rig.mfcs,
                valves=self._rig.valves,
                ttl_lines=[self._ttl_line()] if self._send_ttl else [],
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
        try:
            phase, message, full_scales = self._run_hardware(order)
        except BaseException as error:
            # Ctrl-C or a bug. The hardware branch applied the safe state; end the record.
            phase, message, full_scales = Phase.FAILED, "The run was interrupted.", {}
            if isinstance(error, Exception):
                message = f"The run stopped on an unexpected error: {error!r}"
            raise
        finally:
            self._record("run_end", detail=f"{phase.value}: {message}")
            self._log = None
            problems = []
            try:
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
            except RunLogError as error:
                problems.append(str(error))
            try:
                lock.release()
            except RunLockError as error:
                problems.append(str(error))
            self._publish(phase=phase, message=" ".join([message, *problems]))
        return self.status

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
            self._read_valves(labjack)
            if self._send_ttl:
                # A defined low before the TTL rises; before the counter arms, so a looped-back
                # edge from this write is not counted.
                self._write_lines(labjack, {}, {}, detail="low before the run", ttl=False)
            if self._rig.trigger is not None:
                self._arm_counter(labjack)
            baseline = self._await_trigger(labjack, worker) if self._wait_for_trigger else 0
            if self._rig.trigger is not None:
                self._start_sync(labjack, baseline)
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
        except BaseException as error:
            self._publish(
                phase=Phase.FINISHING, message="Error. Closing all valves, every flow to zero."
            )
            problems = self._apply_final_state(labjack, worker, safe_state(self._rig), "safe_state")
            if not isinstance(error, Exception):
                raise  # Ctrl-C: the safe state is applied; the caller ends the record.
            problems = [problem for problem in problems if problem != str(error)]
            message = f"{error} " + (" ".join(problems) or "All valves closed, every flow zero.")
            return Phase.FAILED, message, worker.full_scales

    def _trigger_line(self) -> str:
        assert self._rig.trigger is not None
        return hardware.digital_channel_name(self._rig.trigger.channel)

    def _ttl_line(self) -> str:
        assert self._rig.ttl_output is not None
        return hardware.digital_channel_name(self._rig.ttl_output.channel)

    def _read_valves(self, labjack: Any) -> None:
        """Record the state of every valve line before the run commands it.

        Nothing on the device changes. Each valve series starts with this row, so
        a valve the recipe never names still has a known state for the whole run.
        A line that is still an input is not driven; the event row says so.
        """
        lines = labjack.read_digital_lines(list(self._rig.valves.values()))
        row = {
            "returned_run_seconds": self.elapsed_seconds(),
            "returned_wall_time": wall_time_now(),
        }
        for name, channel in self._rig.valves.items():
            is_input, level = lines[channel]
            self._record(
                "valve_read",
                device=name,
                value="open" if level else "closed",
                detail="the line is an input; the valve is not driven" if is_input else "",
                **row,
            )
            self._series("valve", name, level, row)

    def _arm_counter(self, labjack: Any) -> None:
        """Count pulses on the trigger line from now on."""
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

    def _start_sync(self, labjack: Any, baseline: int) -> None:
        """Record every pulse above ``baseline`` from the step thread's idle time.

        The record starts when the schedule starts, so the gate pulse is the
        baseline and not a mark.
        """
        self._sync = SyncRecorder(
            labjack.read_counter,
            clock=self.elapsed_seconds,
            on_pulse=self._on_sync_pulse,
            on_error=self._on_sync_error,
            on_stopped=self._on_sync_stopped,
        )
        self._sync.start_from(baseline)
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

    def _await_trigger(self, labjack: Any, worker: MfcWorker) -> int:
        """Hold the recipe end state and poll the counter until the schedule may start.

        Return the pulse count at the end of the wait. A stop request ends the wait
        and lets ``_run_trials`` end the run with no trial. Abort and timeout end
        in the safe state through the callers.
        """
        trigger = self._rig.trigger
        assert trigger is not None
        line = self._trigger_line()
        timeout = trigger.timeout_seconds
        self._publish(
            phase=Phase.WAITING,
            message=f"Waiting for the TTL pulse on {line}."
            + (f" Timeout {timeout:g} s." if timeout is not None else ""),
        )
        self._record("trigger_wait", device=line, detail="counter armed")
        rest = self._recipe.shutdown
        self._apply_step(rest, labjack, worker, {}, detail="rest state while waiting")
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
            self._record(
                "trigger_received",
                returned_run_seconds=result.ended_seconds,
                device=line,
                value=result.outcome.value,
                detail=result.describe(),
            )
            return result.count
        self._record(
            "trigger_end", device=line, value=result.outcome.value, detail=result.describe()
        )
        if result.outcome is TriggerOutcome.ABORTED:
            raise _Aborted
        if result.outcome is TriggerOutcome.TIMED_OUT:
            raise DeviceError(f"No TTL pulse on {line} within {timeout:g} s.")
        return result.count

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
                # The TTL output rises in the packet that switches the first valves.
                first = self._send_ttl and trial_index == 0 and step_index == 0
                rose = self._apply_step(step, labjack, worker, context, ttl=True if first else None)
                self._publish(
                    step_index=step_index,
                    step_started_seconds=origin + cumulative,
                    step_duration_seconds=step.duration_seconds,
                    valves=dict(step.valves),
                    setpoints=dict(step.setpoints),
                )
                output = self._rig.ttl_output
                if first and output is not None and not output.holds_high:
                    # A pulse: the width counts from the moment the high write returned.
                    # In high mode the line falls with the end state instead.
                    assert rose is not None
                    self._wait_until(rose + output.pulse_seconds, worker)
                    self._write_lines(labjack, {}, context, detail="start pulse", ttl=False)
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
            if self._log_failure is not None:
                raise RunLogError(self._log_failure)
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
        self,
        step: Step,
        labjack: Any,
        worker: MfcWorker,
        context: dict[str, Any],
        *,
        detail: str = "",
        ttl: bool | None = None,
    ) -> float | None:
        """Command every changed device. ``context`` holds the schedule fields of each row.

        ``ttl`` drives the TTL output in the same packet as the valves. Return the run
        seconds at which that packet returned, or None when no packet was sent.
        """
        for name, value in step.setpoints.items():
            if self._last_setpoints.get(name) != value:
                worker.command(name, value, context, detail)
        self._last_setpoints = dict(step.setpoints)
        if step.valves == self._last_valves and ttl is None:
            return None
        return self._write_lines(labjack, step.valves, context, detail=detail, ttl=ttl)

    def _write_lines(
        self,
        labjack: Any,
        valves: dict[str, bool],
        context: dict[str, Any],
        *,
        detail: str = "",
        record_all: bool = False,
        ttl: bool | None = None,
    ) -> float:
        """Write the valves, and the TTL output when ``ttl`` is given, in one transaction.

        Each changed valve and the TTL edge get one row with the same times and count.
        Return the run seconds at which the packet returned.
        """
        channels = {self._rig.valves[name]: state for name, state in valves.items()}
        if ttl is not None:
            assert self._rig.ttl_output is not None
            channels[self._rig.ttl_output.channel] = ttl
        sync = self._sync if self._sync is not None and self._sync.active else None
        commanded = self.elapsed_seconds()
        count = labjack.write_digital_lines(channels, read_counter=sync is not None)
        returned = self.elapsed_seconds()
        row = {
            "returned_run_seconds": returned,
            "commanded_run_seconds": commanded,
            "returned_wall_time": wall_time_now(),
            "sync_count": count,
            "detail": detail,
            **context,
        }
        previous = self._last_valves
        if valves:
            self._last_valves = dict(valves)
        for name, state in valves.items():
            if not record_all and previous is not None and previous.get(name) == state:
                continue
            self._record("valve_command", device=name, value="open" if state else "closed", **row)
            self._series("valve", name, state, row)
        if ttl is not None:
            line = self._ttl_line()
            self._record("ttl_command", device=line, value="high" if ttl else "low", **row)
            self._series("ttl", line, ttl, row)
        if sync is not None and count is not None:
            sync.observe(count, returned)
        return returned

    def _apply_final_state(
        self, labjack: Any, worker: MfcWorker, state: Step, event: str
    ) -> list[str]:
        """Apply a final state to every device. Return the problems that remain."""
        problems: list[str] = []
        ttl = False if self._send_ttl else None  # low whenever this run raised it
        try:
            self._write_lines(labjack, state.valves, {}, detail=event, record_all=True, ttl=ttl)
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
        if self._log_failure is not None:
            problems.append(self._log_failure)
        self._publish(valves=dict(state.valves), setpoints=dict(state.setpoints))
        self._record(event, detail="; ".join(problems) or "applied")
        return problems

    def _record(self, event: str, **fields: Any) -> None:
        """Write one event row, then hand the same fields to ``on_event``.

        The callback must not block: the GUI side only puts into a queue. A row
        that cannot be written never stops a hardware command; the first failure
        is kept and ends the run at the next step deadline.
        """
        log = self._log
        if log is None:
            return
        fields.setdefault("returned_run_seconds", self.elapsed_seconds())
        fields.setdefault("returned_wall_time", wall_time_now())
        try:
            log.event(event, **fields)
        except RunLogError as error:
            self._log_failure = self._log_failure or str(error)
        if self._on_event is not None:
            self._on_event(Event(event, **fields))

    def _series(self, kind: str, device: str, state: bool, row: dict[str, Any]) -> None:
        """Write one row of a valve or TTL series from the fields of its event row.

        Same failure rule as ``_record``: the row never stops a hardware command.
        """
        log = self._log
        if log is None:
            return
        try:
            log.digital_state(
                kind,
                device,
                state,
                returned_run_seconds=row["returned_run_seconds"],
                commanded_run_seconds=row.get("commanded_run_seconds"),
                scheduled_run_seconds=row.get("scheduled_run_seconds"),
                trial_index=row.get("trial_index"),
                trial_name=row.get("trial_name", ""),
                step_index=row.get("step_index"),
                sync_count=row.get("sync_count"),
                wall_time=row["returned_wall_time"],
            )
        except RunLogError as error:
            self._log_failure = self._log_failure or str(error)

    def _publish(self, **changes: Any) -> None:
        with self._lock:
            self._status = replace(self._status, **changes)
            status = self._status
        if self._on_status is not None:
            self._on_status(status)

    def _finish(self, phase: Phase, message: str) -> Status:
        self._publish(phase=phase, message=message)
        return self.status
