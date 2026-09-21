"""The sniffler window: author a recipe, run it, watch it, and keep the rig safe."""

import argparse
import asyncio
import random
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from sniffler import hardware
from sniffler.config import ConfigError, Settings, load_settings
from sniffler.executor import Event, Phase, Sample, Status, apply_safe_state
from sniffler.gui import theme
from sniffler.gui.icons import icon
from sniffler.gui.recipe_editor import RecipeEditor
from sniffler.gui.rig_panel import RigPanel, pulse_text
from sniffler.gui.run_view import RunController, RunView
from sniffler.hardware import DeviceError
from sniffler.recipe import (
    Recipe,
    RecipeError,
    RigMap,
    load_recipe,
    rig_map_from_settings,
    save_recipe,
    start_pulse_problem,
)
from sniffler.runlog import LOCK_FILE_NAME, active_run

RECIPE_FILTER = "Recipe files (*.json)"


class MainWindow(QMainWindow):
    """Recipe tab, Run tab, and Config, the read-only rig map."""

    def __init__(
        self,
        settings: Settings,
        rig: RigMap,
        *,
        open_labjack: Callable[..., Any] = hardware.open_labjack,
        open_alicat: Callable[..., Any] = hardware.open_alicat,
        read_full_scale: Callable[..., Any] = hardware.alicat_full_scale,
        store: QSettings | None = None,
        reduced_motion: bool | None = None,
        demo: bool = False,
    ) -> None:
        super().__init__()
        self.resize(1280, 860)
        self.demo = demo
        self.setWindowIcon(theme.window_icon())
        self._settings = settings
        self._rig = rig
        self._factories = {"open_labjack": open_labjack, "open_alicat": open_alicat}
        self._read_full_scale = read_full_scale
        self._store = store if store is not None else QSettings("sniffler", "sniffler-gui")
        self._recipe_path: Path | None = None
        self._running_recipe: Recipe | None = None
        self._shutting_down = False
        self._dirty = False
        self._ask = QMessageBox.question
        self._tell = QMessageBox.warning

        self.editor = RecipeEditor(rig)
        self.run_view = RunView(rig, reduced_motion=reduced_motion)
        self.rig_panel = RigPanel(rig, settings.runs_directory)
        self.controller = RunController(self)

        self._recipe_summary = QLabel("—")
        self._recipe_summary.setWordWrap(True)
        self._seed_label = QLabel("—")
        self._notes = QPlainTextEdit()
        self._notes.setPlaceholderText("Saved with the run")
        self._notes.setMaximumHeight(2 * theme.ROW_PX)
        self._notes.setAccessibleName("Run notes")
        self.start_button = QPushButton(icon("start", theme.PANEL), "Start run")
        self.start_button.setObjectName("primary")
        self.stop_button = QPushButton(icon("stop"), "Stop after this trial")
        self.stop_button.setObjectName("consequential")
        self.abort_button = QPushButton("Abort now")
        self.abort_button.setObjectName("consequential")
        self.abort_button.setToolTip("Stop now: close all valves and set every flow to zero.")
        self.stop_button.setToolTip("Finish the current trial, then apply the end state.")
        self.trigger_box = QCheckBox("Wait for TTL")
        trigger = rig.trigger
        if trigger is None:
            self.trigger_box.hide()
        else:
            line = hardware.digital_channel_name(trigger.channel)
            self.trigger_box.setToolTip(
                f"Hold the recipe end state until the first TTL pulse on {line}, "
                "then start the trials. The line is set in lab.toml; see Config."
            )
            self.trigger_box.setChecked(self._store.value("wait_for_trigger", False, type=bool))
            self.trigger_box.toggled.connect(
                lambda checked: self._store.setValue("wait_for_trigger", checked)
            )
        self.ttl_box = QCheckBox("Send TTL at start")
        output = rig.ttl_output
        if output is None:
            self.ttl_box.hide()
        else:
            line = hardware.digital_channel_name(output.channel)
            if output.holds_high:
                self.ttl_box.setText("TTL high during the run")
                hold = "until the end state"
            else:
                self.ttl_box.setText(f"Send TTL at start ({pulse_text(output.pulse_seconds)})")
                hold = f"for {pulse_text(output.pulse_seconds)}"
            self.ttl_box.setToolTip(
                f"Raise {line} with the first step of the first trial and hold it {hold}. "
                "The line, the mode, and the width are set in lab.toml; see Config."
            )
            self.ttl_box.setChecked(self._store.value("send_ttl", False, type=bool))
            self.ttl_box.toggled.connect(lambda checked: self._store.setValue("send_ttl", checked))
        self.start_now_button = QPushButton("Start now")
        self.start_now_button.setObjectName("consequential")
        self.start_now_button.setToolTip("End the wait for the trigger and start the trials now.")
        self.start_now_button.hide()
        self.shutdown_button = QPushButton("Shut down the rig")
        self.shutdown_button.setObjectName("tabCorner")
        self.shutdown_button.setToolTip(
            "Close all valves and set every flow to zero. During a run, use Abort now."
        )

        self._build_layout()
        self._build_menu()
        self._connect()
        self._refresh_summary()
        self._set_running(False)
        self._refresh_title()
        self.statusBar().showMessage(f"Runs are written to {settings.runs_directory}")
        if demo:
            marker = QLabel("Demo: fake devices, no hardware")
            marker.setFont(theme.font(bold=True))
            self.statusBar().addPermanentWidget(marker)
        self._tick = QTimer(self)
        self._tick.setInterval(100)
        self._tick.timeout.connect(self._on_tick)
        self._restore_session()
        QTimer.singleShot(0, self.check_stale_lock)

    # Layout ------------------------------------------------------------

    def _build_layout(self) -> None:
        form = QFormLayout()
        form.setHorizontalSpacing(theme.SECTION_GAP)
        form.setVerticalSpacing(theme.GAP // 2)
        form.addRow("Recipe", self._recipe_summary)
        form.addRow("Seed", self._seed_label)
        form.addRow("Notes", self._notes)
        ttl = QHBoxLayout()
        ttl.setSpacing(theme.SECTION_GAP)
        ttl.addWidget(self.trigger_box)
        ttl.addWidget(self.ttl_box)
        ttl.addStretch(1)
        form.addRow(ttl)
        buttons = QHBoxLayout()
        buttons.setSpacing(theme.GAP)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.start_now_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.abort_button)
        buttons.addStretch(1)

        run_tab = QWidget()
        run_layout = QVBoxLayout(run_tab)
        run_layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.MARGIN)
        run_layout.setSpacing(theme.SECTION_GAP)
        run_layout.addLayout(form)
        run_layout.addLayout(buttons)
        run_layout.addWidget(self.run_view, stretch=1)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.editor, "Recipe")
        self.tabs.addTab(run_tab, "Run")
        self.tabs.addTab(self.rig_panel, "Config")
        # The rig control shares the tab bar row: no row of its own, in reach from every tab.
        # A corner widget is clamped to the bar's height, so the button has its own compact
        # style. The style draws the tab base line above the bar's bottom edge; the margin
        # lifts the button so that its frame ends on that line.
        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        overlap = self.style().pixelMetric(QStyle.PM_TabBarBaseOverlap, None, self.tabs.tabBar())
        corner_layout.setContentsMargins(0, 0, 0, overlap)
        corner_layout.addWidget(self.shutdown_button)
        self.tabs.setCornerWidget(corner, Qt.TopRightCorner)
        self.setCentralWidget(self.tabs)

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&File")
        toolbar = QToolBar("File")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(theme.ICON_PX, theme.ICON_PX))
        self.addToolBar(toolbar)
        for text, name, shortcut, handler, in_toolbar in (
            ("&New", "new", "Ctrl+N", self.new_recipe, True),
            ("&Open…", "open", "Ctrl+O", self.open_recipe, True),
            ("&Save", "save", "Ctrl+S", self.save_recipe, True),
            ("Save &as…", None, "Ctrl+Shift+S", self.save_recipe_as, False),
        ):
            action = QAction(text, self)
            if name is not None:
                action.setIcon(icon(name))
            action.setShortcut(shortcut)
            action.setToolTip(f"{text.replace('&', '').rstrip('…')} recipe ({shortcut})")
            action.triggered.connect(handler)
            menu.addAction(action)
            if in_toolbar:
                toolbar.addAction(action)
        menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

    def _connect(self) -> None:
        self.editor.changed.connect(self._on_recipe_edited)
        self.start_button.clicked.connect(self.start_run)
        self.stop_button.clicked.connect(self.controller.request_stop)
        self.abort_button.clicked.connect(self.controller.abort)
        self.start_now_button.clicked.connect(self.controller.start_now)
        self.controller.status_changed.connect(self._on_status)
        self.controller.sample_received.connect(self._on_sample)
        self.controller.event_received.connect(self._on_event)
        self.controller.finished.connect(self._on_finished)
        self.rig_panel.read_limits.clicked.connect(self.read_device_limits)
        self.shutdown_button.clicked.connect(self.shut_down)

    # Recipe files ------------------------------------------------------

    def _on_recipe_edited(self) -> None:
        self._dirty = True
        self._refresh_title()
        self._refresh_summary()

    def _mark_clean(self, path: Path | None) -> None:
        self._recipe_path = path
        self._dirty = False
        self._refresh_title()
        if path is not None:
            self._store.setValue("last_recipe", str(path))

    @property
    def recipe_path(self) -> Path | None:
        return self._recipe_path

    def show_recipe(self, recipe: Recipe) -> None:
        """Show a recipe that has no file yet, as a clean start."""
        self.editor.set_recipe(recipe)
        self._mark_clean(None)

    def _refresh_title(self) -> None:
        name = self._recipe_path.name if self._recipe_path is not None else "unsaved recipe"
        program = "sniffler demo" if self.demo else "sniffler"
        self.setWindowTitle(f"{name}[*] - {program}")
        self.setWindowModified(self._dirty)

    def _restore_session(self) -> None:
        geometry = self._store.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        last = self._store.value("last_recipe")
        if last and Path(str(last)).exists():
            self.load_recipe_file(Path(str(last)))

    def offer_to_save(self) -> bool:
        """Ask about unsaved edits. Return False when the operator cancels."""
        if not self._dirty:
            return True
        name = self._recipe_path.name if self._recipe_path is not None else "this recipe"
        answer = self._ask(
            self,
            "Unsaved changes",
            f"Save the changes to {name} first?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            self.save_recipe()
            return not self._dirty
        return True

    def new_recipe(self) -> None:
        if not self.offer_to_save():
            return
        self.editor.set_recipe(None)
        self._mark_clean(None)

    def open_recipe(self) -> None:
        if not self.offer_to_save():
            return
        path_text, _filter = QFileDialog.getOpenFileName(self, "Open recipe", "", RECIPE_FILTER)
        if path_text:
            self.load_recipe_file(Path(path_text))

    def load_recipe_file(self, path: Path) -> None:
        try:
            recipe = load_recipe(path)
        except RecipeError as error:
            self._tell(self, "Cannot open the recipe", str(error))
            return
        self.editor.set_recipe(recipe)
        self._mark_clean(path)
        self.statusBar().showMessage(f"Opened {path}")

    def save_recipe(self) -> None:
        if self._recipe_path is None:
            self.save_recipe_as()
        else:
            self.save_recipe_file(self._recipe_path)

    def save_recipe_as(self) -> None:
        start = str(
            self._recipe_path or Path(self.editor.recipe().name or "recipe").with_suffix(".json")
        )
        path_text, _filter = QFileDialog.getSaveFileName(self, "Save recipe", start, RECIPE_FILTER)
        if path_text:
            path = Path(path_text)
            if path.suffix != ".json":
                path = path.with_suffix(".json")
            self.save_recipe_file(path)

    def save_recipe_file(self, path: Path) -> None:
        try:
            save_recipe(path, self.editor.recipe())
        except RecipeError as error:
            self._tell(self, "Cannot save the recipe", str(error))
            return
        self._mark_clean(path)
        self.statusBar().showMessage(f"Saved {path}")

    # Rig ---------------------------------------------------------------

    def _run_off_gui_thread(
        self, name: str, work: Callable[[], None], finish: Callable[[], None]
    ) -> None:
        """Run ``work`` on its own thread, then ``finish`` on the GUI thread."""
        worker = threading.Thread(target=work, name=name, daemon=True)
        worker.start()

        def poll() -> None:
            if worker.is_alive():
                QTimer.singleShot(100, poll)
                return
            finish()

        QTimer.singleShot(100, poll)

    def read_device_limits(self) -> None:
        """Read every MFC full scale off the GUI thread and apply it to the editor."""
        if not self._rig.mfcs:
            self._tell(self, "Read device limits", "lab.toml has no MFC with a port.")
            return
        self.rig_panel.read_limits.setEnabled(False)
        results: dict[str, tuple[float, str] | str] = {}

        def work() -> None:
            for name, mfc in self._rig.mfcs.items():
                try:
                    results[name] = asyncio.run(
                        self._read_full_scale(
                            mfc.port, mfc.unit, mfc.baud_rate, mfc.timeout_seconds
                        )
                    )
                except DeviceError as error:
                    results[name] = str(error)

        def finish() -> None:
            self.rig_panel.read_limits.setEnabled(True)
            self.apply_device_limits(results)

        self._run_off_gui_thread("sniffler-limits", work, finish)

    def apply_device_limits(self, results: dict[str, tuple[float, str] | str]) -> None:
        rig = self._rig
        problems = []
        for name, result in results.items():
            if isinstance(result, str):
                problems.append(f"{name}: {result}")
            else:
                rig = rig.with_full_scale(name, *result)
        self.set_rig(rig)
        if problems:
            self._tell(self, "Read device limits", "\n".join(problems))
        else:
            self.statusBar().showMessage("Device maximums read and applied to the editor.")

    def set_rig(self, rig: RigMap) -> None:
        """Apply a new rig map. Device limits do not count as recipe edits."""
        dirty = self._dirty
        self._rig = rig
        self.editor.set_rig(rig)
        self.rig_panel.show_rig(rig)
        self._dirty = dirty
        self._refresh_title()

    def shut_down(self) -> None:
        """Close every valve and set every flow to zero, off the GUI thread."""
        if self.controller.is_running or self._shutting_down or self._refuse_active_run():
            return
        self._set_shutting_down(True)
        self.statusBar().showMessage("Closing all valves, every flow to zero.")
        problems: list[str] = []

        def work() -> None:
            problems.extend(apply_safe_state(self._rig, **self._factories))

        def finish() -> None:
            self._set_shutting_down(False)
            if problems:
                self._tell(self, "Some valves or flows might still be on", "\n".join(problems))
            else:
                self.statusBar().showMessage("All valves closed, every flow zero.")

        self._run_off_gui_thread("sniffler-shutdown", work, finish)

    def _set_shutting_down(self, active: bool) -> None:
        """A shutdown holds the device ports as a run does; Start and the reads wait."""
        self._shutting_down = active
        self.start_button.setEnabled(not active and not self.editor.problems())
        self.rig_panel.read_limits.setEnabled(not active)
        self.shutdown_button.setEnabled(not active)

    # Runs --------------------------------------------------------------

    def _refresh_summary(self) -> None:
        recipe = self.editor.recipe()
        problems = self.editor.problems()
        trials = ", ".join(f"{name} x{count}" for name, count in recipe.schedule.counts.items())
        if problems:
            duration = "duration unknown until the recipe is valid"
        else:
            planned = sum(
                recipe.trial(name).duration_seconds * count
                for name, count in recipe.schedule.counts.items()
            )
            duration = f"{planned:.1f} s planned"
        name = recipe.name or "(no name)"
        self._recipe_summary.setText(f"{name}: {trials or 'no trials'}; {duration}")
        if not self.controller.is_running:
            self.run_view.preview(recipe)
        seed = recipe.schedule.seed
        self._seed_label.setText(
            "random, saved with the run" if seed is None else f"{seed} (from the recipe)"
        )
        if not self.controller.is_running and not self._shutting_down:
            self.start_button.setEnabled(not problems)
            self.start_button.setToolTip(
                "\n".join(problems) if problems else "Validate the recipe and start the run."
            )

    def _refuse_active_run(self) -> bool:
        """Report a run that the runs directory marks active. True when there is one."""
        runs_directory = self._settings.runs_directory
        directory = active_run(runs_directory)
        if directory is None:
            return False
        self._tell(
            self,
            "A run is active",
            f"{directory}\nWait for it to end, or remove the file "
            f"{runs_directory / LOCK_FILE_NAME} if that run ended abnormally.",
        )
        return True

    def start_run(self) -> None:
        problems = self.editor.problems()
        if problems:
            self._tell(self, "The recipe is not valid", "\n".join(problems))
            return
        if self.controller.is_running or self._shutting_down or self._refuse_active_run():
            return
        recipe = self.editor.recipe()
        output = self._rig.ttl_output
        if self.send_ttl() and output is not None and not output.holds_high:
            problem = start_pulse_problem(recipe, output.pulse_seconds)
            if problem is not None:
                self._tell(self, "Cannot send the TTL pulse", problem)
                return
        runs_directory = self._settings.runs_directory
        seed = recipe.schedule.seed
        if seed is None:
            seed = random.SystemRandom().randrange(2**31)
        self._running_recipe = recipe
        self.run_view.prepare(recipe)
        self._seed_label.setText(f"{seed} (this run)")
        self._set_running(True)
        self.tabs.setCurrentIndex(1)
        self.controller.start(
            recipe,
            self._rig,
            seed,
            runs_directory,
            self._notes.toPlainText(),
            wait_for_trigger=self._rig.trigger is not None and self.trigger_box.isChecked(),
            send_ttl=self.send_ttl(),
            **self._factories,
        )
        self._tick.start()

    def send_ttl(self) -> bool:
        """Whether this run raises the TTL output with the first trial."""
        return self._rig.ttl_output is not None and self.ttl_box.isChecked()

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running and not self.editor.problems())
        self.trigger_box.setEnabled(not running)
        self.ttl_box.setEnabled(not running)
        self.rig_panel.read_limits.setEnabled(not running)  # the run holds the MFC ports
        self.shutdown_button.setEnabled(not running)  # Abort now ends a run
        self.start_now_button.setVisible(False)
        self.stop_button.setEnabled(running)
        self.abort_button.setEnabled(running)

    def _on_status(self, status: Status) -> None:
        self.run_view.show_status(status, self.controller.elapsed_seconds(), self._running_recipe)
        self.start_now_button.setVisible(status.phase == Phase.WAITING)
        if status.stop_requested:
            self.stop_button.setEnabled(False)

    def _on_sample(self, sample: Sample) -> None:
        self.run_view.plot.add_sample(sample)

    def _on_event(self, event: Event) -> None:
        self.run_view.show_event(event, self.controller.elapsed_seconds())

    def _on_tick(self) -> None:
        executor = self.controller.executor
        if executor is not None:
            self.run_view.tick(executor.status, self.controller.elapsed_seconds())

    def _on_finished(self, status: Status) -> None:
        self._tick.stop()
        self._set_running(False)
        self._refresh_summary()
        self.statusBar().showMessage(status.message)
        if status.phase == Phase.FAILED:
            self._tell(self, "The run did not finish normally", status.message)

    # Lock and close ----------------------------------------------------

    def check_stale_lock(self) -> None:
        runs_directory = self._settings.runs_directory
        directory = active_run(runs_directory)
        if directory is None or self.controller.is_running:
            return
        answer = self._ask(
            self,
            "A previous run did not end normally",
            f"A run is still marked active: {directory}.\nNo run is active in this window. "
            f"Clear the mark so that new runs can start? This removes the file "
            f"{runs_directory / LOCK_FILE_NAME}.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            (runs_directory / LOCK_FILE_NAME).unlink(missing_ok=True)
            self.statusBar().showMessage("Run mark cleared.")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.controller.is_running:
            answer = self._ask(
                self,
                "A run is active",
                "Abort the run now and close? All valves close and every flow goes to zero.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.controller.abort_and_wait()  # a FAILED end reports through _on_finished
            self._tick.stop()
            if self.controller.is_running:
                self._tell(
                    self,
                    "Some valves or flows might still be on",
                    "The run did not stop in time. Check every valve and flow on the rig.",
                )
        if self._shutting_down:
            # The commands that were sent must return and report before the window goes.
            self._tell(
                self, "A shutdown is in progress", "Wait for it to end, then close the window."
            )
            event.ignore()
            return
        if not self.offer_to_save():
            event.ignore()
            return
        self._store.setValue("geometry", self.saveGeometry())
        event.accept()


def main(argv: Sequence[str] | None = None) -> int:
    """Start the window."""
    parser = argparse.ArgumentParser(
        prog="sniffler-gui", description="Build, run, and watch odor presentation experiments."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("lab.toml"),
        help="Configuration file. Default: lab.toml.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Open the window on fake devices with a sample recipe. No hardware is used.",
    )
    arguments = parser.parse_args(argv)
    if arguments.demo:
        from sniffler.gui.demo import demo_window

        application = QApplication.instance() or QApplication(sys.argv[:1])
        theme.apply(application)
        window = demo_window()
        window.show()
        return application.exec()
    if argv is None:
        restarted_status = hardware.relaunch_with_homebrew_exodriver("sniffler.gui.app")
        if restarted_status is not None:
            return restarted_status
    application = QApplication.instance() or QApplication(sys.argv[:1])
    theme.apply(application)
    try:
        settings = load_settings(arguments.config)
        rig = rig_map_from_settings(settings)
    except ConfigError as error:
        QMessageBox.critical(None, "Configuration error", str(error))
        return 2
    window = MainWindow(settings, rig)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
