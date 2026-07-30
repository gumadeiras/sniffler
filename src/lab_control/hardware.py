"""Hardware operations for the LabJack U3 and Alicat MFC."""

import math
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from typing import Any


class DeviceError(RuntimeError):
    """A hardware command failed."""


def normalize_alicat_flow(flow_rate: float) -> float:
    """Return the exact value that the Alicat driver will send."""
    if not math.isfinite(flow_rate):
        raise DeviceError("The Alicat flow rate must be finite; no setpoint was sent.")
    return float(f"{flow_rate:.2f}")


async def _read_alicat_mass_flow_full_scale(controller: Any) -> tuple[float, str]:
    """Read the Alicat mass-flow full scale and its unit."""
    try:
        response = await controller._write_and_read(f"{controller.unit}FPF 5")
    except Exception as error:
        raise DeviceError(
            f"Cannot read the Alicat mass-flow full scale; no setpoint was sent: {error}"
        ) from error

    try:
        response_unit, maximum_text, _unit_value, flow_unit = response.split(maxsplit=3)
        maximum = float(maximum_text)
    except (AttributeError, TypeError, ValueError) as error:
        raise DeviceError(
            f"Invalid Alicat mass-flow full-scale response {response!r}; no setpoint was sent."
        ) from error
    if response_unit != controller.unit or not math.isfinite(maximum) or maximum <= 0:
        raise DeviceError(
            f"Invalid Alicat mass-flow full-scale response {response!r}; no setpoint was sent."
        )
    return maximum, flow_unit


def list_serial_ports() -> list[tuple[str, str]]:
    """Return available serial ports and their descriptions."""
    from serial.tools import list_ports

    try:
        return [(port.device, port.description) for port in list_ports.comports()]
    except Exception as error:
        raise DeviceError(f"Cannot list serial ports: {error}") from error


@contextmanager
def _open_labjack(serial_number: int | None) -> Iterator[Any]:
    device = None
    try:
        import u3

        options = {} if serial_number is None else {"firstFound": False, "serial": serial_number}
        device = u3.U3(**options)
        device.getCalibrationData()
    except Exception as error:
        if device is not None:
            with suppress(Exception):
                device.close()
        raise DeviceError(f"Cannot connect to the LabJack U3: {error}") from error

    try:
        yield device
    finally:
        closing_during_error = sys.exc_info()[0] is not None
        try:
            device.close()
        except Exception as error:
            if not closing_during_error:
                raise DeviceError(f"Cannot close the LabJack U3: {error}") from error


def labjack_status(serial_number: int | None = None) -> dict[str, object]:
    """Connect to a U3 and return its identity."""
    with _open_labjack(serial_number) as device:
        try:
            configuration = device.configU3()
        except Exception as error:
            raise DeviceError(f"Cannot read the LabJack U3 configuration: {error}") from error
        analog_mask = int(configuration.get("FIOAnalog", 0))
        analog_channels = [str(channel) for channel in range(8) if analog_mask & (1 << channel)]
        digital_channels = [
            str(channel) for channel in range(8) if not analog_mask & (1 << channel)
        ]
        return {
            "serial_number": configuration.get("SerialNumber"),
            "local_id": configuration.get("LocalID"),
            "hardware_version": configuration.get("HardwareVersion"),
            "firmware_version": configuration.get("FirmwareVersion"),
            "analog_fio_channels": ", ".join(analog_channels) or "none",
            "digital_fio_channels": ", ".join(digital_channels) or "none",
        }


def read_labjack_analog(channel: int, serial_number: int | None = None) -> float:
    """Read a U3 input that is configured as analog."""
    with _open_labjack(serial_number) as device:
        try:
            analog_mask = int(device.configU3().get("FIOAnalog", 0))
            if not analog_mask & (1 << channel):
                raise DeviceError(f"FIO{channel} is not configured as an analog input.")
            return float(device.getAIN(channel))
        except DeviceError:
            raise
        except Exception as error:
            raise DeviceError(f"Cannot read LabJack AIN{channel}: {error}") from error


def set_labjack_digital(channel: int, state: bool, serial_number: int | None = None) -> bool:
    """Set and read a U3 line that is configured as digital."""
    with _open_labjack(serial_number) as device:
        try:
            analog_mask = int(device.configU3().get("FIOAnalog", 0))
        except Exception as error:
            raise DeviceError(f"Cannot read the LabJack U3 configuration: {error}") from error
        if analog_mask & (1 << channel):
            raise DeviceError(f"FIO{channel} is configured as analog; no output was changed.")

        try:
            device.setDOState(channel, int(state))
        except Exception as error:
            raise DeviceError(
                f"Cannot confirm the FIO{channel} write; the output might have changed: {error}"
            ) from error
        try:
            return bool(device.getDIOState(channel))
        except Exception as error:
            raise DeviceError(
                f"FIO{channel} was written, but its reported state cannot be read: {error}"
            ) from error


@asynccontextmanager
async def _open_alicat(
    port: str, unit: str, baud_rate: int, timeout_seconds: float
) -> AsyncIterator[Any]:
    controller = None
    try:
        from alicat.driver import FlowController

        controller = FlowController(
            address=port,
            unit=unit,
            baudrate=baud_rate,
            timeout=timeout_seconds,
        )
    except Exception as error:
        raise DeviceError(f"Cannot open the Alicat MFC: {error}") from error

    try:
        yield controller
    finally:
        closing_during_error = sys.exc_info()[0] is not None
        try:
            await controller.close()
        except Exception as error:
            if not closing_during_error:
                raise DeviceError(f"Cannot close the Alicat MFC: {error}") from error


async def alicat_status(
    port: str,
    unit: str = "A",
    baud_rate: int = 19200,
    timeout_seconds: float = 0.15,
) -> dict[str, object]:
    """Read the current Alicat state."""
    async with _open_alicat(port, unit, baud_rate, timeout_seconds) as controller:
        try:
            return await controller.get()
        except Exception as error:
            raise DeviceError(f"Cannot read the Alicat MFC: {error}") from error


async def set_alicat_flow(
    port: str,
    flow_rate: float,
    unit: str = "A",
    baud_rate: int = 19200,
    timeout_seconds: float = 0.15,
) -> tuple[float, str | None]:
    """Set and verify the Alicat mass-flow setpoint."""
    applied_flow = normalize_alicat_flow(flow_rate)

    async with _open_alicat(port, unit, baud_rate, timeout_seconds) as controller:
        try:
            state = await controller.get()
        except Exception as error:
            raise DeviceError(
                f"Cannot confirm the Alicat control mode; no setpoint was sent: {error}"
            ) from error

        control_point = state.get("control_point")
        if control_point != "mass flow":
            raise DeviceError(
                f"Refusing to change the setpoint while the control point is {control_point!r}. "
                "Set the controller to mass flow first."
            )

        flow_unit = None
        if applied_flow != 0:
            maximum, flow_unit = await _read_alicat_mass_flow_full_scale(controller)
            if abs(applied_flow) > maximum:
                raise DeviceError(
                    f"The requested flow exceeds the Alicat full scale of "
                    f"{maximum:g} {flow_unit}; no setpoint was sent."
                )

        try:
            await controller.set_flow_rate(applied_flow)
        except Exception as error:
            raise DeviceError(
                "Cannot confirm the Alicat setpoint write; "
                f"the setpoint might have changed: {error}"
            ) from error
        return applied_flow, flow_unit
