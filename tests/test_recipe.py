"""Tests for the recipe contract, validation, and trial ordering."""

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from sniffler.config import AlicatSettings, ConfigError, Settings
from sniffler.recipe import (
    MAX_CONSECUTIVE_TRIALS,
    MfcMap,
    Recipe,
    RecipeError,
    RigMap,
    Schedule,
    Step,
    Trial,
    load_recipe,
    order_problem,
    recipe_from_dict,
    recipe_problems,
    resolve_trial_order,
    resolved_duration_seconds,
    rig_map_from_settings,
    safe_state,
    save_recipe,
    setpoint_problem,
    validate_recipe,
)


def make_mfc(name: str = "mfc-500", **values) -> MfcMap:
    options = {
        "name": name,
        "port": "/dev/mock",
        "unit": "A",
        "baud_rate": 19200,
        "timeout_seconds": 0.15,
        "minimum_flow": 0.0,
        "maximum_flow": None,
        "allow_negative_flow": False,
        "flow_unit": "SCCM",
        "full_scale": None,
    }
    options.update(values)
    return MfcMap(**options)


RIG = RigMap(
    labjack_serial=320107153,
    valves={"odor-1": 8, "odor-2": 9},
    mfcs={"mfc-500": make_mfc(maximum_flow=400.0)},
)


def make_step(duration: float | None = 1.0, odor_1: bool = False, flow: float = 100.0) -> Step:
    return Step(duration, {"odor-1": odor_1, "odor-2": False}, {"mfc-500": flow})


def make_recipe(**overrides) -> Recipe:
    values = {
        "name": "pulses",
        "trials": (
            Trial("odor", (make_step(0.5, True), make_step(0.5, False))),
            Trial("blank", (make_step(1.0),)),
        ),
        "schedule": Schedule({"odor": 3, "blank": 3}, "block-randomized", 7),
        "shutdown": make_step(None, flow=0.0),
    }
    values.update(overrides)
    return Recipe(**values)


class RigMapTests(unittest.TestCase):
    def test_builds_named_devices_from_settings(self) -> None:
        settings = Settings(
            labjack_serial=320107153,
            alicats={"mfc-500": AlicatSettings(port="COM3", units={"mass_flow": "SCCM"})},
            valves={"odor-1": 8},
        )

        rig = rig_map_from_settings(settings)

        self.assertEqual(rig.valves, {"odor-1": 8})
        self.assertEqual(rig.mfcs["mfc-500"].port, "COM3")
        self.assertEqual(rig.mfcs["mfc-500"].flow_unit, "SCCM")
        self.assertIsNone(rig.mfcs["mfc-500"].full_scale)

    def test_leaves_out_the_implicit_default_alicat(self) -> None:
        rig = rig_map_from_settings(Settings(valves={"odor-1": 8}))

        self.assertEqual(rig.mfcs, {})

    def test_refuses_a_named_alicat_without_a_port(self) -> None:
        settings = Settings(alicats={"mfc-500": AlicatSettings()})

        with self.assertRaisesRegex(ConfigError, "alicat.mfc-500.port"):
            rig_map_from_settings(settings)

    def test_records_the_device_full_scale(self) -> None:
        rig = RIG.with_full_scale("mfc-500", 500.0, "SCCM")

        self.assertEqual(rig.mfcs["mfc-500"].full_scale, 500.0)
        self.assertIsNone(RIG.mfcs["mfc-500"].full_scale)
        self.assertEqual(rig.to_dict()["mfcs"]["mfc-500"]["full_scale"], 500.0)


class ValidationTests(unittest.TestCase):
    def test_accepts_a_complete_recipe(self) -> None:
        validate_recipe(make_recipe(), RIG)

    def test_unknown_mfc_hint_quotes_a_name_with_a_space(self) -> None:
        from sniffler.recipe import _step_problems

        step = Step(1.0, dict.fromkeys(RIG.valves, False), {"aux flow": 1.0})
        problems = _step_problems(step, RIG, "Trial 'x', step 1", shutdown=False)
        self.assertIn(
            "Trial 'x', step 1: unknown MFC 'aux flow'. "
            'Add it to [alicat."aux flow"] in lab.toml.',
            problems,
        )

    def test_refuses_unknown_and_missing_devices(self) -> None:
        step = Step(1.0, {"odor-1": True, "odor-9": False}, {})
        recipe = make_recipe(trials=(Trial("odor", (step,)),), schedule=Schedule({"odor": 1}))

        problems = recipe_problems(recipe, RIG)

        self.assertIn(
            "Trial 'odor', step 1: unknown valve 'odor-9'. Add it to [valves] in lab.toml.",
            problems,
        )
        self.assertIn("Trial 'odor', step 1: valve 'odor-2' has no state.", problems)
        self.assertIn("Trial 'odor', step 1: MFC 'mfc-500' has no target flow.", problems)

    def test_enforces_lab_limits_and_full_scale_per_cell(self) -> None:
        mfc = make_mfc(minimum_flow=10.0, maximum_flow=400.0, full_scale=500.0)

        self.assertIsNone(setpoint_problem(10.0, mfc))
        self.assertIn("at least 10", setpoint_problem(9.99, mfc))
        self.assertIn("lab.toml limit of 400", setpoint_problem(400.01, mfc))
        self.assertIn("Negative flow is disabled", setpoint_problem(-1.0, mfc))
        self.assertIn("device maximum of 500", setpoint_problem(550.0, make_mfc(full_scale=500.0)))
        self.assertIsNone(setpoint_problem(450.0, make_mfc(full_scale=500.0)))
        self.assertIn("finite", setpoint_problem(float("nan"), mfc))
        self.assertIn("number", setpoint_problem("12", mfc))

    def test_checks_the_rounded_setpoint(self) -> None:
        mfc = make_mfc(maximum_flow=1.235)

        self.assertIn("lab.toml limit", setpoint_problem(1.235, mfc))
        self.assertIsNone(setpoint_problem(1.234, mfc))

    def test_refuses_bad_durations_and_shutdown_durations(self) -> None:
        recipe = make_recipe(
            trials=(Trial("odor", (make_step(0.0),)),),
            schedule=Schedule({"odor": 1}),
            shutdown=make_step(2.0, flow=0.0),
        )

        problems = recipe_problems(recipe, RIG)

        self.assertIn("Trial 'odor', step 1: The duration must be greater than zero.", problems)
        self.assertIn("End state: the end state has no duration.", problems)

    def test_refuses_schedule_problems(self) -> None:
        recipe = make_recipe(schedule=Schedule({"odor": 0, "ghost": 2}, "random", -1))

        problems = recipe_problems(recipe, RIG)

        self.assertIn("Schedule: unknown ordering 'random'.", problems)
        self.assertIn("Schedule: unknown trial 'ghost'.", problems)
        self.assertIn("Schedule: the seed must be a whole number of zero or more.", problems)

    def test_refuses_duplicate_and_empty_trial_names(self) -> None:
        recipe = make_recipe(
            trials=(
                Trial("odor", (make_step(),)),
                Trial("odor", (make_step(),)),
                Trial(" ", (make_step(),)),
            )
        )

        problems = recipe_problems(recipe, RIG)

        self.assertIn("Trial 'odor': the trial name is used more than once.", problems)
        self.assertIn("Trial #3: the trial needs a name.", problems)

    def test_validate_raises_with_every_problem(self) -> None:
        recipe = make_recipe(trials=(), schedule=Schedule({}))

        with self.assertRaisesRegex(RecipeError, "at least one trial") as context:
            validate_recipe(recipe, RIG)
        self.assertIn("count greater than zero", str(context.exception))

    def test_safe_state_closes_every_valve_and_zeroes_every_mfc(self) -> None:
        state = safe_state(RIG)

        self.assertEqual(state.valves, {"odor-1": False, "odor-2": False})
        self.assertEqual(state.setpoints, {"mfc-500": 0.0})
        self.assertIsNone(state.duration_seconds)


class OrderingTests(unittest.TestCase):
    def test_balances_every_block_and_limits_runs(self) -> None:
        schedule = Schedule({"A": 40, "B": 40, "C": 40}, "block-randomized")

        for seed in range(50):
            order = resolve_trial_order(schedule, seed)
            self.assertEqual(len(order), 120)
            for start in range(0, 120, 3):
                self.assertEqual(Counter(order[start : start + 3]), Counter("ABC"))
            for index in range(len(order) - MAX_CONSECUTIVE_TRIALS):
                window = order[index : index + MAX_CONSECUTIVE_TRIALS + 1]
                self.assertGreater(len(set(window)), 1, f"seed {seed}: run of {window}")

    def test_uses_the_seed_and_actually_shuffles(self) -> None:
        schedule = Schedule({"A": 20, "B": 20}, "block-randomized")

        first = resolve_trial_order(schedule, 12345)
        second = resolve_trial_order(schedule, 12345)
        other = resolve_trial_order(schedule, 54321)

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertNotEqual(first, ["A", "B"] * 20)

    def test_enforces_the_run_limit_across_block_boundaries(self) -> None:
        schedule = Schedule({"A": 3, "B": 1}, "block-randomized")

        for seed in range(20):
            order = resolve_trial_order(schedule, seed)
            self.assertEqual(Counter(order), Counter({"A": 3, "B": 1}))
            self.assertNotEqual(order[1:], ["A", "A", "A"], f"seed {seed}")

    def test_raises_when_the_run_limit_cannot_be_met(self) -> None:
        with self.assertRaisesRegex(RecipeError, "end with more than 2 identical trials"):
            resolve_trial_order(Schedule({"A": 3}, "block-randomized"), 1)
        with self.assertRaisesRegex(RecipeError, "identical trials"):
            resolve_trial_order(Schedule({"A": 10, "B": 5}, "block-randomized"), 1)

    def test_recipe_problems_reports_an_order_that_cannot_keep_the_run_limit(self) -> None:
        # Two identical trials at the end are allowed; three are not, for either ordering.
        self.assertIsNone(order_problem(Schedule({"A": 3, "B": 1}, "block-randomized")))
        self.assertIsNone(order_problem(Schedule({"A": 2, "B": 2}, "as-listed")))
        recipe = make_recipe()
        for ordering in ("block-randomized", "as-listed"):
            bad = Recipe(
                recipe.name,
                recipe.trials,
                Schedule({"odor": 4, "blank": 1}, ordering),
                recipe.shutdown,
            )
            problems = recipe_problems(bad, RIG)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("Schedule:", problems[0])
            self.assertIn("identical trials in a row", problems[0])

    def test_as_listed_keeps_the_written_order(self) -> None:
        order = resolve_trial_order(Schedule({"A": 2, "B": 2}, "as-listed"), 99)

        self.assertEqual(order, ["A", "B", "A", "B"])
        with self.assertRaisesRegex(RecipeError, "as-listed"):
            resolve_trial_order(Schedule({"A": 3}, "as-listed"), 99)

    def test_ignores_zero_counts(self) -> None:
        order = resolve_trial_order(Schedule({"A": 2, "B": 0, "C": 2}, "as-listed"), 0)

        self.assertEqual(order, ["A", "C", "A", "C"])

    def test_computes_the_resolved_duration(self) -> None:
        recipe = make_recipe()

        self.assertAlmostEqual(resolved_duration_seconds(recipe, ["odor", "blank", "odor"]), 3.0)


class FileTests(unittest.TestCase):
    def test_round_trips_through_a_file(self) -> None:
        recipe = make_recipe(
            notes="carrier at 100", valve_contents={"odor-1": "2-heptanone 1:1000 in oil"}
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "pulses.json")
            save_recipe(path, recipe)
            loaded = load_recipe(path)
            data = json.loads(path.read_text())

        self.assertEqual(loaded, recipe)
        self.assertEqual(data["format"], "sniffler-recipe/1")
        self.assertEqual(data["valve_contents"], {"odor-1": "2-heptanone 1:1000 in oil"})
        self.assertEqual(
            data["shutdown"],
            {"valves": {"odor-1": False, "odor-2": False}, "setpoints": {"mfc-500": 0.0}},
        )

    def test_valve_contents_are_optional_in_the_file_and_checked_against_the_rig(self) -> None:
        data = make_recipe().to_dict()
        del data["valve_contents"]
        self.assertEqual(recipe_from_dict(data).valve_contents, {})

        data["valve_contents"] = {"odor-1": 3}
        with self.assertRaisesRegex(RecipeError, "'valve_contents' must be a table"):
            recipe_from_dict(data)

        recipe = make_recipe(valve_contents={"odor-1": "hexanol", "odor-9": "octanol"})
        self.assertEqual(recipe_problems(recipe, RIG), ["Valve contents: unknown valve 'odor-9'."])

    def test_reports_structural_problems_without_a_traceback(self) -> None:
        with self.assertRaisesRegex(RecipeError, "format"):
            recipe_from_dict({"name": "x"})
        with self.assertRaisesRegex(RecipeError, "Trial 'odor', step 1: missing 'valves'"):
            recipe_from_dict(
                {
                    "format": "sniffler-recipe/1",
                    "name": "x",
                    "trials": [{"name": "odor", "steps": [{"duration_seconds": 1}]}],
                    "schedule": {"ordering": "as-listed", "counts": {}},
                    "shutdown": {"valves": {}, "setpoints": {}},
                }
            )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "broken.json")
            path.write_text("{not json")
            with self.assertRaisesRegex(RecipeError, "not a valid recipe file"):
                load_recipe(path)
            with self.assertRaisesRegex(RecipeError, "Cannot read"):
                load_recipe(Path(directory, "missing.json"))


if __name__ == "__main__":
    unittest.main()
