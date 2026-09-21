"""Bench checks: verify the rig with the real devices, one check at a time or all.

Read-only checks run by default. A check marked "actuates" moves valves, writes
setpoints, or changes the U3 counter configuration; it needs ``--actuate`` and
ends in the safe state. The timing, gate, and sync checks drive the same
Executor that the window uses, so there is still one executor.
"""

import argparse
import asyncio
import csv
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from sniffler import hardware
from sniffler.config import ConfigError, Settings, load_settings
from sniffler.executor import Executor, Phase, Status
from sniffler.hardware import DeviceError
from sniffler.recipe import Recipe, RigMap, Schedule, Step, Trial, rig_map_from_settings
from sniffler.runlog import refusal_for_active_run

LOOPBACK = "loopback"
MFC_READS = 20
VALVE_HOLD_SECONDS = 0.2
GATE_TIMEOUT_SECONDS = 10.0
PULSE_SECONDS = 0.1
LEAD_SECONDS = 0.5
SYNC_PULSE_SECONDS = 0.5
SYNC_PULSES = 4


@dataclass
class Result:
    check: str
    outcome: str  # pass, fail, blocked, or skipped
    lines: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = f"[{self.outcome.upper():7}] {self.check}"
        return "\n".join([head, *(f"    {line}" for line in self.lines)])


@dataclass
class Bench:
    """Everything a check needs. Tests inject fake device factories here."""

    settings: Settings
    rig: RigMap
    actuate: bool = False
    loopback: int | None = None
    open_labjack: Callable[..., Any] = hardware.open_labjack
    open_alicat: Callable[..., Any] = hardware.open_alicat
    ask: Callable[[str], str] = input

    def run_rig(self, extra_valves: dict[str, int] | None = None) -> RigMap:
        valves = {**self.rig.valves, **(extra_valves or {})}
        return replace(self.rig, valves=valves)


def milliseconds(values: Sequence[float]) -> str:
    """Summarize a distribution of seconds in milliseconds."""
    if not values:
        return "no values"
    ordered = sorted(value * 1000 for value in values)
    count = len(ordered)

    def percentile(fraction: float) -> float:
        return ordered[min(count - 1, int(fraction * count))]

    return (
        f"n={count} min={ordered[0]:.3f} median={statistics.median(ordered):.3f} "
        f"p95={percentile(0.95):.3f} p99={percentile(0.99):.3f} max={ordered[-1]:.3f} ms"
    )


def events_of(status: Status) -> list[dict[str, str]]:
    assert status.run_directory is not None
    with (status.run_directory / "events.csv").open(newline="") as file:
        return list(csv.DictReader(file))


def _seconds(row: dict[str, str], column: str) -> float:
    return float(row[column])


# Read-only checks ----------------------------------------------------------


def check_drivers(bench: Bench) -> Result:
    """The LabJack answers and its timer and counter configuration is readable."""
    try:
        with bench.open_labjack(bench.rig.labjack_serial) as session:
            values = session.status()
            configuration = session.timer_counter_configuration()
    except DeviceError as error:
        return Result("drivers", "fail", [str(error)])
    lines = [f"{name}: {value}" for name, value in values.items()]
    lines.append(f"timer and counter configuration: {configuration}")
    return Result("drivers", "pass", lines)


def check_ports(bench: Bench) -> Result:
    """Every configured MFC port is present among the serial ports."""
    try:
        ports = dict(hardware.list_serial_ports())
    except DeviceError as error:
        return Result("ports", "fail", [str(error)])
    lines = [f"{device}: {description}" for device, description in ports.items()]
    missing = [
        f"{name}: {mfc.port} is not present"
        for name, mfc in bench.rig.mfcs.items()
        if mfc.port not in ports
    ]
    lines.extend(missing)
    if not bench.rig.mfcs:
        lines.append("lab.toml names no MFC with a port")
    return Result("ports", "fail" if missing else "pass", lines)


async def _mfc_state(bench: Bench, name: str) -> dict[str, object]:
    mfc = bench.rig.mfcs[name]
    async with bench.open_alicat(mfc.port, mfc.unit, mfc.baud_rate, mfc.timeout_seconds) as session:
        return await session.status()


async def _mfc_lines(bench: Bench, name: str) -> tuple[bool, list[str]]:
    mfc = bench.rig.mfcs[name]
    async with bench.open_alicat(mfc.port, mfc.unit, mfc.baud_rate, mfc.timeout_seconds) as session:
        state = await session.status()
        latencies = []
        for _ in range(MFC_READS):
            started = time.perf_counter()
            await session.read()
            latencies.append(time.perf_counter() - started)
        source = state.get("setpoint_source")
        lines = [
            f"{name}: control point {state.get('control_point')!r}, setpoint source {source!r}",
            f"{name}: read latency {milliseconds(latencies)}",
        ]
        try:
            maximum, unit = await session.prepare_setpoints()
        except DeviceError as error:
            lines.append(f"{name}: setpoint writes blocked: {error}")
            return False, lines
        lines.append(f"{name}: device maximum {maximum:g} {unit}; setpoint writes allowed")
        return True, lines


def check_mfcs(bench: Bench) -> Result:
    """Each MFC answers, reports its mode, and its read latency is measured."""
    if not bench.rig.mfcs:
        return Result("mfcs", "skipped", ["lab.toml names no MFC with a port"])
    lines: list[str] = []
    writable = True
    for name in bench.rig.mfcs:
        try:
            allowed, mfc_lines = asyncio.run(_mfc_lines(bench, name))
        except DeviceError as error:
            return Result("mfcs", "fail", [*lines, f"{name}: {error}"])
        writable = writable and allowed
        lines.extend(mfc_lines)
    return Result("mfcs", "pass" if writable else "blocked", lines)


def check_safe(bench: Bench) -> Result:
    """Every valve line and the TTL output read low; every MFC setpoint reads zero."""
    lines: list[str] = []
    problems: list[str] = []
    outputs = dict(bench.rig.valves)
    if bench.rig.ttl_output is not None:
        outputs["TTL output"] = bench.rig.ttl_output.channel
    try:
        with bench.open_labjack(bench.rig.labjack_serial) as session:
            levels = session.read_digital_lines(outputs.values())
        for name, channel in outputs.items():
            _is_input, level = levels[channel]
            lines.append(f"{name} (channel {channel}): {'high' if level else 'low'}")
            if level:
                problems.append(f"{name} is high")
    except DeviceError as error:
        return Result("safe", "fail", [str(error)])
    for name in bench.rig.mfcs:
        try:
            state = asyncio.run(_mfc_state(bench, name))
        except DeviceError as error:
            problems.append(f"{name}: {error}")
            continue
        setpoint = state.get("setpoint")
        lines.append(f"{name}: setpoint {setpoint}")
        if setpoint not in (0, 0.0):
            problems.append(f"{name} setpoint is {setpoint}")
    return Result("safe", "fail" if problems else "pass", [*lines, *problems])


# Checks that actuate --------------------------------------------------------


def check_valves(bench: Bench) -> Result:
    """Each valve opens and closes on its own, then all together; each write is timed."""
    round_trips: list[float] = []
    lines: list[str] = []
    try:
        with bench.open_labjack(bench.rig.labjack_serial) as session:

            def write(states: dict[int, bool]) -> None:
                started = time.perf_counter()
                session.write_digital_lines(states)
                round_trips.append(time.perf_counter() - started)

            channels = list(bench.rig.valves.values())
            try:
                for name, channel in bench.rig.valves.items():
                    write({channel: True})
                    time.sleep(VALVE_HOLD_SECONDS)
                    write({channel: False})
                    lines.append(f"{name} (channel {channel}): opened and closed")
                write(dict.fromkeys(channels, True))
                time.sleep(VALVE_HOLD_SECONDS)
            finally:
                write(dict.fromkeys(channels, False))  # also on Ctrl-C
            levels = session.read_digital_lines(channels)
            open_after = [name for name, channel in bench.rig.valves.items() if levels[channel][1]]
    except DeviceError as error:
        return Result("valves", "fail", [*lines, str(error)])
    lines.append(f"write round trip {milliseconds(round_trips)}")
    if open_after:
        return Result("valves", "fail", [*lines, f"still high: {', '.join(open_after)}"])
    return Result("valves", "pass", lines)


def _pulse_recipe(
    rig: RigMap,
    *,
    valves: Sequence[str],
    pulse_seconds: float,
    pulses: int,
    lead_seconds: float,
    count: int,
    end_state: dict[str, bool] | None = None,
) -> Recipe:
    closed = dict.fromkeys(rig.valves, False)
    zero = dict.fromkeys(rig.mfcs, 0.0)

    def step(seconds: float, *open_valves: str) -> Step:
        states = dict(closed)
        for valve in open_valves:
            states[valve] = True
        return Step(seconds, states, zero)

    trials = []
    for valve in valves:
        steps = [step(lead_seconds)]
        for _ in range(pulses):
            steps += [step(pulse_seconds, valve), step(pulse_seconds)]
        trials.append(Trial(f"pulse {valve}", tuple(steps)))
    end = Step(None, {**closed, **(end_state or {})}, zero)
    return Recipe(
        "bench pulses",
        tuple(trials),
        Schedule(dict.fromkeys((trial.name for trial in trials), count), "as-listed", 1),
        end,
        notes="sniffler-bench",
    )


def _run(bench: Bench, recipe: Recipe, rig: RigMap, **options: Any) -> Status:
    return Executor(
        recipe,
        rig,
        1,
        bench.settings.runs_directory,
        operator_notes="sniffler-bench",
        open_labjack=bench.open_labjack,
        open_alicat=bench.open_alicat,
        **options,
    ).run()


def check_timing(bench: Bench) -> Result:
    """A pulse-train run on the real bus: lateness and round trip of every valve command."""
    valves = list(bench.rig.valves)
    if not valves:
        return Result("timing", "skipped", ["lab.toml names no valve"])
    recipe = _pulse_recipe(
        bench.rig,
        valves=valves,
        pulse_seconds=PULSE_SECONDS,
        pulses=5,
        lead_seconds=LEAD_SECONDS,
        count=2,
    )
    status = _run(bench, recipe, bench.rig)
    lines = [f"run directory: {status.run_directory}", status.message]
    if status.phase is not Phase.DONE:
        outcome = "blocked" if "setpoint" in status.message else "fail"
        return Result("timing", outcome, lines)
    rows = [
        row
        for row in events_of(status)
        if row["event"] == "valve_command" and row["scheduled_run_seconds"]
    ]
    lateness = [
        _seconds(row, "commanded_run_seconds") - _seconds(row, "scheduled_run_seconds")
        for row in rows
    ]
    round_trip = [
        _seconds(row, "returned_run_seconds") - _seconds(row, "commanded_run_seconds")
        for row in rows
    ]
    lines.append(f"scheduled to commanded {milliseconds(lateness)}")
    lines.append(f"commanded to returned {milliseconds(round_trip)}")
    if bench.rig.trigger is not None:
        counted = sum(1 for row in rows if row["sync_count"] != "")
        lines.append(f"valve commands with a sync count: {counted} of {len(rows)}")
    return Result("timing", "pass", lines)


def _loopback_rig(bench: Bench) -> RigMap:
    assert bench.loopback is not None
    return bench.run_rig({LOOPBACK: bench.loopback})


def check_trigger(bench: Bench) -> Result:
    """The counter counts a pulse once; which edge it counts; the configuration is restored."""
    trigger = bench.rig.trigger
    if trigger is None:
        return Result("trigger", "skipped", ["lab.toml has no [trigger] table"])
    lines: list[str] = []
    try:
        with bench.open_labjack(bench.rig.labjack_serial) as session:
            before = session.timer_counter_configuration()
            _is_input, level = session.read_digital(trigger.channel)
            lines.append(f"idle level on channel {trigger.channel}: {'high' if level else 'low'}")
            session.enable_counter(trigger.channel)
            try:
                session.read_counter(reset=True)
                if bench.loopback is not None:
                    session.write_digital_lines({bench.loopback: False})
                    time.sleep(0.05)
                    session.read_counter(reset=True)
                    session.write_digital_lines({bench.loopback: True})
                    time.sleep(0.05)
                    after_rise = session.read_counter()
                    session.write_digital_lines({bench.loopback: False})
                    time.sleep(0.05)
                    after_fall = session.read_counter()
                    edge = "rising" if after_rise > 0 else "falling" if after_fall > 0 else "none"
                    lines.append(
                        f"count after the loop-back rose: {after_rise}; after it fell: "
                        f"{after_fall}; the counter counts the {edge} edge"
                    )
                    if after_fall != 1:
                        lines.append("one full pulse should count exactly once")
                else:
                    bench.ask("Send one TTL pulse to the trigger line, then press Return: ")
                    count = session.read_counter()
                    lines.append(f"count after one pulse by hand: {count}")
                    edge = "unknown"
            finally:
                session.disable_counter()  # also on Ctrl-C
            after = session.timer_counter_configuration()
    except DeviceError as error:
        return Result("trigger", "fail", [*lines, str(error)])
    restored = before == after
    lines.append(f"configuration restored: {restored} ({after})")
    counted_once = (edge in {"rising", "falling"}) if bench.loopback is not None else True
    return Result("trigger", "pass" if restored and counted_once else "fail", lines)


def check_gate(bench: Bench) -> Result:
    """A run waits for the trigger and starts on the loop-back pulse; latency is measured."""
    if bench.rig.trigger is None:
        return Result("gate", "skipped", ["lab.toml has no [trigger] table"])
    if bench.loopback is None:
        return Result("gate", "skipped", ["needs --loopback CHANNEL wired to the trigger input"])
    rig = _loopback_rig(bench)
    trigger = rig.trigger
    assert trigger is not None
    timed = replace(rig, trigger=replace(trigger, timeout_seconds=GATE_TIMEOUT_SECONDS))
    lines: list[str] = []
    # The rest state at arm switches the loop-back: one edge during the wait. The U3
    # counter counts falling edges, so that order is tried first.
    for edge, before, rest in (("falling", True, False), ("rising", False, True)):
        try:
            with bench.open_labjack(bench.rig.labjack_serial) as session:
                session.write_digital_lines({bench.loopback: before})
        except DeviceError as error:
            return Result("gate", "fail", [*lines, str(error)])
        recipe = _pulse_recipe(
            timed,
            valves=[LOOPBACK],
            pulse_seconds=0.1,
            pulses=1,
            lead_seconds=0.2,
            count=1,
            end_state={LOOPBACK: rest},
        )
        status = _run(bench, recipe, timed, wait_for_trigger=True)
        lines.append(f"{edge} edge run: {status.run_directory}: {status.message}")
        if status.phase is Phase.DONE:
            rows = events_of(status)
            rest_write = next(
                row for row in rows if row["event"] == "valve_command" and row["device"] == LOOPBACK
            )
            received = next(row for row in rows if row["event"] == "trigger_received")
            latency = _seconds(received, "returned_run_seconds") - _seconds(
                rest_write, "returned_run_seconds"
            )
            lines.append(f"edge to schedule start: {latency * 1000:.1f} ms; {received['detail']}")
            return Result("gate", "pass", lines)
        if "setpoint" in status.message:
            return Result("gate", "blocked", lines)
    return Result("gate", "fail", [*lines, "neither edge started the run within the timeout"])


def check_sync(bench: Bench) -> Result:
    """Loop-back pulses during a run become sync marks; the lag of each mark is measured."""
    if bench.rig.trigger is None:
        return Result("sync", "skipped", ["lab.toml has no [trigger] table"])
    if bench.loopback is None:
        return Result("sync", "skipped", ["needs --loopback CHANNEL wired to the trigger input"])
    rig = _loopback_rig(bench)
    recipe = _pulse_recipe(
        rig,
        valves=[LOOPBACK],
        pulse_seconds=SYNC_PULSE_SECONDS,
        pulses=SYNC_PULSES,
        lead_seconds=SYNC_PULSE_SECONDS,
        count=1,
    )
    status = _run(bench, recipe, rig)
    lines = [f"run directory: {status.run_directory}", status.message]
    if status.phase is not Phase.DONE:
        return Result("sync", "blocked" if "setpoint" in status.message else "fail", lines)
    rows = events_of(status)
    loopback_rows = [
        row for row in rows if row["event"] == "valve_command" and row["device"] == LOOPBACK
    ]
    edges = [_seconds(row, "returned_run_seconds") for row in loopback_rows]
    marks = [_seconds(row, "returned_run_seconds") for row in rows if row["event"] == "sync_pulse"]
    lags = []
    for mark in marks:
        earlier = [edge for edge in edges if edge <= mark]
        if earlier:
            lags.append(mark - earlier[-1])
    lines.append(f"loop-back edges written: {len(edges)}; sync marks recorded: {len(marks)}")
    lines.append(f"edge to mark lag {milliseconds(lags)}")
    # One polarity of each pulse counts: as many marks as the loop-back opened.
    expected = sum(row["value"] == "open" for row in loopback_rows)
    outcome = "pass" if len(marks) == expected else "fail"
    if outcome == "fail":
        lines.append(f"expected {expected} marks, one per pulse")
    return Result("sync", outcome, lines)


def check_ttl(bench: Bench) -> Result:
    """A run raises the TTL output in its first valve packet; the pulse width is measured.

    With the TTL output wired to the trigger input, the counter sees the pulse once.
    """
    output = bench.rig.ttl_output
    if output is None:
        return Result("ttl", "skipped", ["lab.toml has no [ttl_output] table"])
    valves = list(bench.rig.valves)
    if not valves:
        return Result("ttl", "skipped", ["lab.toml names no valve"])
    recipe = _pulse_recipe(
        bench.rig,
        valves=valves[:1],
        pulse_seconds=PULSE_SECONDS,
        pulses=1,
        lead_seconds=LEAD_SECONDS,
        count=1,
    )
    status = _run(bench, recipe, bench.rig, send_ttl=True)
    lines = [f"run directory: {status.run_directory}", status.message]
    if status.phase is not Phase.DONE:
        return Result("ttl", "blocked" if "setpoint" in status.message else "fail", lines)
    rows = events_of(status)
    edges = [row for row in rows if row["event"] == "ttl_command" and row["value"] != "low"]
    lows = [row for row in rows if row["event"] == "ttl_command" and row["value"] == "low"]
    if len(edges) != 1 or len(lows) < 2:
        lines.append(f"expected one high edge and its low edges, got {len(edges)} and {len(lows)}")
        return Result("ttl", "fail", lines)
    high, low = edges[0], lows[1]  # lows[0] is the defined low before the run
    first_valve = next(
        row for row in rows if row["event"] == "valve_command" and row["step_index"] == "0"
    )
    same_packet = high["returned_run_seconds"] == first_valve["returned_run_seconds"]
    lines.append(f"rose with the first valve command: {same_packet}")
    width = _seconds(low, "returned_run_seconds") - _seconds(high, "returned_run_seconds")
    if output.holds_high:
        lines.append(f"mode high: fell with the {low['detail']} after {width * 1000:.1f} ms")
    else:
        requested = output.pulse_seconds * 1000
        lines.append(f"pulse width: requested {requested:.1f} ms, measured {width * 1000:.1f} ms")
    if bench.rig.trigger is not None:
        marks = sum(row["event"] == "sync_pulse" for row in rows)
        lines.append(f"pulses the counter saw: {marks} (1 when the output feeds the trigger input)")
    return Result("ttl", "pass" if same_packet else "fail", lines)


CHECKS: dict[str, tuple[Callable[[Bench], Result], bool]] = {
    "drivers": (check_drivers, False),
    "ports": (check_ports, False),
    "mfcs": (check_mfcs, False),
    "valves": (check_valves, True),
    "timing": (check_timing, True),
    "trigger": (check_trigger, True),
    "gate": (check_gate, True),
    "sync": (check_sync, True),
    "ttl": (check_ttl, True),
    "safe": (check_safe, False),
}


def run_checks(bench: Bench, names: Sequence[str]) -> list[Result]:
    results = []
    for name in names:
        check, actuates = CHECKS[name]
        if actuates and not bench.actuate:
            results.append(Result(name, "skipped", ["moves hardware; add --actuate"]))
            continue
        results.append(check(bench))
    return results


def build_bench(settings: Settings, actuate: bool, loopback: int | None, no_mfc: bool) -> Bench:
    rig = rig_map_from_settings(settings)
    if no_mfc:
        rig = replace(rig, mfcs={})
    if loopback is not None:
        taken = {*rig.valves.values()}
        for line in (rig.trigger, rig.ttl_output):
            if line is not None:
                taken.add(line.channel)
        if loopback in taken or loopback not in range(4, 20):
            raise ConfigError("--loopback must be a free digital channel from 4 through 19.")
    return Bench(settings, rig, actuate=actuate, loopback=loopback)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sniffler-bench",
        description=(
            "Check the rig with the real devices. Checks that move hardware need --actuate."
        ),
    )
    parser.add_argument(
        "checks", nargs="*", default=["all"], help=f"all or any of: {', '.join(CHECKS)}"
    )
    parser.add_argument("--config", type=Path, default=Path("lab.toml"))
    parser.add_argument(
        "--actuate", action="store_true", help="Allow checks that move valves or write setpoints."
    )
    parser.add_argument(
        "--loopback", type=int, help="Digital output channel wired to the trigger input."
    )
    parser.add_argument(
        "--no-mfc", action="store_true", help="Run the executor checks without the MFCs."
    )
    arguments = parser.parse_args(argv)
    if argv is None:
        restarted = hardware.relaunch_with_homebrew_exodriver("sniffler.bench")
        if restarted is not None:
            return restarted
    names = list(CHECKS) if arguments.checks == ["all"] else arguments.checks
    unknown = [name for name in names if name not in CHECKS]
    if unknown:
        print(
            f"Unknown check: {', '.join(unknown)}. Choose from: {', '.join(CHECKS)}",
            file=sys.stderr,
        )
        return 2
    try:
        settings = load_settings(arguments.config)
        bench = build_bench(settings, arguments.actuate, arguments.loopback, arguments.no_mfc)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    refusal = refusal_for_active_run(settings.runs_directory)
    if refusal is not None:
        print(f"Refused: {refusal}", file=sys.stderr)
        return 3
    try:
        results = run_checks(bench, names)
    except KeyboardInterrupt:
        print(
            "Interrupted. The check that was running ended in the safe state; "
            "run `sniffler-bench safe` to confirm.",
            file=sys.stderr,
        )
        return 130
    for result in results:
        print(result.render())
    failed = [result.check for result in results if result.outcome == "fail"]
    print(
        f"\n{len(results)} checks: {len(failed)} failed"
        + (f" ({', '.join(failed)})" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
