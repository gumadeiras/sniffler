"""The whole run as one picture: a trial bar, one lane per valve, and a pink cursor."""

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
    """Trials shaded by type with step ticks; below, when each valve is planned open."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._segments: list[tuple[float, float, str, list[float]]] = []
        self._colors: dict[str, QColor] = {}
        self._lanes: dict[str, list[tuple[float, float]]] = {}
        self.closed_all_run: list[str] = []
        self._total = 0.0
        self._cursor = 0.0
        self._current: int | None = None
        self._fit()

    def lanes(self) -> dict[str, list[tuple[float, float]]]:
        """Planned open intervals in run seconds, for every valve that opens at least once."""
        return {name: list(intervals) for name, intervals in self._lanes.items()}

    def set_plan(self, recipe: Recipe, order: tuple[str, ...]) -> None:
        self._segments = []
        self._colors = {}
        intervals: dict[str, list[tuple[float, float]]] = {}
        start = 0.0
        for name in order:
            trial = recipe.trial(name)
            boundaries: list[float] = []
            offset = start
            for step in trial.steps:
                duration = step.duration_seconds or 0.0
                for valve, is_open in step.valves.items():
                    lane = intervals.setdefault(valve, [])
                    if not is_open:
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
        self._lanes = {name: lane for name, lane in intervals.items() if lane}
        self.closed_all_run = [name for name, lane in intervals.items() if not lane]
        self._total = start
        self._cursor = 0.0
        self._current = None
        self._fit()
        self.update()

    def set_progress(self, elapsed_seconds: float, trial_index: int | None) -> None:
        self._cursor = max(0.0, min(elapsed_seconds, self._total))
        self._current = trial_index
        self.update()

    def clear(self) -> None:
        self._segments = []
        self._lanes = {}
        self.closed_all_run = []
        self._total = 0.0
        self._cursor = 0.0
        self._current = None
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
            widest = max(metrics.horizontalAdvance(name) for name in self._lanes)
            label_width = min(LABEL_MAX_PX, widest) + theme.GAP
        bar = QRectF(
            self.rect().left() + 1 + label_width, TOP_PAD, self.width() - 2 - label_width, BAR_PX
        )
        painter.setPen(QPen(QColor(theme.LINE)))
        painter.setBrush(QColor(theme.PANEL))
        painter.drawRect(bar)
        if self._total <= 0 or not self._segments:
            painter.setPen(QColor(theme.INK_SOFT))
            painter.drawText(bar, Qt.AlignCenter, "No run planned.")
            return
        scale = bar.width() / self._total
        self._paint_trials(painter, bar, scale)
        bottom = self._paint_lanes(painter, bar, scale, label_width, metrics)
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
            text = metrics.elidedText(name, Qt.ElideRight, label_width - theme.GAP)
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
        painter.setPen(QPen(QColor(theme.INK_SOFT)))
        if self.closed_all_run:
            painter.drawText(x, baseline, "closed all run: " + ", ".join(self.closed_all_run))
        painter.drawText(
            self.rect().adjusted(2, 0, -2, -2),
            Qt.AlignBottom | Qt.AlignRight,
            f"total {self._total:.1f} s",
        )
