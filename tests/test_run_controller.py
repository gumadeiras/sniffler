"""The run controller moves executor callbacks onto the GUI thread in a safe order."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError as error:  # pragma: no cover - depends on the platform libraries
    QApplication = None  # type: ignore[assignment]
    IMPORT_ERROR = str(error)
else:
    IMPORT_ERROR = ""

from sniffler.config import AlicatSettings, Settings, TriggerSettings
from sniffler.executor import Event, Phase, Status
from sniffler.recipe import rig_map_from_settings

RIG = rig_map_from_settings(
    Settings(
        alicats={"mfc-500": AlicatSettings(port="/dev/mfc-500")},
        valves={"odor-1": 8},
        trigger=TriggerSettings(4),
    )
)


@unittest.skipIf(QApplication is None, f"PySide6 is not usable here: {IMPORT_ERROR}")
class DrainOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_a_pulse_queued_with_the_running_status_is_marked_on_the_schedule(self) -> None:
        from sniffler.gui.run_view import RunController, RunView

        controller = RunController()
        view = RunView(RIG)
        controller.status_changed.connect(lambda status: view.show_status(status, 4.0, None))
        controller.event_received.connect(lambda event: view.show_event(event, 4.0))
        # The executor publishes RUNNING with the trigger time, then the first pulse,
        # inside one drain interval. The mark must use that trigger time.
        controller._statuses.put(Status(phase=Phase.RUNNING, trigger_seconds=3.0))
        controller._events.put(Event("sync_pulse", 4.0, "2026-09-18T00:00:00"))

        controller._drain()

        self.assertEqual(view.timeline._marks, [1.0])


if __name__ == "__main__":
    unittest.main()
