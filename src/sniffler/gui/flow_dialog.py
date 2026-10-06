"""Set one MFC to one target flow in every step of a trial or of all trials."""

from collections import Counter

from PySide6.QtCore import QLocale
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QWidget,
)

from sniffler.gui.step_table import StepRow
from sniffler.recipe import RigMap, setpoint_problem


class FlowDialog(QDialog):
    """Ask for the MFC, the flow, and the trials. The dialog shows the flows it replaces."""

    def __init__(
        self,
        rig: RigMap,
        trials: dict[str, list[StepRow]],
        current: str,
        template: StepRow,
        mfc: str | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Set flow")
        self._rig = rig
        self._trials = trials
        self._current = current
        self._template = template

        self._mfc = QComboBox()
        self._mfc.addItems(list(rig.mfcs))
        self._mfc.setAccessibleName("MFC")
        self._flow = QLineEdit()
        self._flow.setAccessibleName("Target flow")
        validator = QDoubleValidator(-1e9, 1e9, 6, self._flow)
        validator.setNotation(QDoubleValidator.StandardNotation)
        validator.setLocale(QLocale.c())
        self._flow.setValidator(validator)
        self._unit = QLabel()
        self._this_trial = QRadioButton(current)
        self._this_trial.setChecked(True)
        self._all_trials = QRadioButton(f"all {len(trials)} trials")
        self._now = QLabel()
        self._problem = QLabel()
        self._problem.setWordWrap(True)
        self._buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._buttons.button(QDialogButtonBox.Ok).setText("Set flow")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        flow = QHBoxLayout()
        flow.addWidget(self._flow)
        flow.addWidget(self._unit)
        scope = QHBoxLayout()
        scope.addWidget(self._this_trial)
        scope.addWidget(self._all_trials)
        scope.addStretch(1)
        form = QFormLayout(self)
        form.addRow(QLabel("Every step gets this flow. The end state does not change."))
        form.addRow("MFC", self._mfc)
        form.addRow("Flow", flow)
        form.addRow("Trials", scope)
        form.addRow("Now", self._now)
        form.addRow(self._problem)
        form.addRow(self._buttons)

        if mfc in rig.mfcs:
            self._mfc.setCurrentText(mfc)
        self._mfc.currentTextChanged.connect(self._on_mfc_changed)
        self._flow.textChanged.connect(self._refresh)
        self._all_trials.toggled.connect(self._refresh)
        self._on_mfc_changed(self._mfc.currentText())

    def mfc(self) -> str:
        return self._mfc.currentText()

    def flow(self) -> float | None:
        try:
            return float(self._flow.text())
        except ValueError:
            return None

    def all_trials(self) -> bool:
        return self._all_trials.isChecked()

    def _on_mfc_changed(self, mfc: str) -> None:
        self._unit.setText(self._rig.mfcs[mfc].flow_unit)
        self._flow.setText(f"{self._template.setpoints.get(mfc, 0.0):g}")
        self._refresh()

    def _refresh(self, *_arguments) -> None:
        mfc = self.mfc()
        unit = self._rig.mfcs[mfc].flow_unit
        names = self._trials if self.all_trials() else [self._current]
        flows = Counter(row.setpoints.get(mfc, 0.0) for name in names for row in self._trials[name])
        self._now.setText(
            ", ".join(
                f"{value:g} {unit} in {count} step{'s' if count != 1 else ''}"
                for value, count in flows.most_common()
            )
            or "no steps"
        )
        problem = setpoint_problem(self.flow(), self._rig.mfcs[mfc])
        self._problem.setText(problem or "")
        self._problem.setVisible(problem is not None)
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(problem is None)
