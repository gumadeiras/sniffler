"""Run directories: the manifest, append-only logs, and the advisory lock file."""

import csv
import json
import os
import re
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

LOCK_FILE_NAME = "active-run.lock"
MANIFEST_NAME = "manifest.json"
EVENTS_NAME = "events.csv"

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
    "sync_count",
)
MFC_COLUMNS = (
    "wall_time",
    "run_seconds",
    "commanded_setpoint",
    "device_setpoint",
    "mass_flow",
    "pressure",
    "temperature",
)
DIGITAL_COLUMNS = (
    "wall_time",
    "returned_run_seconds",
    "commanded_run_seconds",
    "scheduled_run_seconds",
    "trial_index",
    "trial_name",
    "step_index",
    "state",
    "sync_count",
)
TRIGGER_COLUMNS = (
    "wall_time",
    "returned_run_seconds",
    "event",
    "trial_index",
    "trial_name",
    "step_index",
    "value",
    "detail",
    "sync_count",
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
    return f"{now:%Y%m%d-%H%M%S}-{_slug(recipe_name) or 'run'}"


def series_file_name(kind: str, device: str) -> str:
    """Return the file name of one device series, for example ``valve-odor-1.csv``."""
    return f"{kind}-{_slug(device) or 'unnamed'}.csv"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()


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
        content = json.dumps(
            {
                "run_directory": str(self._run_directory),
                "pid": os.getpid(),
                "started_at": wall_time_now(),
            }
        )
        # Keep the directory step out of the FileExistsError handler: on Windows a
        # mkdir under a plain file raises FileExistsError, which is not a held lock.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RunLockError(f"Cannot create the run lock {self.path}: {error}") from error
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


class _CsvStream:
    """One append-only CSV file with fixed columns. Every row is flushed as it is written."""

    def __init__(self, path: Path, columns: tuple[str, ...]) -> None:
        self.path = path
        self._columns = columns
        self._file: TextIO | None = None
        self._writer: Any = None
        self._lock = threading.Lock()

    def open(self) -> None:
        self._file = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(self._columns)
        self._file.flush()

    def append(self, row: list[object]) -> None:
        with self._lock:
            if self._file is None:
                raise RunLogError("The run log is not open.")
            try:
                self._writer.writerow(row)
                self._file.flush()
            except OSError as error:
                raise RunLogError(f"Cannot write {self.path.name}: {error}") from error

    def close(self) -> None:
        with self._lock:
            file, self._file = self._file, None  # a late row is refused, not lost
        if file is not None:
            file.close()


class RunLog:
    """One run directory: a manifest, the event log, and one series file per device.

    Every row is written and flushed when it happens, so a crashed run keeps
    every record up to the crash. ``events.csv`` is the audit of the whole run in
    order. Each MFC, each valve, the TTL output, and the trigger input has its own
    CSV with only its rows; the manifest ``series`` table maps the device names to
    the files.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._events = _CsvStream(directory / EVENTS_NAME, EVENT_COLUMNS)
        self._series: dict[tuple[str, str], _CsvStream] = {}
        self._manifest: dict[str, Any] = {}

    def open(
        self,
        manifest: dict[str, Any],
        *,
        mfcs: Iterable[str] = (),
        valves: Iterable[str] = (),
        ttl_lines: Iterable[str] = (),
        trigger_lines: Iterable[str] = (),
    ) -> None:
        """Create the directory, the manifest, and every log file with its header.

        Each named device gets its series file now, so a device that is never
        commanded still leaves a file. Two devices of one kind whose names map to
        the same file name are refused before anything is written.
        """
        index: dict[str, dict[str, str]] = {}
        kinds = (("mfc", mfcs), ("valve", valves), ("ttl", ttl_lines), ("trigger", trigger_lines))
        for kind, names in kinds:
            for name in names:
                file_name = series_file_name(kind, name)
                taken = next((n for n, f in index.get(kind, {}).items() if f == file_name), None)
                if taken is not None:
                    raise RunLogError(
                        f"The {kind} names {taken!r} and {name!r} both map to the log file "
                        f"{file_name}. Rename one in lab.toml."
                    )
                index.setdefault(kind, {})[name] = file_name
                columns = {"mfc": MFC_COLUMNS, "trigger": TRIGGER_COLUMNS}.get(
                    kind, DIGITAL_COLUMNS
                )
                self._series[kind, name] = _CsvStream(self.directory / file_name, columns)
        try:
            self.directory.mkdir(parents=True, exist_ok=False)
            self._manifest = {**manifest, "series": index}
            self._write_manifest()
            self._events.open()
            for stream in self._series.values():
                stream.open()
        except OSError as error:
            raise RunLogError(
                f"Cannot create the run directory {self.directory}: {error}"
            ) from error

    def _write_manifest(self) -> None:
        target = self.directory / MANIFEST_NAME
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self._manifest, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

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
        sync_count: int | None = None,
    ) -> None:
        """Append one event. Times are seconds since the run started.

        ``sync_count`` is the pulse count on the trigger line when the event was
        recorded, for rows that read it; empty otherwise.
        """
        self._events.append(
            [
                returned_wall_time or wall_time_now(),
                _seconds(scheduled_run_seconds),
                _seconds(commanded_run_seconds),
                _seconds(returned_run_seconds),
                event,
                _blank(trial_index),
                trial_name,
                _blank(step_index),
                device,
                value,
                detail,
                _blank(sync_count),
            ]
        )

    def mfc_sample(
        self,
        mfc: str,
        *,
        run_seconds: float,
        commanded_setpoint: float | None,
        state: dict[str, object],
        wall_time: str | None = None,
    ) -> None:
        """Append one MFC reading next to the setpoint that was commanded."""
        self._stream("mfc", mfc).append(
            [
                wall_time or wall_time_now(),
                _seconds(run_seconds),
                _blank(commanded_setpoint),
                state.get("setpoint", ""),
                state.get("mass_flow", ""),
                state.get("pressure", ""),
                state.get("temperature", ""),
            ]
        )

    def digital_state(
        self,
        kind: str,
        device: str,
        state: bool,
        *,
        returned_run_seconds: float,
        commanded_run_seconds: float | None = None,
        scheduled_run_seconds: float | None = None,
        trial_index: int | None = None,
        trial_name: str = "",
        step_index: int | None = None,
        sync_count: int | None = None,
        wall_time: str | None = None,
    ) -> None:
        """Append one state of a valve or of the TTL output; ``state`` is written as 1 or 0.

        A row from a read has no commanded or scheduled time. A row from a command
        carries the same times and sync count as its ``events.csv`` row.
        """
        self._stream(kind, device).append(
            [
                wall_time or wall_time_now(),
                _seconds(returned_run_seconds),
                _seconds(commanded_run_seconds),
                _seconds(scheduled_run_seconds),
                _blank(trial_index),
                trial_name,
                _blank(step_index),
                int(state),
                _blank(sync_count),
            ]
        )

    def trigger_event(
        self,
        device: str,
        event: str,
        *,
        returned_run_seconds: float,
        trial_index: int | None = None,
        trial_name: str = "",
        step_index: int | None = None,
        value: object = "",
        detail: str = "",
        sync_count: int | None = None,
        wall_time: str | None = None,
    ) -> None:
        """Append one event of the trigger input: the counter, the wait, and each pulse.

        The input is counted, not read as a level, so its file holds the events
        of its line with the same fields as their ``events.csv`` rows.
        """
        self._stream("trigger", device).append(
            [
                wall_time or wall_time_now(),
                _seconds(returned_run_seconds),
                event,
                _blank(trial_index),
                trial_name,
                _blank(step_index),
                value,
                detail,
                _blank(sync_count),
            ]
        )

    def _stream(self, kind: str, device: str) -> _CsvStream:
        try:
            return self._series[kind, device]
        except KeyError:
            raise RunLogError(f"The run log has no series file for {kind} {device!r}.") from None

    def close(self, **final_fields: Any) -> None:
        """Close the logs and record the outcome in the manifest."""
        try:
            self._events.close()
            for stream in self._series.values():
                stream.close()
            if final_fields and self._manifest:
                self._manifest.update(final_fields)
                self._write_manifest()
        except OSError as error:
            raise RunLogError(f"Cannot finish the run log in {self.directory}: {error}") from error


def _seconds(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"


def _blank(value: object) -> object:
    return "" if value is None else value
