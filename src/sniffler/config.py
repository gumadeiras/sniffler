"""Load lab-specific settings from a TOML file."""

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_MISSING = object()


class ConfigError(ValueError):
    """The lab configuration is missing or invalid."""


@dataclass(frozen=True)
class AlicatSettings:
    """Settings for one Alicat controller."""

    port: str | None = None
    unit: str = "A"
    baud_rate: int = 19200
    timeout_seconds: float = 0.15
    minimum_flow: float = 0.0
    maximum_flow: float | None = None
    allow_negative_flow: bool = False
    units: dict[str, str] = field(default_factory=dict)


DIGITAL_OUTPUT_CHANNELS = range(4, 20)
TRIGGER_EDGES = ("rising", "falling")


@dataclass(frozen=True)
class TriggerSettings:
    """A TTL input that can start the trial schedule of a run."""

    channel: int
    edge: str = "rising"
    timeout_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"channel": self.channel, "edge": self.edge, "timeout_seconds": self.timeout_seconds}


@dataclass(frozen=True)
class Settings:
    """Settings that differ between computers or connected devices."""

    labjack_serial: int | None = None
    alicats: dict[str, AlicatSettings] = field(
        default_factory=lambda: {"default": AlicatSettings()}
    )
    valves: dict[str, int] = field(default_factory=dict)
    runs_directory: Path = Path("runs")
    trigger: TriggerSettings | None = None


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table.")
    return value


def _value(table: dict[str, Any], key: str, expected_type: type, default: Any) -> Any:
    value = table.get(key, _MISSING)
    if value is _MISSING:
        return default
    if expected_type is int:
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected_type is float:
        valid = isinstance(value, int | float) and not isinstance(value, bool)
    else:
        valid = isinstance(value, expected_type)
    if not valid:
        raise ConfigError(f"{key} must be {expected_type.__name__}.")
    return expected_type(value)


def _reject_unknown(table: dict[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(table.keys() - allowed)
    if unknown:
        names = ", ".join(unknown)
        raise ConfigError(f"Unknown setting in {location}: {names}.")


_ALICAT_KEYS = {
    "port",
    "unit",
    "baud_rate",
    "timeout_seconds",
    "minimum_flow",
    "maximum_flow",
    "allow_negative_flow",
    "units",
}


def _parse_alicat(alicat: dict[str, Any], location: str) -> AlicatSettings:
    units = _table(alicat, "units")
    _reject_unknown(alicat, _ALICAT_KEYS, location)
    _reject_unknown(
        units,
        {"pressure", "temperature", "volumetric_flow", "mass_flow"},
        f"{location}.units",
    )
    parsed_units = {
        name: _value(units, name, str, None)
        for name in ("pressure", "temperature", "volumetric_flow", "mass_flow")
        if name in units
    }
    return AlicatSettings(
        port=_value(alicat, "port", str, None),
        unit=_value(alicat, "unit", str, "A"),
        baud_rate=_value(alicat, "baud_rate", int, 19200),
        timeout_seconds=_value(alicat, "timeout_seconds", float, 0.15),
        minimum_flow=_value(alicat, "minimum_flow", float, 0.0),
        maximum_flow=_value(alicat, "maximum_flow", float, None),
        allow_negative_flow=_value(alicat, "allow_negative_flow", bool, False),
        units=parsed_units,
    )


def _parse_alicats(alicat: dict[str, Any]) -> dict[str, AlicatSettings]:
    if not alicat:
        return {"default": AlicatSettings()}
    if alicat.keys() & _ALICAT_KEYS:
        return {"default": _parse_alicat(alicat, "[alicat]")}

    controllers: dict[str, AlicatSettings] = {}
    for name, values in alicat.items():
        if not isinstance(values, dict):
            raise ConfigError(f"[alicat].{name} must be a table.")
        controllers[name] = _parse_alicat(values, f"[alicat.{name}]")
    return controllers


def _parse_valves(valves: dict[str, Any]) -> dict[str, int]:
    channels: dict[str, int] = {}
    for name, channel in valves.items():
        if not name.strip():
            raise ConfigError("A valve name in [valves] must not be empty.")
        if isinstance(channel, bool) or not isinstance(channel, int):
            raise ConfigError(f"valves.{name} must be a digital channel number.")
        if channel not in DIGITAL_OUTPUT_CHANNELS:
            raise ConfigError(
                f"valves.{name} must be a digital channel from 4 through 19; "
                f"8-15 is EIO0-EIO7 and 16-19 is CIO0-CIO3."
            )
        used = [other for other, used_channel in channels.items() if used_channel == channel]
        if used:
            raise ConfigError(f"valves.{name} and valves.{used[0]} use the same channel.")
        channels[name] = channel
    return channels


def _parse_trigger(trigger: dict[str, Any]) -> TriggerSettings | None:
    if not trigger:
        return None
    _reject_unknown(trigger, {"channel", "edge", "timeout_seconds"}, "[trigger]")
    if "channel" not in trigger:
        raise ConfigError("Set trigger.channel to the digital channel that receives the TTL.")
    return TriggerSettings(
        channel=_value(trigger, "channel", int, None),
        edge=_value(trigger, "edge", str, "rising"),
        timeout_seconds=_value(trigger, "timeout_seconds", float, None),
    )


def load_settings(path: Path) -> Settings:
    """Load settings, or return defaults when the file does not exist."""
    if not path.exists():
        return Settings(runs_directory=path.parent / "runs")
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"Cannot read {path}: {error}") from error

    _reject_unknown(data, {"labjack", "alicat", "valves", "runs", "trigger"}, "configuration")
    labjack = _table(data, "labjack")
    alicat = _table(data, "alicat")
    runs = _table(data, "runs")
    _reject_unknown(labjack, {"serial"}, "[labjack]")
    _reject_unknown(runs, {"directory"}, "[runs]")
    runs_directory = _value(runs, "directory", str, "runs")
    if not runs_directory.strip():
        raise ConfigError("runs.directory must not be empty.")
    settings = Settings(
        labjack_serial=_value(labjack, "serial", int, None),
        alicats=_parse_alicats(alicat),
        valves=_parse_valves(_table(data, "valves")),
        # A relative path is next to lab.toml; an absolute path, or one that starts
        # with ~, is used as written.
        runs_directory=path.parent / Path(runs_directory).expanduser(),
        trigger=_parse_trigger(_table(data, "trigger")),
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    if settings.labjack_serial is not None and settings.labjack_serial <= 0:
        raise ConfigError("labjack.serial must be greater than zero.")
    trigger = settings.trigger
    if trigger is not None:
        if trigger.channel not in DIGITAL_OUTPUT_CHANNELS:
            raise ConfigError("trigger.channel must be a digital channel from 4 through 19.")
        used = [name for name, channel in settings.valves.items() if channel == trigger.channel]
        if used:
            raise ConfigError(f"trigger.channel {trigger.channel} is also the valve {used[0]!r}.")
        if trigger.edge not in TRIGGER_EDGES:
            raise ConfigError("trigger.edge must be rising or falling.")
        timeout = trigger.timeout_seconds
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ConfigError("trigger.timeout_seconds must be finite and greater than zero.")
    for name, alicat in settings.alicats.items():
        location = "alicat" if name == "default" else f"alicat.{name}"
        if alicat.port is not None and not alicat.port.strip():
            raise ConfigError(f"{location}.port must not be empty.")
        if len(alicat.unit) != 1 or alicat.unit not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            raise ConfigError(f"{location}.unit must be one letter from A through Z.")
        if alicat.baud_rate <= 0:
            raise ConfigError(f"{location}.baud_rate must be greater than zero.")
        if not math.isfinite(alicat.timeout_seconds) or alicat.timeout_seconds <= 0:
            raise ConfigError(f"{location}.timeout_seconds must be finite and greater than zero.")

        bounds = (alicat.minimum_flow, alicat.maximum_flow)
        if any(value is not None and not math.isfinite(value) for value in bounds):
            raise ConfigError(f"{location} flow limits must be finite.")
        if alicat.maximum_flow is not None and alicat.minimum_flow >= alicat.maximum_flow:
            raise ConfigError(f"{location}.minimum_flow must be less than maximum_flow.")
        if alicat.minimum_flow < 0 and not alicat.allow_negative_flow:
            raise ConfigError(
                f"Set {location}.allow_negative_flow to true before using a negative minimum."
            )
        empty = [unit_name for unit_name, unit in alicat.units.items() if not unit.strip()]
        if empty:
            raise ConfigError(f"{location} unit must not be empty: {', '.join(empty)}.")
