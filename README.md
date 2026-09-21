# Sniffler

This project controls an odor presentation rig:

- a LabJack U3 that drives valves through a PS12DC switching board;
- one or more Alicat mass flow controllers (MFCs).

It has two programs. `sniffler-gui` builds, runs, watches, and records
experiments. `sniffler` is a command-line tool for diagnostics: status, one
valve toggle, one setpoint, and the serial port list. The command line never
runs an experiment.

The Python layer supports macOS, Linux, and Windows. The current physical
devices have been tested on macOS.

## Safety rules

Read the device manuals and confirm the wiring, gas path, units, and safe flow
range before you send an output command.

The tool applies these rules:

- A flow value must be finite and inside the full-scale range reported by the
  Alicat.
- A configured flow limit can make the allowed range smaller.
- Negative flow is disabled unless `allow_negative_flow` is `true`.
- A flow command is refused unless the Alicat control point is `mass flow`.
- A flow command is refused unless the Alicat setpoint source is `U`.
- The tool never changes the Alicat control point.
- The tool never changes the Alicat setpoint source.
- The Alicat driver applies setpoints with a resolution of 0.01 device units.
- A LabJack digital command is refused if the selected FIO or EIO line is analog.
- A LabJack digital command changes the line direction to output.
- The reported LabJack state is an internal state. It is not a voltage
  measurement at the connected equipment.

If a write cannot be confirmed, the error states that the output or setpoint
might have changed. Check the physical device before you retry the command.

## 1. Install the device drivers

### LabJack U3

- macOS with Homebrew: `brew install liblabjackusb`
- Linux: install the
  [LabJack Exodriver](https://support.labjack.com/docs/exodriver-downloads-for-ud-series-linux-and-macos-)
- Windows: install the
  [LabJack UD driver](https://support.labjack.com/docs/software-driver)

The LabJack U3 does not use the newer LJM driver.

### Alicat MFC

Connect the Alicat serial cable or USB-to-serial adapter. Install the adapter
driver if the operating system does not show a serial port. Set the Alicat
setpoint source to `U`. This mode permits serial control and resets the setpoint
to zero at power-up. The default project settings are 19200 baud and a
0.15 second timeout.

## 2. Install the project

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). Then run:

```text
git clone https://github.com/gumadeiras/sniffler.git
cd sniffler
uv sync
```

`uv` installs Python 3.12 and the locked Python packages in an isolated
environment.

## 3. Configure the devices

Copy the example:

```text
# macOS or Linux
cp lab.toml.example lab.toml

# Windows PowerShell
Copy-Item lab.toml.example lab.toml
```

Edit `lab.toml`. Use the values shown on the physical devices or in their
manuals. Do not guess the units or safe limits.

```toml
[labjack]
serial = 320000000

[alicat.main]
port = "/dev/cu.usbserial-EXAMPLE"
unit = "A"
baud_rate = 19200
timeout_seconds = 0.15
minimum_flow = 0.0
allow_negative_flow = false

# Optional experiment limit. The tool always enforces the device full scale:
# maximum_flow = <stricter experiment maximum>

# Add each verified unit:
# [alicat.main.units]
# pressure = "<verified pressure unit>"
# temperature = "<verified temperature unit>"
# volumetric_flow = "<verified volumetric-flow unit>"
# mass_flow = "<verified mass-flow unit>"
```

`lab.toml` is local and is not committed to Git. Unknown or invalid settings
cause a clear configuration error. `minimum_flow` and `maximum_flow` are
optional experiment limits in the current Alicat mass-flow unit.

Use one table for each Alicat when the computer has more than one controller:

```toml
[alicat.mfc-500]
port = "/dev/cu.usbserial-FIRST"

[alicat.mfc-2000]
port = "/dev/cu.usbserial-SECOND"
```

The table names are the names used with `--name`. Recipes also use these
names for MFC setpoints.

Name each valve that a recipe can switch. The value is the LabJack digital
channel of the PS12DC switch that drives the valve:

```toml
[valves]
A = 8
B = 9
C = 10
D = 11
```

Recipes switch valves by name only. The GUI shows this map read-only and does
not change it. The names are what the window shows, so choose the words an
operator reads best. TOML accepts a quoted key with a space, for valves and
for Alicat tables alike:

```toml
[valves]
"valve A" = 8
"valve B" = 9

[alicat."carrier flow"]
port = "/dev/cu.usbserial-FIRST"

[alicat."odor flow"]
port = "/dev/cu.usbserial-SECOND"
```

A recipe that names a valve or MFC that is not in `lab.toml` is refused before
any hardware command.

To record TTL pulses from a recording system, and to start runs from its first
pulse, name the input line in an optional `[trigger]` table. Use a spare line
from FIO4 to EIO0 (channel 4 to 8), where the U3 can count pulses in hardware.
FIO0 to FIO3 are analog and the other EIO and CIO lines drive the switching
board. The input is 5 V tolerant; the grounds must be shared.

```toml
[trigger]
channel = 4
# timeout_seconds = 300  # optional limit for the wait at run start
```

To send a TTL to a recording system when the trials start, name the output
line in an optional `[ttl_output]` table. Any spare digital line from channel
4 to 19 works; it must not be a valve channel or the trigger input. The line
goes high with the first step of the first trial. In `pulse` mode, the
default, it falls after `pulse_seconds` (default 0.005). In `high` mode it
stays high until the end state; `pulse_seconds` is refused in that mode.

```toml
[ttl_output]
channel = 5
mode = "pulse"          # or "high": stays high until the end state
pulse_seconds = 0.005
```

Run directories are written to `runs` next to `lab.toml`. Set another
location with an optional `[runs]` table. A relative path is next to
`lab.toml`; an absolute path, or one that starts with `~`, is used as written.
Each run gets its own new folder, named by start time and recipe; nothing is
ever overwritten.

```toml
[runs]
directory = "data/runs"
# or
directory = "/Volumes/lab-data/odor-runs"
```

## 4. Check the connections

List the serial ports:

```text
uv run sniffler ports
```

This command lists all serial ports. It does not identify an Alicat
automatically. Common port names are:

- macOS: `/dev/cu.usbserial-...`
- Linux: `/dev/ttyUSB0`
- Windows: `COM3`

Check both configured devices:

```text
uv run sniffler labjack status
uv run sniffler alicat status
```

Select each controller by name when `lab.toml` contains more than one Alicat:

```text
uv run sniffler alicat status --name mfc-500
uv run sniffler alicat status --name mfc-2000
```

The Alicat reports numeric values without unit names. The tool adds the units
from `lab.toml`. If a unit is missing, the output says
`device units not configured`.

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

## Run an experiment

Start the window:

```text
uv run sniffler-gui
```

Use `--config another-lab.toml` to select another configuration file.

### Recipes

A recipe has three levels and one primitive:

- **Step**: a duration plus the complete rig state. Each step sets every
  valve to open or closed and gives every MFC a target flow. At any moment one
  step describes the rig.
- **Trial**: a named, ordered list of steps.
- **Schedule**: a count for each trial, the ordering policy, and an optional
  interleave trial.
- **End state**: one step with no duration. The executor applies it after
  the last trial or after *Stop after this trial*.

Build the recipe in the *Recipe* tab. Steps are rows in a table. One click
anywhere in a valve cell opens or closes that valve, and Space does the same
from the keyboard. Double-click a trial name to rename it. The icon buttons
under the trial list and under the step table add, remove, duplicate, and move
items; each one names its command in a tooltip. The toolbar holds New, Open,
and Save. Each cell checks its value at once: a duration must be greater
than zero, and a target flow must respect `minimum_flow`, `maximum_flow`, and
`allow_negative_flow` from `lab.toml` and the device maximum. A cell with a
problem is red and shows the reason in its tooltip. The run cannot start while
a problem exists.

*Pulse train…* creates a train of pulses on one valve. The result is
ordinary step rows, one for each pulse and one for each gap, and you can edit
each row.

*Valve contents* has one text field for each valve in `lab.toml`. Write what
the valve holds, for example the odorant and its dilution. The text is saved
in the recipe file and in the manifest of every run, so the analysis can name
the stimulus behind each valve. The *Run* tab shows this text in place of the
valve name: on the timeline lanes, beside the squirrel, in the *Valves* line,
and in the step list. The tooltip of the *Valves* line keeps the valve name.
An empty field is not recorded.

Recipes are saved as `.json` files from the File menu. The file is a record,
not an input format: build and edit recipes in the window. The window title
shows the recipe file and marks unsaved changes; the program asks before it
discards them. The last saved recipe opens again at the next start.

The ordering *shuffled in blocks* (`block-randomized` in the file) shuffles
trials inside blocks that hold one
trial of each type, so a run that stops early is still balanced. No more than
two identical trials follow each other anywhere in the run. The seed that
produced the order is saved with the run in `manifest.json`. Set the seed in the recipe
to repeat the same order, or leave it empty for a new seed for each run.

*Interleave* names one trial that runs after every other trial, for example a
blank between odors. The ordering does not place it and it has no count: its
count in the schedule table is disabled, and it runs as many times as the other
trials together. The run ends with the interleave, then the end state. Because
the interleave separates every pair of trials, the two-in-a-row limit is always
met, so a schedule with one odor trial and an interleave is allowed. The
resolved order in `manifest.json` lists the interleave as a normal trial, and
*Stop after this trial* can end the run before the interleave that follows the
current trial.

*Read device limits* in the *Config* tab reads the maximum of each MFC.
This command changes no output.

### Watch the run

The *Run* tab shows the phase, the current trial and step, the commanded
valves, the whole run as one timeline, and the MFC plot with one readout row
for each MFC: commanded, measured, and deviation. A deviation of more than
5 % of the device maximum is marked with a leading "!" in bold pink. The
timeline names each trial on its own segment when the name fits, and has one
lane for each valve the recipe opens, filled where the recipe plans it open.
Nothing on this tab moves while a run changes the content: the labels have
fixed heights, the plot axes are fixed at run start, and a splitter shares the
width between the status and the plot. The window
uses one light palette on every platform. Pink marks live attention only: the
sniff, the current step, the progress cursor, and a high flow deviation.

When a valve opens during a step, the squirrel sniffs: the scent lines rise
from its nose and the valve name is shown large, so an operator can see each
odor onset from across the room. The cue is driven by the valve event that the
executor records, not by a timer, and a faster pulse train restarts the cue
instead of queueing it. When the operating system reduce-motion setting is on
(macOS: Accessibility, Display; Windows: animation effects off), nothing moves
and the nose lights up instead. Set `SNIFFLER_REDUCE_MOTION=1` to force this
on any system, or `0` to force motion on.

### Demo mode

```text
uv run sniffler-gui --demo
```

Demo mode opens the same window on fake devices with a sample recipe: four
valves named *valve A* to *valve D*, two MFCs named *carrier flow* and
*odor flow*, and trials for each odor, a mixture that opens valve A and
valve B together, and a blank that runs as the interleave after every other
trial. The fake MFCs answer with lag and noise. The
fake trigger line pulses 3 s after the run arms and then once each second, so
*Wait for TTL* starts the trials by itself and the timeline shows sync marks.
The fake TTL output is in `high` mode and *TTL high during the run* starts
checked, so each run logs the line high with the first trial and low with the
end state.
Nothing reaches a serial port or the LabJack. The title bar says *sniffler
demo* and the status bar says *fake devices, no hardware*. Runs are written to
the runs directory of `lab.toml`, or of the file given with `--config`; their
manifests name the program *sniffler demo*. Use it to learn the window or to
debug the interface.

### Stop and abort

- *Stop after this trial* finishes the current trial and then applies the
  recipe end state.
- *Abort now* stops at once, closes all valves, and sets every flow to zero.
  The recipe end state is ignored.

The same all-off state is applied when a device command fails and when the
window closes during a run. It is not configurable. The run log calls it
`safe_state` and the end state `shutdown_state`.

*Shut down the rig*, at the right end of the toolbar, applies the same
all-off state when no run is active: for example after a run whose end state
kept a carrier flow on, or before you leave the rig. It opens the LabJack and
each MFC, closes all valves, sets every setpoint to zero, and closes the
devices again. It is not a run, so nothing is logged, and *Start run* waits
until it has ended. The MFC checks are the same as for a run: the control
point must be `mass flow` and the setpoint source `U`. A device that does not
answer is named in a message that says whether its output might have changed.
During a run the button is off; use *Abort now*.

### Sync pulses and the TTL start

When `lab.toml` has a `[trigger]` table, every run counts the pulses on that
line with the U3 hardware counter and records each one as a `sync_pulse` row in
`events.csv`, with the running count and the trial and step it landed in. The
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
- `events.csv`: every valve and MFC command, trial boundaries, stop and abort
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
- `samples.csv`: each MFC reading next to the setpoint that was commanded.

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

## Troubleshooting

- `A run is active`: wait for the run to end, or remove `runs/active-run.lock`
  if the run ended abnormally.
- `Refusing to change the setpoint while its source is ...` or
  `Cannot confirm the Alicat setpoint source`: the run is refused before any
  setpoint is sent. Select source `U` on the controller.
- `unknown valve` or `unknown MFC` in the recipe editor: the recipe names a
  device that `lab.toml` does not list. Add it to `[valves]` or `[alicat.<name>]`.

- `Set alicat.port in lab.toml`: add the detected serial port or use `--port`.
- `Cannot open the Alicat MFC`: confirm the cable driver, port, baud rate, power,
  unit ID, and `Serial` input mode.
- `Refusing to change the setpoint`: the Alicat is not in mass-flow control
  mode. No setpoint was sent.
- `Set the source to U`: the Alicat uses an analog or saved setpoint source.
  Select source `U` on the controller before you use serial control.
- `FIO... is configured as analog` or `EIO... is configured as analog`: select a
  configured digital line or change the LabJack configuration with the official
  LabJack software.
- `Cannot connect to the LabJack U3`: confirm the UD driver on Windows or the
  Exodriver on macOS and Linux.

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
