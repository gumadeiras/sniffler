"""The whole run as one picture: a trial bar, sync marks, one lane per valve, a pink cursor."""

from collections.abc import Iterable

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

from sniffler.gui import theme
from sniffler.recipe import Recipe

TOP_PAD = theme.UNIT
BAR_PX = 28
LANE_PX = 14
LEGEND_PX = 20
LABEL_MAX_PX = 160


class TimelineWidget(QWidget):
    """Trials shaded by type with step ticks; below, one lane per valve the recipe opens.

    The lanes follow the recipe, not the run: they exist, empty, as soon as the recipe
    names the valves it opens, so the widget has the same height before, during, and
    after a run. Only a recipe edit changes it.
    """

    def __init__(self, valves: Iterable[str] = (), parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._valves = list(valves)
        self._segments: list[tuple[float, float, str, list[float]]] = []
        self._colors: dict[str, QColor] = {}
        self._lanes: dict[str, list[tuple[float, float]]] = {name: [] for name in self._valves}
        self._labels: dict[str, str] = {}  # lane text by valve name; the name when absent
        self._total = 0.0
        self._cursor = 0.0
        self._current: int | None = None
        self._marks: list[float] = []
        self._fit()

    def add_mark(self, schedule_seconds: float) -> None:
        """Mark a sync pulse at its time on the trial schedule."""
        self._marks.append(schedule_seconds)
        self.update()

    def set_valves(self, valves: Iterable[str]) -> None:
        """Choose the lanes. Existing intervals are kept for valves that stay."""
        names = list(valves)
        if names == self._valves:
            return
        self._valves = names
        self._lanes = {name: self._lanes.get(name, []) for name in names}
        self._fit()
        self.update()

    def set_labels(self, labels: dict[str, str]) -> None:
        """Show these texts on the lanes instead of the valve names."""
        if labels == self._labels:
            return
        self._labels = dict(labels)
        self.update()

    def label(self, name: str) -> str:
        return self._labels.get(name, name)

    def lanes(self) -> dict[str, list[tuple[float, float]]]:
        """Planned open intervals in run seconds for every rig valve; empty when never open."""
        return {name: list(intervals) for name, intervals in self._lanes.items()}

    def set_plan(self, recipe: Recipe, order: tuple[str, ...]) -> None:
        self._segments = []
        self._colors = {}
        intervals: dict[str, list[tuple[float, float]]] = {name: [] for name in self._valves}
        start = 0.0
        for name in order:
            trial = recipe.trial(name)
            boundaries: list[float] = []
            offset = start
            for step in trial.steps:
                duration = step.duration_seconds or 0.0
                for valve, is_open in step.valves.items():
                    lane = intervals.get(valve)
                    if lane is None or not is_open:
                        continue
                    if lane and lane[-1][1] == offset:
                        lane[-1] = (lane[-1][0], offset + duration)
                    else:
                        lane.append((offset, offset + duration))
                offset += duration
                if step is not trial.steps[-1]:
                    boundaries.append(offset)
            self._segments.append((start, trial.duration_seconds, name, boundaries))
            if name not in self._colors:
                self._colors[name] = QColor(theme.TRIAL_RAMP[len(self._colors) % 6])
            start += trial.duration_seconds
        self._lanes = intervals
        self._total = start
        self._cursor = 0.0
        self._current = None
        # Marks are not cleared here: a run's first pulses can reach the window in
        # the same tick as its plan. ``clear`` at run start removes the old ones.
        self._fit()
        self.update()

    def set_progress(self, elapsed_seconds: float, trial_index: int | None) -> None:
        self._cursor = max(0.0, min(elapsed_seconds, self._total))
        self._current = trial_index
        self.update()

    def clear(self) -> None:
        self._segments = []
        self._lanes = {name: [] for name in self._valves}
        self._total = 0.0
        self._cursor = 0.0
        self._current = None
        self._marks = []
        self._fit()
        self.update()

    def _fit(self) -> None:
        height = TOP_PAD + BAR_PX + LEGEND_PX + theme.UNIT
        if self._lanes:
            height += theme.UNIT // 2 + LANE_PX * len(self._lanes)
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)

    # Paint ---------------------------------------------------------------

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        metrics = painter.fontMetrics()
        label_width = 0
        if self._lanes:
            widest = max(metrics.horizontalAdvance(self.label(name)) for name in self._lanes) + 4
            label_width = min(LABEL_MAX_PX, widest) + theme.GAP
        bar = QRectF(
            self.rect().left() + 1 + label_width, TOP_PAD, self.width() - 2 - label_width, BAR_PX
        )
        painter.setPen(QPen(QColor(theme.LINE)))
        painter.setBrush(QColor(theme.PANEL))
        painter.drawRect(bar)
        planned = self._total > 0 and bool(self._segments)
        scale = bar.width() / self._total if planned else 0.0
        if planned:
            self._paint_trials(painter, bar, scale)
        else:
            painter.setPen(QColor(theme.INK_SOFT))
            painter.drawText(bar, Qt.AlignCenter, "No run planned.")
        bottom = self._paint_lanes(painter, bar, scale, label_width, metrics)
        if not planned:
            return
        painter.setPen(QPen(QColor(theme.NAVY), 2))
        for mark in self._marks:
            if 0.0 <= mark <= self._total:
                x = int(bar.left() + mark * scale)
                painter.drawLine(x, int(bar.top()) - 6, x, int(bar.top()) - 1)
        cursor_x = bar.left() + self._cursor * scale
        painter.setPen(QPen(QColor(theme.PINK), 2))
        painter.drawLine(int(cursor_x), int(bar.top()) - 6, int(cursor_x), bottom + 2)
        self._paint_legend(painter, metrics)

    def _paint_trials(self, painter: QPainter, bar: QRectF, scale: float) -> None:
        for index, (start, duration, name, boundaries) in enumerate(self._segments):
            left = bar.left() + start * scale
            width = max(1.0, duration * scale)
            color = QColor(self._colors[name])
            if self._current is not None and index != self._current:
                color.setAlpha(150)
            painter.setPen(QPen(QColor(theme.PANEL)))
            painter.setBrush(color)
            painter.drawRect(QRectF(left, bar.top(), width, bar.height()))
            for boundary in boundaries:
                x = bar.left() + boundary * scale
                painter.drawLine(int(x), int(bar.top()) + 4, int(x), int(bar.bottom()) - 4)

    def _paint_lanes(self, painter, bar: QRectF, scale: float, label_width: int, metrics) -> int:
        """Draw one lane per valve under the bar. Return the bottom y of the last lane."""
        top = int(bar.bottom()) + theme.UNIT // 2
        for name, intervals in self._lanes.items():
            lane = QRectF(bar.left(), top, bar.width(), LANE_PX)
            painter.setPen(QPen(QColor(theme.LINE)))
            painter.setBrush(QColor(theme.PANEL))
            painter.drawRect(lane)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.NAVY))
            for start, end in intervals:
                painter.drawRect(
                    QRectF(
                        bar.left() + start * scale,
                        lane.top() + 1,
                        max(1.0, (end - start) * scale),
                        lane.height() - 2,
                    )
                )
            painter.setPen(QPen(QColor(theme.NAVY)))
            text = metrics.elidedText(self.label(name), Qt.ElideRight, label_width - theme.GAP)
            painter.drawText(
                QRectF(self.rect().left() + 1, top, label_width - theme.GAP, LANE_PX),
                Qt.AlignLeft | Qt.AlignVCenter,
                text,
            )
            top += LANE_PX
        return top

    def _paint_legend(self, painter: QPainter, metrics) -> None:
        x = self.rect().left() + 2
        baseline = self.rect().bottom() - 4
        square = metrics.ascent() - 2
        for name, color in self._colors.items():
            painter.fillRect(x, baseline - square, square, square, color)
            painter.setPen(QPen(QColor(theme.NAVY)))
            painter.drawText(x + square + 4, baseline, name)
            x += square + 4 + metrics.horizontalAdvance(name) + theme.SECTION_GAP
        if self._marks:
            painter.setPen(QPen(QColor(theme.NAVY), 2))
            painter.drawLine(x + 2, baseline - square, x + 2, baseline)
            painter.setPen(QPen(QColor(theme.NAVY)))
            painter.drawText(x + 8, baseline, "sync pulse")
        painter.setPen(QPen(QColor(theme.INK_SOFT)))
        painter.drawText(
            self.rect().adjusted(2, 0, -2, -2),
            Qt.AlignBottom | Qt.AlignRight,
            f"total {self._total:.1f} s",
        )
