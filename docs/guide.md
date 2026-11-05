# Using sniffler

[Quick start](../README.md) · [Rig setup](setup.md) · [Hardware and recording reference](reference.md)

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

*Set flow…* gives one MFC the same target flow in every step of the selected
trial or of all trials, for example to try a different carrier flow. The
dialog starts with the MFC of the current cell and shows the flows that the
steps have now, so you can see a step that you set to a different flow on
purpose. The end state does not change.

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
