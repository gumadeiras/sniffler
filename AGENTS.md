# AGENTS.md

## Purpose

This repository is `sniffler`. It runs odor presentation experiments on a LabJack U3 that
drives valves through a PS12DC switching board, and on one or more Alicat mass flow
controllers. Scientists must be able to build, run, watch, and audit an experiment in the
window without editing a file or reading Python.

Two programs share one hardware layer:

- `sniffler-gui` (`src/sniffler/gui/`) authors recipes and runs them through the executor.
- `sniffler` (`src/sniffler/cli.py`) is for diagnostics only: status, one valve toggle, one
  setpoint, the port list. It must never run a recipe; two executors would diverge.
- `sniffler-bench` (`src/sniffler/bench.py`) verifies the rig with the real devices. Its
  timing, gate, and sync checks drive the shared executor with generated recipes; checks that
  move hardware need `--actuate` and end in the safe state.

## Layout

- `hardware.py`: every device-specific command. Nothing else talks to LabJackPython or the
  Alicat driver.
- `config.py`: `lab.toml` (machine-specific, git-ignored): LabJack serial, named Alicats,
  the `[valves]` name to channel map, the runs directory.
- `recipe.py`: the contract (Step, Trial, Schedule, shutdown state), validation against the rig
  map, the block randomizer, recipe files.
- `executor.py`: runs a recipe on its own thread; MFC traffic on a second thread; publishes
  status. `runlog.py`: run directory, manifest, `events.csv`, `samples.csv`, lock file.
- `trigger.py`: the start gate and the sync record on the U3 hardware counter. The step
  thread polls it only in idle time, never within 30 ms of a deadline. Run seconds count from
  device readiness; the schedule origin is the trigger time.
- `gui/`: PySide6 and pyqtgraph. The user chose them over `tkinter` because the recipe editor
  needs an editable table; do not reopen that choice.

## Rules

- Use ASD-STE100 Simplified Technical English in user text and documentation.
- Keep hardware changes explicit. Do not change an output as part of a status command. A
  digital read never changes a line direction. The executor enables the pulse counter on the
  trigger line when a run arms and restores the U3 configuration on every exit path; both
  are logged.
- Refuse writes when units, safe ranges, channel mode, or controller mode are not valid. The
  Alicat setpoint-source guard (source `U`) stays; do not weaken it to make a device work.
- State when a failed write might have changed physical hardware.
- Show a clear error without a Python traceback for expected device failures.
- Recipes name valves and MFCs by the friendly names in `lab.toml`. The GUI shows the map
  read-only and never writes `lab.toml`. Refuse a recipe that names an unknown device before
  any hardware command.
- The safe state is all valves closed and every MFC setpoint zero. It is not configurable. Abort,
  any error, and closing the window during a run apply it. Normal end and Stop apply the recipe
  shutdown state.
- The log records when each command returned, not when it was scheduled. Every row is flushed
  as it happens. The manifest embeds copies of the recipe and rig map.
- Nothing may block the step-timing thread: no MFC read, no GUI work, no callback that does more
  than a queue put.
- Keep computer-specific settings in the ignored `lab.toml` file.
- Add an abstraction only after real repetition exists.
- Add a regression test for each fixed bug when feasible.
- Do not add a dependency when the existing ones are sufficient. New GUI needs should use
  PySide6, pyqtgraph, and shipped SVG assets before a new package.
- Keep source files under about 750 lines.

## Validation

Run these commands before handoff after code, dependency, or packaging changes. For
documentation-only changes, verify the changed commands and links; also run the required build
when the changed document is a build or packaging input, including README.md. Read-only answers
do not require these checks:

```text
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests
uv build
```

The tests use fake hardware and the offscreen Qt platform; they never touch devices. Test with
the physical devices when a change affects communication, and report what was and was not
verified on hardware. Actuating valves and setpoints for a test is allowed only when the rig is
returned to the safe state afterward.

On this network `uv` may fail to reach PyPI while `curl` works. Use `--offline` for the gates
when the lock file already covers the change, and say so in the handoff.
