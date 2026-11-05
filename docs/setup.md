# Set up a rig

[Quick start](../README.md#setup) · [Using sniffler](guide.md)

The Python layer supports macOS, Linux, and Windows. The current physical
devices have been tested on macOS. The demo needs no device drivers.

## 1. Install the device drivers

### LabJack U3

- macOS with Homebrew: `brew install liblabjackusb`
- Linux: install the
  [LabJack Exodriver](https://support.labjack.com/docs/exodriver-downloads-for-ud-series-linux-and-macos-)
- Windows: install the
  [LabJack UD driver](https://support.labjack.com/docs/ud-software-installer-downloads-u3-u6-ue9)

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
