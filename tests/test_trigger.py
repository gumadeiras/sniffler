"""Tests for the TTL trigger wait."""

import unittest

from sniffler.config import TriggerSettings
from sniffler.trigger import TriggerOutcome, wait_for_trigger


class FakeClock:
    def __init__(self, step: float = 0.001) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def scripted(levels: list[bool]):
    """Return one level per read and hold the last level."""
    reads = iter(levels)
    last = levels[-1]

    def read() -> bool:
        return next(reads, last)

    return read


def never() -> bool:
    return False


class TriggerWaitTests(unittest.TestCase):
    def wait(self, levels, settings=None, **requests):
        options = {"should_abort": never, "should_stop": never, "should_start_now": never}
        options.update(requests)
        return wait_for_trigger(
            scripted(levels), settings or TriggerSettings(4), clock=FakeClock(), **options
        )

    def test_rising_edge_counts_only_after_a_low_level(self) -> None:
        result = self.wait([False, False, True])

        self.assertEqual(result.outcome, TriggerOutcome.RECEIVED)
        self.assertEqual(result.reads, 3)
        self.assertTrue(result.armed)
        self.assertGreater(result.ended_seconds, 0)
        self.assertIn("received after", result.describe())

    def test_a_line_that_floats_high_cannot_start_a_rising_edge_run(self) -> None:
        settings = TriggerSettings(4, "rising", timeout_seconds=0.05)

        result = self.wait([True], settings)

        self.assertEqual(result.outcome, TriggerOutcome.TIMED_OUT)
        self.assertFalse(result.armed)
        self.assertIn("never showed the level before the edge", result.describe())

    def test_high_then_low_then_high_is_one_rising_edge(self) -> None:
        result = self.wait([True, True, False, True])

        self.assertEqual(result.outcome, TriggerOutcome.RECEIVED)
        self.assertEqual(result.reads, 4)

    def test_falling_edge(self) -> None:
        result = self.wait([True, False], TriggerSettings(4, "falling"))

        self.assertEqual(result.outcome, TriggerOutcome.RECEIVED)
        self.assertEqual(result.reads, 2)

    def test_operator_requests_end_the_wait_and_abort_wins(self) -> None:
        stopped = self.wait([False], should_stop=lambda: True)
        started = self.wait([False], should_start_now=lambda: True)
        aborted = self.wait([False], should_abort=lambda: True, should_stop=lambda: True)

        self.assertEqual(stopped.outcome, TriggerOutcome.STOPPED)
        self.assertEqual(started.outcome, TriggerOutcome.STARTED_NOW)
        self.assertEqual(aborted.outcome, TriggerOutcome.ABORTED)
        self.assertEqual(aborted.reads, 0)

    def test_timeout_is_measured_on_the_given_clock(self) -> None:
        result = self.wait([False], TriggerSettings(4, timeout_seconds=0.02))

        self.assertEqual(result.outcome, TriggerOutcome.TIMED_OUT)
        self.assertGreaterEqual(result.waited_seconds, 0.02)
        self.assertGreater(result.reads, 5)
        self.assertAlmostEqual(result.seconds_per_read, result.waited_seconds / result.reads)


if __name__ == "__main__":
    unittest.main()
