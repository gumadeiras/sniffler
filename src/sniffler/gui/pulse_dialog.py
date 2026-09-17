"""Generate a pulse train as explicit, editable step rows."""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QWidget,
)

from sniffler.gui.step_table import StepRow


def pulse_train(
    template: StepRow,
    valve: str,
    pulse_seconds: float,
    gap_seconds: float,
    count: int,
    end_with_gap: bool,
) -> list[StepRow]:
    """Expand a pulse train into rows. Every other valve and setpoint copies the template."""
    rows: list[StepRow] = []
    for index in range(count):
        pulse = template.copy()
        pulse.duration = pulse_seconds
        pulse.valves[valve] = True
        rows.append(pulse)
        if index < count - 1 or end_with_gap:
            gap = template.copy()
            gap.duration = gap_seconds
            gap.valves[valve] = False
            rows.append(gap)
    return rows


class PulseTrainDialog(QDialog):
    """Ask for the pulse parameters. The result is a list of ordinary step rows."""

    def __init__(self, valves: list[str], template: StepRow, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Pulse train")
        self._template = template

        self._valve = QComboBox()
        self._valve.addItems(valves)
        self._pulse = QDoubleSpinBox()
        self._pulse.setRange(0.001, 3600.0)
        self._pulse.setDecimals(3)
        self._pulse.setValue(0.1)
        self._pulse.setSuffix(" s")
        self._gap = QDoubleSpinBox()
        self._gap.setRange(0.001, 3600.0)
        self._gap.setDecimals(3)
        self._gap.setValue(0.1)
        self._gap.setSuffix(" s")
        self._count = QSpinBox()
        self._count.setRange(1, 10000)
        self._count.setValue(5)
        self._end_with_gap = QCheckBox("Gap after the last pulse")
        self._end_with_gap.setChecked(True)

        form = QFormLayout(self)
        form.addRow(
            QLabel(
                "Each pulse and each gap becomes one step row. "
                "The other valves and the target flows copy the selected step."
            )
        )
        form.addRow("Valve", self._valve)
        form.addRow("Pulse", self._pulse)
        form.addRow("Gap", self._gap)
        form.addRow("Pulses", self._count)
        form.addRow(self._end_with_gap)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def rows(self) -> list[StepRow]:
        return pulse_train(
            self._template,
            self._valve.currentText(),
            self._pulse.value(),
            self._gap.value(),
            self._count.value(),
            self._end_with_gap.isChecked(),
        )
