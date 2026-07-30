"""Tests for lab configuration."""

import tempfile
import unittest
from pathlib import Path

from lab_control.config import ConfigError, load_settings


class ConfigurationTests(unittest.TestCase):
    def load(self, text: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "lab.toml")
            path.write_text(text)
            return load_settings(path)

    def test_loads_device_identity_safety_limits_and_units(self) -> None:
        settings = self.load(
            """
            [labjack]
            serial = 320123456

            [alicat]
            port = "COM3"
            unit = "B"
            baud_rate = 9600
            timeout_seconds = 0.5
            minimum_flow = -2.0
            maximum_flow = 2.0
            allow_negative_flow = true

            [alicat.units]
            mass_flow = "SCCM"
            """
        )

        self.assertEqual(settings.labjack_serial, 320123456)
        self.assertEqual(settings.alicat_port, "COM3")
        self.assertEqual(settings.alicat_unit, "B")
        self.assertEqual(settings.alicat_baud_rate, 9600)
        self.assertEqual(settings.alicat_timeout_seconds, 0.5)
        self.assertEqual(settings.alicat_minimum_flow, -2.0)
        self.assertEqual(settings.alicat_maximum_flow, 2.0)
        self.assertTrue(settings.alicat_allow_negative_flow)
        self.assertEqual(settings.alicat_units, {"mass_flow": "SCCM"})

    def test_rejects_unknown_settings(self) -> None:
        with self.assertRaisesRegex(ConfigError, "maximum_flwo"):
            self.load("[alicat]\nmaximum_flwo = 2.0\n")

    def test_requires_explicit_negative_flow_permission(self) -> None:
        with self.assertRaisesRegex(ConfigError, "allow_negative_flow"):
            self.load("[alicat]\nminimum_flow = -1.0\nmaximum_flow = 1.0\n")

    def test_rejects_empty_unit_id(self) -> None:
        with self.assertRaisesRegex(ConfigError, "one letter"):
            self.load('[alicat]\nunit = ""\n')

    def test_example_uses_the_device_limit_by_default(self) -> None:
        example = Path(__file__).parents[1] / "lab.toml.example"
        settings = load_settings(example)

        self.assertIsNone(settings.alicat_maximum_flow)
        self.assertEqual(settings.alicat_units, {})


if __name__ == "__main__":
    unittest.main()
