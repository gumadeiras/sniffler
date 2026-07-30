"""Command-line interface for the laboratory hardware."""

import argparse
import asyncio
import math
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from lab_control import hardware
from lab_control.config import AlicatSettings, ConfigError, Settings, load_settings


def _run_with_homebrew_exodriver(argv: Sequence[str] | None) -> int | None:
    """Restart LabJack commands with the Apple Silicon Homebrew driver path."""
    if argv is not None or sys.platform != "darwin":
        return None
    command_arguments = sys.argv[1:]
    if command_arguments[:1] == ["--config"]:
        command_arguments = command_arguments[2:]
    elif command_arguments[:1] and command_arguments[0].startswith("--config="):
        command_arguments = command_arguments[1:]
    if command_arguments[:1] != ["labjack"]:
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


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("flow rate must be finite")
    return number


def _print_values(values: dict[str, object], units: dict[str, str] | None = None) -> None:
    units = units or {}
    for name, value in values.items():
        label = name.replace("_", " ").capitalize()
        if name in {"pressure", "temperature", "volumetric_flow", "mass_flow", "setpoint"}:
            unit = units.get(name, "device units not configured")
            print(f"{label}: {value} {unit}")
        else:
            print(f"{label}: {value}")


def _add_labjack_connection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--serial", type=int, help="Override the LabJack serial number in lab.toml."
    )


def _add_alicat_connection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--name",
        help="Configured Alicat name. Required when lab.toml contains more than one Alicat.",
    )
    parser.add_argument(
        "--port", help="Override the Alicat port in lab.toml, such as COM3 or /dev/ttyUSB0."
    )
    parser.add_argument(
        "--unit",
        choices=tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        help="Override the Alicat unit ID in lab.toml.",
    )


def _labjack_serial(arguments: argparse.Namespace, settings: Settings) -> int | None:
    serial = arguments.serial if arguments.serial is not None else settings.labjack_serial
    if serial is not None and serial <= 0:
        raise ConfigError("The LabJack serial number must be greater than zero.")
    return serial


def _select_alicat(arguments: argparse.Namespace, settings: Settings) -> tuple[str, AlicatSettings]:
    if arguments.name:
        try:
            return arguments.name, settings.alicats[arguments.name]
        except KeyError as error:
            available = ", ".join(sorted(settings.alicats))
            raise ConfigError(
                f"Unknown Alicat name {arguments.name!r}. Available names: {available}."
            ) from error
    if len(settings.alicats) > 1:
        available = ", ".join(sorted(settings.alicats))
        raise ConfigError(f"Select an Alicat with --name. Available names: {available}.")
    return next(iter(settings.alicats.items()))


def _alicat_connection(
    arguments: argparse.Namespace, settings: AlicatSettings
) -> tuple[str, str, int, float]:
    port = arguments.port or settings.port
    if not port:
        raise ConfigError("Set the selected Alicat port in lab.toml or use --port.")
    if not port.strip():
        raise ConfigError("The Alicat port must not be empty.")
    unit = arguments.unit or settings.unit
    return port, unit, settings.baud_rate, settings.timeout_seconds


def _alicat_units(settings: AlicatSettings, control_point: object) -> dict[str, str]:
    units = dict(settings.units)
    if control_point == "mass flow" and "mass_flow" in units:
        units["setpoint"] = units["mass_flow"]
    elif control_point == "vol flow" and "volumetric_flow" in units:
        units["setpoint"] = units["volumetric_flow"]
    elif isinstance(control_point, str) and "pressure" in control_point and "pressure" in units:
        units["setpoint"] = units["pressure"]
    return units


def _validate_requested_flow(flow_rate: float, settings: AlicatSettings) -> float:
    maximum = settings.maximum_flow
    if flow_rate < 0 and not settings.allow_negative_flow:
        raise ConfigError("Negative flow is disabled in lab.toml.")
    applied_flow = hardware.normalize_alicat_flow(flow_rate)
    if applied_flow < settings.minimum_flow:
        unit = settings.units.get("mass_flow", "current device units")
        raise ConfigError(f"Flow must be at least {settings.minimum_flow} {unit}.")
    if maximum is not None and applied_flow > maximum:
        unit = settings.units.get("mass_flow", "current device units")
        raise ConfigError(f"Flow must be at most the configured limit of {maximum} {unit}.")
    return applied_flow


def _alicat_label(name: str) -> str:
    return "Alicat" if name == "default" else f"Alicat {name}"


def _ports(_arguments: argparse.Namespace, _settings: Settings) -> None:
    ports = hardware.list_serial_ports()
    if not ports:
        print("No serial ports found.")
        return
    for device, description in ports:
        print(f"{device}: {description}")


def _labjack_status(arguments: argparse.Namespace, settings: Settings) -> None:
    values = hardware.labjack_status(_labjack_serial(arguments, settings))
    print("LabJack U3 connected.")
    _print_values(values)


def _labjack_read_analog(arguments: argparse.Namespace, settings: Settings) -> None:
    voltage = hardware.read_labjack_analog(arguments.channel, _labjack_serial(arguments, settings))
    print(f"AIN{arguments.channel}: {voltage:.6f} V")


def _labjack_set_digital(arguments: argparse.Namespace, settings: Settings) -> None:
    expected = arguments.state == "high"
    actual = hardware.set_labjack_digital(
        arguments.channel, expected, _labjack_serial(arguments, settings)
    )
    print(f"FIO{arguments.channel} reported state: {'high' if actual else 'low'}")


def _alicat_status(arguments: argparse.Namespace, settings: Settings) -> None:
    name, alicat = _select_alicat(arguments, settings)
    connection = _alicat_connection(arguments, alicat)
    values = asyncio.run(hardware.alicat_status(*connection))
    label = "Alicat MFC" if name == "default" else _alicat_label(name)
    print(f"{label} connected.")
    _print_values(values, _alicat_units(alicat, values.get("control_point")))


def _alicat_set_flow(arguments: argparse.Namespace, settings: Settings) -> None:
    name, alicat = _select_alicat(arguments, settings)
    applied_flow = _validate_requested_flow(arguments.flow_rate, alicat)
    port, unit, baud_rate, timeout_seconds = _alicat_connection(arguments, alicat)
    applied_flow, device_unit = asyncio.run(
        hardware.set_alicat_flow(
            port,
            applied_flow,
            unit,
            baud_rate,
            timeout_seconds,
        )
    )
    flow_unit = device_unit or alicat.units.get("mass_flow", "device units")
    print(f"{_alicat_label(name)} mass-flow setpoint changed to {applied_flow:.2f} {flow_unit}.")


def _alicat_stop(arguments: argparse.Namespace, settings: Settings) -> None:
    name, alicat = _select_alicat(arguments, settings)
    port, unit, baud_rate, timeout_seconds = _alicat_connection(arguments, alicat)
    asyncio.run(
        hardware.set_alicat_flow(
            port,
            0.0,
            unit,
            baud_rate,
            timeout_seconds,
        )
    )
    print(f"{_alicat_label(name)} mass-flow setpoint changed to zero.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab-control",
        description="Control a LabJack U3 and Alicat mass flow controllers.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("lab.toml"),
        help="Configuration file. Default: lab.toml.",
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
    read_analog.add_argument(
        "--channel", type=int, choices=range(4), default=0, help="Analog FIO channel."
    )
    _add_labjack_connection(read_analog)
    read_analog.set_defaults(handler=_labjack_read_analog)

    set_digital = labjack_commands.add_parser(
        "set-digital", help="Set default digital output FIO4 through FIO7."
    )
    set_digital.add_argument(
        "--channel", type=int, choices=range(4, 8), default=4, help="Digital FIO channel."
    )
    set_digital.add_argument(
        "--state", required=True, choices=("high", "low"), help="Requested output state."
    )
    _add_labjack_connection(set_digital)
    set_digital.set_defaults(handler=_labjack_set_digital)

    alicat = commands.add_parser("alicat", help="Connect to an Alicat MFC.")
    alicat_commands = alicat.add_subparsers(dest="alicat_command", required=True)

    alicat_status = alicat_commands.add_parser("status", help="Read the current state.")
    _add_alicat_connection(alicat_status)
    alicat_status.set_defaults(handler=_alicat_status)

    set_flow = alicat_commands.add_parser("set-flow", help="Set the mass flow rate.")
    set_flow.add_argument(
        "flow_rate", type=_finite_float, help="Target in the device mass-flow unit."
    )
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
    try:
        settings = load_settings(arguments.config)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    handler: Callable[[argparse.Namespace, Settings], None] = arguments.handler
    try:
        handler(arguments, settings)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except hardware.DeviceError as error:
        print(f"Hardware error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
