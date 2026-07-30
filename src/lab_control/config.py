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
class Settings:
    """Settings that differ between computers or connected devices."""

    labjack_serial: int | None = None
    alicat_port: str | None = None
    alicat_unit: str = "A"
    alicat_baud_rate: int = 19200
    alicat_timeout_seconds: float = 0.15
    alicat_minimum_flow: float = 0.0
    alicat_maximum_flow: float | None = None
    alicat_allow_negative_flow: bool = False
    alicat_units: dict[str, str] = field(default_factory=dict)


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
    units = _table(alicat, "units")
    _reject_unknown(labjack, {"serial"}, "[labjack]")
    _reject_unknown(
        alicat,
        {
            "port",
            "unit",
            "baud_rate",
            "timeout_seconds",
            "minimum_flow",
            "maximum_flow",
            "allow_negative_flow",
            "units",
        },
        "[alicat]",
    )
    _reject_unknown(
        units,
        {"pressure", "temperature", "volumetric_flow", "mass_flow"},
        "[alicat.units]",
    )

    parsed_units = {
        name: _value(units, name, str, None)
        for name in ("pressure", "temperature", "volumetric_flow", "mass_flow")
        if name in units
    }
    settings = Settings(
        labjack_serial=_value(labjack, "serial", int, None),
        alicat_port=_value(alicat, "port", str, None),
        alicat_unit=_value(alicat, "unit", str, "A"),
        alicat_baud_rate=_value(alicat, "baud_rate", int, 19200),
        alicat_timeout_seconds=_value(alicat, "timeout_seconds", float, 0.15),
        alicat_minimum_flow=_value(alicat, "minimum_flow", float, 0.0),
        alicat_maximum_flow=_value(alicat, "maximum_flow", float, None),
        alicat_allow_negative_flow=_value(alicat, "allow_negative_flow", bool, False),
        alicat_units=parsed_units,
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    if settings.labjack_serial is not None and settings.labjack_serial <= 0:
        raise ConfigError("labjack.serial must be greater than zero.")
    if settings.alicat_port is not None and not settings.alicat_port.strip():
        raise ConfigError("alicat.port must not be empty.")
    if len(settings.alicat_unit) != 1 or settings.alicat_unit not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        raise ConfigError("alicat.unit must be one letter from A through Z.")
    if settings.alicat_baud_rate <= 0:
        raise ConfigError("alicat.baud_rate must be greater than zero.")
    if not math.isfinite(settings.alicat_timeout_seconds) or settings.alicat_timeout_seconds <= 0:
        raise ConfigError("alicat.timeout_seconds must be finite and greater than zero.")

    bounds = (settings.alicat_minimum_flow, settings.alicat_maximum_flow)
    if any(value is not None and not math.isfinite(value) for value in bounds):
        raise ConfigError("Alicat flow limits must be finite.")
    if (
        settings.alicat_maximum_flow is not None
        and settings.alicat_minimum_flow >= settings.alicat_maximum_flow
    ):
        raise ConfigError("alicat.minimum_flow must be less than alicat.maximum_flow.")
    if settings.alicat_minimum_flow < 0 and not settings.alicat_allow_negative_flow:
        raise ConfigError("Set alicat.allow_negative_flow to true before using a negative minimum.")
    if settings.alicat_units:
        empty = [name for name, unit in settings.alicat_units.items() if not unit.strip()]
        if empty:
            raise ConfigError(f"Alicat unit must not be empty: {', '.join(empty)}.")
