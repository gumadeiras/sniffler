"""Fake LabJack and Alicat sessions for executor and GUI tests."""

import asyncio
import time
from contextlib import asynccontextmanager, contextmanager

from sniffler.hardware import DeviceError


class FakeLabJack:
    """Record every multi-line write with the time it was made."""

    def __init__(self) -> None:
        self.writes: list[tuple[float, dict[int, bool]]] = []
        self.fail_on_write: int | None = None
        self.write_delay = 0.0
        self.closed = False

    def write_digital_lines(self, states: dict[int, bool]) -> None:
        if self.fail_on_write is not None and len(self.writes) == self.fail_on_write:
            self.fail_on_write = None
            raise DeviceError("usb gone; the outputs might have changed")
        if self.write_delay:
            time.sleep(self.write_delay)
        self.writes.append((time.perf_counter(), dict(states)))


class FakeAlicat:
    """Record setpoints and answer reads from the last setpoint."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.setpoints: list[float] = []
        self.reads = 0
        self.prepared = False
        self.prepare_error: str | None = None
        self.read_delay = 0.0
        self.closed = False

    async def prepare_setpoints(self) -> tuple[float, str]:
        if self.prepare_error:
            raise DeviceError(self.prepare_error)
        self.prepared = True
        return 500.0, "SCCM"

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
        return {"setpoint": setpoint, "mass_flow": setpoint * 0.98, "pressure": 14.7}


class FakeRig:
    def __init__(self) -> None:
        self.labjack = FakeLabJack()
        self.alicats = {"mfc-500": FakeAlicat("mfc-500"), "mfc-2000": FakeAlicat("mfc-2000")}
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

    @asynccontextmanager
    async def open_alicat(self, port, unit, baud_rate, timeout_seconds):
        alicat = next(alicat for alicat in self.alicats.values() if alicat.name in port)
        try:
            yield alicat
        finally:
            alicat.closed = True
