"""GUI tests under the offscreen Qt platform: authoring, validation, and window close."""

import gc
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEvent, QPointF, QSettings, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError as error:  # pragma: no cover - depends on the platform libraries
    QApplication = None  # type: ignore[assignment]
    IMPORT_ERROR = str(error)
else:
    IMPORT_ERROR = ""

from sniffler.config import AlicatSettings, Settings
from sniffler.executor import Phase
from sniffler.fakes import FakeRig
from sniffler.recipe import Recipe, Schedule, Step, Trial, rig_map_from_settings
from sniffler.runlog import LOCK_FILE_NAME, RunLock

SETTINGS = Settings(
    labjack_serial=320107153,
    alicats={
        "mfc-500": AlicatSettings(
            port="/dev/mfc-500", maximum_flow=400.0, units={"mass_flow": "SCCM"}
        ),
        "mfc-2000": AlicatSettings(port="/dev/mfc-2000", units={"mass_flow": "SCCM"}),
    },
    valves={"odor-1": 8, "odor-2": 9, "final": 16},
)
RIG = rig_map_from_settings(SETTINGS)


def step(duration: float, odor_1: bool = False, flow: float = 100.0) -> Step:
    return Step(
        duration,
        {"odor-1": odor_1, "odor-2": False, "final": False},
        {"mfc-500": flow, "mfc-2000": 1000.0},
    )


def make_recipe(step_seconds: float) -> Recipe:
    return Recipe(
        name="GUI pulses",
        trials=(
            Trial("odor", (step(step_seconds, True), step(step_seconds))),
            Trial("blank", (step(step_seconds),)),
        ),
        schedule=Schedule({"odor": 2, "blank": 2}, "block-randomized", 11),
        shutdown=Step(
            None,
            {"odor-1": False, "odor-2": False, "final": True},
            {"mfc-500": 50.0, "mfc-2000": 0.0},
        ),
        notes="carrier stays on",
    )


@unittest.skipIf(QApplication is None, f"PySide6 is not usable here: {IMPORT_ERROR}")
class GuiTestCase(unittest.TestCase):
    application: "QApplication"

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def tearDown(self) -> None:
        # Collect the test's widgets here, on the GUI thread. A cycle of Qt wrappers
        # collected on an executor or MFC thread frees the C++ object there and crashes.
        self.application.processEvents()
        gc.collect()

    def process_events(self, seconds: float = 0.0) -> None:
        deadline = time.monotonic() + seconds
        self.application.processEvents()
        while time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.005)

    def wait_until(self, condition, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("condition not met in time")
            self.application.processEvents()
            time.sleep(0.005)


class RecipeEditorTests(GuiTestCase):
    def test_authors_a_complete_recipe_without_a_file(self) -> None:
        from sniffler.gui.recipe_editor import RecipeEditor

        editor = RecipeEditor(RIG)
        editor._name.setText("GUI pulses")
        editor._notes.setPlainText("carrier stays on")
        editor.rename_trial(0, "odor")
        model = editor._steps
        # Step 1: 0.5 s, odor-1 open, mfc-500 at 100, mfc-2000 at 1000.
        self.assertTrue(model.setData(model.index(0, 0), "0.5"))
        self.assertTrue(model.setData(model.index(0, 1), Qt.Checked, Qt.CheckStateRole))
        self.assertTrue(model.setData(model.index(0, 4), "100"))
        self.assertTrue(model.setData(model.index(0, 5), "1000"))
        editor._on_add_step()
        self.assertEqual(model.rowCount(), 2)
        self.assertTrue(model.setData(model.index(1, 1), Qt.Unchecked, Qt.CheckStateRole))
        editor._on_add_trial()
        editor.rename_trial(1, "blank")
        model = editor._steps
        self.assertTrue(model.setData(model.index(0, 0), "0.5"))
        self.assertTrue(model.setData(model.index(0, 4), "100"))
        self.assertTrue(model.setData(model.index(0, 5), "1000"))
        editor._set_count(0, 2)
        editor._set_count(1, 2)
        editor._seed.setText("11")
        shutdown = editor._shutdown
        self.assertTrue(shutdown.setData(shutdown.index(0, 2), Qt.Checked, Qt.CheckStateRole))
        self.assertTrue(shutdown.setData(shutdown.index(0, 3), "50"))

        recipe = editor.recipe()

        self.assertEqual(editor.problems(), [])
        self.assertEqual(recipe, make_recipe(0.5))

    def test_loads_and_round_trips_a_recipe(self) -> None:
        from sniffler.gui.recipe_editor import RecipeEditor

        editor = RecipeEditor(RIG)
        editor.set_recipe(make_recipe(0.25))
        editor._trial_list.setCurrentRow(1)
        editor._trial_list.setCurrentRow(0)

        self.assertEqual(editor.recipe(), make_recipe(0.25))
        self.assertEqual(editor.problems(), [])

    def test_marks_cells_that_break_lab_limits_and_blocks_the_run(self) -> None:
        from sniffler.gui.recipe_editor import RecipeEditor
        from sniffler.gui.step_table import PROBLEM_BRUSH

        editor = RecipeEditor(RIG.with_full_scale("mfc-2000", 2000.0, "SCCM"))
        editor._name.setText("limits")
        model = editor._steps
        self.assertFalse(model.setData(model.index(0, 4), "abc"), "letters are refused")
        self.assertTrue(model.setData(model.index(0, 4), "400.01"))
        self.assertTrue(model.setData(model.index(0, 5), "2500"))
        self.assertTrue(model.setData(model.index(0, 0), "0"))

        self.assertEqual(model.data(model.index(0, 4), Qt.BackgroundRole), PROBLEM_BRUSH)
        self.assertIn("lab.toml limit of 400", model.data(model.index(0, 4), Qt.ToolTipRole))
        self.assertIn("full scale of 2000", model.data(model.index(0, 5), Qt.ToolTipRole))
        self.assertIn("greater than zero", model.data(model.index(0, 0), Qt.ToolTipRole))
        self.assertIsNone(model.data(model.index(0, 1), Qt.BackgroundRole))
        problems = editor.problems()
        self.assertEqual(len(problems), 3)
        self.assertTrue(all(problem.startswith("Trial 'trial 1', step 1") for problem in problems))

    def test_refuses_a_recipe_that_names_an_unknown_device(self) -> None:
        from sniffler.gui.recipe_editor import RecipeEditor

        recipe = make_recipe(0.5)
        ghost = Step(
            0.5,
            {**recipe.trials[0].steps[0].valves, "ghost": True},
            recipe.trials[0].steps[0].setpoints,
        )
        recipe = Recipe(
            recipe.name,
            (Trial("odor", (ghost,)), recipe.trials[1]),
            recipe.schedule,
            recipe.shutdown,
        )
        editor = RecipeEditor(RIG)
        editor.set_recipe(recipe)

        problems = editor.problems()

        self.assertEqual(
            problems,
            ["Trial 'odor', step 1: unknown valve 'ghost'. Add it to [valves] in lab.toml."],
        )

    def test_pulse_train_expands_into_explicit_step_rows(self) -> None:
        from sniffler.gui.pulse_dialog import pulse_train
        from sniffler.gui.recipe_editor import RecipeEditor
        from sniffler.gui.step_table import StepRow

        editor = RecipeEditor(RIG)
        template = StepRow(
            1.0,
            {"odor-1": False, "odor-2": True, "final": False},
            {"mfc-500": 100.0, "mfc-2000": 0.0},
        )
        rows = pulse_train(template, "odor-1", 0.1, 0.4, 3, end_with_gap=False)
        editor.insert_steps(1, rows)

        steps = editor._steps.steps()
        self.assertEqual(len(steps), 6)
        self.assertEqual([s.duration_seconds for s in steps[1:]], [0.1, 0.4, 0.1, 0.4, 0.1])
        self.assertEqual([s.valves["odor-1"] for s in steps[1:]], [True, False, True, False, True])
        self.assertTrue(
            all(s.valves["odor-2"] for s in steps[1:]), "other valves copy the template"
        )
        self.assertTrue(all(s.setpoints["mfc-500"] == 100.0 for s in steps[1:]))
        # Each generated row is an ordinary, editable step.
        self.assertTrue(editor._steps.setData(editor._steps.index(2, 0), "0.25"))
        self.assertEqual(editor._steps.steps()[2].duration_seconds, 0.25)


class EditingTests(GuiTestCase):
    def test_remove_trial_asks_first_and_names_the_scope(self) -> None:
        from sniffler.gui.recipe_editor import RecipeEditor

        editor = RecipeEditor(RIG)
        editor.set_recipe(make_recipe(0.5))
        asked: list[str] = []

        def decline(_parent, _title, text, *_rest):
            asked.append(text)
            return QMessageBox.No

        editor._confirm = decline
        editor._trial_list.setCurrentRow(0)
        editor._on_remove_trial()
        self.assertEqual([trial.name for trial in editor.recipe().trials], ["odor", "blank"])
        self.assertIn("'odor' and its 2 steps", asked[0])

        editor._confirm = lambda *_arguments: QMessageBox.Yes
        editor._on_remove_trial()
        self.assertEqual([trial.name for trial in editor.recipe().trials], ["blank"])

    def test_trial_names_edit_in_place_and_reject_duplicates(self) -> None:
        from sniffler.gui.recipe_editor import RecipeEditor

        editor = RecipeEditor(RIG)
        editor.set_recipe(make_recipe(0.5))
        item = editor._trial_list.item(1)
        self.assertTrue(item.flags() & Qt.ItemIsEditable)

        item.setText("control")
        self.assertEqual([trial.name for trial in editor.recipe().trials], ["odor", "control"])
        self.assertEqual(editor.recipe().schedule.counts, {"odor": 2, "control": 2})

        editor._trial_list.item(1).setText("odor")
        self.assertEqual([trial.name for trial in editor.recipe().trials], ["odor", "control"])
        self.assertEqual(editor._trial_list.item(1).text(), "control")
        self.assertIn("already used", editor._problems.text())

    def test_one_click_anywhere_in_a_valve_cell_toggles_it(self) -> None:
        from sniffler.gui.recipe_editor import RecipeEditor

        editor = RecipeEditor(RIG)
        editor.set_recipe(make_recipe(0.5))
        editor.resize(1200, 700)
        editor.show()
        self.process_events()
        view = editor._step_view
        model = editor._steps
        index = model.index(1, 1)  # odor-1 in step 2, closed
        self.assertEqual(model.data(index, Qt.CheckStateRole), Qt.Unchecked)
        rect = view.visualRect(index)
        point = QPointF(rect.right() - 4, rect.center().y())  # far from the check box

        for event_type in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            event = QMouseEvent(
                event_type,
                point,
                view.viewport().mapToGlobal(point.toPoint()),
                Qt.LeftButton,
                Qt.LeftButton,
                Qt.NoModifier,
            )
            QApplication.sendEvent(view.viewport(), event)
        self.assertEqual(model.data(index, Qt.CheckStateRole), Qt.Checked)
        self.assertTrue(editor.recipe().trials[0].steps[1].valves["odor-1"])
        editor.close()

    def test_seed_field_accepts_digits_only(self) -> None:
        from PySide6.QtTest import QTest

        from sniffler.gui.recipe_editor import RecipeEditor

        editor = RecipeEditor(RIG)
        QTest.keyClicks(editor._seed, "12ab3")

        self.assertEqual(editor._seed.text(), "123")
        self.assertEqual(editor.recipe().schedule.seed, 123)
        editor._seed.clear()
        self.assertIsNone(editor.recipe().schedule.seed)

    def test_add_step_opens_the_duration_for_typing(self) -> None:
        from sniffler.gui.recipe_editor import RecipeEditor

        editor = RecipeEditor(RIG)
        editor.show()
        self.process_events()
        editor._on_add_step()

        view = editor._step_view
        self.assertEqual(view.currentIndex().row(), 1)
        self.assertEqual(view.currentIndex().column(), 0)
        self.assertEqual(view.state(), view.State.EditingState)
        editor.close()

    def test_deviation_indicator_says_it_in_words(self) -> None:
        from sniffler.executor import Sample
        from sniffler.gui.run_view import MfcPlot

        plot = MfcPlot(RIG)
        plot.add_sample(Sample("mfc-500", 1.0, 100.0, 100.0, 99.0, "t"))
        self.assertIn("within limit", plot._labels["mfc-500"].text())
        plot.add_sample(Sample("mfc-500", 2.0, 100.0, 100.0, 60.0, "t"))
        self.assertIn("HIGH deviation", plot._labels["mfc-500"].text())


class SniffCueTests(GuiTestCase):
    """The squirrel sniffs on each valve onset: driven by events, fast, and interruptible."""

    def test_new_onset_restarts_the_cue_instead_of_queueing(self) -> None:
        from sniffler.gui.squirrel import SniffWidget

        widget = SniffWidget(reduced=False)
        widget.sniff(["A"])
        self.process_events(0.2)
        widget.sniff(["B"])
        self.assertEqual(widget.cue_name, "B")
        self.assertTrue(widget.animating)
        self.assertLess(widget._animation.currentTime(), 100, "the cue restarted from the onset")
        widget.sniff(["A", "C"])
        self.assertEqual(widget.cue_name, "A + C")

    def test_reduced_motion_shows_the_static_cue_without_an_animation(self) -> None:
        from sniffler.gui.squirrel import SniffWidget

        widget = SniffWidget(reduced=True)
        widget.sniff(["A"])
        self.assertTrue(widget.cue_active)
        self.assertFalse(widget.animating)
        self.assertTrue(widget._hold.isActive(), "the nose stays lit for the cue duration")
        widget.clear()
        self.assertFalse(widget.cue_active)

    def test_reduce_motion_reads_the_override_variable(self) -> None:
        from unittest import mock

        from sniffler.gui.squirrel import reduce_motion

        with mock.patch.dict(os.environ, {"SNIFFLER_REDUCE_MOTION": "1"}):
            self.assertTrue(reduce_motion())
        with mock.patch.dict(os.environ, {"SNIFFLER_REDUCE_MOTION": "0"}):
            self.assertFalse(reduce_motion())


class MainWindowTests(GuiTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runs = Path(self.temporary.name, "runs")
        self.settings = Settings(
            labjack_serial=SETTINGS.labjack_serial,
            alicats=SETTINGS.alicats,
            valves=SETTINGS.valves,
            runs_directory=self.runs,
        )
        self.rig = FakeRig()

    def tearDown(self) -> None:
        super().tearDown()
        self.temporary.cleanup()

    def store(self) -> "QSettings":
        return QSettings(str(Path(self.temporary.name, "gui.ini")), QSettings.IniFormat)

    def window(self, answer=QMessageBox.Yes, reduced_motion: bool = True):
        from sniffler.gui.app import MainWindow

        window = MainWindow(
            self.settings,
            RIG,
            open_labjack=self.rig.open_labjack,
            open_alicat=self.rig.open_alicat,
            store=self.store(),
            reduced_motion=reduced_motion,
        )
        window._ask = lambda *_arguments, **_options: answer
        window.warnings = []
        window._tell = lambda _parent, title, text: window.warnings.append((title, text))
        return window

    def test_window_close_during_a_run_aborts_and_forces_the_safe_state(self) -> None:
        window = self.window()
        window.editor.set_recipe(make_recipe(1.0))
        window.start_run()
        self.wait_until(
            lambda: (
                window.controller.executor is not None
                and window.controller.executor.status.phase == Phase.RUNNING
            )
        )
        self.assertTrue(window.controller.is_running)
        self.assertTrue(window.abort_button.isEnabled())

        window.close()
        self.process_events(0.1)

        self.assertFalse(window.controller.is_running)
        self.assertEqual(window.controller.executor.status.phase, Phase.ABORTED)
        self.assertEqual(self.rig.labjack.writes[-1][1], {8: False, 9: False, 16: False})
        self.assertEqual(self.rig.alicats["mfc-500"].setpoints[-1], 0.0)
        self.assertNotIn(50.0, self.rig.alicats["mfc-500"].setpoints)
        self.assertFalse((self.runs / LOCK_FILE_NAME).exists())

    def test_window_close_is_refused_when_the_operator_declines(self) -> None:
        window = self.window(answer=QMessageBox.No)
        window.editor.set_recipe(make_recipe(1.0))
        window.start_run()
        self.wait_until(lambda: window.controller.is_running)

        window.close()

        self.assertTrue(window.isVisible() or window.controller.is_running)
        self.assertTrue(window.controller.is_running)
        window._ask = lambda *_arguments, **_options: QMessageBox.Yes
        window.close()
        self.assertFalse(window.controller.is_running)

    def test_run_from_the_window_publishes_status_and_samples_to_the_view(self) -> None:
        window = self.window()
        window.editor.set_recipe(make_recipe(0.05))
        window._notes.setPlainText("bench notes")
        window.start_run()

        self.wait_until(
            lambda: not window.controller.is_running and not window.stop_button.isEnabled()
        )
        self.process_events(0.2)

        status = window.controller.executor.status
        self.assertEqual(status.phase, Phase.DONE, status.message)
        self.assertEqual(window.run_view.status_panel._phase.text(), "done")
        self.assertIn("4 of 4", window.run_view.status_panel._trial.text())
        self.assertGreater(len(window.run_view.plot._history["mfc-500"]), 0)
        self.assertIn("mfc-500", window.run_view.status_panel._readback.text())
        self.assertTrue(window.start_button.isEnabled())
        self.assertEqual(window.warnings, [])
        manifest = (status.run_directory / "manifest.json").read_text()
        self.assertIn("bench notes", manifest)

    def test_sniff_cue_follows_each_valve_onset_within_one_gui_tick(self) -> None:
        window = self.window(reduced_motion=False)
        window.editor.set_recipe(make_recipe(0.1))
        squirrel = window.run_view.status_panel.squirrel
        seen: list[str] = []
        window.controller.event_received.connect(
            lambda event: (
                seen.append(event.device)
                if event.event == "valve_command"
                and event.value == "open"
                and event.step_index is not None
                else None
            )
        )
        window.start_run()
        self.wait_until(lambda: not window.controller.is_running)
        self.process_events(0.2)

        self.assertEqual(window.controller.executor.status.phase, Phase.DONE)
        self.assertEqual(seen, ["odor-1", "odor-1"], "one onset for each odor trial")
        self.assertEqual(squirrel.cue_name, "odor-1")
        self.assertEqual(len(window.run_view.cue_latencies), 2)
        self.assertTrue(
            all(0 <= latency < 0.1 for latency in window.run_view.cue_latencies),
            f"event to cue latencies {window.run_view.cue_latencies}",
        )

    def test_rapid_start_clicks_start_one_run(self) -> None:
        window = self.window()
        window.editor.set_recipe(make_recipe(0.2))
        window.start_button.click()
        window.start_button.click()
        window.start_run()
        self.wait_until(lambda: window.controller.is_running)
        self.assertFalse(window.start_button.isEnabled())
        self.wait_until(lambda: not window.controller.is_running)
        self.process_events(0.2)

        self.assertEqual(self.rig.labjack_opens, 1)
        self.assertEqual(window.warnings, [])
        self.assertEqual(len(list(self.runs.iterdir())), 1)

    def test_keyboard_only_authoring_and_start(self) -> None:
        """Tab order: name, notes, trials, trial tools, steps, step tools, shutdown, schedule."""
        from PySide6.QtTest import QTest

        window = self.window()
        window.show()
        self.process_events()
        editor = window.editor
        editor._name.setFocus()
        QTest.keyClicks(editor._name, "Keyboard recipe")
        QTest.keyClick(window, Qt.Key_Tab)
        self.assertIs(QApplication.focusWidget(), editor._notes)
        QTest.keyClick(window, Qt.Key_Tab)
        self.assertIs(QApplication.focusWidget(), editor._trial_list)
        QTest.keyClick(window, Qt.Key_Tab)
        self.assertIs(QApplication.focusWidget(), editor._add_trial)
        QTest.keyClick(editor._add_trial, Qt.Key_Space)  # trial 2, selected
        self.assertEqual(len(editor.recipe().trials), 2)
        for expected in (editor._remove_trial, editor._duplicate_trial, editor._rename_trial):
            QTest.keyClick(window, Qt.Key_Tab)
            self.assertIs(QApplication.focusWidget(), expected)
        QTest.keyClick(window, Qt.Key_Tab)
        self.assertIs(QApplication.focusWidget(), editor._step_view)
        # Space toggles the valve in the current cell; the arrow keys move between
        # cells; the platform edit key (F2, or Return on macOS) opens the number editor.
        view = editor._step_view
        view.setCurrentIndex(editor._steps.index(0, 1))
        QTest.keyClick(view, Qt.Key_Space)
        self.assertTrue(editor.recipe().trials[1].steps[0].valves["odor-1"])
        QTest.keyClick(view, Qt.Key_Left)
        QTest.keyClick(view, Qt.Key_F2)
        if view.state() != view.State.EditingState:
            QTest.keyClick(view, Qt.Key_Return)
        line = view.indexWidget(view.currentIndex())
        self.assertIsNotNone(line, "the edit key opens the duration for typing")
        QTest.keyClick(line, Qt.Key_A, Qt.ControlModifier)
        QTest.keyClicks(line, "0.25")
        QTest.keyClick(line, Qt.Key_Return)
        self.process_events()
        self.assertEqual(editor.recipe().trials[1].steps[0].duration_seconds, 0.25)
        for expected in (
            editor._add_step,
            editor._remove_step,
            editor._duplicate_step,
            editor._step_up,
            editor._step_down,
            editor._pulse_train,
            editor._shutdown_view,
            editor._schedule,
        ):
            QTest.keyClick(window, Qt.Key_Tab)
            self.assertIs(QApplication.focusWidget(), expected)
        self.assertEqual(editor.problems(), [])
        window.start_button.setFocus()
        QTest.keyClick(window.start_button, Qt.Key_Space)
        self.wait_until(lambda: window.controller.is_running)
        self.assertEqual(window.tabs.currentIndex(), 1)
        window.controller.abort_and_wait()
        self.process_events(0.2)
        window.close()

    def test_start_is_refused_while_the_recipe_has_problems(self) -> None:
        window = self.window()
        window.editor._steps.setData(window.editor._steps.index(0, 4), "999")

        window.start_run()

        self.assertFalse(window.start_button.isEnabled())
        self.assertEqual(len(window.warnings), 1)
        self.assertIn("lab.toml limit of 400", window.warnings[0][1])
        self.assertEqual(self.rig.labjack_opens, 0)

    def test_stale_lock_is_reported_and_removed_on_request(self) -> None:
        RunLock(self.runs, self.runs / "20260917-101500-old").acquire()
        window = self.window()

        window.check_stale_lock()

        self.assertFalse((self.runs / LOCK_FILE_NAME).exists())
        self.assertIn("Lock file removed", window.statusBar().currentMessage())

    def test_new_recipe_asks_about_unsaved_edits(self) -> None:
        window = self.window(answer=QMessageBox.Cancel)
        window.editor._name.setText("draft")
        self.assertTrue(window.isWindowModified())
        self.assertIn("unsaved recipe", window.windowTitle())

        window.new_recipe()
        self.assertEqual(window.editor.recipe().name, "draft", "Cancel keeps the edits")

        window._ask = lambda *_arguments, **_options: QMessageBox.Discard
        window.new_recipe()
        self.assertEqual(window.editor.recipe().name, "")
        self.assertFalse(window.isWindowModified())

    def test_save_clears_the_modified_state_and_restores_on_next_start(self) -> None:
        path = Path(self.temporary.name, "pulses.json")
        window = self.window()
        window.editor.set_recipe(make_recipe(0.5))
        window.save_recipe_file(path)
        self.assertFalse(window.isWindowModified())
        self.assertEqual(window.windowTitle(), "pulses.json[*] - sniffler")

        again = self.window()
        self.assertEqual(again.editor.recipe(), make_recipe(0.5))
        self.assertFalse(again.isWindowModified())
        again.editor._name.setText("changed")
        self.assertTrue(again.isWindowModified())
        again.apply_device_limits({"mfc-500": (500.0, "SCCM")})
        self.assertTrue(again.isWindowModified(), "device limits do not change the dirty state")
        window.editor.set_recipe(None)
        window.apply_device_limits({"mfc-500": (500.0, "SCCM")})

    def test_device_limits_apply_to_the_editor_and_the_rig_panel(self) -> None:
        window = self.window()

        window.apply_device_limits(
            {"mfc-500": (500.0, "SCCM"), "mfc-2000": "Cannot open the Alicat MFC"}
        )

        self.assertEqual(window.editor.rig.mfcs["mfc-500"].full_scale, 500.0)
        self.assertIsNone(window.editor.rig.mfcs["mfc-2000"].full_scale)
        self.assertEqual(len(window.warnings), 1)
        self.assertIn("mfc-2000: Cannot open", window.warnings[0][1])


if __name__ == "__main__":
    unittest.main()
