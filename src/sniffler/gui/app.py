"""The sniffler window: author a recipe, run it, watch it, and keep the rig safe."""

import argparse
import asyncio
import random
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sniffler import hardware
from sniffler.config import ConfigError, Settings, load_settings
from sniffler.executor import Phase, Sample, Status
from sniffler.gui.recipe_editor import RecipeEditor
from sniffler.gui.run_view import RunController, RunView
from sniffler.hardware import DeviceError
from sniffler.recipe import (
    Recipe,
    RecipeError,
    RigMap,
    load_recipe,
    resolve_trial_order,
    resolved_duration_seconds,
    rig_map_from_settings,
    save_recipe,
)
from sniffler.runlog import LOCK_FILE_NAME, active_run

RECIPE_FILTER = "Recipe files (*.json)"


class RigPanel(QWidget):
    """The name to channel map from lab.toml, read-only."""

    def __init__(self, rig: RigMap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Name", "Kind", "Hardware", "Limits"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.read_limits = QPushButton("Read device limits")
        self.read_limits.setToolTip(
            "Read the full scale of every MFC. This command changes no output."
        )
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Recipes use these names. The map comes from lab.toml and cannot be changed here."
            )
        )
        layout.addWidget(self._table, stretch=1)
        layout.addWidget(self.read_limits, alignment=Qt.AlignLeft)
        self.show_rig(rig)

    def show_rig(self, rig: RigMap) -> None:
        rows: list[tuple[str, str, str, str]] = []
        for name, channel in rig.valves.items():
            line = hardware.digital_channel_name(channel)
            rows.append((name, "valve", f"channel {channel} ({line})", "open or closed"))
        for name, mfc in rig.mfcs.items():
            limits = [f"minimum {mfc.minimum_flow:g}"]
            if mfc.maximum_flow is not None:
                limits.append(f"lab.toml maximum {mfc.maximum_flow:g}")
            limits.append(
                "full scale not read yet"
                if mfc.full_scale is None
                else f"device full scale {mfc.full_scale:g}"
            )
            if mfc.allow_negative_flow:
                limits.append("negative flow allowed")
            rows.append(
                (
                    name,
                    "MFC",
                    f"{mfc.port}, unit {mfc.unit}",
                    ", ".join(limits) + f" {mfc.flow_unit}",
                )
            )
        self._table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self._table.setItem(row, column, QTableWidgetItem(value))


class MainWindow(QMainWindow):
    """Recipe tab, Run tab, and the rig map."""

    def __init__(
        self,
        settings: Settings,
        rig: RigMap,
        *,
        open_labjack: Callable[..., Any] = hardware.open_labjack,
        open_alicat: Callable[..., Any] = hardware.open_alicat,
        read_full_scale: Callable[..., Any] = hardware.alicat_full_scale,
    ) -> None:
        super().__init__()
        self.setWindowTitle("sniffler")
        self.resize(1280, 860)
        self._settings = settings
        self._rig = rig
        self._factories = {"open_labjack": open_labjack, "open_alicat": open_alicat}
        self._read_full_scale = read_full_scale
        self._recipe_path: Path | None = None
        self._running_recipe: Recipe | None = None
        self._ask = QMessageBox.question
        self._tell = QMessageBox.warning

        self.editor = RecipeEditor(rig)
        self.run_view = RunView(rig)
        self.rig_panel = RigPanel(rig)
        self.controller = RunController(self)

        self._recipe_summary = QLabel("—")
        self._recipe_summary.setWordWrap(True)
        self._seed_label = QLabel("—")
        self._notes = QPlainTextEdit()
        self._notes.setPlaceholderText("Operator notes for this run (stored in the manifest)")
        self._notes.setMaximumHeight(70)
        self.start_button = QPushButton("Start run")
        self.stop_button = QPushButton("Stop after this trial")
        self.abort_button = QPushButton("Abort now")
        self.abort_button.setStyleSheet("font-weight: bold; color: #9b1c1c;")
        self.abort_button.setToolTip(
            "Stop now and force the safe state: all valves closed, every MFC setpoint zero."
        )
        self.stop_button.setToolTip("Finish the current trial, then apply the shutdown state.")

        self._build_layout()
        self._build_menu()
        self._connect()
        self._refresh_summary()
        self._set_running(False)
        self.statusBar().showMessage(f"Runs are written to {settings.runs_directory}")
        self._tick = QTimer(self)
        self._tick.setInterval(100)
        self._tick.timeout.connect(self._on_tick)
        QTimer.singleShot(0, self.check_stale_lock)

    # Layout ------------------------------------------------------------

    def _build_layout(self) -> None:
        controls = QGroupBox("Run controls")
        form = QFormLayout()
        form.addRow("Recipe", self._recipe_summary)
        form.addRow("Seed", self._seed_label)
        form.addRow("Operator notes", self._notes)
        buttons = QHBoxLayout()
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.abort_button)
        buttons.addStretch(1)
        controls_layout = QVBoxLayout(controls)
        controls_layout.addLayout(form)
        controls_layout.addLayout(buttons)

        run_tab = QWidget()
        run_layout = QVBoxLayout(run_tab)
        run_layout.addWidget(controls)
        run_layout.addWidget(self.run_view, stretch=1)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.editor, "Recipe")
        self.tabs.addTab(run_tab, "Run")
        self.tabs.addTab(self.rig_panel, "Rig map")
        self.setCentralWidget(self.tabs)

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&File")
        for text, shortcut, handler in (
            ("&New recipe", "Ctrl+N", self.new_recipe),
            ("&Open recipe…", "Ctrl+O", self.open_recipe),
            ("&Save recipe", "Ctrl+S", self.save_recipe),
            ("Save recipe &as…", "Ctrl+Shift+S", self.save_recipe_as),
        ):
            action = QAction(text, self)
            action.setShortcut(shortcut)
            action.triggered.connect(handler)
            menu.addAction(action)
        menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

    def _connect(self) -> None:
        self.editor.changed.connect(self._refresh_summary)
        self.start_button.clicked.connect(self.start_run)
        self.stop_button.clicked.connect(self.controller.request_stop)
        self.abort_button.clicked.connect(self.controller.abort)
        self.controller.status_changed.connect(self._on_status)
        self.controller.sample_received.connect(self._on_sample)
        self.controller.finished.connect(self._on_finished)
        self.rig_panel.read_limits.clicked.connect(self.read_device_limits)

    # Recipe files ------------------------------------------------------

    def new_recipe(self) -> None:
        self.editor.set_recipe(None)
        self._recipe_path = None

    def open_recipe(self) -> None:
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
        self._recipe_path = path
        self.statusBar().showMessage(f"Opened {path}")

    def save_recipe(self) -> None:
        if self._recipe_path is None:
            self.save_recipe_as()
        else:
            self.save_recipe_file(self._recipe_path)

    def save_recipe_as(self) -> None:
        path_text, _filter = QFileDialog.getSaveFileName(self, "Save recipe", "", RECIPE_FILTER)
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
        self._recipe_path = path
        self.statusBar().showMessage(f"Saved {path}")

    # Rig ---------------------------------------------------------------

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

        worker = threading.Thread(target=work, name="sniffler-limits", daemon=True)
        worker.start()

        def finish() -> None:
            if worker.is_alive():
                QTimer.singleShot(100, finish)
                return
            self.rig_panel.read_limits.setEnabled(True)
            self.apply_device_limits(results)

        QTimer.singleShot(100, finish)

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
            self.statusBar().showMessage("Device full scales read and applied to the editor.")

    def set_rig(self, rig: RigMap) -> None:
        self._rig = rig
        self.editor.set_rig(rig)
        self.rig_panel.show_rig(rig)

    # Runs --------------------------------------------------------------

    def _refresh_summary(self) -> None:
        recipe = self.editor.recipe()
        problems = self.editor.problems()
        trials = ", ".join(f"{name} x{count}" for name, count in recipe.schedule.counts.items())
        try:
            planned = resolved_duration_seconds(
                recipe, resolve_trial_order(recipe.schedule, recipe.schedule.seed or 0)
            )
            duration = f"{planned:.1f} s planned"
        except (RecipeError, KeyError):
            duration = "duration unknown until the recipe is valid"
        name = recipe.name or "(no name)"
        self._recipe_summary.setText(f"{name}: {trials or 'no trials'}; {duration}")
        seed = recipe.schedule.seed
        self._seed_label.setText(
            "a new random seed for each run, recorded in the manifest"
            if seed is None
            else f"{seed} (from the recipe)"
        )
        if not self.controller.is_running:
            self.start_button.setEnabled(not problems)
            self.start_button.setToolTip(
                "\n".join(problems) if problems else "Validate the recipe and start the run."
            )

    def start_run(self) -> None:
        problems = self.editor.problems()
        if problems:
            self._tell(self, "The recipe is not valid", "\n".join(problems))
            return
        if self.controller.is_running:
            return
        recipe = self.editor.recipe()
        runs_directory = self._settings.runs_directory
        directory = active_run(runs_directory)
        if directory is not None:
            self._tell(
                self,
                "A run is active",
                f"{directory}\nWait for it to end, or remove {runs_directory / LOCK_FILE_NAME} "
                "if that run ended abnormally.",
            )
            return
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
            **self._factories,
        )
        self._tick.start()

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running and not self.editor.problems())
        self.stop_button.setEnabled(running)
        self.abort_button.setEnabled(running)

    def _on_status(self, status: Status) -> None:
        self.run_view.show_status(status, self.controller.elapsed_seconds(), self._running_recipe)
        if status.stop_requested:
            self.stop_button.setEnabled(False)

    def _on_sample(self, sample: Sample) -> None:
        self.run_view.plot.add_sample(sample)
        self.run_view.status_panel.show_sample(sample)

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
            f"The lock file names {directory}.\nNo run is active in this window. "
            "Remove the lock file so that new runs can start?",
        )
        if answer == QMessageBox.Yes:
            (runs_directory / LOCK_FILE_NAME).unlink(missing_ok=True)
            self.statusBar().showMessage("Lock file removed.")

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.controller.is_running:
            event.accept()
            return
        answer = self._ask(
            self,
            "A run is active",
            "Abort the run now and close? The safe state is applied: "
            "all valves closed, every MFC setpoint zero.",
        )
        if answer != QMessageBox.Yes:
            event.ignore()
            return
        status = self.controller.abort_and_wait()
        self._tick.stop()
        if status is not None and status.phase == Phase.FAILED:
            self._tell(self, "The safe state might not be complete", status.message)
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
    arguments = parser.parse_args(argv)
    if argv is None:
        restarted_status = hardware.relaunch_with_homebrew_exodriver("sniffler.gui.app")
        if restarted_status is not None:
            return restarted_status
    application = QApplication.instance() or QApplication(sys.argv[:1])
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
