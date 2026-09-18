"""Fake LabJack and Alicat sessions for the tests and for the GUI demo mode.

They record every command and never open a port. The default readings are exact
and deterministic for the tests; ``realistic=True`` adds lag and noise for the demo.
"""

import asyncio
import random
import time
from collections.abc import Iterable
from contextlib import asynccontextmanager, contextmanager

from sniffler.hardware import DeviceError

DEFAULT_MFCS = ("mfc-500", "mfc-2000")
LAG_FRACTION = 0.35
NOISE_FRACTION = 0.004


class FakeLabJack:
    """Record every multi-line write with the time it was made."""

    def __init__(self) -> None:
        self.writes: list[tuple[float, dict[int, bool]]] = []
        self.fail_on_write: int | None = None
        self.write_delay = 0.0
        self.closed = False
        self.input_level = False
        # The pulse counter: one count per read, the last count held forever.
        self.counts: list[int] = [0]
        self.count_reads = 0
        self.counter_channel: int | None = None
        self.counter_restored = False
        self.counter_error: str | None = None

    def read_digital(self, channel: int) -> tuple[bool, bool]:
        return True, self.input_level

    def enable_counter(self, channel: int) -> None:
        self.counter_channel = channel

    def disable_counter(self) -> None:
        self.counter_restored = True

    def read_counter(self, reset: bool = False) -> int:
        if self.counter_error is not None:
            raise DeviceError(self.counter_error)
        if reset:
            return 0
        count = self.counts[min(self.count_reads, len(self.counts) - 1)]
        self.count_reads += 1
        return count

    def write_digital_lines(self, states: dict[int, bool]) -> None:
        if self.fail_on_write is not None and len(self.writes) == self.fail_on_write:
            self.fail_on_write = None
            raise DeviceError("usb gone; the outputs might have changed")
        if self.write_delay:
            time.sleep(self.write_delay)
        self.writes.append((time.perf_counter(), dict(states)))


class FakeAlicat:
    """Record setpoints and answer reads from the last setpoint."""

    def __init__(self, name: str, full_scale: float = 500.0, *, realistic: bool = False) -> None:
        self.name = name
        self.full_scale = full_scale
        self.realistic = realistic
        self.setpoints: list[float] = []
        self.reads = 0
        self.prepared = False
        self.prepare_error: str | None = None
        self.read_delay = 0.0
        self.closed = False
        self._flow = 0.0
        self._random = random.Random(hash(name) & 0xFFFF)

    async def prepare_setpoints(self) -> tuple[float, str]:
        if self.prepare_error:
            raise DeviceError(self.prepare_error)
        self.prepared = True
        return self.full_scale, "SCCM"

    async def write_setpoint(self, flow_rate: float) -> float:
        if not self.prepared:
            raise DeviceError("Call prepare_setpoints before write_setpoint; no setpoint was sent.")
        self.setpoints.append(flow_rate)
        return flow_rate

    async def read(self) -> dict[str, object]:
        if self.read_delay:
            await asyncio.sleep(self.read_delay)
        self.reads += 1
        setpoint = self.setpoints[-1] if self.setpoints else 0.0
        if self.realistic:
            # First-order approach to the setpoint plus a little noise, like a real MFC.
            self._flow += (setpoint - self._flow) * LAG_FRACTION
            noise = self._random.gauss(0.0, NOISE_FRACTION * self.full_scale)
            mass_flow = round(max(0.0, self._flow + noise), 2)
        else:
            mass_flow = setpoint * 0.98
        return {"setpoint": setpoint, "mass_flow": mass_flow, "pressure": 14.7}


class FakeRig:
    """One fake LabJack and one fake Alicat for each MFC name."""

    def __init__(
        self,
        mfc_names: Iterable[str] = DEFAULT_MFCS,
        full_scales: dict[str, float] | None = None,
        *,
        realistic: bool = False,
    ) -> None:
        self.labjack = FakeLabJack()
        self.alicats = {
            name: FakeAlicat(name, (full_scales or {}).get(name, 500.0), realistic=realistic)
            for name in mfc_names
        }
        self.labjack_opens = 0
        self.labjack_error: str | None = None

    @contextmanager
    def open_labjack(self, serial_number):
        self.labjack_opens += 1
        if self.labjack_error:
            raise DeviceError(self.labjack_error)
        try:
            yield self.labjack
        finally:
            self.labjack.closed = True

    def _alicat_for(self, port: str) -> FakeAlicat:
        return next(alicat for alicat in self.alicats.values() if alicat.name in port)

    @asynccontextmanager
    async def open_alicat(self, port, unit, baud_rate, timeout_seconds):
        alicat = self._alicat_for(port)
        try:
            yield alicat
        finally:
            alicat.closed = True

    async def read_full_scale(self, port, unit, baud_rate, timeout_seconds) -> tuple[float, str]:
        """Stand-in for hardware.alicat_full_scale."""
        return self._alicat_for(port).full_scale, "SCCM"
