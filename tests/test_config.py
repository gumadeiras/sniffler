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
        alicat = settings.alicats["default"]
        self.assertEqual(alicat.port, "COM3")
        self.assertEqual(alicat.unit, "B")
        self.assertEqual(alicat.baud_rate, 9600)
        self.assertEqual(alicat.timeout_seconds, 0.5)
        self.assertEqual(alicat.minimum_flow, -2.0)
        self.assertEqual(alicat.maximum_flow, 2.0)
        self.assertTrue(alicat.allow_negative_flow)
        self.assertEqual(alicat.units, {"mass_flow": "SCCM"})

    def test_loads_multiple_named_alicats(self) -> None:
        settings = self.load(
            """
            [alicat.mfc-500]
            port = "COM3"

            [alicat.mfc-2000]
            port = "COM4"
            """
        )

        self.assertEqual(set(settings.alicats), {"mfc-500", "mfc-2000"})
        self.assertEqual(settings.alicats["mfc-500"].port, "COM3")
        self.assertEqual(settings.alicats["mfc-2000"].port, "COM4")

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

        alicat = settings.alicats["main"]
        self.assertIsNone(alicat.maximum_flow)
        self.assertEqual(alicat.units, {})


if __name__ == "__main__":
    unittest.main()
