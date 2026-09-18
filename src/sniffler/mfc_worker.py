"""The MFC thread: one event loop that owns every Alicat connection during a run.

It applies setpoints that the step-timing thread hands over and samples each MFC
with one ``get()`` round trip, so a slow serial read never delays a valve.
"""

import asyncio
import queue
import threading
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from sniffler.hardware import DeviceError
from sniffler.recipe import RigMap
from sniffler.runlog import RunLog, RunLogError, wall_time_now

READ_FAILURE_LIMIT = 5


@dataclass(frozen=True)
class Sample:
    """One MFC reading next to the setpoint that was commanded at that time."""

    mfc: str
    run_seconds: float
    commanded_setpoint: float | None
    device_setpoint: float | None
    mass_flow: float | None
    wall_time: str


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


class MfcWorker(threading.Thread):
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
        self._commands: queue.Queue[tuple[str, float, float, dict[str, Any], str]] = queue.Queue()
        self._finished = threading.Event()
        self._final: tuple[dict[str, float], str] | None = None
        self._commanded: dict[str, float | None] = dict.fromkeys(rig.mfcs)
        self.ready = threading.Event()
        self.error: str | None = None
        self.full_scales: dict[str, tuple[float, str]] = {}

    @property
    def failed(self) -> bool:
        return self.error is not None

    def command(self, name: str, value: float, context: dict[str, Any], detail: str = "") -> None:
        """Queue a setpoint. ``context`` holds the schedule fields of the log row."""
        self._commands.put((name, value, self._clock(), context, detail))

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
            except (DeviceError, RunLogError) as error:
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
                if failures[name] >= READ_FAILURE_LIMIT:
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
                name, value, commanded_seconds, context, detail = self._commands.get_nowait()
            except queue.Empty:
                return applied
            applied = True
            await self._write(sessions, name, value, commanded_seconds, context, detail)

    async def _write(
        self,
        sessions: dict[str, Any],
        name: str,
        value: float,
        commanded_seconds: float,
        context: dict[str, Any],
        detail: str,
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
                detail=f"{detail}: {error}" if detail else str(error),
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
            detail=detail,
            **context,
        )

    async def _apply_final(self, sessions: dict[str, Any]) -> None:
        """Write the final setpoint to every MFC. No log failure skips a device."""
        setpoints, detail = self._final or (dict.fromkeys(sessions, 0.0), "safe state")
        problems: list[str] = []
        for name, session in sessions.items():
            value = setpoints.get(name, 0.0)
            commanded = self._clock()
            try:
                applied = await session.write_setpoint(value)
            except DeviceError as error:
                problems.append(f"MFC {name}: {error}")
                event, logged, note = "error", value, f"{detail}: {error}"
            else:
                self._commanded[name] = applied
                event, logged, note = "mfc_command", applied, detail
            try:
                self._log.event(
                    event,
                    returned_run_seconds=self._clock(),
                    commanded_run_seconds=commanded,
                    device=name,
                    value=logged,
                    detail=note,
                )
            except RunLogError as error:
                if str(error) not in problems:
                    problems.append(str(error))
        if problems:
            self.error = " ".join([*([self.error] if self.error else []), *problems])
