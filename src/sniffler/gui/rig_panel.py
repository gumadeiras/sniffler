"""The Config tab: the name to channel map from lab.toml, read-only."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sniffler import hardware
from sniffler.gui import theme
from sniffler.gui.icons import icon
from sniffler.recipe import RigMap


class RigPanel(QWidget):
    """The name to channel map from lab.toml, read-only."""

    def __init__(
        self, rig: RigMap, runs_directory: Path | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Name", "Kind", "Hardware", "Limits"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setWordWrap(True)
        self.read_limits = QPushButton(icon("read-limits"), "Read device limits")
        self.read_limits.setToolTip(
            "Read the device maximum of every MFC. This command changes no output."
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.MARGIN)
        layout.setSpacing(theme.GAP)
        source = QLabel("The devices, read from lab.toml. Edit that file to change them.")
        layout.addWidget(source)
        layout.addWidget(self._table, stretch=1)
        layout.addWidget(self.read_limits, alignment=Qt.AlignLeft)
        if runs_directory is not None:
            runs = QLabel(
                f"Runs are saved in {runs_directory}. Each run gets its own new folder; "
                "nothing is overwritten. Set [runs] directory in lab.toml to move them."
            )
            runs.setWordWrap(True)
            runs.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(runs)
        self.show_rig(rig)

    def show_rig(self, rig: RigMap) -> None:
        rows: list[tuple[str, str, str, str]] = []
        for name, channel in rig.valves.items():
            line = hardware.digital_channel_name(channel)
            rows.append((name, "valve", f"channel {channel} ({line})", "—"))
        if rig.trigger is not None:
            trigger = rig.trigger
            line = hardware.digital_channel_name(trigger.channel)
            timeout = trigger.timeout_seconds
            rows.append(
                (
                    "TTL trigger",
                    "input",
                    f"channel {trigger.channel} ({line})",
                    "no time limit" if timeout is None else f"timeout {timeout:g} s",
                )
            )
        if rig.ttl_output is not None:
            output = rig.ttl_output
            line = hardware.digital_channel_name(output.channel)
            rows.append(
                (
                    "TTL output",
                    "output",
                    f"channel {output.channel} ({line})",
                    "high from the first trial to the end state"
                    if output.holds_high
                    else f"one pulse of {pulse_text(output.pulse_seconds)} at the first trial",
                )
            )
        for name, mfc in rig.mfcs.items():
            limits = [f"minimum {mfc.minimum_flow:g}"]
            if mfc.maximum_flow is not None:
                limits.append(f"lab.toml maximum {mfc.maximum_flow:g}")
            limits.append(
                "device maximum not read yet"
                if mfc.full_scale is None
                else f"device maximum {mfc.full_scale:g}"
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
        self._table.resizeRowsToContents()


def pulse_text(seconds: float) -> str:
    return f"{seconds * 1000:g} ms" if seconds < 1.0 else f"{seconds:g} s"
