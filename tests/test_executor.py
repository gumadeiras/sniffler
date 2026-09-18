"""Executor tests with fake hardware: exit paths, timing, and the run log."""

import csv
import json
import statistics
import tempfile
import threading
import time
import unittest
from pathlib import Path

from sniffler.config import TriggerSettings
from sniffler.executor import Event, Executor, Phase, Sample, Status
from sniffler.fakes import FakeRig, PulseTrain
from sniffler.hardware import DeviceError
from sniffler.recipe import MfcMap, Recipe, RigMap, Schedule, Step, Trial
from sniffler.runlog import LOCK_FILE_NAME, RunLock


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


RIG_WITH_TRIGGER = RigMap(RIG.labjack_serial, RIG.valves, RIG.mfcs, TriggerSettings(4))

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

    def executor(self, recipe: Recipe | None = None, rig: RigMap = RIG, **options) -> Executor:
        return Executor(
            recipe or make_recipe(),
            rig,
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
        self.assertIn("All valves closed, every flow zero", status.message)
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


class EventCallbackTests(ExecutorTestCase):
    def test_on_event_receives_every_valve_command_in_log_order(self) -> None:
        received: list[Event] = []
        threads: set[str] = set()

        def on_event(event: Event) -> None:
            received.append(event)
            threads.add(threading.current_thread().name)

        status = self.executor(on_event=on_event).run()

        self.assertEqual(status.phase, Phase.DONE, status.message)
        logged = [row for row in self.events(status) if row["event"] == "valve_command"]
        seen = [event for event in received if event.event == "valve_command"]
        self.assertGreaterEqual(len(logged), 8)
        self.assertEqual(
            [(event.device, event.value) for event in seen],
            [(row["device"], row["value"]) for row in logged],
            "the callback sees each valve command once, in the order the log has",
        )
        for event, row in zip(seen, logged, strict=True):
            self.assertEqual(f"{event.returned_run_seconds:.6f}", row["returned_run_seconds"])
            self.assertEqual(f"{event.commanded_run_seconds:.6f}", row["commanded_run_seconds"])
            self.assertEqual(event.returned_wall_time, row["returned_wall_time"])
            self.assertEqual(
                "" if event.trial_index is None else str(event.trial_index), row["trial_index"]
            )
            self.assertEqual(
                "" if event.step_index is None else str(event.step_index), row["step_index"]
            )
            self.assertEqual(event.trial_name, row["trial_name"])
            self.assertEqual(event.detail, row["detail"])
        self.assertEqual(threads, {"MainThread"}, "events come from the step-timing thread")
        self.assertIn("run_start", [event.event for event in received])
        self.assertEqual(received[-1].event, "run_end")


class TimingTests(ExecutorTestCase):
    def test_slow_mfc_reads_do_not_delay_valve_steps(self) -> None:
        read_delay = 0.1
        for alicat in self.rig.alicats.values():
            alicat.read_delay = read_delay
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
        # A step thread that waited for one MFC read would be late by the whole read
        # delay. The host scheduler can wake the thread tens of milliseconds late on
        # a loaded machine, so only the typical step must land inside the spin margin.
        self.assertLess(max(lateness), read_delay, f"lateness {lateness}")
        self.assertLess(statistics.median(lateness), 0.015, f"lateness {lateness}")
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


class TriggerTests(ExecutorTestCase):
    def test_waits_for_the_pulse_then_starts_the_schedule_from_it(self) -> None:
        self.rig.labjack.counts = [0] * 25 + [1]

        status = self.executor(rig=RIG_WITH_TRIGGER, wait_for_trigger=True).run()

        self.assertEqual(status.phase, Phase.DONE, status.message)
        self.assertEqual(self.rig.labjack.counter_channel, 4)
        self.assertTrue(self.rig.labjack.counter_restored)
        events = self.events(status)
        kinds = [event["event"] for event in events]
        for earlier, later in (
            ("run_start", "counter_enabled"),
            ("counter_enabled", "trigger_wait"),
            ("trigger_wait", "trigger_received"),
            ("trigger_received", "trial_start"),
            ("shutdown_state", "run_end"),
        ):
            self.assertLess(kinds.index(earlier), kinds.index(later), f"{earlier} before {later}")
        self.assertLess(kinds.index("counter_restored"), kinds.index("shutdown_state"))
        received = events[kinds.index("trigger_received")]
        self.assertEqual(received["value"], "received")
        # The rest-state valve write took the first scripted count; the gate read the rest.
        self.assertIn("25 reads", received["detail"])
        before = events[: kinds.index("trigger_received")]
        rest_valves = [event for event in before if event["event"] == "valve_command"]
        self.assertTrue(rest_valves)
        self.assertTrue(all(event["detail"] == "rest state while waiting" for event in rest_valves))
        self.assertEqual(self.rig.labjack.writes[0][1], {8: False, 9: False, 16: True})
        self.assertEqual(self.rig.alicats["mfc-500"].setpoints[0], 50.0)
        trigger_seconds = float(received["returned_run_seconds"])
        self.assertGreater(trigger_seconds, 0.0)
        first_trial = events[kinds.index("trial_start")]
        self.assertEqual(float(first_trial["scheduled_run_seconds"]), trigger_seconds)
        self.assertAlmostEqual(status.trigger_seconds, trigger_seconds, places=6)
        self.assertNotIn("sync_pulse", kinds, "the start pulse is not a sync mark")
        self.assertEqual(status.sync_pulses, 0)
        manifest = self.manifest(status)
        self.assertTrue(manifest["wait_for_trigger"])
        self.assertEqual(manifest["trigger_seconds"], status.trigger_seconds)
        self.assertEqual(manifest["sync_pulses"], 0)
        self.assertIsNone(manifest["sync_recording_stopped_seconds"])
        self.assertEqual(manifest["rig_map"]["trigger"], {"channel": 4, "timeout_seconds": None})

    def test_records_every_sync_pulse_with_the_time_it_was_seen(self) -> None:
        # Pulses come from the clock, not from a per-read script: how many idle polls
        # happen before a valve write depends on the host's timer. Three pulses land
        # inside the 0.3 s run; the fourth would arrive 60 ms after its end.
        labjack = self.rig.labjack
        labjack.pulse_train = PulseTrain(first_seconds=0.03, period_seconds=0.11)
        recipe = make_recipe(step_seconds=0.1, counts={"odor": 1, "blank": 1})

        status = self.executor(recipe, rig=RIG_WITH_TRIGGER).run()

        self.assertEqual(status.phase, Phase.DONE, status.message)
        events = self.events(status)
        pulses = [event for event in events if event["event"] == "sync_pulse"]
        self.assertEqual([event["value"] for event in pulses], ["1", "2", "3"])
        seen = [float(event["returned_run_seconds"]) for event in pulses]
        for arrived, seen_at in zip((0.03, 0.14, 0.25), seen, strict=True):
            self.assertGreaterEqual(seen_at, arrived)
        self.assertEqual(seen, sorted(seen))
        for event in pulses:
            self.assertEqual(event["device"], "FIO4")
            self.assertIn(event["trial_index"], {"0", "1"})
            self.assertTrue(event["trial_name"])
        self.assertEqual(status.sync_pulses, 3)
        self.assertEqual(status.trigger_seconds, 0.0)
        self.assertEqual(self.manifest(status)["sync_pulses"], 3)
        self.assertGreater(labjack.count_reads, len(labjack.writes), "idle polls happened")

    def test_valve_commands_carry_the_count_and_mark_pulses_in_short_steps(self) -> None:
        # Steps of 20 ms never leave 30 ms of idle time, so the counter is read
        # only inside the valve write packets.
        self.rig.labjack.counts = [0, 0, 1]
        recipe = make_recipe(step_seconds=0.02, counts={"odor": 1, "blank": 1})

        status = self.executor(recipe, rig=RIG_WITH_TRIGGER).run()

        self.assertEqual(status.phase, Phase.DONE, status.message)
        events = self.events(status)
        valve_rows = [e for e in events if e["event"] == "valve_command"]
        writes = len(self.rig.labjack.writes)
        self.assertEqual(self.rig.labjack.count_reads, writes, "one counter read per write")
        self.assertTrue(all(row["sync_count"] != "" for row in valve_rows))
        pulses = [e for e in events if e["event"] == "sync_pulse"]
        self.assertEqual(len(pulses), 1)
        marked_at = pulses[0]["returned_run_seconds"]
        self.assertIn(marked_at, {row["returned_run_seconds"] for row in valve_rows})
        self.assertEqual(pulses[0]["sync_count"], "1")
        self.assertEqual(status.sync_pulses, 1)

    def test_a_plain_rig_leaves_the_count_column_empty(self) -> None:
        status = self.executor(rig=RIG).run()

        rows = self.events(status)
        self.assertIn("sync_count", rows[0])
        self.assertTrue(all(row["sync_count"] == "" for row in rows))

    def test_a_dead_counter_stops_the_record_and_the_run_goes_on(self) -> None:
        self.rig.labjack.counter_error = "usb gone"
        # Long steps leave enough idle time for five polls on a host with coarse timers.
        recipe = make_recipe(step_seconds=0.2, counts={"odor": 1, "blank": 1})

        status = self.executor(recipe, rig=RIG_WITH_TRIGGER).run()

        self.assertEqual(
            status.phase, Phase.FAILED, "arming reads the counter once, so arming fails"
        )
        self.assertIn("usb gone", status.message)

        # The counter dies after arming: the record stops, the trials finish. Only
        # the idle polls fail; a failed valve packet would end the run instead.
        self.rig = type(self.rig)()
        labjack = self.rig.labjack
        labjack.poll_error = "usb gone"
        status = self.executor(recipe, rig=RIG_WITH_TRIGGER).run()

        self.assertEqual(status.phase, Phase.DONE, status.message)
        events = self.events(status)
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds.count("error"), 5)
        self.assertEqual(kinds.count("sync_recording_stopped"), 1)
        self.assertEqual(kinds.count("trial_end"), 2)
        self.assertIsNotNone(status.sync_stopped_seconds)
        manifest = self.manifest(status)
        self.assertEqual(manifest["sync_recording_stopped_seconds"], status.sync_stopped_seconds)
        self.assertEqual(manifest["sync_pulses"], 0)
        self.assertTrue(labjack.counter_restored)

    def test_a_failure_while_arming_still_restores_the_counter(self) -> None:
        labjack = self.rig.labjack

        def read_digital(channel):
            raise DeviceError("line read failed")

        labjack.read_digital = read_digital

        status = self.executor(rig=RIG_WITH_TRIGGER).run()

        self.assertEqual(status.phase, Phase.FAILED, status.message)
        self.assertIn("line read failed", status.message)
        self.assertEqual(labjack.counter_channel, 4)
        self.assertTrue(labjack.counter_restored)
        self.assertEqual(self.final_valves(), {8: False, 9: False, 16: False})

    def test_abort_during_the_wait_forces_the_safe_state(self) -> None:
        executor = self.executor(rig=RIG_WITH_TRIGGER, wait_for_trigger=True)
        executor.start()
        self.wait_for(lambda: executor.status.phase == Phase.WAITING)

        executor.abort()
        executor.join(10)
        status = executor.status

        self.assertEqual(status.phase, Phase.ABORTED, status.message)
        self.assertEqual(self.final_valves(), {8: False, 9: False, 16: False})
        self.assertEqual(self.rig.alicats["mfc-500"].setpoints[-1], 0.0)
        self.assertTrue(self.rig.labjack.counter_restored)
        kinds = [event["event"] for event in self.events(status)]
        self.assertNotIn("trial_start", kinds)
        self.assertNotIn("trigger_received", kinds)
        end = next(event for event in self.events(status) if event["event"] == "trigger_end")
        self.assertEqual(end["value"], "aborted")

    def test_stop_during_the_wait_ends_with_the_end_state_and_no_trial(self) -> None:
        executor = self.executor(rig=RIG_WITH_TRIGGER, wait_for_trigger=True)
        executor.start()
        self.wait_for(lambda: executor.status.phase == Phase.WAITING)

        executor.request_stop()
        executor.join(10)
        status = executor.status

        self.assertEqual(status.phase, Phase.STOPPED, status.message)
        self.assertIn("Stopped after trial 0 of 4", status.message)
        self.assertEqual(self.final_valves(), {8: False, 9: False, 16: True})
        self.assertEqual(self.rig.alicats["mfc-500"].setpoints[-1], 50.0)
        self.assertTrue(self.rig.labjack.counter_restored)
        kinds = [event["event"] for event in self.events(status)]
        self.assertNotIn("trial_start", kinds)
        self.assertIn("trigger_end", kinds)

    def test_start_now_skips_the_rest_of_the_wait(self) -> None:
        executor = self.executor(rig=RIG_WITH_TRIGGER, wait_for_trigger=True)
        executor.start()
        self.wait_for(lambda: executor.status.phase == Phase.WAITING)

        executor.start_now()
        executor.join(10)
        status = executor.status

        self.assertEqual(status.phase, Phase.DONE, status.message)
        received = next(e for e in self.events(status) if e["event"] == "trigger_received")
        self.assertEqual(received["value"], "started now")
        self.assertAlmostEqual(
            status.trigger_seconds, float(received["returned_run_seconds"]), places=6
        )

    def test_timeout_fails_safe_and_restores_the_counter(self) -> None:
        rig = RigMap(RIG.labjack_serial, RIG.valves, RIG.mfcs, TriggerSettings(4, 0.05))

        status = self.executor(rig=rig, wait_for_trigger=True).run()

        self.assertEqual(status.phase, Phase.FAILED, status.message)
        self.assertIn("No TTL pulse on FIO4 within 0.05 s", status.message)
        self.assertEqual(self.final_valves(), {8: False, 9: False, 16: False})
        self.assertEqual(self.rig.alicats["mfc-500"].setpoints[-1], 0.0)
        self.assertTrue(self.rig.labjack.counter_restored)
        end = next(e for e in self.events(status) if e["event"] == "trigger_end")
        self.assertEqual(end["value"], "timed out")

    def test_wait_without_a_trigger_table_is_refused_before_hardware(self) -> None:
        status = self.executor(rig=RIG, wait_for_trigger=True).run()

        self.assertEqual(status.phase, Phase.FAILED)
        self.assertIn("no [trigger] table", status.message)
        self.assertEqual(self.rig.labjack_opens, 0)
        self.assertFalse(self.runs.exists())

    def test_a_rig_without_a_trigger_line_never_touches_the_counter(self) -> None:
        status = self.executor(rig=RIG).run()

        self.assertEqual(status.phase, Phase.DONE, status.message)
        self.assertEqual(self.rig.labjack.count_reads, 0)
        self.assertIsNone(self.rig.labjack.counter_channel)
        self.assertFalse(self.rig.labjack.counter_restored)
        self.assertIsNone(status.sync_pulses)
        self.assertEqual(status.trigger_seconds, 0.0)
        manifest = self.manifest(status)
        self.assertFalse(manifest["wait_for_trigger"])
        self.assertIsNone(manifest["sync_pulses"])


if __name__ == "__main__":
    unittest.main()
