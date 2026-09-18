"""Tests for lab configuration."""

import tempfile
import unittest
from pathlib import Path

from sniffler.config import ConfigError, TriggerSettings, load_settings


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

    def test_loads_valves_and_the_runs_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "lab.toml")
            path.write_text(
                """
                [valves]
                odor-1 = 8
                odor-2 = 9
                final = 16

                [runs]
                directory = "data/runs"
                """
            )
            settings = load_settings(path)

        self.assertEqual(settings.valves, {"odor-1": 8, "odor-2": 9, "final": 16})
        self.assertEqual(settings.runs_directory, Path(directory, "data/runs"))

    def test_absolute_and_home_runs_directories_are_used_as_written(self) -> None:
        for written, expected in (
            ("/data/odor-runs", Path("/data/odor-runs")),
            ("~/odor-runs", Path.home() / "odor-runs"),
        ):
            with self.subTest(directory=written), tempfile.TemporaryDirectory() as directory:
                path = Path(directory, "lab.toml")
                path.write_text(f'[runs]\ndirectory = "{written}"\n')
                self.assertEqual(load_settings(path).runs_directory, expected)

    def test_defaults_the_runs_directory_next_to_the_configuration(self) -> None:
        settings = self.load("[labjack]\nserial = 1\n")

        self.assertEqual(settings.runs_directory.name, "runs")
        self.assertEqual(
            load_settings(Path("missing/lab.toml")).runs_directory, Path("missing/runs")
        )

    def test_rejects_invalid_valve_channels(self) -> None:
        with self.assertRaisesRegex(ConfigError, "4 through 19"):
            self.load("[valves]\nodor = 3\n")
        with self.assertRaisesRegex(ConfigError, "same channel"):
            self.load("[valves]\nodor = 8\nblank = 8\n")
        with self.assertRaisesRegex(ConfigError, "channel number"):
            self.load('[valves]\nodor = "EIO0"\n')

    def test_loads_the_trigger_and_checks_its_channel(self) -> None:
        settings = self.load("[valves]\nA = 8\n\n[trigger]\nchannel = 4\ntimeout_seconds = 30\n")

        self.assertEqual(settings.trigger, TriggerSettings(4, 30.0))
        self.assertIsNone(self.load("[labjack]\nserial = 1\n").trigger)
        self.assertEqual(self.load("[trigger]\nchannel = 5\n").trigger, TriggerSettings(5))
        with self.assertRaisesRegex(ConfigError, "also the valve 'A'"):
            self.load("[valves]\nA = 8\n\n[trigger]\nchannel = 8\n")
        with self.assertRaisesRegex(ConfigError, "Unknown setting in \\[trigger\\]: edge"):
            self.load('[trigger]\nchannel = 4\nedge = "rising"\n')
        with self.assertRaisesRegex(ConfigError, "Set trigger.channel"):
            self.load("[trigger]\ntimeout_seconds = 3\n")
        for channel in (2, 9, 16):
            with self.assertRaisesRegex(ConfigError, "4 through 8"):
                self.load(f"[trigger]\nchannel = {channel}\n")
        with self.assertRaisesRegex(ConfigError, "timeout_seconds"):
            self.load("[trigger]\nchannel = 4\ntimeout_seconds = 0\n")

    def test_example_uses_the_device_limit_by_default(self) -> None:
        example = Path(__file__).parents[1] / "lab.toml.example"
        settings = load_settings(example)

        alicat = settings.alicats["main"]
        self.assertIsNone(alicat.maximum_flow)
        self.assertEqual(alicat.units, {})


if __name__ == "__main__":
    unittest.main()
