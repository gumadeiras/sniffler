"""An editable table of steps with per-cell validation against the rig map."""

from typing import Any

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractTableModel,
    QEvent,
    QLocale,
    QModelIndex,
    QObject,
    QRectF,
    Qt,
)
from PySide6.QtGui import QBrush, QColor, QDoubleValidator, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QLineEdit,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from sniffler.gui import theme
from sniffler.recipe import RigMap, Step, duration_problem, setpoint_problem

# An invalid cell sits on the cream from the artwork; pink stays for live attention.
PROBLEM_BRUSH = QBrush(QColor(theme.CREAM))


class StepRow:
    """One editable step. Values stay as entered so a problem can be shown."""

    def __init__(
        self, duration: float | None, valves: dict[str, bool], setpoints: dict[str, float]
    ):
        self.duration = duration
        self.valves = dict(valves)
        self.setpoints = dict(setpoints)

    @classmethod
    def from_step(cls, step: Step) -> "StepRow":
        return cls(step.duration_seconds, step.valves, step.setpoints)

    def copy(self) -> "StepRow":
        return StepRow(self.duration, self.valves, self.setpoints)

    def to_step(self, rig: RigMap, with_duration: bool) -> Step:
        """Build the step. Names that are not in the rig stay, so validation can refuse them."""
        valves = {name: bool(self.valves.get(name, False)) for name in rig.valves}
        valves.update({name: bool(state) for name, state in self.valves.items()})
        setpoints = {name: self.setpoints.get(name, 0.0) for name in rig.mfcs}
        setpoints.update(self.setpoints)
        return Step(self.duration if with_duration else None, valves, setpoints)


class StepTableModel(QAbstractTableModel):
    """Rows are steps. Columns are the duration, then every valve, then every MFC."""

    def __init__(self, rig: RigMap, with_duration: bool = True, parent: QObject | None = None):
        super().__init__(parent)
        self._rig = rig
        self._with_duration = with_duration
        self._rows: list[StepRow] = []
        self._valves = list(rig.valves)
        self._mfcs = list(rig.mfcs)

    @property
    def rig(self) -> RigMap:
        return self._rig

    def set_rig(self, rig: RigMap) -> None:
        """Replace the rig map, for example after the device full scale was read."""
        self.beginResetModel()
        self._rig = rig
        self._valves = list(rig.valves)
        self._mfcs = list(rig.mfcs)
        self.endResetModel()

    def rows(self) -> list[StepRow]:
        return self._rows

    def set_rows(self, rows: list[StepRow]) -> None:
        self.beginResetModel()
        self._rows = [row.copy() for row in rows]
        self.endResetModel()

    def steps(self) -> list[Step]:
        return [row.to_step(self._rig, self._with_duration) for row in self._rows]

    def blank_row(self) -> StepRow:
        template = self._rows[-1] if self._rows else None
        if template is not None:
            row = template.copy()
            row.duration = template.duration if self._with_duration else None
            return row
        return StepRow(
            1.0 if self._with_duration else None,
            dict.fromkeys(self._valves, False),
            dict.fromkeys(self._mfcs, 0.0),
        )

    def insert_rows(self, position: int, rows: list[StepRow]) -> None:
        if not rows:
            return
        position = max(0, min(position, len(self._rows)))
        self.beginInsertRows(QModelIndex(), position, position + len(rows) - 1)
        self._rows[position:position] = [row.copy() for row in rows]
        self.endInsertRows()

    def remove_row(self, position: int) -> None:
        if 0 <= position < len(self._rows):
            self.beginRemoveRows(QModelIndex(), position, position)
            del self._rows[position]
            self.endRemoveRows()

    def move_row(self, position: int, offset: int) -> int:
        target = position + offset
        if not (0 <= position < len(self._rows) and 0 <= target < len(self._rows)):
            return position
        destination = target + 1 if offset > 0 else target
        self.beginMoveRows(QModelIndex(), position, position, QModelIndex(), destination)
        self._rows.insert(target, self._rows.pop(position))
        self.endMoveRows()
        return target

    # Column layout -----------------------------------------------------

    def _duration_column(self) -> int | None:
        return 0 if self._with_duration else None

    def _valve_at(self, column: int) -> str | None:
        offset = 1 if self._with_duration else 0
        index = column - offset
        return self._valves[index] if 0 <= index < len(self._valves) else None

    def _mfc_at(self, column: int) -> str | None:
        offset = (1 if self._with_duration else 0) + len(self._valves)
        index = column - offset
        return self._mfcs[index] if 0 <= index < len(self._mfcs) else None

    def is_valve_column(self, column: int) -> bool:
        return self._valve_at(column) is not None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid():
            return 0
        return (1 if self._with_duration else 0) + len(self._valves) + len(self._mfcs)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Vertical:
            return section + 1
        if section == self._duration_column():
            return "Duration (s)"
        if (valve := self._valve_at(section)) is not None:
            return valve
        if (mfc := self._mfc_at(section)) is not None:
            return f"{mfc}\n{self._rig.mfcs[mfc].flow_unit}"
        return None

    # Validation --------------------------------------------------------

    def problem(self, row: int, column: int) -> str | None:
        """Return why a cell is not allowed, or None when it is."""
        step = self._rows[row]
        if column == self._duration_column():
            return duration_problem(step.duration)
        if (mfc := self._mfc_at(column)) is not None:
            return setpoint_problem(step.setpoints.get(mfc), self._rig.mfcs[mfc])
        return None

    def problems(self) -> list[tuple[int, str]]:
        found: list[tuple[int, str]] = []
        for row in range(len(self._rows)):
            for column in range(self.columnCount()):
                problem = self.problem(row, column)
                if problem is not None:
                    header = str(self.headerData(column, Qt.Horizontal)).replace("\n", " ")
                    found.append((row, f"{header}: {problem}"))
        return found

    # Qt data interface -------------------------------------------------

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if self.is_valve_column(index.column()):
            return base | Qt.ItemIsUserCheckable
        return base | Qt.ItemIsEditable

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        step = self._rows[index.row()]
        column = index.column()
        valve = self._valve_at(column)
        if valve is not None:
            state = bool(step.valves.get(valve, False))
            if role == Qt.CheckStateRole:
                return Qt.Checked if state else Qt.Unchecked
            if role == Qt.DisplayRole:
                return "open" if state else "closed"
            return None
        if role == Qt.TextAlignmentRole:
            return Qt.AlignCenter
        if role in {Qt.DisplayRole, Qt.EditRole}:
            if column == self._duration_column():
                value = step.duration
            else:
                value = step.setpoints.get(self._mfc_at(column) or "")
            if value is None:
                return ""
            return f"{value:g}" if role == Qt.DisplayRole else str(value)
        problem = self.problem(index.row(), column)
        if role == Qt.BackgroundRole and problem is not None:
            return PROBLEM_BRUSH
        if role == Qt.ToolTipRole:
            return problem
        return None

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.EditRole) -> bool:
        if not index.isValid():
            return False
        step = self._rows[index.row()]
        column = index.column()
        valve = self._valve_at(column)
        if valve is not None:
            if role != Qt.CheckStateRole:
                return False
            step.valves[valve] = Qt.CheckState(value) == Qt.Checked
        elif role == Qt.EditRole:
            number = _parse_number(value)
            if number is None:
                return False
            if column == self._duration_column():
                step.duration = number
            else:
                step.setpoints[self._mfc_at(column) or ""] = number
        else:
            return False
        self.dataChanged.emit(index, index, [role, Qt.DisplayRole, Qt.BackgroundRole])
        return True


def _parse_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return None


class StepDelegate(QStyledItemDelegate):
    """Numbers only in duration and setpoint cells; one click anywhere toggles a valve cell.

    A valve cell is painted here: a navy check box and the word open or closed,
    centered as one group, the same on every platform and visible on a selected row.
    """

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        model = index.model()
        if not (isinstance(model, StepTableModel) and model.is_valve_column(index.column())):
            super().paint(painter, option, index)
            return
        style = option.widget.style() if option.widget else None
        if style is not None:
            style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter, option.widget)
        selected = bool(option.state & QStyle.State_Selected)
        ink = QColor(theme.PANEL if selected else theme.NAVY)
        fill = QColor(theme.NAVY if selected else theme.PANEL)
        is_open = model.data(index, Qt.CheckStateRole) == Qt.Checked
        text = "open" if is_open else "closed"
        metrics = option.fontMetrics
        gap = theme.UNIT // 2
        group = theme.CHECK_PX + gap + metrics.horizontalAdvance("closed")
        left = option.rect.center().x() - group / 2
        box = QRectF(
            left, option.rect.center().y() - theme.CHECK_PX / 2, theme.CHECK_PX, theme.CHECK_PX
        )
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(ink, 1.5))
        painter.setBrush(ink if is_open else fill)
        painter.drawRoundedRect(box, 3, 3)
        if is_open:
            mark = QPainterPath()
            mark.moveTo(box.left() + box.width() * 0.22, box.top() + box.height() * 0.52)
            mark.lineTo(box.left() + box.width() * 0.42, box.top() + box.height() * 0.72)
            mark.lineTo(box.left() + box.width() * 0.80, box.top() + box.height() * 0.30)
            painter.setPen(QPen(fill, 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(mark)
        painter.setPen(ink)
        painter.setFont(option.font)
        painter.drawText(
            QRectF(box.right() + gap, option.rect.top(), group, option.rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            text,
        )
        painter.restore()

    def editorEvent(
        self,
        event: QEvent,
        model: QAbstractItemModel,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> bool:
        if isinstance(model, StepTableModel) and model.is_valve_column(index.column()):
            toggled = (
                event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton
            ) or (event.type() == QEvent.KeyPress and event.key() in {Qt.Key_Space, Qt.Key_Select})
            if toggled:
                current = model.data(index, Qt.CheckStateRole)
                target = Qt.Unchecked if current == Qt.Checked else Qt.Checked
                return model.setData(index, target, Qt.CheckStateRole)
            if event.type() in {QEvent.MouseButtonPress, QEvent.MouseButtonDblClick}:
                return True
        return super().editorEvent(event, model, option, index)

    def createEditor(
        self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex
    ) -> QWidget:
        editor = QLineEdit(parent)
        validator = QDoubleValidator(-1e9, 1e9, 6, editor)
        validator.setNotation(QDoubleValidator.StandardNotation)
        validator.setLocale(QLocale.c())
        editor.setValidator(validator)
        return editor
