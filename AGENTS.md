# AGENTS.md

## Purpose

This repository controls a LabJack U3 and an Alicat mass flow controller.
Scientists must be able to use the command line without reading Python code.

## Rules

- Use ASD-STE100 Simplified Technical English in user text and documentation.
- Keep hardware changes explicit. Do not change an output as part of a status command.
- Show a clear error without a Python traceback for expected device failures.
- Keep the device-specific code in `hardware.py` and the user interface in `cli.py`.
- Add an abstraction only after real repetition exists.
- Add a regression test for each fixed bug when feasible.
- Do not add a dependency when the Python standard library is sufficient.

## Validation

Run these commands before each handoff:

```text
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests
uv build
```

Test with the physical devices when a change affects communication.
