"""Tests for lab configuration."""

import tempfile
import unittest
from pathlib import Path

from sniffler.config import ConfigError, TriggerSettings, TtlOutputSettings, load_settings


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
        # A path with a root but no drive is not absolute on Windows, so build one
        # that is absolute on the platform that runs the test.
        absolute = Path(tempfile.gettempdir()).resolve() / "odor-runs"
        for written, expected in (
            (absolute.as_posix(), absolute),
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

    def test_loads_the_ttl_output_and_checks_its_channel(self) -> None:
        settings = self.load(
            "[valves]\nA = 8\n\n[trigger]\nchannel = 4\n\n[ttl_output]\nchannel = 5\n"
        )

        self.assertEqual(settings.ttl_output, TtlOutputSettings(5, "pulse", 0.005))
        self.assertFalse(settings.ttl_output.holds_high)
        self.assertIsNone(self.load("[labjack]\nserial = 1\n").ttl_output)
        pulse = self.load("[ttl_output]\nchannel = 19\npulse_seconds = 0.05\n").ttl_output
        self.assertEqual(pulse, TtlOutputSettings(19, "pulse", 0.05))
        high = self.load('[ttl_output]\nchannel = 6\nmode = "high"\n').ttl_output
        self.assertEqual(high, TtlOutputSettings(6, "high", 0.005))
        self.assertTrue(high.holds_high)
        with self.assertRaisesRegex(ConfigError, "Set ttl_output.channel"):
            self.load('[ttl_output]\nmode = "high"\n')
        with self.assertRaisesRegex(ConfigError, 'must be "pulse" or "high"'):
            self.load('[ttl_output]\nchannel = 6\nmode = "level"\n')
        with self.assertRaisesRegex(ConfigError, "pulse_seconds has no effect"):
            self.load('[ttl_output]\nchannel = 6\nmode = "high"\npulse_seconds = 0.01\n')
        for width in ("0", "-0.1", "inf"):
            with self.assertRaisesRegex(ConfigError, "pulse_seconds must be finite"):
                self.load(f"[ttl_output]\nchannel = 6\npulse_seconds = {width}\n")
        with self.assertRaisesRegex(ConfigError, "also the valve 'A'"):
            self.load("[valves]\nA = 8\n\n[ttl_output]\nchannel = 8\n")
        with self.assertRaisesRegex(ConfigError, "also the trigger input"):
            self.load("[trigger]\nchannel = 4\n\n[ttl_output]\nchannel = 4\n")
        for channel in (3, 20):
            with self.assertRaisesRegex(ConfigError, "4 through 19"):
                self.load(f"[ttl_output]\nchannel = {channel}\n")
        with self.assertRaisesRegex(ConfigError, "Unknown setting in \\[ttl_output\\]: width"):
            self.load("[ttl_output]\nchannel = 5\nwidth = 1\n")
        with self.assertRaisesRegex(ConfigError, "channel must be int"):
            self.load('[ttl_output]\nchannel = "FIO5"\n')

    def test_example_uses_the_device_limit_by_default(self) -> None:
        example = Path(__file__).parents[1] / "lab.toml.example"
        settings = load_settings(example)

        alicat = settings.alicats["main"]
        self.assertIsNone(alicat.maximum_flow)
        self.assertEqual(alicat.units, {})


if __name__ == "__main__":
    unittest.main()
