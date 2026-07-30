"""Command-line interface for the laboratory hardware."""

import argparse
import asyncio
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from lab_control import hardware


def _run_with_homebrew_exodriver(argv: Sequence[str] | None) -> int | None:
    """Restart LabJack commands with the Apple Silicon Homebrew driver path."""
    if argv is not None or sys.platform != "darwin" or sys.argv[1:2] != ["labjack"]:
        return None

    driver_directory = Path("/opt/homebrew/lib")
    driver = driver_directory / "liblabjackusb.dylib"
    current_path = os.environ.get("DYLD_LIBRARY_PATH", "").split(os.pathsep)
    if not driver.exists() or str(driver_directory) in current_path:
        return None

    environment = os.environ.copy()
    environment["DYLD_LIBRARY_PATH"] = os.pathsep.join(
        [str(driver_directory), *filter(None, current_path)]
    )
    command = [sys.executable, "-m", "lab_control.cli", *sys.argv[1:]]
    return subprocess.run(command, env=environment, check=False).returncode


def _nonnegative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("flow rate must be zero or greater")
    return number


def _print_values(values: dict[str, object]) -> None:
    for name, value in values.items():
        print(f"{name.replace('_', ' ').capitalize()}: {value}")


def _add_labjack_connection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--serial", type=int, help="Use this LabJack serial number.")


def _add_alicat_connection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", required=True, help="Serial port, such as COM3 or /dev/ttyUSB0.")
    parser.add_argument("--unit", default="A", choices=tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))


def _ports(_arguments: argparse.Namespace) -> None:
    ports = hardware.list_serial_ports()
    if not ports:
        print("No serial ports found.")
        return
    for device, description in ports:
        print(f"{device}: {description}")


def _labjack_status(arguments: argparse.Namespace) -> None:
    values = hardware.labjack_status(arguments.serial)
    print("LabJack U3 connected.")
    _print_values(values)


def _labjack_read_analog(arguments: argparse.Namespace) -> None:
    voltage = hardware.read_labjack_analog(arguments.channel, arguments.serial)
    print(f"AIN{arguments.channel}: {voltage:.6f} V")


def _labjack_set_digital(arguments: argparse.Namespace) -> None:
    expected = arguments.state == "high"
    actual = hardware.set_labjack_digital(arguments.channel, expected, arguments.serial)
    print(f"FIO{arguments.channel}: {'high' if actual else 'low'}")


def _alicat_status(arguments: argparse.Namespace) -> None:
    values = asyncio.run(hardware.alicat_status(arguments.port, arguments.unit))
    print("Alicat MFC connected.")
    _print_values(values)


def _alicat_set_flow(arguments: argparse.Namespace) -> None:
    values = asyncio.run(
        hardware.set_alicat_flow(arguments.port, arguments.flow_rate, arguments.unit)
    )
    print("Alicat flow setpoint changed.")
    _print_values(values)


def _alicat_stop(arguments: argparse.Namespace) -> None:
    values = asyncio.run(hardware.set_alicat_flow(arguments.port, 0.0, arguments.unit))
    print("Alicat flow setpoint is zero.")
    _print_values(values)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab-control",
        description="Control a LabJack U3 and an Alicat mass flow controller.",
    )
    commands = parser.add_subparsers(dest="device", required=True)

    ports = commands.add_parser("ports", help="List available serial ports.")
    ports.set_defaults(handler=_ports)

    labjack = commands.add_parser("labjack", help="Connect to a LabJack U3.")
    labjack_commands = labjack.add_subparsers(dest="labjack_command", required=True)

    labjack_status = labjack_commands.add_parser("status", help="Check the connection.")
    _add_labjack_connection(labjack_status)
    labjack_status.set_defaults(handler=_labjack_status)

    read_analog = labjack_commands.add_parser("read-analog", help="Read AIN0 through AIN3.")
    read_analog.add_argument("--channel", type=int, choices=range(4), default=0)
    _add_labjack_connection(read_analog)
    read_analog.set_defaults(handler=_labjack_read_analog)

    set_digital = labjack_commands.add_parser(
        "set-digital", help="Set default digital output FIO4 through FIO7."
    )
    set_digital.add_argument("--channel", type=int, choices=range(4, 8), default=4)
    set_digital.add_argument("--state", required=True, choices=("high", "low"))
    _add_labjack_connection(set_digital)
    set_digital.set_defaults(handler=_labjack_set_digital)

    alicat = commands.add_parser("alicat", help="Connect to an Alicat MFC.")
    alicat_commands = alicat.add_subparsers(dest="alicat_command", required=True)

    alicat_status = alicat_commands.add_parser("status", help="Read the current state.")
    _add_alicat_connection(alicat_status)
    alicat_status.set_defaults(handler=_alicat_status)

    set_flow = alicat_commands.add_parser("set-flow", help="Set the mass flow rate.")
    set_flow.add_argument("flow_rate", type=_nonnegative_float)
    _add_alicat_connection(set_flow)
    set_flow.set_defaults(handler=_alicat_set_flow)

    stop = alicat_commands.add_parser("stop", help="Set the flow rate to zero.")
    _add_alicat_connection(stop)
    stop.set_defaults(handler=_alicat_stop)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line tool."""
    restarted_status = _run_with_homebrew_exodriver(argv)
    if restarted_status is not None:
        return restarted_status

    parser = _build_parser()
    arguments = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], None] = arguments.handler
    try:
        handler(arguments)
    except hardware.DeviceError as error:
        print(f"Error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
