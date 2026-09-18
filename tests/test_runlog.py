"""Tests for run directories, logs, and the advisory lock."""

import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from sniffler.runlog import (
    LOCK_FILE_NAME,
    RunLock,
    RunLockError,
    RunLog,
    RunLogError,
    active_run,
    new_run_directory,
    refusal_for_active_run,
    run_id,
)


class RunLockTests(unittest.TestCase):
    def test_names_the_active_run_and_refuses_a_second_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory, "runs")
            self.assertIsNone(active_run(runs))
            self.assertIsNone(refusal_for_active_run(runs))

            first = RunLock(runs, runs / "20260917-101500-pulses")
            first.acquire()
            self.assertEqual(active_run(runs), str(runs / "20260917-101500-pulses"))
            content = json.loads((runs / LOCK_FILE_NAME).read_text())
            self.assertIn("pid", content)

            second = RunLock(runs, runs / "20260917-101600-other")
            with self.assertRaisesRegex(RunLockError, "20260917-101500-pulses"):
                second.acquire()
            second.release()
            self.assertTrue((runs / LOCK_FILE_NAME).exists())

            first.release()
            self.assertIsNone(active_run(runs))
            first.release()

    def test_refuses_a_runs_directory_that_cannot_be_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory, "file")
            blocker.write_text("not a directory")

            with self.assertRaisesRegex(RunLockError, "Cannot create the run lock"):
                RunLock(blocker / "runs", blocker / "runs" / "x").acquire()

    def test_reports_an_unreadable_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            (runs / LOCK_FILE_NAME).write_text("garbage")

            self.assertIn("unknown run", active_run(runs))
            self.assertIn(LOCK_FILE_NAME, refusal_for_active_run(runs))


class RunLogTests(unittest.TestCase):
    def test_writes_flushed_rows_and_a_final_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = RunLog(Path(directory, "run"))
            log.open({"run_id": "run", "seed": 1})
            log.event("run_start", returned_run_seconds=0.0)
            log.event(
                "valve_command",
                returned_run_seconds=1.0034,
                commanded_run_seconds=1.0021,
                scheduled_run_seconds=1.0,
                trial_index=0,
                trial_name="odor",
                step_index=2,
                device="odor-1",
                value="open",
                sync_count=4,
            )
            log.sample(
                "mfc-500",
                run_seconds=1.5,
                commanded_setpoint=100.0,
                state={"setpoint": 100.0, "mass_flow": 99.2},
            )

            with (log.directory / "events.csv").open(newline="") as file:
                events = list(csv.DictReader(file))
            with (log.directory / "samples.csv").open(newline="") as file:
                samples = list(csv.DictReader(file))
            manifest = json.loads((log.directory / "manifest.json").read_text())
            self.assertEqual(len(events), 2, "rows are visible before close")
            self.assertEqual(events[0]["sync_count"], "")
            self.assertEqual(events[1]["sync_count"], "4")
            self.assertEqual(events[1]["returned_run_seconds"], "1.003400")
            self.assertEqual(events[1]["commanded_run_seconds"], "1.002100")
            self.assertEqual(events[1]["step_index"], "2")
            self.assertEqual(samples[0]["mass_flow"], "99.2")
            self.assertEqual(samples[0]["pressure"], "")
            self.assertEqual(manifest, {"run_id": "run", "seed": 1})

            log.close(outcome="done")
            manifest = json.loads((log.directory / "manifest.json").read_text())
            self.assertEqual(manifest["outcome"], "done")
            with self.assertRaisesRegex(RunLogError, "not open"):
                log.event("late", returned_run_seconds=2.0)

    def test_a_disk_error_on_a_row_is_a_run_log_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = RunLog(Path(directory, "run"))
            log.open({})
            full_disk = Mock(wraps=log._events)
            full_disk.flush.side_effect = OSError(28, "No space left on device")
            log._events = full_disk

            with self.assertRaisesRegex(RunLogError, "Cannot write events.csv.*No space left"):
                log.event("late", returned_run_seconds=1.0)
            log.sample("mfc", run_seconds=1.0, commanded_setpoint=None, state={})
            log.close(outcome="failed")

    def test_refuses_to_reuse_a_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            RunLog(Path(directory, "run")).open({})
            with self.assertRaisesRegex(RunLogError, "Cannot create the run directory"):
                RunLog(Path(directory, "run")).open({})

    def test_new_run_directory_never_reuses_a_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            first = new_run_directory(runs, "pulses")
            first.mkdir()
            second = new_run_directory(runs, "pulses")
            second.mkdir()
            third = new_run_directory(runs, "pulses")

        self.assertEqual(second.name, f"{first.name}-2")
        self.assertEqual(third.name, f"{first.name}-3")

    def test_run_id_is_a_timestamp_and_a_slug(self) -> None:
        moment = datetime(2026, 9, 17, 10, 15, 0)

        self.assertEqual(
            run_id("Odor pulses / test #2", moment), "20260917-101500-odor-pulses-test-2"
        )
        self.assertEqual(run_id("***", moment), "20260917-101500-run")


if __name__ == "__main__":
    unittest.main()
