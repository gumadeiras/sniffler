"""Tests for the scientist-facing command line."""

import contextlib
import io
import os
import unittest
from unittest.mock import AsyncMock, patch

from lab_control.cli import _run_with_homebrew_exodriver, main
from lab_control.hardware import DeviceError


class CommandLineTests(unittest.TestCase):
    def run_command(self, arguments: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(arguments)
        return status, output.getvalue()

    @patch("lab_control.cli.hardware.list_serial_ports")
    def test_lists_serial_ports(self, list_serial_ports) -> None:
        list_serial_ports.return_value = [("/dev/cu.usbserial-1", "USB serial adapter")]

        status, output = self.run_command(["ports"])

        self.assertEqual(status, 0)
        self.assertEqual(output, "/dev/cu.usbserial-1: USB serial adapter\n")

    @patch("lab_control.cli.hardware.set_labjack_digital")
    def test_sets_labjack_digital_output(self, set_digital) -> None:
        set_digital.return_value = True

        status, output = self.run_command(
            ["labjack", "set-digital", "--channel", "4", "--state", "high"]
        )

        self.assertEqual(status, 0)
        self.assertEqual(output, "FIO4: high\n")
        set_digital.assert_called_once_with(4, True, None)

    @patch("lab_control.cli.hardware.set_alicat_flow", new_callable=AsyncMock)
    def test_stops_alicat_flow(self, set_flow) -> None:
        set_flow.return_value = {"setpoint": 0.0}

        status, output = self.run_command(["alicat", "stop", "--port", "COM3"])

        self.assertEqual(status, 0)
        self.assertIn("Alicat flow setpoint is zero.", output)
        set_flow.assert_awaited_once_with("COM3", 0.0, "A")

    @patch("lab_control.cli.hardware.labjack_status")
    def test_shows_device_errors_without_a_traceback(self, labjack_status) -> None:
        labjack_status.side_effect = DeviceError("LabJack U3 command failed: device not found")

        status, output = self.run_command(["labjack", "status"])

        self.assertEqual(status, 1)
        self.assertEqual(output, "Error: LabJack U3 command failed: device not found\n")

    @patch("lab_control.cli.subprocess.run")
    @patch("lab_control.cli.Path.exists", return_value=True)
    @patch("lab_control.cli.sys.platform", "darwin")
    @patch("lab_control.cli.sys.argv", ["lab-control", "labjack", "status"])
    def test_uses_apple_silicon_homebrew_driver_path(self, _exists, run) -> None:
        run.return_value.returncode = 0

        with patch.dict(os.environ, {"DYLD_LIBRARY_PATH": ""}):
            status = _run_with_homebrew_exodriver(None)

        self.assertEqual(status, 0)
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["DYLD_LIBRARY_PATH"], "/opt/homebrew/lib")


if __name__ == "__main__":
    unittest.main()
