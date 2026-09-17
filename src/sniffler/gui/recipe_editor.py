"""The recipe editor: trials, steps, schedule, and the end state."""

from itertools import pairwise

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sniffler.gui import theme
from sniffler.gui.icons import icon
from sniffler.gui.pulse_dialog import PulseTrainDialog
from sniffler.gui.step_table import StepDelegate, StepRow, StepTableModel
from sniffler.recipe import ORDERINGS, Recipe, RigMap, Schedule, Step, Trial, recipe_problems

# The recipe file keeps the ordering keys; the window shows them in plain words.
ORDERING_LABELS = {"block-randomized": "shuffled in blocks", "as-listed": "as listed"}


class _TrialData:
    def __init__(self, name: str, rows: list[StepRow], count: int = 1) -> None:
        self.name = name
        self.rows = rows
        self.count = count


def _step_view(model: StepTableModel) -> QTableView:
    view = QTableView()
    view.setModel(model)
    view.setItemDelegate(StepDelegate(view))
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(QAbstractItemView.SingleSelection)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    view.horizontalHeader().setMinimumSectionSize(7 * theme.UNIT)
    view.verticalHeader().setDefaultSectionSize(theme.ROW_PX)
    return view


def _tool(name: str, text: str) -> QToolButton:
    """An icon-only button with the command as tooltip and accessible name."""
    button = QToolButton()
    button.setIcon(icon(name))
    button.setIconSize(QSize(theme.ICON_PX, theme.ICON_PX))
    button.setToolTip(text)
    button.setAccessibleName(text)
    button.setAutoRaise(True)
    button.setFocusPolicy(Qt.StrongFocus)
    return button


def _section(title: str) -> QGroupBox:
    box = QGroupBox(title)
    box.setFont(theme.font(bold=True))
    return box


def _button_row(*buttons: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(theme.GAP // 2)
    for button in buttons:
        button.setFont(theme.font())
        row.addWidget(button)
    row.addStretch(1)
    return row


class RecipeEditor(QWidget):
    """Author a complete recipe without editing a file."""

    changed = Signal()

    def __init__(self, rig: RigMap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rig = rig
        self._trials: list[_TrialData] = []
        self._current: int | None = None

        self._name = QLineEdit()
        self._name.setAccessibleName("Recipe name")
        self._notes = QPlainTextEdit()
        self._notes.setAccessibleName("Recipe notes")
        self._notes.setMaximumHeight(2 * theme.ROW_PX)

        self._trial_list = QListWidget()
        self._trial_list.setAccessibleName("Trials")
        self._add_trial = _tool("add", "Add trial")
        self._remove_trial = _tool("remove", "Remove trial")
        self._rename_trial = QPushButton("Rename")
        self._rename_trial.setToolTip("Rename the selected trial (double-click does the same)")
        self._duplicate_trial = _tool("duplicate", "Duplicate trial")

        self._steps = StepTableModel(rig, with_duration=True, parent=self)
        self._step_view = _step_view(self._steps)
        self._step_view.setAccessibleName("Steps")
        self._add_step = _tool("add", "Add step")
        self._remove_step = _tool("remove", "Remove step")
        self._duplicate_step = _tool("duplicate", "Duplicate step")
        self._step_up = _tool("move-up", "Move step up")
        self._step_down = _tool("move-down", "Move step down")
        self._pulse_train = QPushButton(icon("pulse-train"), "Pulse train…")
        self._pulse_train.setToolTip("Insert a train of pulses on one valve as editable steps")

        self._schedule = QTableWidget(0, 2)
        self._schedule.setHorizontalHeaderLabels(["Trial", "Count"])
        self._schedule.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._schedule.verticalHeader().setVisible(False)
        self._ordering = QComboBox()
        for value in ORDERINGS:
            self._ordering.addItem(ORDERING_LABELS.get(value, value), value)
        self._seed = QLineEdit()
        self._seed.setValidator(QIntValidator(0, 2_000_000_000, self._seed))
        self._seed.setPlaceholderText("random when empty")
        self._seed.setToolTip(
            "Empty: a new random trial order for each run. A number: the same order every run."
        )
        self._confirm = QMessageBox.question

        self._shutdown = StepTableModel(rig, with_duration=False, parent=self)
        self._shutdown.set_rows([self._shutdown.blank_row()])
        self._shutdown_view = _step_view(self._shutdown)
        self._shutdown_view.setMaximumHeight(3 * theme.ROW_PX + 2)
        self._shutdown_view.setAccessibleName("End state")

        self._problems = QLabel()
        self._problems.setWordWrap(True)
        self._problems.setStyleSheet(f"color: {theme.NAVY}; background: {theme.CREAM};")
        self._problems.setMargin(theme.GAP // 2)
        self._problems.setAccessibleName("Recipe problems")

        self._build_layout()
        self._connect()
        self.set_recipe(None)

    # Layout ------------------------------------------------------------

    def _build_layout(self) -> None:
        header = QFormLayout()
        header.setHorizontalSpacing(theme.SECTION_GAP)
        header.setVerticalSpacing(theme.GAP)
        header.addRow("Name", self._name)
        header.addRow("Notes", self._notes)

        trial_buttons = _button_row(
            self._add_trial, self._remove_trial, self._duplicate_trial, self._rename_trial
        )
        trials_box = _section("Trials")
        trials_layout = QVBoxLayout(trials_box)
        trials_layout.setSpacing(theme.GAP)
        trials_layout.addWidget(self._trial_list)
        trials_layout.addLayout(trial_buttons)

        step_buttons = _button_row(
            self._add_step,
            self._remove_step,
            self._duplicate_step,
            self._step_up,
            self._step_down,
            self._pulse_train,
        )
        steps_box = _section("Steps")
        steps_layout = QVBoxLayout(steps_box)
        steps_layout.setSpacing(theme.GAP)
        steps_layout.addWidget(self._step_view)
        steps_layout.addLayout(step_buttons)

        schedule_box = _section("Schedule")
        schedule_layout = QFormLayout(schedule_box)
        schedule_layout.setHorizontalSpacing(theme.SECTION_GAP)
        schedule_layout.setVerticalSpacing(theme.GAP)
        schedule_layout.addRow(self._schedule)
        schedule_layout.addRow("Ordering", self._ordering)
        schedule_layout.addRow("Seed", self._seed)

        shutdown_box = _section("End state, after the last trial or after Stop")
        shutdown_layout = QVBoxLayout(shutdown_box)
        shutdown_layout.addWidget(self._shutdown_view)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(theme.SECTION_GAP)
        left_layout.addWidget(trials_box)
        left_layout.addWidget(schedule_box)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(theme.SECTION_GAP)
        right_layout.addWidget(steps_box, stretch=3)
        right_layout.addWidget(shutdown_box)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setHandleWidth(theme.SECTION_GAP)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.MARGIN)
        layout.setSpacing(theme.SECTION_GAP)
        layout.addLayout(header)
        layout.addWidget(splitter, stretch=1)
        layout.addWidget(self._problems)

        # Keyboard order follows the work, not the columns: trials and their steps
        # first, then the shutdown state, then the schedule.
        order = (
            self._name,
            self._notes,
            self._trial_list,
            self._add_trial,
            self._remove_trial,
            self._duplicate_trial,
            self._rename_trial,
            self._step_view,
            self._add_step,
            self._remove_step,
            self._duplicate_step,
            self._step_up,
            self._step_down,
            self._pulse_train,
            self._shutdown_view,
            self._schedule,
            self._ordering,
            self._seed,
        )
        for first, second in pairwise(order):
            QWidget.setTabOrder(first, second)

    def _connect(self) -> None:
        self._name.textChanged.connect(self._emit_changed)
        self._notes.textChanged.connect(self._emit_changed)
        self._trial_list.currentRowChanged.connect(self._select_trial)
        self._trial_list.itemChanged.connect(self._on_trial_item_edited)
        self._add_trial.clicked.connect(self._on_add_trial)
        self._remove_trial.clicked.connect(self._on_remove_trial)
        self._rename_trial.clicked.connect(self._on_rename_trial)
        self._duplicate_trial.clicked.connect(self._on_duplicate_trial)
        self._steps.dataChanged.connect(self._emit_changed)
        self._steps.rowsInserted.connect(self._emit_changed)
        self._steps.rowsRemoved.connect(self._emit_changed)
        self._steps.rowsMoved.connect(self._emit_changed)
        self._add_step.clicked.connect(self._on_add_step)
        self._remove_step.clicked.connect(self._on_remove_step)
        self._duplicate_step.clicked.connect(self._on_duplicate_step)
        self._step_up.clicked.connect(lambda: self._on_move_step(-1))
        self._step_down.clicked.connect(lambda: self._on_move_step(1))
        self._pulse_train.clicked.connect(self._on_pulse_train)
        self._schedule.cellChanged.connect(self._on_count_changed)
        self._ordering.currentIndexChanged.connect(self._emit_changed)
        self._seed.textChanged.connect(self._emit_changed)
        self._shutdown.dataChanged.connect(self._emit_changed)

    # Rig ---------------------------------------------------------------

    @property
    def rig(self) -> RigMap:
        return self._rig

    def set_rig(self, rig: RigMap) -> None:
        self._rig = rig
        self._steps.set_rig(rig)
        self._shutdown.set_rig(rig)
        self._emit_changed()

    # Recipe in and out -------------------------------------------------

    def set_recipe(self, recipe: Recipe | None) -> None:
        """Load a recipe, or start a new one when None."""
        self._store_current_rows()
        self._current = None
        with QSignalBlocker(self._trial_list):
            self._trial_list.clear()
        if recipe is None:
            self._name.setText("")
            self._notes.setPlainText("")
            self._trials = [_TrialData("trial 1", [self._steps.blank_row()], 1)]
            self._ordering.setCurrentIndex(self._ordering.findData("block-randomized"))
            self._seed.setText("")
            self._shutdown.set_rows([self._shutdown.blank_row()])
        else:
            self._name.setText(recipe.name)
            self._notes.setPlainText(recipe.notes)
            self._trials = [
                _TrialData(
                    trial.name,
                    [StepRow.from_step(step) for step in trial.steps],
                    recipe.schedule.counts.get(trial.name, 0),
                )
                for trial in recipe.trials
            ]
            self._ordering.setCurrentIndex(
                max(0, self._ordering.findData(recipe.schedule.ordering))
            )
            seed = recipe.schedule.seed
            self._seed.setText("" if seed is None else str(seed))
            self._shutdown.set_rows([StepRow.from_step(recipe.shutdown)])
        self._refresh_trial_list()
        self._trial_list.setCurrentRow(0 if self._trials else -1)
        self._emit_changed()

    def recipe(self) -> Recipe:
        """Build the recipe as it is now, valid or not."""
        self._store_current_rows()
        trials = tuple(
            Trial(
                trial.name,
                tuple(row.to_step(self._rig, True) for row in trial.rows),
            )
            for trial in self._trials
        )
        seed_text = self._seed.text().strip()
        seed = int(seed_text) if seed_text.isdigit() else None
        schedule = Schedule(
            counts={trial.name: trial.count for trial in self._trials},
            ordering=self._ordering.currentData(),
            seed=seed,
        )
        shutdown_rows = self._shutdown.steps()
        shutdown = shutdown_rows[0] if shutdown_rows else Step(None)
        return Recipe(
            name=self._name.text().strip(),
            trials=trials,
            schedule=schedule,
            shutdown=shutdown,
            notes=self._notes.toPlainText(),
        )

    def problems(self) -> list[str]:
        """Return every problem that stops this recipe from running."""
        return recipe_problems(self.recipe(), self._rig)

    # Trials ------------------------------------------------------------

    def _store_current_rows(self) -> None:
        if self._current is not None and self._current < len(self._trials):
            self._trials[self._current].rows = [row.copy() for row in self._steps.rows()]

    def _refresh_trial_list(self) -> None:
        with QSignalBlocker(self._trial_list):
            current = self._trial_list.currentRow()
            self._trial_list.clear()
            for trial in self._trials:
                item = QListWidgetItem(trial.name)
                item.setFlags(item.flags() | Qt.ItemIsEditable)
                self._trial_list.addItem(item)
            if 0 <= current < len(self._trials):
                self._trial_list.setCurrentRow(current)
        self._refresh_schedule()

    def _refresh_schedule(self) -> None:
        with QSignalBlocker(self._schedule):
            self._schedule.setRowCount(len(self._trials))
            for row, trial in enumerate(self._trials):
                name = QTableWidgetItem(trial.name)
                name.setFlags(Qt.ItemIsEnabled)
                self._schedule.setItem(row, 0, name)
                spin = QSpinBox()
                spin.setRange(0, 100000)
                spin.setValue(trial.count)
                spin.valueChanged.connect(lambda value, index=row: self._set_count(index, value))
                self._schedule.setCellWidget(row, 1, spin)

    def _set_count(self, index: int, value: int) -> None:
        if 0 <= index < len(self._trials):
            self._trials[index].count = value
            self._emit_changed()

    def _on_count_changed(self, _row: int, _column: int) -> None:
        self._emit_changed()

    def _select_trial(self, index: int) -> None:
        self._store_current_rows()
        if 0 <= index < len(self._trials):
            self._current = index
            self._steps.set_rows(self._trials[index].rows)
        else:
            self._current = None
            self._steps.set_rows([])
        enabled = self._current is not None
        for button in (self._add_step, self._pulse_train, self._remove_trial, self._rename_trial):
            button.setEnabled(enabled)

    def _unique_name(self, base: str) -> str:
        names = {trial.name for trial in self._trials}
        if base not in names:
            return base
        number = 2
        while f"{base} {number}" in names:
            number += 1
        return f"{base} {number}"

    def _on_add_trial(self) -> None:
        self._store_current_rows()
        name = self._unique_name(f"trial {len(self._trials) + 1}")
        self._trials.append(_TrialData(name, [self._steps.blank_row()], 1))
        self._refresh_trial_list()
        self._trial_list.setCurrentRow(len(self._trials) - 1)
        self._emit_changed()

    def _on_remove_trial(self) -> None:
        index = self._trial_list.currentRow()
        if not (0 <= index < len(self._trials)):
            return
        self._store_current_rows()
        trial = self._trials[index]
        steps = len(trial.rows)
        answer = self._confirm(
            self,
            "Remove trial",
            f"Remove the trial {trial.name!r} and its {steps} step{'s' if steps != 1 else ''}? "
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._current = None
        del self._trials[index]
        self._refresh_trial_list()
        self._trial_list.setCurrentRow(min(index, len(self._trials) - 1))
        self._select_trial(self._trial_list.currentRow())
        self._emit_changed()

    def _on_rename_trial(self) -> None:
        item = self._trial_list.currentItem()
        if item is not None:
            self._trial_list.editItem(item)

    def _on_trial_item_edited(self, item: QListWidgetItem) -> None:
        index = self._trial_list.row(item)
        if 0 <= index < len(self._trials) and item.text() != self._trials[index].name:
            self.rename_trial(index, item.text())

    def rename_trial(self, index: int, name: str) -> None:
        """Rename a trial. The old name stays when the new one is empty or already used."""
        name = name.strip()
        problem = None
        if not name:
            problem = "The trial needs a name."
        elif any(
            other.name == name for position, other in enumerate(self._trials) if position != index
        ):
            problem = f"The name {name!r} is already used."
        if problem is not None:
            self._problems.setText(problem)
            self._refresh_trial_list()
            return
        self._trials[index].name = name
        self._refresh_trial_list()
        self._emit_changed()

    def _on_duplicate_trial(self) -> None:
        index = self._trial_list.currentRow()
        if not (0 <= index < len(self._trials)):
            return
        self._store_current_rows()
        source = self._trials[index]
        copy = _TrialData(
            self._unique_name(source.name), [row.copy() for row in source.rows], source.count
        )
        self._trials.insert(index + 1, copy)
        self._refresh_trial_list()
        self._trial_list.setCurrentRow(index + 1)
        self._emit_changed()

    # Steps -------------------------------------------------------------

    def _selected_step(self) -> int:
        indexes = self._step_view.selectionModel().selectedRows()
        return indexes[0].row() if indexes else self._steps.rowCount() - 1

    def _on_add_step(self) -> None:
        position = self._selected_step() + 1
        self._steps.insert_rows(position, [self._steps.blank_row()])
        self._step_view.selectRow(position)
        duration = self._steps.index(position, 0)
        self._step_view.setCurrentIndex(duration)
        self._step_view.edit(duration)

    def _on_remove_step(self) -> None:
        position = self._selected_step()
        if position >= 0:
            self._steps.remove_row(position)

    def _on_duplicate_step(self) -> None:
        position = self._selected_step()
        if position >= 0:
            self._steps.insert_rows(position + 1, [self._steps.rows()[position]])
            self._step_view.selectRow(position + 1)

    def _on_move_step(self, offset: int) -> None:
        position = self._selected_step()
        if position >= 0:
            self._step_view.selectRow(self._steps.move_row(position, offset))

    def _on_pulse_train(self) -> None:
        if not self._rig.valves:
            QMessageBox.information(self, "Generate pulse train", "lab.toml has no [valves].")
            return
        position = self._selected_step()
        template = self._steps.rows()[position] if position >= 0 else self._steps.blank_row()
        dialog = PulseTrainDialog(list(self._rig.valves), template, self)
        if dialog.exec():
            self.insert_steps(position + 1, dialog.rows())

    def insert_steps(self, position: int, rows: list[StepRow]) -> None:
        self._steps.insert_rows(position, rows)
        if rows:
            self._step_view.selectRow(position)

    # Change tracking ---------------------------------------------------

    def _emit_changed(self, *_arguments) -> None:
        problems = self.problems()
        self._problems.setText("\n".join(problems[:6] + (["…"] if len(problems) > 6 else [])))
        self._problems.setVisible(bool(problems))
        self.changed.emit()
