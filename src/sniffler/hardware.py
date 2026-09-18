"""Hardware operations for the LabJack U3 and Alicat MFC."""

import math
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import Any

HOMEBREW_DRIVER_DIRECTORY = Path("/opt/homebrew/lib")


class DeviceError(RuntimeError):
    """A hardware command failed."""


_ALICAT_SETPOINT_SOURCES = {
    "A": "analog",
    "S": "serial or display, saved",
    "U": "serial or display, zero on power-up",
}


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


async def _read_alicat_setpoint_source(controller: Any) -> str:
    """Read the Alicat setpoint source."""
    try:
        response = await controller._write_and_read(f"{controller.unit}LSS")
    except Exception as error:
        raise DeviceError(f"Cannot read the Alicat setpoint source: {error}") from error

    values = response.split() if isinstance(response, str) else []
    if (
        len(values) != 2
        or values[0] != controller.unit
        or values[1] not in _ALICAT_SETPOINT_SOURCES
    ):
        raise DeviceError(f"Invalid Alicat setpoint-source response {response!r}.")
    return values[1]


def homebrew_exodriver_environment() -> dict[str, str] | None:
    """Return an environment where dyld finds the Homebrew Exodriver, or None when not needed.

    LabJackPython looks for liblabjackusb.dylib in /usr/local/lib. Homebrew on
    Apple silicon installs it in /opt/homebrew/lib, and dyld reads
    DYLD_LIBRARY_PATH only when the process starts.
    """
    if sys.platform != "darwin":
        return None
    driver = HOMEBREW_DRIVER_DIRECTORY / "liblabjackusb.dylib"
    current_path = os.environ.get("DYLD_LIBRARY_PATH", "").split(os.pathsep)
    if not driver.exists() or str(HOMEBREW_DRIVER_DIRECTORY) in current_path:
        return None
    environment = os.environ.copy()
    environment["DYLD_LIBRARY_PATH"] = os.pathsep.join(
        [str(HOMEBREW_DRIVER_DIRECTORY), *filter(None, current_path)]
    )
    return environment


def relaunch_with_homebrew_exodriver(module: str) -> int | None:
    """Run ``python -m module`` again with the Homebrew driver path when that is needed."""
    environment = homebrew_exodriver_environment()
    if environment is None:
        return None
    command = [sys.executable, "-m", module, *sys.argv[1:]]
    return subprocess.run(command, env=environment, check=False).returncode


def list_serial_ports() -> list[tuple[str, str]]:
    """Return available serial ports and their descriptions."""
    from serial.tools import list_ports

    try:
        return [(port.device, port.description) for port in list_ports.comports()]
    except Exception as error:
        raise DeviceError(f"Cannot list serial ports: {error}") from error


def digital_channel_name(channel: int) -> str:
    """Return the U3 line name for a unified digital channel number."""
    if channel < 8:
        return f"FIO{channel}"
    if channel < 16:
        return f"EIO{channel - 8}"
    return f"CIO{channel - 16}"


class LabJackSession:
    """An open U3 connection that serves repeated commands.

    The device configuration is read once, when the session opens. Change the
    U3 configuration outside a session.
    """

    def __init__(self, device: Any) -> None:
        self._device = device
        self._counter_previous: dict[str, Any] | None = None
        try:
            self._configuration = device.configU3()
        except Exception as error:
            raise DeviceError(f"Cannot read the LabJack U3 configuration: {error}") from error

    def status(self) -> dict[str, object]:
        """Return the device identity and the FIO channel configuration."""
        analog_mask = int(self._configuration.get("FIOAnalog", 0))
        analog_channels = [str(channel) for channel in range(8) if analog_mask & (1 << channel)]
        digital_channels = [
            str(channel) for channel in range(8) if not analog_mask & (1 << channel)
        ]
        return {
            "serial_number": self._configuration.get("SerialNumber"),
            "local_id": self._configuration.get("LocalID"),
            "hardware_version": self._configuration.get("HardwareVersion"),
            "firmware_version": self._configuration.get("FirmwareVersion"),
            "analog_fio_channels": ", ".join(analog_channels) or "none",
            "digital_fio_channels": ", ".join(digital_channels) or "none",
        }

    def read_analog(self, channel: int) -> float:
        """Read an input that is configured as analog."""
        try:
            if not int(self._configuration.get("FIOAnalog", 0)) & (1 << channel):
                raise DeviceError(f"FIO{channel} is not configured as an analog input.")
            return float(self._device.getAIN(channel))
        except DeviceError:
            raise
        except Exception as error:
            raise DeviceError(f"Cannot read LabJack AIN{channel}: {error}") from error

    def _require_digital(self, channel: int) -> str:
        name = digital_channel_name(channel)
        is_analog = False
        if channel < 8:
            is_analog = bool(int(self._configuration.get("FIOAnalog", 0)) & (1 << channel))
        elif channel < 16:
            is_analog = bool(int(self._configuration.get("EIOAnalog", 0)) & (1 << (channel - 8)))
        if is_analog:
            raise DeviceError(f"{name} is configured as analog; no output was changed.")
        return name

    def set_digital(self, channel: int, state: bool) -> bool:
        """Set a line that is configured as digital and read back its state."""
        name = self._require_digital(channel)

        try:
            self._device.setDOState(channel, int(state))
        except Exception as error:
            raise DeviceError(
                f"Cannot confirm the {name} write; the output might have changed: {error}"
            ) from error
        try:
            return bool(self._device.getDIOState(channel))
        except Exception as error:
            raise DeviceError(
                f"{name} was written, but its reported state cannot be read: {error}"
            ) from error

    def read_digital(self, channel: int) -> tuple[bool, bool]:
        """Return (is_input, level) of a digital line. Nothing on the device changes."""
        name = self._require_digital(channel)
        import u3

        try:
            direction, level = self._device.getFeedback(
                u3.BitDirRead(IONumber=channel), u3.BitStateRead(IONumber=channel)
            )
        except Exception as error:
            raise DeviceError(f"Cannot read {name}: {error}") from error
        return not bool(direction), bool(level)

    def enable_counter(self, channel: int) -> None:
        """Count pulses on a digital line with hardware counter 0.

        This changes the U3 timer and counter configuration on purpose; call
        ``disable_counter`` to put it back. The U3 can place the counter on FIO4
        through EIO0 only.
        """
        name = self._require_digital(channel)
        if channel not in range(4, 9):
            raise DeviceError(f"{name} cannot host the pulse counter; use FIO4 through EIO0.")
        try:
            previous = self._device.configIO()
            self._device.configIO(
                EnableCounter0=True, NumberOfTimersEnabled=0, TimerCounterPinOffset=channel
            )
        except Exception as error:
            raise DeviceError(
                f"Cannot enable the pulse counter on {name}; "
                f"the U3 timer and counter configuration might have changed: {error}"
            ) from error
        self._counter_previous = {
            key: previous[key]
            for key in (
                "EnableCounter0",
                "EnableCounter1",
                "NumberOfTimersEnabled",
                "TimerCounterPinOffset",
            )
        }

    def disable_counter(self) -> None:
        """Restore the timer and counter configuration that ``enable_counter`` replaced."""
        previous = self._counter_previous
        if previous is None:
            return
        try:
            self._device.configIO(**previous)
        except Exception as error:
            raise DeviceError(
                "Cannot restore the U3 timer and counter configuration; "
                f"it might have changed: {error}"
            ) from error
        self._counter_previous = None

    def read_counter(self, reset: bool = False) -> int:
        """Return the pulse count of counter 0, and reset it when asked."""
        import u3

        try:
            (count,) = self._device.getFeedback(u3.Counter(counter=0, Reset=reset))
        except Exception as error:
            raise DeviceError(f"Cannot read the pulse counter: {error}") from error
        return int(count)

    def write_digital_lines(
        self, states: dict[int, bool], *, read_counter: bool = False
    ) -> int | None:
        """Set several digital output lines in one device transaction.

        Every listed line becomes an output with the given state at the same
        time. Lines that are not listed do not change. There is no readback,
        so a failed write reports that the outputs might have changed. With
        ``read_counter`` the same packet also reads counter 0 right after the
        lines switch and returns the count; otherwise the result is None.
        """
        names = [self._require_digital(channel) for channel in states]
        if not states:
            return None
        import u3

        mask = [0, 0, 0]
        levels = [0, 0, 0]
        for channel, state in states.items():
            port, bit = divmod(channel, 8)
            mask[port] |= 1 << bit
            if state:
                levels[port] |= 1 << bit
        commands = [
            u3.PortDirWrite(Direction=mask, WriteMask=mask),
            u3.PortStateWrite(State=levels, WriteMask=mask),
        ]
        if read_counter:
            commands.append(u3.Counter(counter=0, Reset=False))
        try:
            results = self._device.getFeedback(*commands)
        except Exception as error:
            raise DeviceError(
                f"Cannot confirm the write to {', '.join(names)}; "
                f"the outputs might have changed: {error}"
            ) from error
        return int(results[-1]) if read_counter else None


@contextmanager
def open_labjack(serial_number: int | None = None) -> Iterator[LabJackSession]:
    """Open a U3 and keep it open for the whole block."""
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
        if isinstance(error, AttributeError) and "LJUSB" in str(error):
            raise DeviceError(
                "The LabJack driver is not loaded. Install the Exodriver on macOS or Linux, "
                "or the UD driver on Windows, and start the program again."
            ) from error
        raise DeviceError(f"Cannot connect to the LabJack U3: {error}") from error

    try:
        yield LabJackSession(device)
    finally:
        closing_during_error = sys.exc_info()[0] is not None
        try:
            device.close()
        except Exception as error:
            if not closing_during_error:
                raise DeviceError(f"Cannot close the LabJack U3: {error}") from error


def labjack_status(serial_number: int | None = None) -> dict[str, object]:
    """Connect to a U3 and return its identity."""
    with open_labjack(serial_number) as session:
        return session.status()


def read_labjack_analog(channel: int, serial_number: int | None = None) -> float:
    """Read a U3 input that is configured as analog."""
    with open_labjack(serial_number) as session:
        return session.read_analog(channel)


def read_labjack_digital(channel: int, serial_number: int | None = None) -> tuple[bool, bool]:
    """Return (is_input, level) of a U3 digital line without changing it."""
    with open_labjack(serial_number) as session:
        return session.read_digital(channel)


def set_labjack_digital(channel: int, state: bool, serial_number: int | None = None) -> bool:
    """Set and read a U3 line that is configured as digital."""
    with open_labjack(serial_number) as session:
        return session.set_digital(channel, state)


async def _check_setpoint_control(controller: Any) -> None:
    """Refuse serial setpoints unless the controller is in mass-flow, source U."""
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

    try:
        source = await _read_alicat_setpoint_source(controller)
    except DeviceError as error:
        raise DeviceError(
            f"Cannot confirm the Alicat setpoint source; no setpoint was sent: {error}"
        ) from error
    if source != "U":
        description = _ALICAT_SETPOINT_SOURCES[source]
        raise DeviceError(
            f"Refusing to change the setpoint while its source is {description}. "
            "Set the source to U for serial control with zero on power-up."
        )


class AlicatSession:
    """An open Alicat connection that serves repeated commands."""

    def __init__(self, controller: Any) -> None:
        self._controller = controller
        self._full_scale: tuple[float, str] | None = None

    async def read(self) -> dict[str, object]:
        """Read the current state with one device round trip."""
        try:
            return await self._controller.get()
        except Exception as error:
            raise DeviceError(f"Cannot read the Alicat MFC: {error}") from error

    async def prepare_setpoints(self) -> tuple[float, str]:
        """Check the control mode once and read the full scale for later writes.

        The setpoint source does not change during a run, so ``write_setpoint``
        does not read it again.
        """
        await _check_setpoint_control(self._controller)
        self._full_scale = await _read_alicat_mass_flow_full_scale(self._controller)
        return self._full_scale

    async def write_setpoint(self, flow_rate: float) -> float:
        """Write a mass-flow setpoint after ``prepare_setpoints`` checked the device."""
        if self._full_scale is None:
            raise DeviceError("Call prepare_setpoints before write_setpoint; no setpoint was sent.")
        applied_flow = normalize_alicat_flow(flow_rate)
        maximum, flow_unit = self._full_scale
        if abs(applied_flow) > maximum:
            raise DeviceError(
                f"The requested flow exceeds the Alicat full scale of "
                f"{maximum:g} {flow_unit}; no setpoint was sent."
            )
        try:
            await self._controller.set_flow_rate(applied_flow)
        except Exception as error:
            raise DeviceError(
                "Cannot confirm the Alicat setpoint write; "
                f"the setpoint might have changed: {error}"
            ) from error
        return applied_flow

    async def status(self) -> dict[str, object]:
        """Read the current state."""
        try:
            state = await self._controller.get()
        except Exception as error:
            raise DeviceError(f"Cannot read the Alicat MFC: {error}") from error
        try:
            source = await _read_alicat_setpoint_source(self._controller)
        except DeviceError:
            state["setpoint_source"] = "unavailable"
        else:
            state["setpoint_source"] = _ALICAT_SETPOINT_SOURCES[source]
        return state

    async def set_flow(self, flow_rate: float) -> tuple[float, str | None]:
        """Set and verify the mass-flow setpoint."""
        applied_flow = normalize_alicat_flow(flow_rate)
        controller = self._controller
        await _check_setpoint_control(controller)

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


@asynccontextmanager
async def open_alicat(
    port: str,
    unit: str = "A",
    baud_rate: int = 19200,
    timeout_seconds: float = 0.15,
) -> AsyncIterator[AlicatSession]:
    """Open an Alicat MFC and keep it open for the whole block."""
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
        yield AlicatSession(controller)
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
    async with open_alicat(port, unit, baud_rate, timeout_seconds) as session:
        return await session.status()


async def alicat_full_scale(
    port: str,
    unit: str = "A",
    baud_rate: int = 19200,
    timeout_seconds: float = 0.15,
) -> tuple[float, str]:
    """Read the Alicat mass-flow full scale and unit. This changes nothing."""
    async with open_alicat(port, unit, baud_rate, timeout_seconds) as session:
        return await _read_alicat_mass_flow_full_scale(session._controller)


async def set_alicat_flow(
    port: str,
    flow_rate: float,
    unit: str = "A",
    baud_rate: int = 19200,
    timeout_seconds: float = 0.15,
) -> tuple[float, str | None]:
    """Set and verify the Alicat mass-flow setpoint."""
    applied_flow = normalize_alicat_flow(flow_rate)
    async with open_alicat(port, unit, baud_rate, timeout_seconds) as session:
        return await session.set_flow(applied_flow)
