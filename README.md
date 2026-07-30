# LabJack U3 and Alicat control

This project provides a small command-line tool for:

- a LabJack U3;
- an Alicat mass flow controller (MFC).

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
- The tool never changes the Alicat control point.
- The Alicat driver applies setpoints with a resolution of 0.01 device units.
- A LabJack digital command is refused if the selected FIO line is analog.
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
driver if the operating system does not show a serial port. Set the Alicat input
mode to `Serial`. The default project settings are 19200 baud and a 0.15 second
timeout.

## 2. Install the project

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). Then run:

```text
git clone https://github.com/gumadeiras/labjack-alicat-control.git
cd labjack-alicat-control
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

[alicat]
port = "/dev/cu.usbserial-EXAMPLE"
unit = "A"
baud_rate = 19200
timeout_seconds = 0.15
minimum_flow = 0.0
allow_negative_flow = false

# Optional experiment limit. The tool always enforces the device full scale:
# maximum_flow = <stricter experiment maximum>

# Add each verified unit:
# [alicat.units]
# pressure = "<verified pressure unit>"
# temperature = "<verified temperature unit>"
# volumetric_flow = "<verified volumetric-flow unit>"
# mass_flow = "<verified mass-flow unit>"
```

`lab.toml` is local and is not committed to Git. Unknown or invalid settings
cause a clear configuration error. `minimum_flow` and `maximum_flow` are
optional experiment limits in the current Alicat mass-flow unit.

## 4. Check the connections

List the serial ports:

```text
uv run lab-control ports
```

This command lists all serial ports. It does not identify an Alicat
automatically. Common port names are:

- macOS: `/dev/cu.usbserial-...`
- Linux: `/dev/ttyUSB0`
- Windows: `COM3`

Check both configured devices:

```text
uv run lab-control labjack status
uv run lab-control alicat status
```

The Alicat reports numeric values without unit names. The tool adds the units
from `lab.toml`. If a unit is missing, the output says
`device units not configured`.

## Read and control the LabJack

Read AIN0:

```text
uv run lab-control labjack read-analog --channel 0
```

Set FIO4 high:

```text
uv run lab-control labjack set-digital --channel 4 --state high
```

The first command supports AIN0 through AIN3. The second command supports FIO4
through FIO7. Each command checks the current analog or digital configuration
before it continues.

## Control the Alicat

Set the mass-flow setpoint:

```text
uv run lab-control alicat set-flow 1.0
```

Set the mass-flow setpoint to zero:

```text
uv run lab-control alicat stop
```

Both commands first read the current control point. They stop without sending a
setpoint if the control point is not `mass flow`. Change the control point on
the Alicat itself, confirm the gas system, and then retry.

Before each nonzero command, `set-flow` reads the mass-flow full scale and unit
from the Alicat. It refuses values outside the device range. `maximum_flow` is
optional and can set a stricter experiment limit. `stop` does not depend on the
full-scale query, but it still requires a valid Alicat connection and a
mass-flow control point.

## Temporary command overrides

You can override device identity without changing `lab.toml`:

```text
uv run lab-control labjack status --serial 320123456
uv run lab-control alicat status --port COM3 --unit B
```

Baud rate, timeout, units, and safety limits always come from `lab.toml`.

Use a different configuration file:

```text
uv run lab-control --config another-lab.toml alicat status
```

## Troubleshooting

- `Set alicat.port in lab.toml`: add the detected serial port or use `--port`.
- `Cannot open the Alicat MFC`: confirm the cable driver, port, baud rate, power,
  unit ID, and `Serial` input mode.
- `Refusing to change the setpoint`: the Alicat is not in mass-flow control
  mode. No setpoint was sent.
- `FIO... is configured as analog`: select a configured digital line or change
  the LabJack configuration with the official LabJack software.
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
serial client. They do not send commands to physical hardware. Cross-platform
CI tests the Python layer, package build, command routing, and driver protocol
logic.
