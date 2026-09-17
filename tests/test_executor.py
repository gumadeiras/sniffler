"""Executor tests with fake hardware: exit paths, timing, and the run log."""

import asyncio
import csv
import json
import tempfile
import threading
import time
import unittest
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from sniffler.executor import Executor, Phase, Sample, Status
from sniffler.hardware import DeviceError
from sniffler.recipe import MfcMap, Recipe, RigMap, Schedule, Step, Trial
from sniffler.runlog import LOCK_FILE_NAME, RunLock


class FakeLabJack:
    """Record every multi-line write with the time it was made."""

    def __init__(self) -> None:
        self.writes: list[tuple[float, dict[int, bool]]] = []
        self.fail_on_write: int | None = None
        self.write_delay = 0.0
        self.closed = False

    def write_digital_lines(self, states: dict[int, bool]) -> None:
        if self.fail_on_write is not None and len(self.writes) == self.fail_on_write:
            self.fail_on_write = None
            raise DeviceError("usb gone; the outputs might have changed")
        if self.write_delay:
            time.sleep(self.write_delay)
        self.writes.append((time.perf_counter(), dict(states)))


class FakeAlicat:
    """Record setpoints and answer reads from the last setpoint."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.setpoints: list[float] = []
        self.reads = 0
        self.prepared = False
        self.prepare_error: str | None = None
        self.read_delay = 0.0
        self.closed = False

    async def prepare_setpoints(self) -> tuple[float, str]:
        if self.prepare_error:
            raise DeviceError(self.prepare_error)
        self.prepared = True
        return 500.0, "SCCM"

    async def write_setpoint(self, flow_rate: float) -> float:
        if not self.prepared:
            raise DeviceError("Call prepare_setpoints before write_setpoint; no setpoint was sent.")
        self.setpoints.append(flow_rate)
        return flow_rate

    async def read(self) -> dict[str, object]:
        if self.read_delay:
            await asyncio.sleep(self.read_delay)
        self.reads += 1
        setpoint = self.setpoints[-1] if self.setpoints else 0.0
        return {"setpoint": setpoint, "mass_flow": setpoint * 0.98, "pressure": 14.7}


class FakeRig:
    def __init__(self) -> None:
        self.labjack = FakeLabJack()
        self.alicats = {"mfc-500": FakeAlicat("mfc-500"), "mfc-2000": FakeAlicat("mfc-2000")}
        self.labjack_opens = 0
        self.labjack_error: str | None = None

    @contextmanager
    def open_labjack(self, serial_number):
        self.labjack_opens += 1
        if self.labjack_error:
            raise DeviceError(self.labjack_error)
        try:
            yield self.labjack
        finally:
            self.labjack.closed = True

    @asynccontextmanager
    async def open_alicat(self, port, unit, baud_rate, timeout_seconds):
        alicat = next(alicat for alicat in self.alicats.values() if alicat.name in port)
        try:
            yield alicat
        finally:
            alicat.closed = True


def make_mfc(name: str) -> MfcMap:
    return MfcMap(name, f"/dev/{name}", "A", 19200, 0.15, 0.0, None, False, "SCCM")


RIG = RigMap(
    labjack_serial=320107153,
    valves={"odor-1": 8, "odor-2": 9, "final": 16},
    mfcs={"mfc-500": make_mfc("mfc-500"), "mfc-2000": make_mfc("mfc-2000")},
)


def step(duration: float, odor_1: bool = False, odor_2: bool = False, flow: float = 100.0) -> Step:
    return Step(
        duration,
        {"odor-1": odor_1, "odor-2": odor_2, "final": False},
        {"mfc-500": flow, "mfc-2000": 1000.0},
    )


SHUTDOWN = Step(
    None, {"odor-1": False, "odor-2": False, "final": True}, {"mfc-500": 50.0, "mfc-2000": 0.0}
)


def make_recipe(step_seconds: float = 0.03, counts: dict[str, int] | None = None) -> Recipe:
    return Recipe(
        name="Fake pulses",
        trials=(
            Trial("odor", (step(step_seconds, odor_1=True), step(step_seconds, flow=200.0))),
            Trial("blank", (step(step_seconds, odor_2=True),)),
        ),
        schedule=Schedule(counts or {"odor": 2, "blank": 2}, "block-randomized", 3),
        shutdown=SHUTDOWN,
    )


class ExecutorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runs = Path(self.temporary.name, "runs")
        self.rig = FakeRig()
        self.statuses: list[Status] = []
        self.samples: list[Sample] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def executor(self, recipe: Recipe | None = None, **options) -> Executor:
        return Executor(
            recipe or make_recipe(),
            RIG,
            seed=3,
            runs_directory=self.runs,
            operator_notes="fake bench",
            open_labjack=self.rig.open_labjack,
            open_alicat=self.rig.open_alicat,
            on_status=self.statuses.append,
            on_sample=self.samples.append,
            sample_interval_seconds=0.01,
            **options,
        )

    def wait_for(self, condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("condition not met in time")
            time.sleep(0.001)

    def events(self, status: Status) -> list[dict[str, str]]:
        assert status.run_directory is not None
        with (status.run_directory / "events.csv").open(newline="") as file:
            return list(csv.DictReader(file))

    def samples_csv(self, status: Status) -> list[dict[str, str]]:
        assert status.run_directory is not None
        with (status.run_directory / "samples.csv").open(newline="") as file:
            return list(csv.DictReader(file))

    def manifest(self, status: Status) -> dict:
        assert status.run_directory is not None
        return json.loads((status.run_directory / "manifest.json").read_text())

    def final_valves(self) -> dict[int, bool]:
        return self.rig.labjack.writes[-1][1]


class ExitPathTests(ExecutorTestCase):
    def test_normal_end_applies_the_shutdown_state_and_writes_a_complete_record(self) -> None:
        status = self.executor().run()

        self.assertEqual(status.phase, Phase.DONE, status.message)
        self.assertEqual(self.final_valves(), {8: False, 9: False, 16: True})
        self.assertEqual(self.rig.alicats["mfc-500"].setpoints[-1], 50.0)
        self.assertEqual(self.rig.alicats["mfc-2000"].setpoints[-1], 0.0)
        self.assertTrue(self.rig.labjack.closed)
        self.assertTrue(all(alicat.closed for alicat in self.rig.alicats.values()))
        self.assertFalse((self.runs / LOCK_FILE_NAME).exists())

        events = self.events(status)
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds[0], "run_start")
        self.assertEqual(kinds[-1], "run_end")
        self.assertEqual(kinds.count("trial_start"), 4)
        self.assertEqual(kinds.count("trial_end"), 4)
        self.assertEqual(kinds.count("shutdown_state"), 1)
        self.assertNotIn("safe_state", kinds)
        shutdown = next(event for event in events if event["event"] == "shutdown_state")
        self.assertEqual(shutdown["detail"], "applied")
        valve_events = [event for event in events if event["event"] == "valve_command"]
        for event in valve_events:
            self.assertLessEqual(
                float(event["commanded_run_seconds"]), float(event["returned_run_seconds"])
            )
            self.assertRegex(event["returned_wall_time"], r"^\d{4}-\d\d-\d\dT")
        mfc_events = [event for event in events if event["event"] == "mfc_command"]
        self.assertTrue(mfc_events)
        self.assertEqual(
            [event["value"] for event in mfc_events if event["device"] == "mfc-2000"],
            ["1000.0", "0.0"],
            "an unchanged setpoint is commanded once, then the shutdown value",
        )

        manifest = self.manifest(status)
        self.assertEqual(manifest["recipe"], make_recipe().to_dict())
        self.assertEqual(manifest["rig_map"], RIG.to_dict())
        self.assertEqual(manifest["seed"], 3)
        self.assertEqual(manifest["operator_notes"], "fake bench")
        self.assertEqual(
            sorted(manifest["resolved_trial_order"]), ["blank", "blank", "odor", "odor"]
        )
        self.assertEqual(manifest["outcome"], "done")
        self.assertEqual(
            manifest["mfc_full_scales"], {"mfc-500": [500.0, "SCCM"], "mfc-2000": [500.0, "SCCM"]}
        )
        self.assertIn("ended_at", manifest)
        self.assertTrue(manifest["software_version"])

        samples = self.samples_csv(status)
        self.assertGreater(len(samples), 2)
        self.assertEqual({sample["mfc"] for sample in samples}, {"mfc-500", "mfc-2000"})
        self.assertTrue(all(sample["mass_flow"] for sample in samples))
        self.assertEqual(len(self.samples), len(samples))
        self.assertEqual(
            {status.phase for status in self.statuses} & {Phase.RUNNING, Phase.DONE},
            {Phase.RUNNING, Phase.DONE},
        )

    def test_stop_finishes_the_current_trial_then_applies_the_shutdown_state(self) -> None:
        executor = self.executor(make_recipe(step_seconds=0.1, counts={"odor": 3, "blank": 3}))
        executor.start()
        self.wait_for(lambda: executor.status.phase == Phase.RUNNING)

        executor.request_stop()
        executor.join(10)
        status = executor.status

        self.assertEqual(status.phase, Phase.STOPPED, status.message)
        self.assertIn("Stopped after trial 1 of 6", status.message)
        events = self.events(status)
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds.count("trial_start"), 1)
        self.assertEqual(kinds.count("trial_end"), 1)
        self.assertLess(kinds.index("stop_requested"), kinds.index("trial_end"))
        self.assertIn("shutdown_state", kinds)
        self.assertNotIn("safe_state", kinds)
        self.assertEqual(self.final_valves(), {8: False, 9: False, 16: True})
        self.assertEqual(self.rig.alicats["mfc-500"].setpoints[-1], 50.0)
        self.assertEqual(self.manifest(status)["outcome"], "stopped")

    def test_abort_forces_the_safe_state_and_ignores_the_shutdown_state(self) -> None:
        executor = self.executor(make_recipe(step_seconds=0.5))
        executor.start()
        self.wait_for(
            lambda: (
                executor.status.phase == Phase.RUNNING and executor.status.step_index is not None
            )
        )
        started = time.perf_counter()

        executor.abort()
        executor.join(10)
        status = executor.status

        self.assertLess(time.perf_counter() - started, 0.4, "abort must not wait for the step")
        self.assertEqual(status.phase, Phase.ABORTED, status.message)
        self.assertEqual(self.final_valves(), {8: False, 9: False, 16: False})
        self.assertEqual(self.rig.alicats["mfc-500"].setpoints[-1], 0.0)
        self.assertEqual(self.rig.alicats["mfc-2000"].setpoints[-1], 0.0)
        self.assertNotIn(50.0, self.rig.alicats["mfc-500"].setpoints)
        kinds = [event["event"] for event in self.events(status)]
        self.assertIn("abort_requested", kinds)
        self.assertIn("safe_state", kinds)
        self.assertNotIn("shutdown_state", kinds)
        self.assertEqual(self.manifest(status)["outcome"], "aborted")
        self.assertFalse((self.runs / LOCK_FILE_NAME).exists())

    def test_valve_failure_forces_the_safe_state_and_reports_the_error(self) -> None:
        self.rig.labjack.fail_on_write = 1

        status = self.executor().run()

        self.assertEqual(status.phase, Phase.FAILED, status.message)
        self.assertIn("usb gone", status.message)
        self.assertIn("safe state was applied", status.message)
        self.assertEqual(self.final_valves(), {8: False, 9: False, 16: False})
        self.assertEqual(self.rig.alicats["mfc-500"].setpoints[-1], 0.0)
        self.assertEqual(self.rig.alicats["mfc-2000"].setpoints[-1], 0.0)
        events = self.events(status)
        safe = next(event for event in events if event["event"] == "safe_state")
        self.assertEqual(safe["detail"], "applied")
        self.assertEqual(self.manifest(status)["outcome"], "failed")

    def test_mfc_refusal_before_the_run_closes_valves_and_sends_no_setpoint(self) -> None:
        self.rig.alicats[
            "mfc-2000"
        ].prepare_error = "Refusing to change the setpoint while its source is analog."

        status = self.executor().run()

        self.assertEqual(status.phase, Phase.FAILED, status.message)
        self.assertIn("MFC mfc-2000", status.message)
        self.assertIn("source is analog", status.message)
        self.assertEqual(self.rig.alicats["mfc-500"].setpoints, [])
        self.assertEqual(self.rig.alicats["mfc-2000"].setpoints, [])
        self.assertEqual(
            self.rig.labjack.writes,
            [(self.rig.labjack.writes[0][0], {8: False, 9: False, 16: False})],
        )
        kinds = [event["event"] for event in self.events(status)]
        self.assertNotIn("trial_start", kinds)
        self.assertIn("safe_state", kinds)

    def test_missing_labjack_sends_no_command_and_leaves_a_record(self) -> None:
        self.rig.labjack_error = "Cannot connect to the LabJack U3: not found"

        status = self.executor().run()

        self.assertEqual(status.phase, Phase.FAILED)
        self.assertIn("No hardware command was sent", status.message)
        self.assertEqual(self.rig.labjack.writes, [])
        self.assertEqual(self.rig.alicats["mfc-500"].setpoints, [])
        self.assertEqual(self.manifest(status)["outcome"], "failed")
        self.assertFalse((self.runs / LOCK_FILE_NAME).exists())

    def test_invalid_recipe_is_refused_before_any_hardware_or_directory(self) -> None:
        recipe = make_recipe()
        bad = Recipe(
            recipe.name,
            (Trial("odor", (Step(1.0, {"ghost": True}, {}),)),),
            Schedule({"odor": 1}),
            recipe.shutdown,
        )

        status = self.executor(bad).run()

        self.assertEqual(status.phase, Phase.FAILED)
        self.assertIn("unknown valve 'ghost'", status.message)
        self.assertIn("No hardware was used", status.message)
        self.assertEqual(self.rig.labjack_opens, 0)
        self.assertFalse(self.runs.exists())

    def test_active_lock_refuses_the_run_before_any_hardware(self) -> None:
        other = RunLock(self.runs, self.runs / "20260101-000000-other")
        other.acquire()

        status = self.executor().run()

        self.assertEqual(status.phase, Phase.FAILED)
        self.assertIn("A run is active", status.message)
        self.assertIn("20260101-000000-other", status.message)
        self.assertEqual(self.rig.labjack_opens, 0)
        self.assertTrue((self.runs / LOCK_FILE_NAME).exists(), "the other lock is kept")


class TimingTests(ExecutorTestCase):
    def test_slow_mfc_reads_do_not_delay_valve_steps(self) -> None:
        for alicat in self.rig.alicats.values():
            alicat.read_delay = 0.1
        recipe = make_recipe(step_seconds=0.02, counts={"odor": 4, "blank": 4})

        status = self.executor(recipe).run()

        self.assertEqual(status.phase, Phase.DONE, status.message)
        events = [
            event
            for event in self.events(status)
            if event["event"] == "valve_command" and event["scheduled_run_seconds"]
        ]
        self.assertGreaterEqual(len(events), 12)
        lateness = [
            float(event["commanded_run_seconds"]) - float(event["scheduled_run_seconds"])
            for event in events
        ]
        self.assertTrue(all(late >= 0 for late in lateness))
        self.assertLess(max(lateness), 0.015, f"lateness {lateness}")
        self.assertGreater(len(self.samples), 0, "sampling ran in parallel")

    def test_step_deadlines_do_not_drift(self) -> None:
        self.rig.labjack.write_delay = 0.004
        recipe = make_recipe(step_seconds=0.02, counts={"odor": 5, "blank": 5})

        status = self.executor(recipe).run()

        self.assertEqual(status.phase, Phase.DONE, status.message)
        run_start = next(event for event in self.events(status) if event["event"] == "run_start")
        trial_ends = [event for event in self.events(status) if event["event"] == "trial_end"]
        self.assertEqual(len(trial_ends), 10)
        last = trial_ends[-1]
        self.assertAlmostEqual(float(last["scheduled_run_seconds"]), 0.3, places=6)
        self.assertLess(
            float(last["returned_run_seconds"]) - float(last["scheduled_run_seconds"]), 0.02
        )
        self.assertLess(float(run_start["returned_run_seconds"]), 0.01)

    def test_status_reports_progress_from_the_worker_thread(self) -> None:
        executor = self.executor(make_recipe(step_seconds=0.05))
        seen_threads: set[str] = set()
        executor._on_status = lambda status: seen_threads.add(threading.current_thread().name)
        executor.start()
        executor.join(10)

        self.assertEqual(executor.status.phase, Phase.DONE, executor.status.message)
        self.assertIn("sniffler-executor", seen_threads)
        self.assertEqual(executor.status.trial_index, 3)
        self.assertEqual(executor.status.valves, SHUTDOWN.valves)
        self.assertEqual(executor.status.setpoints, SHUTDOWN.setpoints)
        self.assertFalse(executor.is_alive)


if __name__ == "__main__":
    unittest.main()
