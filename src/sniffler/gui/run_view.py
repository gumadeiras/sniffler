"""Watch a run: the squirrel and status, the timeline with valve lanes, the MFC plot.

Every box on this tab keeps its size and place while a run changes the content.
Labels have fixed heights, numbers use a fixed-pitch font in fixed-width cells, the
plot axes are fixed at run start, and a splitter, not the content, decides how the
width is shared.
"""

import queue
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontDatabase, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sniffler.executor import Event, Executor, Phase, Sample, Status
from sniffler.gui import theme
from sniffler.gui.squirrel import SniffWidget
from sniffler.gui.timeline import TimelineWidget
from sniffler.recipe import Recipe, RigMap

DEVIATION_FRACTION = 0.05
PLOT_POINTS = 6000
STEP_ROWS = 4
SQUIRREL_PX = 128
AXIS_WIDTH_PX = 64


class RunController(QObject):
    """Own one executor and move its callbacks onto the GUI thread."""

    status_changed = Signal(object)
    sample_received = Signal(object)
    event_received = Signal(object)
    finished = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._executor: Executor | None = None
        self._statuses: queue.SimpleQueue[Status] = queue.SimpleQueue()
        self._samples: queue.SimpleQueue[Sample] = queue.SimpleQueue()
        self._events: queue.SimpleQueue[Event] = queue.SimpleQueue()
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._drain)
        self._last: Status | None = None

    @property
    def executor(self) -> Executor | None:
        return self._executor

    @property
    def is_running(self) -> bool:
        return self._executor is not None and self._executor.is_alive

    def elapsed_seconds(self) -> float:
        return 0.0 if self._executor is None else self._executor.elapsed_seconds()

    def start(
        self,
        recipe: Recipe,
        rig: RigMap,
        seed: int,
        runs_directory: Path,
        operator_notes: str,
        **factories: Callable[..., Any],
    ) -> None:
        if self.is_running:
            raise RuntimeError("A run is already active.")
        self._last = None
        self._executor = Executor(
            recipe,
            rig,
            seed,
            runs_directory,
            operator_notes=operator_notes,
            on_status=self._statuses.put,
            on_sample=self._samples.put,
            on_event=self._events.put,  # a queue put only; nothing else may run here
            **factories,
        )
        self._timer.start()
        self._executor.start()

    def request_stop(self) -> None:
        if self._executor is not None:
            self._executor.request_stop()

    def abort(self) -> None:
        if self._executor is not None:
            self._executor.abort()

    def abort_and_wait(self, timeout_seconds: float = 30.0) -> Status | None:
        """Abort now and block until the executor has applied the safe state."""
        if self._executor is None:
            return None
        self._executor.abort()
        self._executor.join(timeout_seconds)
        self._drain()
        return self._executor.status

    def _drain(self) -> None:
        for source, signal in (
            (self._samples, self.sample_received),
            (self._events, self.event_received),
        ):
            while True:
                try:
                    item = source.get_nowait()
                except queue.Empty:
                    break
                signal.emit(item)
        status: Status | None = None
        while True:
            try:
                status = self._statuses.get_nowait()
            except queue.Empty:
                break
            self._last = status
            self.status_changed.emit(status)
        if self._executor is not None and not self._executor.is_alive and self._timer.isActive():
            final = self._executor.status
            if final.phase.is_final:
                self._timer.stop()
                if self._last is not final:
                    self.status_changed.emit(final)
                self.finished.emit(final)


def _fixed_font() -> Any:
    font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    font.setPixelSize(theme.BODY_PX)
    return font


def _one_line(label: QLabel) -> QLabel:
    """A label that never wraps or grows; the full text stays in the tooltip."""
    label.setWordWrap(False)
    label.setFixedHeight(QFontMetrics(label.font()).lineSpacing() + theme.UNIT // 2)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


def _valves_text(valves: dict[str, bool]) -> str:
    """Name the open valves; count the closed ones. Twelve names of 'closed' say nothing."""
    if not valves:
        return "—"
    opened = [name for name, state in valves.items() if state]
    closed = len(valves) - len(opened)
    if not opened:
        return f"all {closed} closed"
    text = ", ".join(f"{name} open" for name in opened)
    if closed:
        text += f"; {closed} closed"
    return text


class MfcPlot(QWidget):
    """Commanded versus measured flow, with one fixed-format readout row per MFC."""

    def __init__(self, rig: RigMap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rig = rig
        self._plot = pg.PlotWidget(background=theme.PANEL)
        self._plot.setLabel("bottom", "run time", units="s", color=theme.INK_SOFT)
        self._plot.setLabel("left", "mass flow", color=theme.INK_SOFT)
        self._plot.showGrid(x=True, y=True, alpha=0.15)
        self._plot.hideButtons()
        self._plot.setMenuEnabled(False)
        self._plot.setMouseEnabled(x=False, y=False)
        for axis in ("bottom", "left"):
            self._plot.getAxis(axis).setPen(pg.mkPen(theme.LINE))
            self._plot.getAxis(axis).setTextPen(pg.mkPen(theme.INK_SOFT))
        # A fixed axis width: tick labels growing from 0.9 to 1000 must not move the plot.
        self._plot.getAxis("left").setWidth(AXIS_WIDTH_PX)
        self._plot.getAxis("left").enableAutoSIPrefix(False)
        self._plot.getAxis("bottom").enableAutoSIPrefix(False)
        self._commanded: dict[str, Any] = {}
        self._actual: dict[str, Any] = {}
        self._history: dict[str, deque[tuple[float, float | None, float | None]]] = {}
        self._cells: dict[str, dict[str, QLabel]] = {}
        self._labels: dict[str, QLabel] = {}  # the deviation cell, which carries the verdict
        readout = QGridLayout()
        readout.setHorizontalSpacing(theme.SECTION_GAP)
        readout.setVerticalSpacing(theme.GAP // 2)
        for column, title in enumerate(("", "commanded", "measured", "deviation")):
            header = QLabel(title)
            header.setStyleSheet(f"color: {theme.INK_SOFT};")
            readout.addWidget(header, 0, column, alignment=Qt.AlignRight)
        number_width = QFontMetrics(_fixed_font()).horizontalAdvance("! +00000.00 SCCM")
        for row, (name, mfc) in enumerate(rig.mfcs.items(), start=1):
            color = QColor(theme.SERIES[(row - 1) % len(theme.SERIES)])
            self._commanded[name] = self._plot.plot(
                pen=pg.mkPen(color, width=2, style=Qt.DashLine), name=f"{name} commanded"
            )
            self._actual[name] = self._plot.plot(
                pen=pg.mkPen(color, width=2), name=f"{name} measured"
            )
            self._history[name] = deque(maxlen=PLOT_POINTS)
            title = QLabel(f'<span style="color:{color.name()}">■</span> {name} ({mfc.flow_unit})')
            readout.addWidget(title, row, 0)
            cells: dict[str, QLabel] = {}
            for column, key in enumerate(("commanded", "measured", "deviation"), start=1):
                cell = QLabel("—")
                cell.setFont(_fixed_font())
                cell.setMinimumWidth(number_width)
                cell.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                readout.addWidget(cell, row, column)
                cells[key] = cell
            self._cells[name] = cells
            self._labels[name] = cells["deviation"]
        readout.setColumnStretch(4, 1)
        caption = QLabel("dashed commanded, solid measured")
        caption.setStyleSheet(f"color: {theme.INK_SOFT};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.GAP)
        layout.addWidget(self._plot, stretch=1)
        layout.addWidget(caption)
        layout.addLayout(readout)
        if not rig.mfcs:
            layout.addWidget(QLabel("lab.toml has no MFC with a port."))
        self.clear()

    def clear(self) -> None:
        for name in self._history:
            self._history[name].clear()
            self._commanded[name].setData([], [])
            self._actual[name].setData([], [])
            for key in ("commanded", "measured", "deviation"):
                self._cells[name][key].setText("—")
            self._cells[name]["deviation"].setStyleSheet("")
            self._cells[name]["deviation"].setToolTip("")
        self._plot.setXRange(0.0, 1.0, padding=0.02)
        self._plot.setYRange(0.0, 1.0, padding=0.05)

    def set_ranges(self, recipe: Recipe, planned_seconds: float) -> None:
        """Fix both axes for the whole run, so the picture never jumps."""
        values = [
            value
            for trial in recipe.trials
            for step in trial.steps
            for value in step.setpoints.values()
        ] + list(recipe.shutdown.setpoints.values())
        high = max([0.0, *values])
        low = min([0.0, *values])
        span = max(high - low, 1.0)
        self._plot.setYRange(low - 0.05 * span, high + 0.1 * span, padding=0)
        self._plot.setXRange(0.0, max(planned_seconds, 1.0), padding=0.02)

    def add_sample(self, sample: Sample) -> None:
        history = self._history.get(sample.mfc)
        if history is None:
            return
        history.append((sample.run_seconds, sample.commanded_setpoint, sample.mass_flow))
        times = [point[0] for point in history]
        self._commanded[sample.mfc].setData(
            times, [point[1] if point[1] is not None else float("nan") for point in history]
        )
        self._actual[sample.mfc].setData(
            times, [point[2] if point[2] is not None else float("nan") for point in history]
        )
        self._describe(sample)

    def _describe(self, sample: Sample) -> None:
        mfc = self._rig.mfcs[sample.mfc]
        cells = self._cells[sample.mfc]
        cells["measured"].setText(
            "—" if sample.mass_flow is None else f"{sample.mass_flow:.2f} {mfc.flow_unit}"
        )
        if sample.commanded_setpoint is None or sample.mass_flow is None:
            cells["commanded"].setText(
                "—"
                if sample.commanded_setpoint is None
                else f"{sample.commanded_setpoint:.2f} {mfc.flow_unit}"
            )
            cells["deviation"].setText("—")
            cells["deviation"].setStyleSheet("")
            return
        cells["commanded"].setText(f"{sample.commanded_setpoint:.2f} {mfc.flow_unit}")
        deviation = sample.mass_flow - sample.commanded_setpoint
        reference = mfc.full_scale or mfc.maximum_flow or abs(sample.commanded_setpoint) or 1.0
        limit = max(DEVIATION_FRACTION * reference, 0.01)
        high = abs(deviation) > limit
        # The deviation cell carries the verdict: a leading "!" and bold pink when the
        # deviation is more than the limit. The mark, not the color alone, says it.
        cells["deviation"].setText(f"{'! ' if high else ''}{deviation:+.2f} {mfc.flow_unit}")
        cells["deviation"].setToolTip(
            f"{'More' if high else 'Not more'} than the limit of {limit:.2f} {mfc.flow_unit}."
        )
        cells["deviation"].setStyleSheet(
            f"color: {theme.PINK_TEXT}; font-weight: bold;" if high else ""
        )


class StatusPanel(QWidget):
    """Phase and message, the squirrel, trial and time, the commanded valves, the steps."""

    def __init__(
        self, rig: RigMap, parent: QWidget | None = None, *, reduced_motion: bool | None = None
    ) -> None:
        super().__init__(parent)
        self._rig = rig
        self._recipe: Recipe | None = None
        self.squirrel = SniffWidget(reduced=reduced_motion)
        self.squirrel.setFixedHeight(SQUIRREL_PX)
        self._phase = QLabel("idle")
        self._phase.setFont(theme.font(theme.TITLE_PX, bold=True))
        self._phase.setFixedHeight(QFontMetrics(self._phase.font()).lineSpacing() + theme.UNIT)
        self._message = QLabel("")
        self._message.setWordWrap(True)
        self._message.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._message.setFixedHeight(2 * QFontMetrics(self._message.font()).lineSpacing() + 4)
        self._trial = _one_line(QLabel("—"))
        self._time = _one_line(QLabel("—"))
        self._time.setFont(_fixed_font())
        self._directory = _one_line(QLabel("—"))
        self._valves = _one_line(QLabel("—"))

        self._steps = QTableWidget(0, 3)
        self._steps.setHorizontalHeaderLabels(["Step", "Duration (s)", "State"])
        header = self._steps.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setStretchLastSection(True)
        header.resizeSection(0, 7 * theme.UNIT)
        header.resizeSection(1, 14 * theme.UNIT)
        self._steps.verticalHeader().setVisible(False)
        self._steps.verticalHeader().setDefaultSectionSize(theme.ROW_PX)
        self._steps.setSelectionMode(QAbstractItemView.NoSelection)
        self._steps.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._steps.setFocusPolicy(Qt.NoFocus)
        self._steps.setFixedHeight(header.sizeHint().height() + STEP_ROWS * theme.ROW_PX + 2)
        self._steps_trial: str | None = None

        headline = QVBoxLayout()
        headline.setSpacing(theme.GAP // 2)
        headline.addWidget(self._phase)
        headline.addWidget(self._message)
        headline.addStretch(1)
        top = QHBoxLayout()
        top.setSpacing(theme.GAP)
        top.addLayout(headline, stretch=1)
        top.addWidget(self.squirrel, stretch=2)
        top.setAlignment(self.squirrel, Qt.AlignTop)

        form = QFormLayout()
        form.setHorizontalSpacing(theme.SECTION_GAP)
        form.setVerticalSpacing(theme.GAP // 2)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.addRow("Trial", self._trial)
        form.addRow("Time", self._time)
        form.addRow("Run folder", self._directory)
        form.addRow("Valves", self._valves)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.GAP)
        layout.addLayout(top)
        layout.addLayout(form)
        layout.addWidget(self._steps)
        layout.addStretch(1)

    def set_recipe(self, recipe: Recipe | None) -> None:
        self._recipe = recipe
        self._steps_trial = None
        self._steps.setRowCount(0)
        self.squirrel.clear()

    def show_status(self, status: Status, elapsed: float) -> None:
        self._phase.setText(status.phase.value)
        # "running" over "Running." says nothing twice; keep the line, drop the echo.
        message = status.message
        if message.strip(" .").lower() == status.phase.value:
            message = ""
        self._message.setText(message)
        self._message.setToolTip(message)
        self.squirrel.set_phase(status.phase)
        if status.run_directory is not None:
            self._directory.setText(status.run_directory.name)
            self._directory.setToolTip(str(status.run_directory))
        total = len(status.order)
        if status.trial_index is None or total == 0:
            self._trial.setText("—")
        else:
            self._trial.setText(f"{status.trial_index + 1} of {total}: {status.trial_name}")
        self._trial.setToolTip(self._trial.text())
        self._valves.setText(_valves_text(status.valves))
        self._valves.setToolTip(
            ", ".join(
                f"{name} {'open' if state else 'closed'}" for name, state in status.valves.items()
            )
        )
        self._show_steps(status)
        self.show_time(status, elapsed)

    def show_time(self, status: Status, elapsed: float) -> None:
        if status.phase in {Phase.RUNNING, Phase.FINISHING} or status.phase.is_final:
            remaining = max(0.0, status.planned_seconds - elapsed)
            width = len(f"{status.planned_seconds:.1f}")
            self._time.setText(
                f"{elapsed:{width}.1f} s of {status.planned_seconds:.1f} s, "
                f"{remaining:{width}.1f} s left"
            )
        else:
            self._time.setText("—")
        if status.step_index is not None and self._steps.rowCount() > status.step_index:
            for row in range(self._steps.rowCount()):
                for column in range(3):
                    item = self._steps.item(row, column)
                    if item is not None:
                        item.setBackground(
                            QColor(theme.PINK_WASH)
                            if row == status.step_index
                            else QColor(theme.PANEL)
                        )
            self._steps.scrollToItem(self._steps.item(status.step_index, 0))

    def _show_steps(self, status: Status) -> None:
        name = status.trial_name
        if self._recipe is None or name is None:
            return
        if name != self._steps_trial:
            self._steps_trial = name
            trial = self._recipe.trial(name)
            self._steps.setRowCount(len(trial.steps))
            for row, step in enumerate(trial.steps):
                state = ", ".join(
                    [f"{valve} open" for valve, open_ in step.valves.items() if open_]
                    + [f"{mfc} {value:g}" for mfc, value in step.setpoints.items()]
                )
                number = QTableWidgetItem(str(row + 1))
                number.setTextAlignment(Qt.AlignCenter)
                duration = QTableWidgetItem(f"{step.duration_seconds:g}")
                duration.setTextAlignment(Qt.AlignCenter)
                item = QTableWidgetItem(state or "all valves closed")
                item.setToolTip(item.text())
                self._steps.setItem(row, 0, number)
                self._steps.setItem(row, 1, duration)
                self._steps.setItem(row, 2, item)


class RunView(QWidget):
    """The Run tab body: status with the squirrel, the MFC plot, and the timeline."""

    def __init__(
        self, rig: RigMap, parent: QWidget | None = None, *, reduced_motion: bool | None = None
    ) -> None:
        super().__init__(parent)
        self._rig = rig
        self.status_panel = StatusPanel(rig, reduced_motion=reduced_motion)
        self.timeline = TimelineWidget()
        self.plot = MfcPlot(rig)
        self.cue_latencies: list[float] = []
        # The panel scrolls when a large rig needs more lines than the window has;
        # overlapping text is never an option.
        scroll = QScrollArea()
        scroll.setWidget(self.status_panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        self.status_panel.setAutoFillBackground(False)
        # A splitter shares the width. Content never moves the boundary; the operator can.
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(theme.SECTION_GAP)
        self.splitter.addWidget(scroll)
        self.splitter.addWidget(self.plot)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        # Equal weights larger than any window: Qt scales them down in proportion,
        # so the first layout is an even split and later resizes keep the ratio.
        self.splitter.setSizes([10_000, 10_000])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SECTION_GAP)
        layout.addWidget(self.splitter, stretch=1)
        layout.addWidget(self.timeline)

    def preview(self, recipe: Recipe) -> None:
        """Lay out the lanes for the valves this recipe opens, before any run."""
        opened = {
            valve
            for trial in recipe.trials
            for step in trial.steps
            for valve, is_open in step.valves.items()
            if is_open
        }
        self.timeline.set_valves(name for name in self._rig.valves if name in opened)

    def prepare(self, recipe: Recipe) -> None:
        self.status_panel.set_recipe(recipe)
        self.timeline.clear()
        self.preview(recipe)
        self.plot.clear()
        self.cue_latencies = []

    def show_status(self, status: Status, elapsed: float, recipe: Recipe | None) -> None:
        if recipe is not None and status.order and status.phase == Phase.STARTING:
            self.timeline.set_plan(recipe, status.order)
            self.plot.set_ranges(recipe, status.planned_seconds)
        self.status_panel.show_status(status, elapsed)
        self.timeline.set_progress(elapsed, status.trial_index)

    def show_event(self, event: Event, elapsed: float) -> None:
        """A valve that opens during a step makes the squirrel sniff; its name stays while open."""
        if event.event != "valve_command":
            return
        squirrel = self.status_panel.squirrel
        if event.value != "open":
            squirrel.close(event.device)
        elif event.step_index is None:
            squirrel.show_open(event.device)  # the shutdown or safe state, not an odor onset
        else:
            self.cue_latencies.append(elapsed - event.returned_run_seconds)
            squirrel.sniff([event.device])

    def tick(self, status: Status, elapsed: float) -> None:
        self.status_panel.show_time(status, elapsed)
        self.timeline.set_progress(elapsed, status.trial_index)
