"""Experiment recipes: steps, trials, schedules, trial order, and the rig map."""

import json
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sniffler.config import AlicatSettings, ConfigError, Settings, TriggerSettings
from sniffler.hardware import normalize_alicat_flow

RECIPE_FORMAT = "sniffler-recipe/1"
ORDERINGS = ("block-randomized", "as-listed")
MAX_CONSECUTIVE_TRIALS = 2
ORDER_RETRY_CAP = 1000


class RecipeError(ValueError):
    """A recipe is not valid for the configured rig."""


@dataclass(frozen=True)
class MfcMap:
    """One named mass flow controller and its limits."""

    name: str
    port: str
    unit: str
    baud_rate: int
    timeout_seconds: float
    minimum_flow: float
    maximum_flow: float | None
    allow_negative_flow: bool
    flow_unit: str
    full_scale: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "unit": self.unit,
            "baud_rate": self.baud_rate,
            "timeout_seconds": self.timeout_seconds,
            "minimum_flow": self.minimum_flow,
            "maximum_flow": self.maximum_flow,
            "allow_negative_flow": self.allow_negative_flow,
            "flow_unit": self.flow_unit,
            "full_scale": self.full_scale,
        }


@dataclass(frozen=True)
class RigMap:
    """The friendly names that a recipe can use and the hardware behind them."""

    labjack_serial: int | None
    valves: dict[str, int]
    mfcs: dict[str, MfcMap]
    trigger: TriggerSettings | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "labjack_serial": self.labjack_serial,
            "valves": dict(self.valves),
            "mfcs": {name: mfc.to_dict() for name, mfc in self.mfcs.items()},
            "trigger": None if self.trigger is None else self.trigger.to_dict(),
        }

    def with_full_scale(self, name: str, full_scale: float, flow_unit: str) -> "RigMap":
        """Return a copy that records the full scale read from one device."""
        mfcs = dict(self.mfcs)
        current = mfcs[name]
        mfcs[name] = MfcMap(
            **{**current.__dict__, "full_scale": full_scale, "flow_unit": flow_unit}
        )
        return RigMap(self.labjack_serial, dict(self.valves), mfcs, self.trigger)


def _mfc_map(name: str, alicat: AlicatSettings) -> MfcMap:
    if alicat.port is None:
        raise ConfigError(f"Set alicat.{name}.port in lab.toml before you use {name} in a recipe.")
    return MfcMap(
        name=name,
        port=alicat.port,
        unit=alicat.unit,
        baud_rate=alicat.baud_rate,
        timeout_seconds=alicat.timeout_seconds,
        minimum_flow=alicat.minimum_flow,
        maximum_flow=alicat.maximum_flow,
        allow_negative_flow=alicat.allow_negative_flow,
        flow_unit=alicat.units.get("mass_flow", "device units"),
    )


def rig_map_from_settings(settings: Settings) -> RigMap:
    """Build the rig map from lab.toml.

    The implicit ``default`` Alicat without a port is a command-line convenience,
    not a configured device, so it is not part of the rig.
    """
    mfcs = {
        name: _mfc_map(name, alicat)
        for name, alicat in settings.alicats.items()
        if not (name == "default" and alicat.port is None)
    }
    return RigMap(settings.labjack_serial, dict(settings.valves), mfcs, settings.trigger)


@dataclass(frozen=True)
class Step:
    """One complete rig state held for a duration.

    ``duration_seconds`` is ``None`` only for the shutdown state.
    """

    duration_seconds: float | None
    valves: dict[str, bool] = field(default_factory=dict)
    setpoints: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_seconds": self.duration_seconds,
            "valves": dict(self.valves),
            "setpoints": dict(self.setpoints),
        }


@dataclass(frozen=True)
class Trial:
    """A named, ordered list of steps."""

    name: str
    steps: tuple[Step, ...]

    @property
    def duration_seconds(self) -> float:
        return sum(step.duration_seconds or 0.0 for step in self.steps)


@dataclass(frozen=True)
class Schedule:
    """Trial counts and the ordering policy."""

    counts: dict[str, int]
    ordering: str = "block-randomized"
    seed: int | None = None


@dataclass(frozen=True)
class Recipe:
    """Everything the executor needs, except the rig map and the seed."""

    name: str
    trials: tuple[Trial, ...]
    schedule: Schedule
    shutdown: Step
    notes: str = ""
    # What each valve holds, by valve name, for the run record. Empty entries are left out.
    valve_contents: dict[str, str] = field(default_factory=dict)

    def trial(self, name: str) -> Trial:
        for trial in self.trials:
            if trial.name == name:
                return trial
        raise KeyError(name)

    def valve_label(self, name: str) -> str:
        """What the valve holds when the recipe records it, else the valve name."""
        return self.valve_contents.get(name) or name

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": RECIPE_FORMAT,
            "name": self.name,
            "notes": self.notes,
            "valve_contents": dict(self.valve_contents),
            "trials": [
                {"name": trial.name, "steps": [step.to_dict() for step in trial.steps]}
                for trial in self.trials
            ],
            "schedule": {
                "ordering": self.schedule.ordering,
                "counts": dict(self.schedule.counts),
                "seed": self.schedule.seed,
            },
            "shutdown": {
                "valves": dict(self.shutdown.valves),
                "setpoints": dict(self.shutdown.setpoints),
            },
        }


def safe_state(rig: RigMap) -> Step:
    """All valves closed and every MFC setpoint zero. Not configurable."""
    return Step(
        duration_seconds=None,
        valves=dict.fromkeys(rig.valves, False),
        setpoints=dict.fromkeys(rig.mfcs, 0.0),
    )


def setpoint_problem(value: object, mfc: MfcMap) -> str | None:
    """Return why a setpoint is not allowed for this MFC, or None when it is."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "The target flow must be a number."
    if not math.isfinite(value):
        return "The target flow must be finite."
    applied = normalize_alicat_flow(float(value))
    unit = mfc.flow_unit
    if applied < 0 and not mfc.allow_negative_flow:
        return f"Negative flow is disabled for {mfc.name} in lab.toml."
    if applied < mfc.minimum_flow:
        return f"The target flow must be at least {mfc.minimum_flow:g} {unit}."
    if mfc.maximum_flow is not None and applied > mfc.maximum_flow:
        return f"The target flow must be at most the lab.toml limit of {mfc.maximum_flow:g} {unit}."
    if mfc.full_scale is not None and abs(applied) > mfc.full_scale:
        return f"The target flow is more than the device maximum of {mfc.full_scale:g} {unit}."
    return None


def duration_problem(value: object) -> str | None:
    """Return why a step duration is not allowed, or None when it is."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "The duration must be a number of seconds."
    if not math.isfinite(value) or value <= 0:
        return "The duration must be greater than zero."
    return None


def _toml_key(name: str) -> str:
    """Quote a table key the way lab.toml needs it when it is not a bare key."""
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)


def _step_problems(step: Step, rig: RigMap, location: str, shutdown: bool) -> list[str]:
    problems: list[str] = []
    if shutdown:
        if step.duration_seconds is not None:
            problems.append(f"{location}: the end state has no duration.")
    elif (problem := duration_problem(step.duration_seconds)) is not None:
        problems.append(f"{location}: {problem}")

    for name in sorted(step.valves.keys() - rig.valves.keys()):
        problems.append(f"{location}: unknown valve {name!r}. Add it to [valves] in lab.toml.")
    for name in sorted(rig.valves.keys() - step.valves.keys()):
        problems.append(f"{location}: valve {name!r} has no state.")
    for name, state in step.valves.items():
        if not isinstance(state, bool):
            problems.append(f"{location}: valve {name!r} must be open or closed.")

    for name in sorted(step.setpoints.keys() - rig.mfcs.keys()):
        problems.append(
            f"{location}: unknown MFC {name!r}. Add it to [alicat.{_toml_key(name)}] in lab.toml."
        )
    for name in sorted(rig.mfcs.keys() - step.setpoints.keys()):
        problems.append(f"{location}: MFC {name!r} has no target flow.")
    for name, value in step.setpoints.items():
        if name in rig.mfcs and (problem := setpoint_problem(value, rig.mfcs[name])) is not None:
            problems.append(f"{location}: MFC {name!r}: {problem}")
    return problems


def recipe_problems(recipe: Recipe, rig: RigMap) -> list[str]:
    """Return every problem that makes the recipe unfit for this rig."""
    problems: list[str] = []
    if not recipe.name.strip():
        problems.append("The recipe needs a name.")
    if not recipe.trials:
        problems.append("The recipe needs at least one trial.")

    seen: set[str] = set()
    for index, trial in enumerate(recipe.trials, start=1):
        label = trial.name.strip() or f"#{index}"
        if not trial.name.strip():
            problems.append(f"Trial {label}: the trial needs a name.")
        elif trial.name in seen:
            problems.append(f"Trial {label!r}: the trial name is used more than once.")
        seen.add(trial.name)
        if not trial.steps:
            problems.append(f"Trial {label!r}: the trial needs at least one step.")
        for step_index, step in enumerate(trial.steps, start=1):
            location = f"Trial {label!r}, step {step_index}"
            problems.extend(_step_problems(step, rig, location, shutdown=False))

    problems.extend(_step_problems(recipe.shutdown, rig, "End state", shutdown=True))
    for name in recipe.valve_contents:
        if name not in rig.valves:
            problems.append(f"Valve contents: unknown valve {name!r}.")

    schedule = recipe.schedule
    if schedule.ordering not in ORDERINGS:
        problems.append(f"Schedule: unknown ordering {schedule.ordering!r}.")
    if schedule.seed is not None and (isinstance(schedule.seed, bool) or schedule.seed < 0):
        problems.append("Schedule: the seed must be a whole number of zero or more.")
    trial_names = {trial.name for trial in recipe.trials}
    counts_valid = True
    for name, count in schedule.counts.items():
        if name not in trial_names:
            problems.append(f"Schedule: unknown trial {name!r}.")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            problems.append(f"Schedule: the count for {name!r} must be a whole number.")
            counts_valid = False
    if not any(
        isinstance(count, int) and not isinstance(count, bool) and count > 0
        for count in schedule.counts.values()
    ):
        problems.append("Schedule: at least one trial needs a count greater than zero.")
    elif counts_valid and schedule.ordering in ORDERINGS:
        problem = order_problem(schedule)
        if problem is not None:
            problems.append(f"Schedule: {problem}")
    return problems


def validate_recipe(recipe: Recipe, rig: RigMap) -> None:
    """Raise RecipeError with every problem, or return when the recipe is valid."""
    problems = recipe_problems(recipe, rig)
    if problems:
        raise RecipeError("\n".join(problems))


def _has_long_run(sequence: list[str]) -> bool:
    run = 0
    previous = None
    for name in sequence:
        run = run + 1 if name == previous else 1
        previous = name
        if run > MAX_CONSECUTIVE_TRIALS:
            return True
    return False


def _blocks(counts: dict[str, int]) -> list[list[str]]:
    """Split the counts into blocks that hold one trial of each type that remains."""
    remaining = {name: count for name, count in counts.items() if count > 0}
    blocks: list[list[str]] = []
    while remaining:
        blocks.append(list(remaining))
        remaining = {name: count - 1 for name, count in remaining.items() if count > 1}
    return blocks


def order_problem(schedule: Schedule) -> str | None:
    """Return why no trial order can keep the run limit, or None when one can.

    Block randomized: blocks shrink as trial types run out, so only the last
    blocks, which hold one trial type each, can force identical trials in a row.
    """
    blocks = _blocks(schedule.counts)
    if schedule.ordering == "as-listed":
        if _has_long_run([name for block in blocks for name in block]):
            return (
                f"The as-listed order puts more than {MAX_CONSECUTIVE_TRIALS} identical trials "
                "in a row. Add another trial type or reduce the unequal counts."
            )
        return None
    single = 0
    for block in reversed(blocks):
        if len(block) != 1:
            break
        single += 1
    if single > MAX_CONSECUTIVE_TRIALS:
        return (
            f"The counts end with more than {MAX_CONSECUTIVE_TRIALS} identical trials in a row. "
            "Add another trial type or make the counts more equal."
        )
    return None


def resolve_trial_order(schedule: Schedule, seed: int) -> list[str]:
    """Return the trial names in run order.

    Block randomized: each block holds one trial of each type that still has a
    count, and the order inside each block is shuffled. The same seed always
    gives the same order. No more than MAX_CONSECUTIVE_TRIALS identical trials
    may follow each other anywhere in the sequence.
    """
    if schedule.ordering not in ORDERINGS:
        raise RecipeError(f"Unknown ordering {schedule.ordering!r}.")
    problem = order_problem(schedule)
    if problem is not None:
        raise RecipeError(problem)
    blocks = _blocks(schedule.counts)
    if schedule.ordering == "as-listed":
        return [name for block in blocks for name in block]

    generator = random.Random(seed)
    for _attempt in range(ORDER_RETRY_CAP):
        sequence: list[str] = []
        for block in blocks:
            shuffled = list(block)
            generator.shuffle(shuffled)
            sequence.extend(shuffled)
        if not _has_long_run(sequence):
            return sequence
    raise RecipeError(
        f"Cannot find a trial order with no more than {MAX_CONSECUTIVE_TRIALS} identical trials "
        f"in a row after {ORDER_RETRY_CAP} attempts. Add another trial type or make the counts "
        "more equal."
    )


def _require(data: dict[str, Any], key: str, expected: type, location: str) -> Any:
    if key not in data:
        raise RecipeError(f"{location}: missing {key!r}.")
    value = data[key]
    if expected is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise RecipeError(f"{location}: {key!r} must be a number.")
        return float(value)
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        raise RecipeError(f"{location}: {key!r} must be {expected.__name__}.")
    return value


def _step_from_dict(data: Any, location: str, shutdown: bool) -> Step:
    if not isinstance(data, dict):
        raise RecipeError(f"{location}: a step must be a table.")
    valves = _require(data, "valves", dict, location)
    setpoints = _require(data, "setpoints", dict, location)
    duration = None if shutdown else _require(data, "duration_seconds", float, location)
    return Step(
        duration_seconds=duration,
        valves={str(name): state for name, state in valves.items()},
        setpoints={str(name): value for name, value in setpoints.items()},
    )


def recipe_from_dict(data: Any) -> Recipe:
    """Build a recipe from parsed JSON. Structural problems raise RecipeError."""
    if not isinstance(data, dict):
        raise RecipeError("The recipe file must contain one recipe table.")
    if data.get("format") != RECIPE_FORMAT:
        raise RecipeError(f"The recipe format must be {RECIPE_FORMAT!r}.")
    name = _require(data, "name", str, "Recipe")
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        raise RecipeError("Recipe: 'notes' must be text.")
    valve_contents = data.get("valve_contents", {})
    if not isinstance(valve_contents, dict) or not all(
        isinstance(text, str) for text in valve_contents.values()
    ):
        raise RecipeError("Recipe: 'valve_contents' must be a table of valve name to text.")

    trials: list[Trial] = []
    for index, item in enumerate(_require(data, "trials", list, "Recipe"), start=1):
        location = f"Trial #{index}"
        if not isinstance(item, dict):
            raise RecipeError(f"{location}: a trial must be a table.")
        trial_name = _require(item, "name", str, location)
        steps = tuple(
            _step_from_dict(step, f"Trial {trial_name!r}, step {step_index}", shutdown=False)
            for step_index, step in enumerate(_require(item, "steps", list, location), start=1)
        )
        trials.append(Trial(trial_name, steps))

    schedule_data = _require(data, "schedule", dict, "Recipe")
    counts = _require(schedule_data, "counts", dict, "Schedule")
    seed = schedule_data.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise RecipeError("Schedule: 'seed' must be a whole number or empty.")
    schedule = Schedule(
        counts={str(trial_name): count for trial_name, count in counts.items()},
        ordering=_require(schedule_data, "ordering", str, "Schedule"),
        seed=seed,
    )
    shutdown = _step_from_dict(_require(data, "shutdown", dict, "Recipe"), "Shutdown", True)
    return Recipe(
        name=name,
        trials=tuple(trials),
        schedule=schedule,
        shutdown=shutdown,
        notes=notes,
        valve_contents={str(valve): text for valve, text in valve_contents.items()},
    )


def load_recipe(path: Path) -> Recipe:
    """Read a recipe file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise RecipeError(f"Cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise RecipeError(f"{path} is not a valid recipe file: {error}") from error
    return recipe_from_dict(data)


def save_recipe(path: Path, recipe: Recipe) -> None:
    """Write a recipe file."""
    try:
        path.write_text(json.dumps(recipe.to_dict(), indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        raise RecipeError(f"Cannot write {path}: {error}") from error


def resolved_duration_seconds(recipe: Recipe, order: list[str]) -> float:
    """Return the planned run time for a resolved trial order."""
    return sum(recipe.trial(name).duration_seconds for name in order)
