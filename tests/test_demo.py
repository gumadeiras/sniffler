"""Demo mode: fake devices only, a valid sample recipe, and a visible marker."""

import gc
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from sniffler.executor import Phase
from sniffler.gui import demo
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
        rig = rig_map_from_settings(demo.demo_settings())
        recipe = demo.demo_recipe()
        self.assertEqual(recipe_problems(recipe, rig), [])
        mixture = recipe.trial("mixture A + B")
        together = [step for step in mixture.steps if sum(step.valves.values()) == 2]
        self.assertEqual(len(together), 3, "three pulses open valve A and valve B together")
        self.assertTrue(all(step.valves["valve A"] and step.valves["valve B"] for step in together))
        self.assertEqual(list(rig.valves), ["valve A", "valve B", "valve C", "valve D"])
        self.assertEqual(list(rig.mfcs), ["carrier flow", "odor flow"])
        self.assertTrue(all(mfc.port.startswith("fake:") for mfc in rig.mfcs.values()))

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

        window = demo.demo_window(self.store, Path(self.temporary.name, "runs-demo"))

        self.assertIsNone(window.recipe_path)
        self.assertEqual(window.editor.recipe().name, "demo pulses")
        self.assertTrue(window.start_button.isEnabled())

    def test_demo_window_is_marked_and_runs_on_fake_devices(self) -> None:
        window = demo.demo_window(self.store, Path(self.temporary.name, "runs-demo"))
        warnings: list[tuple[str, str]] = []
        window._tell = lambda _parent, title, text: warnings.append((title, text))
        window._ask = lambda *_arguments, **_options: QMessageBox.Discard
        self.assertIn("sniffler demo", window.windowTitle())
        self.assertFalse(window.isWindowModified())
        self.assertEqual(window.editor.recipe().name, "demo pulses")
        self.assertTrue(window.start_button.isEnabled())

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


if __name__ == "__main__":
    unittest.main()
