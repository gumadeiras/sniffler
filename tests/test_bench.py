"""Tests for the bench checks on the fake rig."""

import contextlib
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from sniffler.bench import Bench, build_bench, main, milliseconds, run_checks
from sniffler.config import (
    AlicatSettings,
    ConfigError,
    Settings,
    TriggerSettings,
    TtlOutputSettings,
)
from sniffler.fakes import FakeRig
from sniffler.recipe import rig_map_from_settings


class BenchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Settings(
            labjack_serial=320107153,
            alicats={"mfc-500": AlicatSettings(port="/dev/mfc-500", units={"mass_flow": "SCCM"})},
            valves={"A": 8, "B": 9},
            runs_directory=Path(self.temporary.name, "runs"),
            trigger=TriggerSettings(4),
        )
        self.fake = FakeRig(("mfc-500",))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def bench(self, **options) -> Bench:
        bench = build_bench(
            self.settings, options.pop("actuate", True), options.pop("loopback", 5), False
        )
        bench.open_labjack = self.fake.open_labjack
        bench.open_alicat = self.fake.open_alicat
        for name, value in options.items():
            setattr(bench, name, value)
        return bench

    def test_milliseconds_summarizes_a_distribution(self) -> None:
        text = milliseconds([0.001, 0.002, 0.010])

        self.assertIn("n=3", text)
        self.assertIn("median=2.000", text)
        self.assertIn("max=10.000 ms", text)
        self.assertEqual(milliseconds([]), "no values")

    def test_actuating_checks_are_skipped_without_the_flag(self) -> None:
        results = run_checks(self.bench(actuate=False), ["valves", "timing", "safe"])

        self.assertEqual([result.outcome for result in results], ["skipped", "skipped", "pass"])
        self.assertIn("mfc-500: setpoint 0.0", results[2].lines)
        self.assertIn("--actuate", results[0].lines[0])
        self.assertEqual(self.fake.labjack.writes, [])

    def test_valves_check_clicks_each_valve_then_all_and_ends_closed(self) -> None:
        result = run_checks(self.bench(), ["valves"])[0]

        self.assertEqual(result.outcome, "pass", result.lines)
        writes = [states for _time, states in self.fake.labjack.writes]
        self.assertEqual(writes[:4], [{8: True}, {8: False}, {9: True}, {9: False}])
        self.assertEqual(writes[-2:], [{8: True, 9: True}, {8: False, 9: False}])
        self.assertIn("write round trip", result.lines[-1])

    def test_timing_check_reports_the_distributions_from_a_real_run_directory(self) -> None:
        with (
            patch("sniffler.bench.PULSE_SECONDS", 0.01),
            patch("sniffler.bench.LEAD_SECONDS", 0.02),
        ):
            result = run_checks(self.bench(), ["timing"])[0]

        self.assertEqual(result.outcome, "pass", result.lines)
        self.assertTrue(any(line.startswith("scheduled to commanded n=") for line in result.lines))
        self.assertTrue(any(line.startswith("commanded to returned n=") for line in result.lines))
        self.assertTrue(any("sync count" in line for line in result.lines))
        run_directory = Path(result.lines[0].split(": ", 1)[1])
        self.assertTrue((run_directory / "events.csv").exists())

    def test_trigger_check_finds_the_counted_edge_and_the_restored_configuration(self) -> None:
        # The fake counter counts on the second read after the reset: the loop-back rose.
        self.fake.labjack.counts = [1, 1]

        result = run_checks(self.bench(), ["trigger"])[0]

        self.assertEqual(result.outcome, "pass", result.lines)
        self.assertTrue(any("counts the rising edge" in line for line in result.lines))
        self.assertTrue(any("configuration restored: True" in line for line in result.lines))
        self.assertTrue(self.fake.labjack.counter_restored)

    def test_gate_and_sync_need_a_loop_back(self) -> None:
        results = run_checks(self.bench(loopback=None), ["gate", "sync"])

        self.assertEqual([result.outcome for result in results], ["skipped", "skipped"])
        self.assertIn("--loopback", results[0].lines[0])

    def test_gate_check_measures_edge_to_schedule_latency(self) -> None:
        # The gate's third read sees the pulse; the U3 counts falling edges, tried first.
        self.fake.labjack.counts = [0, 0, 1]

        result = run_checks(self.bench(), ["gate"])[0]

        self.assertEqual(result.outcome, "pass", result.lines)
        self.assertTrue(any("edge to schedule start:" in line for line in result.lines))
        self.assertTrue(any(line.startswith("falling edge run") for line in result.lines))
        self.assertIn("3 reads", result.lines[-1])

    def test_sync_check_expects_one_mark_per_loop_back_pulse(self) -> None:
        labjack = self.fake.labjack
        write = labjack.write_digital_lines
        level = {"high": False}

        # The loop-back on channel 5 feeds the counter, which counts falling edges
        # like the U3: one count per pulse, none for the lead and end-state writes.
        def loop_back(states: dict[int, bool], *, read_counter: bool = False) -> int | None:
            high = states.get(5, level["high"])
            if level["high"] and not high and labjack.counter_channel is not None:
                labjack.counts = [labjack.counts[-1] + 1]
            level["high"] = high
            return write(states, read_counter=read_counter)

        labjack.write_digital_lines = loop_back
        with patch("sniffler.bench.SYNC_PULSE_SECONDS", 0.02):
            result = run_checks(self.bench(), ["sync"])[0]

        self.assertEqual(result.outcome, "pass", result.lines)
        self.assertIn("loop-back edges written: 10; sync marks recorded: 4", result.lines)

    def test_ttl_check_measures_the_width_and_counts_the_pulse(self) -> None:
        self.settings = replace(self.settings, ttl_output=TtlOutputSettings(6, "pulse", 0.01))
        # The counter sees the pulse on the read in the packet that ends it.
        self.fake.labjack.counts = [0, 1]

        with patch("sniffler.bench.LEAD_SECONDS", 0.05):
            result = run_checks(self.bench(), ["ttl"])[0]

        self.assertEqual(result.outcome, "pass", result.lines)
        self.assertIn("rose with the first valve command: True", result.lines)
        self.assertTrue(
            any(line.startswith("pulse width: requested 10.0 ms") for line in result.lines)
        )
        self.assertTrue(any(line.startswith("pulses the counter saw: 1") for line in result.lines))
        self.assertEqual(self.fake.labjack.writes[-1][1], {8: False, 9: False, 6: False})

    def test_ttl_check_in_high_mode_reports_the_fall_with_the_end_state(self) -> None:
        self.settings = replace(self.settings, ttl_output=TtlOutputSettings(6, "high"))

        with (
            patch("sniffler.bench.LEAD_SECONDS", 0.05),
            patch("sniffler.bench.PULSE_SECONDS", 0.02),
        ):
            result = run_checks(self.bench(), ["ttl"])[0]

        self.assertEqual(result.outcome, "pass", result.lines)
        self.assertTrue(
            any(line.startswith("mode high: fell with the shutdown_state") for line in result.lines)
        )

    def test_ttl_check_is_skipped_without_the_table(self) -> None:
        result = run_checks(self.bench(), ["ttl"])[0]

        self.assertEqual(result.outcome, "skipped")
        self.assertIn("[ttl_output]", result.lines[0])

    def test_loopback_must_be_a_free_channel(self) -> None:
        with self.assertRaisesRegex(ConfigError, "free digital channel"):
            build_bench(self.settings, True, 8, False)
        with self.assertRaisesRegex(ConfigError, "free digital channel"):
            build_bench(self.settings, True, 4, False)
        with self.assertRaisesRegex(ConfigError, "free digital channel"):
            build_bench(replace(self.settings, ttl_output=TtlOutputSettings(6)), True, 6, False)
        rig = build_bench(self.settings, True, None, True).rig
        self.assertEqual(rig.mfcs, {})
        self.assertEqual(rig.valves, rig_map_from_settings(self.settings).valves)

    def test_main_rejects_unknown_checks_and_refuses_an_active_run(self) -> None:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            status = main(["nonsense"])
        self.assertEqual(status, 2)
        self.assertIn("Unknown check", errors.getvalue())

        from sniffler.runlog import RunLock

        RunLock(self.settings.runs_directory, self.settings.runs_directory / "x").acquire()
        errors = io.StringIO()
        with (
            patch("sniffler.bench.load_settings", return_value=self.settings),
            contextlib.redirect_stderr(errors),
        ):
            status = main(["safe"])
        self.assertEqual(status, 3)
        self.assertIn("A run is active", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
