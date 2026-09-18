"""Tests for the pulse counter wait and the sync recorder."""

import unittest

from sniffler.config import TriggerSettings
from sniffler.hardware import DeviceError
from sniffler.trigger import READ_FAILURE_LIMIT, SyncRecorder, TriggerOutcome, wait_for_trigger


class FakeClock:
    def __init__(self, step: float = 0.001) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def scripted(counts: list[int]):
    """Return one count per read and hold the last count."""
    reads = iter(counts)
    last = counts[-1]

    def read() -> int:
        return next(reads, last)

    return read


def never() -> bool:
    return False


class TriggerWaitTests(unittest.TestCase):
    def wait(self, counts, settings=None, **requests):
        options = {"should_abort": never, "should_stop": never, "should_start_now": never}
        options.update(requests)
        return wait_for_trigger(
            scripted(counts),
            settings or TriggerSettings(4),
            clock=FakeClock(),
            poll_seconds=0.0,
            **options,
        )

    def test_the_first_counted_pulse_ends_the_wait(self) -> None:
        result = self.wait([0, 0, 1])

        self.assertEqual(result.outcome, TriggerOutcome.RECEIVED)
        self.assertEqual((result.reads, result.count), (3, 1))
        self.assertGreater(result.ended_seconds, 0)
        self.assertIn("received after", result.describe())
        self.assertNotIn("pulses arrived", result.describe())

    def test_several_pulses_inside_one_poll_are_reported(self) -> None:
        result = self.wait([0, 3])

        self.assertEqual(result.outcome, TriggerOutcome.RECEIVED)
        self.assertEqual(result.count, 3)
        self.assertIn("3 pulses arrived", result.describe())

    def test_operator_requests_end_the_wait_and_abort_wins(self) -> None:
        stopped = self.wait([0], should_stop=lambda: True)
        started = self.wait([0], should_start_now=lambda: True)
        aborted = self.wait([0], should_abort=lambda: True, should_stop=lambda: True)

        self.assertEqual(stopped.outcome, TriggerOutcome.STOPPED)
        self.assertEqual(started.outcome, TriggerOutcome.STARTED_NOW)
        self.assertEqual(aborted.outcome, TriggerOutcome.ABORTED)
        self.assertEqual(aborted.reads, 0)

    def test_timeout_is_measured_on_the_given_clock(self) -> None:
        result = self.wait([0], TriggerSettings(4, timeout_seconds=0.02))

        self.assertEqual(result.outcome, TriggerOutcome.TIMED_OUT)
        self.assertGreaterEqual(result.waited_seconds, 0.02)
        self.assertGreater(result.reads, 5)
        self.assertAlmostEqual(result.seconds_per_read, result.waited_seconds / result.reads)


class SyncRecorderTests(unittest.TestCase):
    def recorder(self, read, clock=None):
        self.pulses: list[tuple[int, float, int]] = []
        self.errors: list[str] = []
        self.stopped: list[float] = []
        return SyncRecorder(
            read,
            clock=clock or FakeClock(),
            on_pulse=lambda *pulse: self.pulses.append(pulse),
            on_error=self.errors.append,
            on_stopped=self.stopped.append,
            poll_seconds=0.005,
        )

    def test_reports_each_pulse_with_the_count_and_the_time_it_was_seen(self) -> None:
        recorder = self.recorder(scripted([0, 1, 1, 3]))

        for _ in range(4):
            recorder.poll()

        self.assertEqual(
            [(count, arrived) for count, _, arrived in self.pulses], [(1, 1), (2, 2), (3, 2)]
        )
        self.assertEqual(recorder.pulses, 3)
        self.assertEqual(recorder.count, 3)
        self.assertTrue(recorder.active)
        times = [seen for _, seen, _ in self.pulses]
        self.assertEqual(times, sorted(times))

    def test_the_gate_pulse_is_the_baseline_not_a_mark(self) -> None:
        recorder = self.recorder(scripted([1, 2]))
        recorder.start_from(1)

        recorder.poll()
        recorder.poll()

        self.assertEqual([count for count, _, _ in self.pulses], [2])

    def test_a_count_read_by_another_packet_counts_as_a_poll(self) -> None:
        recorder = self.recorder(scripted([2]))

        recorder.observe(2, 0.5)
        recorder.poll()

        self.assertEqual([(count, seen) for count, seen, _ in self.pulses], [(1, 0.5), (2, 0.5)])
        self.assertEqual(recorder.pulses, 2)

    def test_keeps_the_poll_interval(self) -> None:
        clock = FakeClock(step=0.0)
        recorder = self.recorder(scripted([0]), clock)

        self.assertEqual(recorder.seconds_until_poll(clock.now), 0.0)
        recorder.poll()
        self.assertAlmostEqual(recorder.seconds_until_poll(clock.now), 0.005)
        self.assertAlmostEqual(recorder.seconds_until_poll(clock.now + 0.003), 0.002)
        self.assertEqual(recorder.seconds_until_poll(clock.now + 0.1), 0.0)

    def test_stops_after_repeated_read_failures_and_says_when(self) -> None:
        def broken() -> int:
            raise DeviceError("usb gone")

        recorder = self.recorder(broken)

        for _ in range(READ_FAILURE_LIMIT + 2):
            recorder.poll()

        self.assertEqual(len(self.errors), READ_FAILURE_LIMIT, "no reads after it stopped")
        self.assertEqual(len(self.stopped), 1)
        self.assertFalse(recorder.active)
        self.assertEqual(recorder.stopped_seconds, self.stopped[0])

    def test_one_good_read_clears_the_failure_streak(self) -> None:
        answers = iter([DeviceError("x")] * (READ_FAILURE_LIMIT - 1) + [0] + [DeviceError("x")] * 2)

        def flaky() -> int:
            answer = next(answers)
            if isinstance(answer, DeviceError):
                raise answer
            return answer

        recorder = self.recorder(flaky)
        for _ in range(READ_FAILURE_LIMIT + 2):
            recorder.poll()

        self.assertTrue(recorder.active)


if __name__ == "__main__":
    unittest.main()
