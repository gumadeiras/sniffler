# Hardware and recording reference

[Quick start](../README.md) · [Rig setup](setup.md) · [Using sniffler](guide.md)

`sniffler-gui` builds, runs, watches, and records experiments. The `sniffler`
command line is for diagnostics and single-device commands; it never runs
an experiment. Read the [safety rules](guide.md#safety-rules) before using
commands that change outputs.

## Read and control the LabJack

Read AIN0:

```text
uv run sniffler labjack read-analog --channel 0
```

Set FIO4 high:

```text
uv run sniffler labjack set-digital --channel 4 --state high
```

Read the level of a digital line, for example the TTL trigger input:

```text
uv run sniffler labjack read-digital --channel 4
```

The first command supports AIN0 through AIN3. The other two support channels
4 through 19: 4-7 is FIO4-FIO7, 8-15 is EIO0-EIO7, and 16-19 is CIO0-CIO3.
Each command checks the current analog or digital configuration before it
continues. `read-digital` reports the line direction and level and changes
nothing; an open input reads high because of the internal pull-up.

The EIO and CIO lines are the control lines for a PS12DC power switching board.
Switches S0 through S7 map to EIO0 through EIO7, which is channel 8 through 15.
Switches S8 through S11 map to CIO0 through CIO3, which is channel 16 through 19.

## Control the Alicat

Set the mass-flow setpoint:

```text
uv run sniffler alicat set-flow 1.0
```

Add `--name`, for example `--name mfc-500`, when more than one Alicat is
configured.

Set the mass-flow setpoint to zero:

```text
uv run sniffler alicat stop
```

Both commands first read the current control point and setpoint source. They
stop without sending a setpoint unless the control point is `mass flow` and the
source is `U`. Source `U` permits serial control and resets the setpoint to zero
at power-up. Change these settings on the Alicat itself, confirm the gas system,
and then retry.

Before each nonzero command, `set-flow` reads the mass-flow full scale and unit
from the Alicat. It refuses values outside the device range. `maximum_flow` is
optional and can set a stricter experiment limit. `stop` does not depend on the
full-scale query, but it still requires a valid Alicat connection and a
mass-flow control point.

## Synchronization and recording

### Sync pulses and the TTL start

When `lab.toml` has a `[trigger]` table, every run counts the pulses on that
line with the U3 hardware counter and records each one as a `sync_pulse` row in
`events.csv`, with the running count and the trial and step it landed in. The
same rows, with the counter enable and restore, the wait, and any counter
error, also go to the line's own file, `trigger-<line>.csv`. The
counter is enabled when the run arms and the device configuration is restored
when the run ends, on every exit path; both are logged. The step thread reads
the counter only while the next valve deadline is more than 30 ms away, at
most every 5 ms, so a read never delays a valve. A pulse shorter than one read
still counts, because the counter saw it; its mark lands on the first read
after it, so each mark is late by at most one read plus one USB round trip.
Every valve write also reads the counter in the same USB packet, so each
`valve_command` row carries the exact count at the moment the valves switched,
and a pulse that arrives during steps too short to poll is marked at the next
valve command. The run log states the number of reads and the time per read.
The Run tab shows the count, and the timeline marks each pulse. If the counter stops
answering, the run goes on: the error is logged and, after five failed reads in
a row, a `sync_recording_stopped` row and the manifest say from when the
alignment data is missing.

The *Run* tab also offers *Wait for TTL*, and the *Config* tab shows the line
and the time limit. With the box checked, *Start run* opens the devices, arms
the counter, applies the recipe end state as the rest state (so a carrier flow
can settle), and waits for the first pulse. The phase shows `waiting`, the time
shows how long the run has waited, and *Start now* ends the wait by hand. The
trials start at that pulse; the timeline and the time readout count from that
moment, and that pulse is the baseline, not a sync mark. *Stop after this
trial* during the wait ends the run with no trial and the end state. *Abort
now*, a device error, or the optional timeout end in the all-off state.

The U3 counter increments on one edge polarity (falling, according to the
LabJackPython examples; confirm on the bench). For a pulse that only shifts
the mark by the pulse width.

### The TTL output

When `lab.toml` has a `[ttl_output]` table, the *Run* tab offers a checkbox
and the *Config* tab shows the line, the mode, and the width. In `pulse` mode
the box reads *Send TTL at start* with the width; in `high` mode it reads
*TTL high during the run*. With the box checked, the run drives the line low
when it arms, before the counter is enabled, so the TTL starts from a defined
level. The line goes high in the same LabJack transaction as the valves of the
first step of the first trial, so the TTL and the first valve switch are
simultaneous in hardware. With *Wait for TTL* also checked, that moment is the
trigger time.

In `pulse` mode the line goes low again after `pulse_seconds`, counted from
the moment the high write returned, as one extra command inside the first
step. The pulse must end at least 30 ms before the first step of every trial
that can run first ends, so that the low write never delays the second step;
*Start run* refuses a longer pulse before the run begins. In `high`
mode the line stays high through the trials and falls with the end state. In
both modes the end state, the all-off state, and *Shut down the rig* drive the
line low. The checkbox is remembered between sessions; the manifest records
`send_ttl` and, in the rig map, the line, the mode, and the width.

Each edge is a `ttl_command` row in `events.csv` with the line name, `high`
or `low`, the commanded and returned times, and the sync count. The width
jitter is one USB round trip, a few milliseconds; the bench `ttl` check
measures it. If the output is wired to the trigger input, the TTL also counts
as a sync pulse.

### Run directories

Each run writes one directory under `runs`, named by the start time and the
recipe name:

- `manifest.json`: copies of the recipe (with the valve contents) and rig map,
  the seed, the resolved trial order, the start time, the software version,
  the operator notes, and the outcome.
- `events.csv`: the audit of the whole run in order: the state of every valve
  line read before the first command (`valve_read`), every valve and MFC
  command, trial boundaries, stop and abort
  requests, errors, the counter enable and restore, the trigger wait and its
  end, every sync pulse, each edge of the TTL output (`ttl_command`), and
  the end state or the all-off state. The time columns are seconds since the run started, which is
  the moment the devices were ready. `returned_run_seconds` is when the command
  returned from the device. `commanded_run_seconds` is when it was sent.
  `scheduled_run_seconds` is the planned time. `sync_count` is the pulse count
  on the trigger line at the moment of the row, on every valve command and
  every sync pulse; it is empty when the rig has no trigger line. In a run that
  waited for a trigger, `trigger_received` marks the trial schedule origin and the manifest
  repeats it as `trigger_seconds`. The manifest also holds `sync_pulses` and,
  when the record stopped early, `sync_recording_stopped_seconds`.
- One series file for each device, for analysis: `mfc-<name>.csv`,
  `valve-<name>.csv`, when the run sends a TTL `ttl-<line>.csv`, and when
  the rig has a trigger input `trigger-<line>.csv`. The
  file names are made from the `lab.toml` names; the `series` table in the
  manifest maps each device name to its file, by kind. An MFC file has one
  row for each reading: `wall_time`, `run_seconds`, `commanded_setpoint`,
  `device_setpoint`, `mass_flow`, `pressure`, `temperature`. A valve or TTL
  file has one row for each state: `wall_time`, `returned_run_seconds`,
  `commanded_run_seconds`, `scheduled_run_seconds`, `trial_index`,
  `trial_name`, `step_index`, `state` (`1` open or high, `0` closed or low),
  and `sync_count`. The first row of each valve file is the state read when
  the devices were ready, with no command time; the other rows have the same
  times and count as their `events.csv` rows. A device that the recipe never
  commands still has its file, with the header and the read row. The trigger
  input is counted, not read as a level, so its file has one row for each
  event of that line, in order: `wall_time`, `returned_run_seconds`, `event`
  (`counter_enabled`, `trigger_wait`, `trigger_received` or `trigger_end`,
  `sync_pulse`, `error`, `sync_recording_stopped`, `counter_restored`),
  `trial_index`, `trial_name`, `step_index`, `value`, `detail`, and
  `sync_count`; each row repeats its `events.csv` row.

Every row is written when it happens, so a crashed run keeps its record.

While a run is active, `runs/active-run.lock` names the run directory. The
command line refuses hardware commands while the lock exists. If the program
ended abnormally, the window offers to remove the lock at the next start.

### Timing

Valve steps are scheduled from the run start, so step times do not drift. All
valves change in one LabJack transaction. MFC commands and readings run on a
separate thread, so a slow MFC read never delays a valve. A single MFC read
costs about 30 ms on the tested hardware; readings are taken at 10 Hz.

## Bench checks

`sniffler-bench` verifies the rig with the real devices, one check at a time or
all of them, and prints a pass, fail, blocked, or skipped line with its numbers:

```text
uv run sniffler-bench                      # read-only: drivers, ports, mfcs, safe
uv run sniffler-bench valves timing --actuate
uv run sniffler-bench trigger gate sync --actuate --loopback 5
uv run sniffler-bench ttl --actuate
uv run sniffler-bench timing --actuate --no-mfc
```

A check that moves valves, writes setpoints, or changes the U3 counter
configuration needs `--actuate` and ends in the safe state. `--loopback` names a
spare digital output wired to the trigger input, so the script can make its own
pulses: `trigger` finds which edge the counter counts and confirms the
configuration is restored, `gate` measures the time from the edge to the start
of the trial schedule, and `sync` measures the lag of each sync mark.
`ttl` runs one short recipe with the TTL output and reports the measured
width, or in `high` mode the fall with the end state, whether the line rose in
the first valve packet, and, when the `[ttl_output]` line is wired to the
trigger input, that the counter saw it once.
`--no-mfc`
runs the executor checks without the MFCs while their setpoint source is not
`U`. The timing, gate, and sync checks drive the same executor as the window;
their run directories are written under `runs` and named in the output.

## Temporary command overrides

You can override device identity without changing `lab.toml`:

```text
uv run sniffler labjack status --serial 320123456
uv run sniffler alicat status --port COM3 --unit B
```

Baud rate, timeout, units, and safety limits always come from `lab.toml`.

Use a different configuration file:

```text
uv run sniffler --config another-lab.toml alicat status
```

## Development checks

```text
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests
uv build
```

The automated tests use fake LabJack hardware and the Alicat package's mock
serial client. They do not send commands to physical hardware. The GUI tests
run under the offscreen Qt platform. CI on macOS and Windows tests the Python layer,
package build, command routing, and driver protocol logic.

## README images

The UI screenshot comes from the demo window, with fake devices, temporary run
files, and separate Qt settings. The squirrel animation comes from the app's
`SniffWidget`. Neither capture opens a serial port or a LabJack.
