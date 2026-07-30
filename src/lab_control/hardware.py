"""Hardware operations for the LabJack U3 and Alicat MFC."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any


class DeviceError(RuntimeError):
    """A hardware command failed."""


def list_serial_ports() -> list[tuple[str, str]]:
    """Return available serial ports and their descriptions."""
    from serial.tools import list_ports

    return [(port.device, port.description) for port in list_ports.comports()]


@contextmanager
def _open_labjack(serial_number: int | None) -> Iterator[Any]:
    device = None
    command_failed = False
    try:
        import u3

        options = {} if serial_number is None else {"firstFound": False, "serial": serial_number}
        device = u3.U3(**options)
        device.getCalibrationData()
        yield device
    except Exception as error:
        command_failed = True
        raise DeviceError(f"LabJack U3 command failed: {error}") from error
    finally:
        if device is not None:
            try:
                device.close()
            except Exception as error:
                if not command_failed:
                    raise DeviceError(f"LabJack U3 close failed: {error}") from error


def labjack_status(serial_number: int | None = None) -> dict[str, object]:
    """Connect to a U3 and return its identity."""
    with _open_labjack(serial_number) as device:
        configuration = device.configU3()
        return {
            "serial_number": configuration.get("SerialNumber"),
            "local_id": configuration.get("LocalID"),
            "hardware_version": configuration.get("HardwareVersion"),
            "firmware_version": configuration.get("FirmwareVersion"),
        }


def read_labjack_analog(channel: int, serial_number: int | None = None) -> float:
    """Read one of the default U3 analog inputs."""
    with _open_labjack(serial_number) as device:
        return float(device.getAIN(channel))


def set_labjack_digital(channel: int, state: bool, serial_number: int | None = None) -> bool:
    """Set and read one of the default U3 digital outputs."""
    with _open_labjack(serial_number) as device:
        device.setDOState(channel, int(state))
        return bool(device.getDIOState(channel))


@asynccontextmanager
async def _open_alicat(port: str, unit: str) -> AsyncIterator[Any]:
    try:
        from alicat.driver import FlowController

        async with FlowController(address=port, unit=unit) as controller:
            yield controller
    except Exception as error:
        raise DeviceError(f"Alicat MFC command failed: {error}") from error


async def alicat_status(port: str, unit: str = "A") -> dict[str, object]:
    """Read the current Alicat state."""
    async with _open_alicat(port, unit) as controller:
        return await controller.get()


async def set_alicat_flow(port: str, flow_rate: float, unit: str = "A") -> dict[str, object]:
    """Set the Alicat flow rate and return its new state."""
    async with _open_alicat(port, unit) as controller:
        await controller.set_flow_rate(flow_rate)
        return await controller.get()
