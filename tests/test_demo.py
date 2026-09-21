"""Demo mode: fake devices only, a valid sample recipe, and a visible marker."""

import csv
import gc
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from sniffler.executor import Phase
from sniffler.fakes import FakeLabJack, PulseTrain
from sniffler.gui import app, demo
from sniffler.recipe import (
    Recipe,
    Schedule,
    Step,
    Trial,
    recipe_problems,
    rig_map_from_settings,
    save_recipe,
)


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = QSettings(str(Path(self.temporary.name, "demo.ini")), QSettings.IniFormat)

    def tearDown(self) -> None:
        # Collect the window on the GUI thread; see GuiTestCase.tearDown.
        self.application.processEvents()
        gc.collect()
        self.temporary.cleanup()

    def wait_until(self, condition, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("condition not met in time")
            self.application.processEvents()
            time.sleep(0.005)

    def test_demo_recipe_is_valid_for_the_demo_rig(self) -> None:
        rig = rig_map_from_settings(demo.demo_settings(Path(self.temporary.name, "runs")))
        recipe = demo.demo_recipe()
        self.assertEqual(recipe_problems(recipe, rig), [])
        self.assertEqual(recipe.schedule.interleave, "blank")
        self.assertEqual(recipe.schedule.run_counts()["blank"], 6, "one blank after each trial")
        mixture = recipe.trial("mixture A + B")
        together = [step for step in mixture.steps if sum(step.valves.values()) == 2]
        self.assertEqual(len(together), 3, "three pulses open valve A and valve B together")
        self.assertTrue(all(step.valves["valve A"] and step.valves["valve B"] for step in together))
        self.assertEqual(list(rig.valves), ["valve A", "valve B", "valve C", "valve D"])
        self.assertEqual(set(recipe.valve_contents), set(rig.valves), "every valve names an odor")
        self.assertEqual(recipe.valve_label("valve C"), "geosmin")
        self.assertEqual(list(rig.mfcs), ["carrier flow", "odor flow"])
        self.assertTrue(all(mfc.port.startswith("fake:") for mfc in rig.mfcs.values()))

    def test_demo_writes_runs_to_the_configured_directory(self) -> None:
        config = Path(self.temporary.name, "lab.toml")
        config.write_text('[runs]\ndirectory = "demo-out"\n', encoding="utf-8")
        windows: list = []
        open_demo = demo.demo_window

        def record_window(runs_directory: Path):
            # The test store keeps the demo away from the real demo settings.
            windows.append(open_demo(runs_directory, self.store))
            return windows[-1]

        with (
            mock.patch.object(demo, "demo_window", record_window),
            mock.patch.object(QApplication, "exec", lambda _application: 0),
        ):
            status = app.main(["--demo", "--config", str(config)])
        self.assertEqual(status, 0)
        self.assertEqual(len(windows), 1)
        window = windows[0]
        self.assertIn("sniffler demo", window.windowTitle())
        self.assertEqual(window._settings.runs_directory, config.parent / "demo-out")
        self.assertIn("demo-out", window.statusBar().currentMessage())
        window.close()

    def test_demo_opens_its_own_recipe_even_when_another_was_remembered(self) -> None:
        other = Path(self.temporary.name, "real-rig.json")
        save_recipe(
            other,
            Recipe(
                "real rig",
                (Trial("t", (Step(1.0, {"odor-1": True}, {"mfc-500": 1.0}),)),),
                Schedule({"t": 1}),
                Step(None, {"odor-1": False}, {"mfc-500": 0.0}),
            ),
        )
        self.store.setValue("last_recipe", str(other))
        self.store.sync()

        window = demo.demo_window(Path(self.temporary.name, "runs"), self.store)

        self.assertIsNone(window.recipe_path)
        self.assertEqual(window.editor.recipe().name, "demo pulses")
        self.assertTrue(window.start_button.isEnabled())

    def test_demo_window_is_marked_and_runs_on_fake_devices(self) -> None:
        window = demo.demo_window(Path(self.temporary.name, "runs"), self.store)
        warnings: list[tuple[str, str]] = []
        window._tell = lambda _parent, title, text: warnings.append((title, text))
        window._ask = lambda *_arguments, **_options: QMessageBox.Discard
        self.assertIn("sniffler demo", window.windowTitle())
        self.assertFalse(window.isWindowModified())
        self.assertEqual(window.editor.recipe().name, "demo pulses")
        self.assertTrue(window.start_button.isEnabled())
        self.assertEqual(window.ttl_box.text(), "TTL high during the run")
        self.assertTrue(window.send_ttl(), "the demo sends the TTL by default")

        recipe = demo.demo_recipe()
        short = Recipe(
            recipe.name,
            (Trial("odor A", (demo._step(0.4, "valve A"), demo._step(0.4))),),
            Schedule({"odor A": 2}, seed=1),
            recipe.shutdown,
        )
        window.editor.set_recipe(short)
        window.start_run()
        self.wait_until(lambda: not window.controller.is_running)
        self.application.processEvents()

        status = window.controller.executor.status
        self.assertEqual(status.phase, Phase.DONE, status.message)
        self.assertEqual(warnings, [])
        self.assertEqual(
            window.run_view.status_panel.squirrel.cue_name, "", "every valve closed at the end"
        )
        cells = window.run_view.plot._cells
        self.assertNotEqual(cells["carrier flow"]["measured"].text(), "—")
        self.assertNotEqual(cells["odor flow"]["measured"].text(), "—")
        history = window.run_view.plot._history["carrier flow"]
        self.assertGreater(len(history), 0)
        self.assertGreater(len(history), 8)
        self.assertLess(
            abs(history[-1][2] - demo.CARRIER),
            0.1 * demo.CARRIER,
            "the fake carrier flow approaches its setpoint",
        )
        window.close()

    def test_fake_pulse_train_counts_from_the_counter_reset(self) -> None:
        now = [0.0]
        labjack = FakeLabJack(
            PulseTrain(first_seconds=3.0, period_seconds=1.0), clock=lambda: now[0]
        )
        now[0] = 10.0
        self.assertEqual(labjack.read_counter(reset=True), 0)
        now[0] = 12.9
        self.assertEqual(labjack.read_counter(), 0, "nothing before the first pulse")
        now[0] = 13.0
        self.assertEqual(labjack.read_counter(), 1)
        now[0] = 15.5
        self.assertEqual(labjack.read_counter(), 3)
        self.assertEqual(labjack.write_digital_lines({8: True}, read_counter=True), 3)
        self.assertEqual(labjack.count_reads, 4, "the reset is not a counted read")

    def test_demo_run_starts_on_the_fake_pulse_and_marks_the_train(self) -> None:
        window = demo.demo_window(Path(self.temporary.name, "runs"), self.store)
        warnings: list[tuple[str, str]] = []
        window._tell = lambda _parent, title, text: warnings.append((title, text))
        window._ask = lambda *_arguments, **_options: QMessageBox.Discard
        recipe = demo.demo_recipe()
        short = Recipe(
            recipe.name,
            (Trial("odor A", (demo._step(0.8, "valve A"), demo._step(0.8))),),
            Schedule({"odor A": 1}),
            recipe.shutdown,
        )
        window.editor.set_recipe(short)
        window.trigger_box.setChecked(True)

        window.start_run()
        self.wait_until(lambda: not window.controller.is_running)
        self.application.processEvents()

        status = window.controller.executor.status
        self.assertEqual(status.phase, Phase.DONE, status.message)
        self.assertEqual(warnings, [])
        self.assertAlmostEqual(status.trigger_seconds, demo.PULSES.first_seconds, delta=0.5)
        self.assertGreaterEqual(status.sync_pulses, 1, "the train continues through the trials")
        self.assertGreaterEqual(len(window.run_view.timeline._marks), 1)
        with (status.run_directory / "events.csv").open(newline="") as handle:
            events = {row["event"]: row for row in csv.DictReader(handle)}
        self.assertEqual(events["trigger_received"]["value"], "received")
        self.assertEqual(events["trigger_received"]["sync_count"], "")
        self.assertEqual(events["sync_pulse"]["sync_count"], "2", "the start pulse is not a mark")
        with (status.run_directory / "trigger-fio4.csv").open(newline="") as handle:
            trigger = [row["event"] for row in csv.DictReader(handle)]
        self.assertEqual(trigger[:3], ["counter_enabled", "trigger_wait", "trigger_received"])
        self.assertIn("sync_pulse", trigger)
        self.assertEqual(trigger[-1], "counter_restored")
        with (status.run_directory / "events.csv").open(newline="") as handle:
            ttl = [row["value"] for row in csv.DictReader(handle) if row["event"] == "ttl_command"]
        self.assertEqual(ttl, ["low", "high", "low"], "high from the first trial to the end state")
        window.close()


if __name__ == "__main__":
    unittest.main()
