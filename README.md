# LabJack U3 and Alicat control

This project provides a small command-line tool for two devices:

- LabJack U3
- Alicat mass flow controller (MFC)

The tool can find serial ports, check each connection, read values, and change
basic outputs. It supports macOS, Linux, and Windows.

## Safety

Confirm the gas path and the device flow units before you set a flow rate. The
tool uses the units configured on the Alicat. Start with a setpoint of zero when
you test a new connection.

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
driver if the operating system does not show a serial port.

## 2. Install the project

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). Then run:

```text
git clone https://github.com/gumadeiras/labjack-alicat-control.git
cd labjack-alicat-control
uv sync
```

`uv` installs the correct Python version and all Python packages in an isolated
environment.

## 3. Find the Alicat serial port

```text
uv run lab-control ports
```

Example ports:

- macOS: `/dev/cu.usbserial-AV0KB0LI`
- Linux: `/dev/ttyUSB0`
- Windows: `COM3`

## 4. Check each connection

Check the first connected LabJack U3:

```text
uv run lab-control labjack status
```

Check an Alicat with unit ID `A`:

```text
uv run lab-control alicat status --port /dev/cu.usbserial-AV0KB0LI
```

Use `--unit B` if the Alicat unit ID is `B`. Run
`uv run lab-control --help` to see all commands.

## Basic control

Read LabJack analog input AIN0:

```text
uv run lab-control labjack read-analog --channel 0
```

Set LabJack digital output FIO4 high:

```text
uv run lab-control labjack set-digital --channel 4 --state high
```

Set the Alicat flow rate:

```text
uv run lab-control alicat set-flow --port /dev/cu.usbserial-AV0KB0LI 1.0
```

Stop flow:

```text
uv run lab-control alicat stop --port /dev/cu.usbserial-AV0KB0LI
```

## Development checks

```text
uv sync --group dev
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests
uv build
```

Tests do not send commands to physical hardware.
