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


@dataclass(frozen=True)
class Settings:
    """Settings that differ between computers or connected devices."""

    labjack_serial: int | None = None
    alicats: dict[str, AlicatSettings] = field(
        default_factory=lambda: {"default": AlicatSettings()}
    )


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


def load_settings(path: Path) -> Settings:
    """Load settings, or return defaults when the file does not exist."""
    if not path.exists():
        return Settings()
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"Cannot read {path}: {error}") from error

    _reject_unknown(data, {"labjack", "alicat"}, "configuration")
    labjack = _table(data, "labjack")
    alicat = _table(data, "alicat")
    _reject_unknown(labjack, {"serial"}, "[labjack]")
    settings = Settings(
        labjack_serial=_value(labjack, "serial", int, None),
        alicats=_parse_alicats(alicat),
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    if settings.labjack_serial is not None and settings.labjack_serial <= 0:
        raise ConfigError("labjack.serial must be greater than zero.")
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
