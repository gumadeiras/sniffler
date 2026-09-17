"""Tests for the scientist-facing command line."""

import argparse
import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sniffler.cli import _finite_float, _run_with_homebrew_exodriver, main
from sniffler.config import AlicatSettings, Settings
from sniffler.hardware import DeviceError


class CommandLineTests(unittest.TestCase):
    @staticmethod
    def settings_with_alicat(name: str = "default", **values) -> Settings:
        return Settings(alicats={name: AlicatSettings(**values)})

    def run_command(
        self, arguments: list[str], settings: Settings | None = None
    ) -> tuple[int, str, str]:
        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch("sniffler.cli.load_settings", return_value=settings or Settings()),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            status = main(arguments)
        return status, output.getvalue(), errors.getvalue()

    @patch("sniffler.cli.hardware.list_serial_ports")
    def test_lists_serial_ports(self, list_serial_ports) -> None:
        list_serial_ports.return_value = [("/dev/cu.usbserial-1", "USB serial adapter")]

        status, output, errors = self.run_command(["ports"])

        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(output, "/dev/cu.usbserial-1: USB serial adapter\n")

    @patch("sniffler.cli.hardware.set_labjack_digital")
    def test_routes_configured_labjack_serial(self, set_digital) -> None:
        set_digital.return_value = True
        settings = Settings(labjack_serial=320123456)

        status, output, errors = self.run_command(
            ["labjack", "set-digital", "--channel", "4", "--state", "high"],
            settings,
        )

        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(output, "FIO4 reported state: high\n")
        set_digital.assert_called_once_with(4, True, 320123456)

    @patch("sniffler.cli.hardware.set_alicat_flow", new_callable=AsyncMock)
    def test_routes_alicat_stop_connection(self, set_flow) -> None:
        set_flow.return_value = (0.0, None)
        settings = self.settings_with_alicat(port="COM3", unit="B", baud_rate=9600)

        status, output, errors = self.run_command(["alicat", "stop"], settings)

        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(output, "Alicat mass-flow setpoint changed to zero.\n")
        set_flow.assert_awaited_once_with("COM3", 0.0, "B", 9600, 0.15)

    @patch("sniffler.cli.hardware.set_alicat_flow", new_callable=AsyncMock)
    def test_uses_the_device_unit_without_a_configured_maximum(self, set_flow) -> None:
        set_flow.return_value = (1.23, "SCCM")
        settings = self.settings_with_alicat("mfc-500", port="COM3")

        status, output, errors = self.run_command(
            ["alicat", "set-flow", "1.234", "--name", "mfc-500"], settings
        )

        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(output, "Alicat mfc-500 mass-flow setpoint changed to 1.23 SCCM.\n")
        set_flow.assert_awaited_once_with("COM3", 1.23, "A", 19200, 0.15)

    @patch("sniffler.cli.hardware.set_alicat_flow", new_callable=AsyncMock)
    def test_requires_a_name_for_multiple_alicats(self, set_flow) -> None:
        settings = Settings(
            alicats={
                "mfc-500": AlicatSettings(port="COM3"),
                "mfc-2000": AlicatSettings(port="COM4"),
            }
        )

        status, output, errors = self.run_command(["alicat", "set-flow", "1.0"], settings)

        self.assertEqual((status, output), (2, ""))
        self.assertIn("Select an Alicat with --name", errors)
        self.assertIn("mfc-2000, mfc-500", errors)
        set_flow.assert_not_awaited()

    @patch("sniffler.cli.hardware.set_alicat_flow", new_callable=AsyncMock)
    def test_rejects_flow_outside_configured_limits(self, set_flow) -> None:
        settings = self.settings_with_alicat(
            port="COM3",
            maximum_flow=2.0,
            units={"mass_flow": "SCCM"},
        )

        status, output, errors = self.run_command(["alicat", "set-flow", "3.0"], settings)

        self.assertEqual((status, output), (2, ""))
        self.assertIn("configured limit of 2.0 SCCM", errors)
        set_flow.assert_not_awaited()

    @patch("sniffler.cli.hardware.set_alicat_flow", new_callable=AsyncMock)
    def test_checks_rounded_flow_against_the_safe_maximum(self, set_flow) -> None:
        settings = self.settings_with_alicat(
            port="COM3",
            maximum_flow=1.235,
            units={"mass_flow": "SCCM"},
        )

        status, output, errors = self.run_command(["alicat", "set-flow", "1.235"], settings)

        self.assertEqual((status, output), (2, ""))
        self.assertIn("configured limit of 1.235 SCCM", errors)
        set_flow.assert_not_awaited()

    @patch("sniffler.cli.hardware.labjack_status")
    def test_writes_hardware_errors_to_stderr(self, labjack_status) -> None:
        labjack_status.side_effect = DeviceError("device not found")

        status, output, errors = self.run_command(["labjack", "status"])

        self.assertEqual((status, output), (1, ""))
        self.assertEqual(errors, "Hardware error: device not found\n")

    def test_rejects_nonfinite_flow(self) -> None:
        for value in ("nan", "inf", "-inf", "1e309"):
            with (
                self.subTest(value=value),
                self.assertRaises(argparse.ArgumentTypeError),
            ):
                _finite_float(value)

    @patch("sniffler.cli.subprocess.run")
    @patch("sniffler.cli.Path.exists", return_value=True)
    @patch("sniffler.cli.sys.platform", "darwin")
    @patch(
        "sniffler.cli.sys.argv",
        ["sniffler", "--config", "other.toml", "labjack", "status"],
    )
    def test_uses_homebrew_driver_with_config_argument(self, _exists, run) -> None:
        run.return_value.returncode = 0

        with patch.dict(os.environ, {"DYLD_LIBRARY_PATH": ""}):
            status = _run_with_homebrew_exodriver(None)

        self.assertEqual(status, 0)
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["DYLD_LIBRARY_PATH"], str(Path("/opt/homebrew/lib")))


if __name__ == "__main__":
    unittest.main()
