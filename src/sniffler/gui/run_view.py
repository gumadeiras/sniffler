"""Watch a run: status panel, whole-run timeline, and the MFC commanded-vs-actual plot."""

import queue
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sniffler.executor import Executor, Phase, Sample, Status
from sniffler.recipe import Recipe, RigMap

TRIAL_COLORS = [
    QColor(94, 129, 172),
    QColor(208, 135, 112),
    QColor(163, 190, 140),
    QColor(180, 142, 173),
    QColor(235, 203, 139),
    QColor(136, 192, 208),
]
DEVIATION_FRACTION = 0.05
PLOT_POINTS = 6000


class RunController(QObject):
    """Own one executor and move its callbacks onto the GUI thread."""

    status_changed = Signal(object)
    sample_received = Signal(object)
    finished = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._executor: Executor | None = None
        self._statuses: queue.SimpleQueue[Status] = queue.SimpleQueue()
        self._samples: queue.SimpleQueue[Sample] = queue.SimpleQueue()
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
        while True:
            try:
                sample = self._samples.get_nowait()
            except queue.Empty:
                break
            self.sample_received.emit(sample)
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
    """The whole run as one bar: trials colored by type, steps as ticks, a cursor."""

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
                self._colors[name] = TRIAL_COLORS[len(self._colors) % len(TRIAL_COLORS)]
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
        painter.setPen(QPen(QColor(120, 120, 120)))
        painter.setBrush(QColor(245, 245, 245))
        painter.drawRect(area)
        if self._total <= 0 or not self._segments:
            painter.drawText(self.rect(), Qt.AlignCenter, "No run planned.")
            return
        scale = area.width() / self._total
        for index, (start, duration, name, boundaries) in enumerate(self._segments):
            left = area.left() + start * scale
            width = max(1.0, duration * scale)
            color = QColor(self._colors[name])
            if self._current is not None and index != self._current:
                color.setAlpha(140)
            painter.setPen(QPen(QColor(255, 255, 255)))
            painter.setBrush(color)
            painter.drawRect(QRectF(left, area.top(), width, area.height()))
            painter.setPen(QPen(QColor(255, 255, 255, 180)))
            for boundary in boundaries:
                x = area.left() + boundary * scale
                painter.drawLine(int(x), area.top() + 4, int(x), area.bottom() - 4)
        cursor_x = area.left() + self._cursor * scale
        painter.setPen(QPen(QColor(20, 20, 20), 2))
        painter.drawLine(int(cursor_x), area.top() - 6, int(cursor_x), area.bottom() + 6)
        painter.setPen(QPen(QColor(60, 60, 60)))
        legend = "   ".join(f"■ {name}" for name in self._colors)
        painter.drawText(
            self.rect().adjusted(2, 0, -2, -2),
            Qt.AlignBottom | Qt.AlignLeft,
            f"{legend}    total {self._total:.1f} s",
        )


class MfcPlot(QWidget):
    """Commanded versus actual flow for every MFC, with a deviation indicator."""

    def __init__(self, rig: RigMap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rig = rig
        self._plot = pg.PlotWidget(background="w")
        self._plot.addLegend(offset=(10, 10))
        self._plot.setLabel("bottom", "run time", units="s")
        self._plot.setLabel("left", "mass flow")
        self._plot.showGrid(x=True, y=True, alpha=0.2)
        self._commanded: dict[str, Any] = {}
        self._actual: dict[str, Any] = {}
        self._history: dict[str, deque[tuple[float, float | None, float | None]]] = {}
        self._labels: dict[str, QLabel] = {}
        labels = QVBoxLayout()
        for index, name in enumerate(rig.mfcs):
            color = TRIAL_COLORS[index % len(TRIAL_COLORS)]
            self._commanded[name] = self._plot.plot(
                pen=pg.mkPen(color, width=2, style=Qt.DashLine), name=f"{name} commanded"
            )
            self._actual[name] = self._plot.plot(
                pen=pg.mkPen(color, width=2), name=f"{name} actual"
            )
            self._history[name] = deque(maxlen=PLOT_POINTS)
            label = QLabel(f"{name}: no reading yet")
            self._labels[name] = label
            labels.addWidget(label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
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
        label.setText(
            f"{sample.mfc}: commanded {sample.commanded_setpoint:.2f}, "
            f"actual {sample.mass_flow:.2f} {unit}, deviation {deviation:+.2f} {unit}"
        )
        if abs(deviation) > limit:
            label.setStyleSheet("color: #9b1c1c; font-weight: bold;")
        else:
            label.setStyleSheet("color: #1c7a3a;")


class StatusPanel(QWidget):
    """Current trial, highlighted current step, elapsed and remaining time, commanded state."""

    def __init__(self, rig: RigMap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rig = rig
        self._recipe: Recipe | None = None
        self._phase = QLabel("idle")
        self._phase.setStyleSheet("font-size: 16px; font-weight: bold;")
        self._message = QLabel("")
        self._message.setWordWrap(True)
        self._trial = QLabel("—")
        self._time = QLabel("—")
        self._directory = QLabel("—")
        self._directory.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._valves = QLabel("—")
        self._valves.setWordWrap(True)
        self._setpoints = QLabel("—")
        self._setpoints.setWordWrap(True)
        self._readback = QLabel("—")
        self._readback.setWordWrap(True)
        self._latest: dict[str, Sample] = {}

        self._steps = QTableWidget(0, 3)
        self._steps.setHorizontalHeaderLabels(["Step", "Duration (s)", "State"])
        self._steps.horizontalHeader().setStretchLastSection(True)
        self._steps.verticalHeader().setVisible(False)
        self._steps.setSelectionMode(QAbstractItemView.NoSelection)
        self._steps.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._steps.setMaximumHeight(160)
        self._steps_trial: str | None = None

        form = QFormLayout()
        form.addRow("Phase", self._phase)
        form.addRow("Message", self._message)
        form.addRow("Trial", self._trial)
        form.addRow("Time", self._time)
        form.addRow("Run directory", self._directory)
        form.addRow("Commanded valves", self._valves)
        form.addRow("Commanded setpoints", self._setpoints)
        form.addRow("Live readback", self._readback)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(form)
        layout.addWidget(QLabel("Steps of the current trial"))
        layout.addWidget(self._steps)

    def set_recipe(self, recipe: Recipe | None) -> None:
        self._recipe = recipe
        self._steps_trial = None
        self._steps.setRowCount(0)
        self._latest = {}

    def show_status(self, status: Status, elapsed: float) -> None:
        self._phase.setText(status.phase.value)
        self._message.setText(status.message)
        if status.run_directory is not None:
            self._directory.setText(str(status.run_directory))
        total = len(status.order)
        if status.trial_index is None or total == 0:
            self._trial.setText("—")
        else:
            self._trial.setText(f"{status.trial_index + 1} of {total}: {status.trial_name}")
        self._valves.setText(
            ", ".join(
                f"{name} {'open' if state else 'closed'}" for name, state in status.valves.items()
            )
            or "—"
        )
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
                            QColor(255, 236, 170)
                            if row == status.step_index
                            else QColor(255, 255, 255)
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
                self._steps.setItem(row, 2, QTableWidgetItem(state or "all valves closed"))

    def show_sample(self, sample: Sample) -> None:
        self._latest[sample.mfc] = sample
        parts = []
        for name, latest in self._latest.items():
            unit = self._rig.mfcs[name].flow_unit if name in self._rig.mfcs else ""
            actual = "?" if latest.mass_flow is None else f"{latest.mass_flow:.2f}"
            device = "?" if latest.device_setpoint is None else f"{latest.device_setpoint:.2f}"
            parts.append(f"{name} flow {actual} {unit} (device setpoint {device})")
        self._readback.setText("; ".join(parts) or "—")


class RunView(QWidget):
    """The Run tab body: status, timeline, and MFC plot."""

    def __init__(self, rig: RigMap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.status_panel = StatusPanel(rig)
        self.timeline = TimelineWidget()
        self.plot = MfcPlot(rig)
        status_box = QGroupBox("Status")
        QVBoxLayout(status_box).addWidget(self.status_panel)
        timeline_box = QGroupBox("Run timeline")
        QVBoxLayout(timeline_box).addWidget(self.timeline)
        plot_box = QGroupBox("MFC commanded versus actual")
        QVBoxLayout(plot_box).addWidget(self.plot)
        top = QHBoxLayout()
        top.addWidget(status_box, stretch=1)
        top.addWidget(plot_box, stretch=1)
        layout = QVBoxLayout(self)
        layout.addLayout(top, stretch=1)
        layout.addWidget(timeline_box)

    def prepare(self, recipe: Recipe) -> None:
        self.status_panel.set_recipe(recipe)
        self.timeline.clear()
        self.plot.clear()

    def show_status(self, status: Status, elapsed: float, recipe: Recipe | None) -> None:
        if recipe is not None and status.order and status.phase == Phase.STARTING:
            self.timeline.set_plan(recipe, status.order)
        self.status_panel.show_status(status, elapsed)
        self.timeline.set_progress(elapsed, status.trial_index)

    def tick(self, status: Status, elapsed: float) -> None:
        self.status_panel.show_time(status, elapsed)
        self.timeline.set_progress(elapsed, status.trial_index)
