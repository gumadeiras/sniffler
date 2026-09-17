"""Run directories: the manifest, append-only logs, and the advisory lock file."""

import csv
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

LOCK_FILE_NAME = "active-run.lock"
MANIFEST_NAME = "manifest.json"
EVENTS_NAME = "events.csv"
SAMPLES_NAME = "samples.csv"

EVENT_COLUMNS = (
    "returned_wall_time",
    "scheduled_run_seconds",
    "commanded_run_seconds",
    "returned_run_seconds",
    "event",
    "trial_index",
    "trial_name",
    "step_index",
    "device",
    "value",
    "detail",
)
SAMPLE_COLUMNS = (
    "wall_time",
    "run_seconds",
    "mfc",
    "commanded_setpoint",
    "device_setpoint",
    "mass_flow",
    "pressure",
    "temperature",
)


class RunLockError(RuntimeError):
    """Another run is active."""


class RunLogError(RuntimeError):
    """The run directory cannot be written."""


def wall_time_now() -> str:
    """Return the local wall-clock time with microseconds and the UTC offset."""
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def run_id(recipe_name: str, now: datetime | None = None) -> str:
    """Return a timestamped directory name for a run."""
    now = now or datetime.now()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", recipe_name).strip("-").lower() or "run"
    return f"{now:%Y%m%d-%H%M%S}-{slug}"


def new_run_directory(runs_directory: Path, recipe_name: str) -> Path:
    """Return a run directory path that does not exist yet."""
    base = runs_directory / run_id(recipe_name)
    candidate = base
    number = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{number}")
        number += 1
    return candidate


def active_run(runs_directory: Path) -> str | None:
    """Return the run directory named by the lock file, or None when no run is active."""
    lock = runs_directory / LOCK_FILE_NAME
    try:
        text = lock.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RunLockError(f"Cannot read the run lock {lock}: {error}") from error
    try:
        return str(json.loads(text)["run_directory"])
    except (ValueError, KeyError, TypeError):
        return f"unknown run (lock file {lock})"


def refusal_for_active_run(runs_directory: Path) -> str | None:
    """Return the message that refuses hardware commands while a run is active."""
    directory = active_run(runs_directory)
    if directory is None:
        return None
    return (
        f"A run is active: {directory}. Wait for it to end or abort it in the GUI. "
        f"If the run ended abnormally, remove {runs_directory / LOCK_FILE_NAME}."
    )


class RunLock:
    """An advisory lock file that names the active run directory."""

    def __init__(self, runs_directory: Path, run_directory: Path) -> None:
        self.path = runs_directory / LOCK_FILE_NAME
        self._run_directory = run_directory
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            {
                "run_directory": str(self._run_directory),
                "pid": os.getpid(),
                "started_at": wall_time_now(),
            }
        )
        try:
            with self.path.open("x", encoding="utf-8") as file:
                file.write(content + "\n")
        except FileExistsError as error:
            raise RunLockError(
                refusal_for_active_run(self.path.parent) or f"A run is active ({self.path})."
            ) from error
        except OSError as error:
            raise RunLockError(f"Cannot create the run lock {self.path}: {error}") from error
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise RunLockError(f"Cannot remove the run lock {self.path}: {error}") from error


class RunLog:
    """One run directory with a manifest and two append-only CSV logs.

    Every row is written and flushed when it happens, so a crashed run keeps
    every record up to the crash.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._events: TextIO | None = None
        self._samples: TextIO | None = None
        self._event_writer: Any = None
        self._sample_writer: Any = None
        self._manifest: dict[str, Any] = {}
        self._lock = threading.Lock()

    def open(self, manifest: dict[str, Any]) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=False)
            self._manifest = dict(manifest)
            self._write_manifest()
            self._events = (self.directory / EVENTS_NAME).open("a", newline="", encoding="utf-8")
            self._samples = (self.directory / SAMPLES_NAME).open("a", newline="", encoding="utf-8")
        except OSError as error:
            raise RunLogError(
                f"Cannot create the run directory {self.directory}: {error}"
            ) from error
        self._event_writer = csv.writer(self._events)
        self._sample_writer = csv.writer(self._samples)
        self._event_writer.writerow(EVENT_COLUMNS)
        self._sample_writer.writerow(SAMPLE_COLUMNS)
        self._events.flush()
        self._samples.flush()

    def _write_manifest(self) -> None:
        target = self.directory / MANIFEST_NAME
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self._manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)

    def event(
        self,
        event: str,
        *,
        returned_run_seconds: float,
        scheduled_run_seconds: float | None = None,
        commanded_run_seconds: float | None = None,
        trial_index: int | None = None,
        trial_name: str = "",
        step_index: int | None = None,
        device: str = "",
        value: object = "",
        detail: str = "",
        returned_wall_time: str | None = None,
    ) -> None:
        """Append one event. Times are seconds since the run started."""
        row = [
            returned_wall_time or wall_time_now(),
            _seconds(scheduled_run_seconds),
            _seconds(commanded_run_seconds),
            _seconds(returned_run_seconds),
            event,
            "" if trial_index is None else trial_index,
            trial_name,
            "" if step_index is None else step_index,
            device,
            value,
            detail,
        ]
        with self._lock:
            if self._events is None or self._event_writer is None:
                raise RunLogError("The run log is not open.")
            self._event_writer.writerow(row)
            self._events.flush()

    def sample(
        self,
        mfc: str,
        *,
        run_seconds: float,
        commanded_setpoint: float | None,
        state: dict[str, object],
        wall_time: str | None = None,
    ) -> None:
        """Append one MFC reading next to the setpoint that was commanded."""
        row = [
            wall_time or wall_time_now(),
            _seconds(run_seconds),
            mfc,
            "" if commanded_setpoint is None else commanded_setpoint,
            state.get("setpoint", ""),
            state.get("mass_flow", ""),
            state.get("pressure", ""),
            state.get("temperature", ""),
        ]
        with self._lock:
            if self._samples is None or self._sample_writer is None:
                raise RunLogError("The run log is not open.")
            self._sample_writer.writerow(row)
            self._samples.flush()

    def close(self, **final_fields: Any) -> None:
        """Close the logs and record the outcome in the manifest."""
        with self._lock:
            for file in (self._events, self._samples):
                if file is not None:
                    file.close()
            self._events = self._samples = None
        if final_fields and self._manifest:
            self._manifest.update(final_fields)
            self._write_manifest()


def _seconds(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"
