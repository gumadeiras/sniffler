"""Watch a run: the squirrel and status, the whole-run timeline, the MFC plot."""

import queue
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontDatabase, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sniffler.executor import Event, Executor, Phase, Sample, Status
from sniffler.gui import theme
from sniffler.gui.squirrel import SniffWidget
from sniffler.recipe import Recipe, RigMap

DEVIATION_FRACTION = 0.05
PLOT_POINTS = 6000


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


class TimelineWidget(QWidget):
    """The whole run as one bar: trials shaded by type, steps as ticks, a pink cursor."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(64)
        self._segments: list[tuple[float, float, str, list[float]]] = []
        self._colors: dict[str, QColor] = {}
        self._total = 0.0
        self._cursor = 0.0
        self._current: int | None = None

    def set_plan(self, recipe: Recipe, order: tuple[str, ...]) -> None:
        self._segments = []
        self._colors = {}
        start = 0.0
        for name in order:
            trial = recipe.trial(name)
            boundaries: list[float] = []
            offset = start
            for step in trial.steps[:-1]:
                offset += step.duration_seconds or 0.0
                boundaries.append(offset)
            self._segments.append((start, trial.duration_seconds, name, boundaries))
            if name not in self._colors:
                self._colors[name] = QColor(theme.TRIAL_RAMP[len(self._colors) % 6])
            start += trial.duration_seconds
        self._total = start
        self._cursor = 0.0
        self._current = None
        self.update()

    def set_progress(self, elapsed_seconds: float, trial_index: int | None) -> None:
        self._cursor = max(0.0, min(elapsed_seconds, self._total))
        self._current = trial_index
        self.update()

    def clear(self) -> None:
        self._segments = []
        self._total = 0.0
        self._cursor = 0.0
        self._current = None
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        area = self.rect().adjusted(1, 8, -1, -20)
        painter.setPen(QPen(QColor(theme.LINE)))
        painter.setBrush(QColor(theme.PANEL))
        painter.drawRect(area)
        if self._total <= 0 or not self._segments:
            painter.setPen(QColor(theme.INK_SOFT))
            painter.drawText(self.rect(), Qt.AlignCenter, "No run planned.")
            return
        scale = area.width() / self._total
        for index, (start, duration, name, boundaries) in enumerate(self._segments):
            left = area.left() + start * scale
            width = max(1.0, duration * scale)
            color = QColor(self._colors[name])
            if self._current is not None and index != self._current:
                color.setAlpha(150)
            painter.setPen(QPen(QColor(theme.PANEL)))
            painter.setBrush(color)
            painter.drawRect(QRectF(left, area.top(), width, area.height()))
            painter.setPen(QPen(QColor(theme.PANEL)))
            for boundary in boundaries:
                x = area.left() + boundary * scale
                painter.drawLine(int(x), area.top() + 4, int(x), area.bottom() - 4)
        cursor_x = area.left() + self._cursor * scale
        painter.setPen(QPen(QColor(theme.PINK), 2))
        painter.drawLine(int(cursor_x), area.top() - 6, int(cursor_x), area.bottom() + 6)
        metrics = painter.fontMetrics()
        x = self.rect().left() + 2
        baseline = self.rect().bottom() - 4
        square = metrics.ascent() - 2
        for name, color in self._colors.items():
            painter.fillRect(x, baseline - square, square, square, color)
            painter.setPen(QPen(QColor(theme.NAVY)))
            painter.drawText(x + square + 4, baseline, name)
            x += square + 4 + metrics.horizontalAdvance(name) + theme.SECTION_GAP
        painter.setPen(QPen(QColor(theme.INK_SOFT)))
        painter.drawText(
            self.rect().adjusted(2, 0, -2, -2),
            Qt.AlignBottom | Qt.AlignRight,
            f"total {self._total:.1f} s",
        )


class MfcPlot(QWidget):
    """Commanded versus actual flow for every MFC, with the deviation stated in words."""

    def __init__(self, rig: RigMap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rig = rig
        self._plot = pg.PlotWidget(background=theme.PANEL)
        self._plot.addLegend(offset=(10, 10), labelTextColor=theme.NAVY)
        self._plot.setLabel("bottom", "run time", units="s", color=theme.INK_SOFT)
        self._plot.setLabel("left", "mass flow", color=theme.INK_SOFT)
        self._plot.showGrid(x=True, y=True, alpha=0.15)
        for axis in ("bottom", "left"):
            self._plot.getAxis(axis).setPen(pg.mkPen(theme.LINE))
            self._plot.getAxis(axis).setTextPen(pg.mkPen(theme.INK_SOFT))
        self._commanded: dict[str, Any] = {}
        self._actual: dict[str, Any] = {}
        self._history: dict[str, deque[tuple[float, float | None, float | None]]] = {}
        self._labels: dict[str, QLabel] = {}
        labels = QVBoxLayout()
        labels.setSpacing(theme.GAP // 2)
        for index, name in enumerate(rig.mfcs):
            color = QColor(theme.SERIES[index % len(theme.SERIES)])
            self._commanded[name] = self._plot.plot(
                pen=pg.mkPen(color, width=2, style=Qt.DashLine), name=f"{name} commanded"
            )
            self._actual[name] = self._plot.plot(
                pen=pg.mkPen(color, width=2), name=f"{name} actual"
            )
            self._history[name] = deque(maxlen=PLOT_POINTS)
            label = QLabel(f"{name}: no reading yet")
            label.setWordWrap(True)
            self._labels[name] = label
            labels.addWidget(label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.GAP)
        layout.addWidget(self._plot, stretch=1)
        layout.addLayout(labels)
        if not rig.mfcs:
            layout.addWidget(QLabel("lab.toml has no MFC with a port."))

    def clear(self) -> None:
        for name in self._history:
            self._history[name].clear()
            self._commanded[name].setData([], [])
            self._actual[name].setData([], [])
            self._labels[name].setText(f"{name}: no reading yet")
            self._labels[name].setStyleSheet("")

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
        unit = mfc.flow_unit
        label = self._labels[sample.mfc]
        if sample.mass_flow is None or sample.commanded_setpoint is None:
            actual = "?" if sample.mass_flow is None else f"{sample.mass_flow:.2f}"
            label.setText(f"{sample.mfc}: actual {actual} {unit}, no setpoint commanded yet")
            label.setStyleSheet("")
            return
        deviation = sample.mass_flow - sample.commanded_setpoint
        reference = mfc.full_scale or mfc.maximum_flow or abs(sample.commanded_setpoint) or 1.0
        limit = max(DEVIATION_FRACTION * reference, 0.01)
        high = abs(deviation) > limit
        verdict = f"HIGH deviation, more than {limit:.2f}" if high else "within limit"
        label.setText(
            f"{sample.mfc}: commanded {sample.commanded_setpoint:.2f}, "
            f"actual {sample.mass_flow:.2f} {unit}, deviation {deviation:+.2f}: {verdict}"
        )
        label.setStyleSheet(f"color: {theme.PINK_TEXT}; font-weight: bold;" if high else "")


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


class StatusPanel(QWidget):
    """Phase and message, the squirrel, trial and time, commanded and measured state."""

    def __init__(
        self, rig: RigMap, parent: QWidget | None = None, *, reduced_motion: bool | None = None
    ) -> None:
        super().__init__(parent)
        self._rig = rig
        self._recipe: Recipe | None = None
        self.squirrel = SniffWidget(reduced=reduced_motion)
        self._phase = QLabel("idle")
        self._phase.setFont(theme.font(theme.TITLE_PX, bold=True))
        self._message = QLabel("")
        self._message.setWordWrap(True)
        self._trial = QLabel("—")
        self._trial.setWordWrap(True)
        self._time = QLabel("—")
        time_font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        time_font.setPixelSize(theme.BODY_PX)
        self._time.setFont(time_font)
        # The run directory name only; the status bar names the runs folder.
        self._directory = QLabel("—")
        self._directory.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._directory.setAccessibleName("Run directory")
        self._valves = QLabel("—")
        self._valves.setWordWrap(True)
        self._setpoints = QLabel("—")
        self._setpoints.setWordWrap(True)
        self._readback = QLabel("—")  # one line per MFC, so its height is known
        self._latest: dict[str, Sample] = {}

        self._steps = QTableWidget(0, 3)
        self._steps.setHorizontalHeaderLabels(["Step", "Duration (s)", "State"])
        self._steps.horizontalHeader().setStretchLastSection(True)
        self._steps.verticalHeader().setVisible(False)
        self._steps.setSelectionMode(QAbstractItemView.NoSelection)
        self._steps.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._steps.setFocusPolicy(Qt.NoFocus)
        self._steps.setMinimumHeight(3 * theme.ROW_PX)
        self._steps.setMaximumHeight(6 * theme.ROW_PX)
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
        form.addRow("Trial", self._trial)
        form.addRow("Time", self._time)
        form.addRow("Directory", self._directory)
        commanded = QLabel("Commanded")
        commanded.setFont(theme.font(bold=True))
        form.addRow(commanded)
        form.addRow("Valves", self._valves)
        form.addRow("Setpoints", self._setpoints)
        measured = QLabel("Measured")
        measured.setFont(theme.font(bold=True))
        form.addRow(measured, self._readback)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.GAP)
        layout.addLayout(top)
        layout.addLayout(form)
        layout.addWidget(self._steps)

    def set_recipe(self, recipe: Recipe | None) -> None:
        self._recipe = recipe
        self._steps_trial = None
        self._steps.setRowCount(0)
        self._latest = {}
        self.squirrel.clear()

    def show_status(self, status: Status, elapsed: float) -> None:
        self._phase.setText(status.phase.value)
        self._message.setText(status.message)
        self.squirrel.set_phase(status.phase)
        if status.run_directory is not None:
            self._directory.setText(status.run_directory.name)
            self._directory.setToolTip(str(status.run_directory))
        total = len(status.order)
        if status.trial_index is None or total == 0:
            self._trial.setText("—")
        else:
            self._trial.setText(f"{status.trial_index + 1} of {total}: {status.trial_name}")
        self._valves.setText(_valves_text(status.valves))
        self._setpoints.setText(
            ", ".join(
                f"{name} {value:g} {self._rig.mfcs[name].flow_unit}"
                for name, value in status.setpoints.items()
                if name in self._rig.mfcs
            )
            or "—"
        )
        self._show_steps(status)
        self.show_time(status, elapsed)

    def show_time(self, status: Status, elapsed: float) -> None:
        if status.phase in {Phase.RUNNING, Phase.FINISHING} or status.phase.is_final:
            remaining = max(0.0, status.planned_seconds - elapsed)
            self._time.setText(
                f"elapsed {elapsed:7.1f} s, remaining {remaining:7.1f} s "
                f"of {status.planned_seconds:.1f} s"
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
                self._steps.setItem(row, 0, QTableWidgetItem(str(row + 1)))
                self._steps.setItem(row, 1, QTableWidgetItem(f"{step.duration_seconds:g}"))
                item = QTableWidgetItem(state or "all valves closed")
                item.setToolTip(item.text())
                self._steps.setItem(row, 2, item)

    def show_sample(self, sample: Sample) -> None:
        self._latest[sample.mfc] = sample
        parts = []
        for name, latest in self._latest.items():
            unit = self._rig.mfcs[name].flow_unit if name in self._rig.mfcs else ""
            actual = "?" if latest.mass_flow is None else f"{latest.mass_flow:.2f}"
            device = "?" if latest.device_setpoint is None else f"{latest.device_setpoint:.2f}"
            parts.append(f"{name} flow {actual} {unit} (device setpoint {device})")
        self._readback.setText("\n".join(parts) or "—")


class RunView(QWidget):
    """The Run tab body: status with the squirrel, the MFC plot, and the timeline."""

    def __init__(
        self, rig: RigMap, parent: QWidget | None = None, *, reduced_motion: bool | None = None
    ) -> None:
        super().__init__(parent)
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
        top = QHBoxLayout()
        top.setSpacing(theme.SECTION_GAP)
        top.addWidget(scroll, stretch=1)
        top.addWidget(self.plot, stretch=1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SECTION_GAP)
        layout.addLayout(top, stretch=1)
        layout.addWidget(self.timeline)

    def prepare(self, recipe: Recipe) -> None:
        self.status_panel.set_recipe(recipe)
        self.timeline.clear()
        self.plot.clear()
        self.cue_latencies = []

    def show_status(self, status: Status, elapsed: float, recipe: Recipe | None) -> None:
        if recipe is not None and status.order and status.phase == Phase.STARTING:
            self.timeline.set_plan(recipe, status.order)
        self.status_panel.show_status(status, elapsed)
        self.timeline.set_progress(elapsed, status.trial_index)

    def show_event(self, event: Event, elapsed: float) -> None:
        """A valve that opened during a step makes the squirrel sniff."""
        if event.event != "valve_command" or event.value != "open" or event.step_index is None:
            return
        self.cue_latencies.append(elapsed - event.returned_run_seconds)
        self.status_panel.squirrel.sniff([event.device])

    def tick(self, status: Status, elapsed: float) -> None:
        self.status_panel.show_time(status, elapsed)
        self.timeline.set_progress(elapsed, status.trial_index)
