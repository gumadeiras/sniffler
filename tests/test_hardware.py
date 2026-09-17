"""Driver-level tests that do not use physical hardware."""

import sys
import types
import unittest
from typing import ClassVar
from unittest.mock import patch

from alicat.driver import FlowMeter
from alicat.mock import Client as MockAlicatClient

from sniffler.hardware import (
    DeviceError,
    alicat_status,
    digital_channel_name,
    read_labjack_analog,
    set_alicat_flow,
    set_labjack_digital,
)


class FakeU3:
    instances: ClassVar[list["FakeU3"]] = []

    def __init__(self, **options) -> None:
        self.options = options
        self.calibrated = False
        self.closed = False
        self.digital_write: tuple[int, int] | None = None
        self.analog_mask = 0b00001111
        self.eio_analog_mask = 0b00000001
        self.instances.append(self)

    def getCalibrationData(self) -> None:
        self.calibrated = True

    def configU3(self) -> dict[str, int]:
        return {"FIOAnalog": self.analog_mask, "EIOAnalog": self.eio_analog_mask}

    def getAIN(self, channel: int) -> float:
        return 1.25 + channel

    def setDOState(self, channel: int, state: int) -> None:
        self.digital_write = (channel, state)

    def getDIOState(self, _channel: int) -> int:
        return self.digital_write[1] if self.digital_write else 0

    def close(self) -> None:
        self.closed = True


class LabJackTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeU3.instances.clear()
        self.u3_module = types.SimpleNamespace(U3=FakeU3)

    def test_routes_serial_calibrates_reads_and_closes(self) -> None:
        with patch.dict(sys.modules, {"u3": self.u3_module}):
            voltage = read_labjack_analog(2, 320123456)

        device = FakeU3.instances[0]
        self.assertEqual(voltage, 3.25)
        self.assertEqual(device.options, {"firstFound": False, "serial": 320123456})
        self.assertTrue(device.calibrated)
        self.assertTrue(device.closed)

    def test_sets_only_a_configured_digital_line(self) -> None:
        with patch.dict(sys.modules, {"u3": self.u3_module}):
            state = set_labjack_digital(4, True)

        device = FakeU3.instances[0]
        self.assertTrue(state)
        self.assertEqual(device.digital_write, (4, 1))
        self.assertTrue(device.closed)

        with (
            patch.dict(sys.modules, {"u3": self.u3_module}),
            self.assertRaisesRegex(DeviceError, "configured as analog"),
        ):
            set_labjack_digital(0, True)

    def test_sets_switching_board_lines_and_checks_the_eio_analog_mask(self) -> None:
        with patch.dict(sys.modules, {"u3": self.u3_module}):
            eio_state = set_labjack_digital(9, True)
            cio_state = set_labjack_digital(16, True)

        self.assertTrue(eio_state)
        self.assertEqual(FakeU3.instances[0].digital_write, (9, 1))
        self.assertTrue(cio_state)
        self.assertEqual(FakeU3.instances[1].digital_write, (16, 1))

        with (
            patch.dict(sys.modules, {"u3": self.u3_module}),
            self.assertRaisesRegex(DeviceError, "EIO0 is configured as analog"),
        ):
            set_labjack_digital(8, True)

    def test_names_each_digital_channel_group(self) -> None:
        self.assertEqual(digital_channel_name(4), "FIO4")
        self.assertEqual(digital_channel_name(8), "EIO0")
        self.assertEqual(digital_channel_name(15), "EIO7")
        self.assertEqual(digital_channel_name(19), "CIO3")


class MassFlowClient(MockAlicatClient):
    def __init__(self, address: str, **_options) -> None:
        super().__init__(address)
        self.control_point = "mass flow"

    def _handle_write(self, data: bytes) -> None:
        message = data.decode().strip()
        if message[1:] == "LSS":
            self.unit = message[0]
            self._next_reply = f"{self.unit} U"
            return
        if message[1:] == "FPF 5":
            self.unit = message[0]
            self._next_reply = f"{self.unit} 2.0000 12 SCCM"
            return
        super()._handle_write(data)


class AnalogSetpointClient(MassFlowClient):
    def _handle_write(self, data: bytes) -> None:
        message = data.decode().strip()
        if message[1:] == "LSS":
            self.unit = message[0]
            self._next_reply = f"{self.unit} A"
            return
        super()._handle_write(data)


class PressureClient(MockAlicatClient):
    def __init__(self, address: str, **_options) -> None:
        super().__init__(address)
        self.control_point = "abs pressure"


class UnconfirmedWriteClient(MassFlowClient):
    def _handle_write(self, data: bytes) -> None:
        message = data.decode().strip()
        if len(message) > 1 and message[1] == "S":
            self.unit = message[0]
            self.state["setpoint"] = float(message[2:])
            self._next_reply = ""
            return
        super()._handle_write(data)


class AlicatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FlowMeter.open_ports.clear()

    async def asyncTearDown(self) -> None:
        FlowMeter.open_ports.clear()

    async def test_status_routes_connection_parameters(self) -> None:
        options: list[dict[str, object]] = []

        def client(address: str, **kwargs):
            options.append({"address": address, **kwargs})
            return MassFlowClient(address)

        with patch("alicat.driver.SerialClient", side_effect=client):
            state = await alicat_status("/dev/mock", "B", 9600, 0.5)

        self.assertEqual(state["control_point"], "mass flow")
        self.assertEqual(
            options,
            [{"address": "/dev/mock", "baudrate": 9600, "timeout": 0.5}],
        )
        self.assertEqual(state["setpoint_source"], "serial or display, zero on power-up")

    async def test_rounds_and_sets_flow_without_an_extra_status_read(self) -> None:
        clients: list[MassFlowClient] = []

        def client(address: str, **_options):
            controller = MassFlowClient(address)
            clients.append(controller)
            return controller

        with patch("alicat.driver.SerialClient", side_effect=client):
            applied = await set_alicat_flow("/dev/mock", 1.234)

        messages = [
            call.args[0].decode().strip() for call in clients[0].writer.write.call_args_list
        ]
        self.assertEqual(applied, (1.23, "SCCM"))
        self.assertEqual(messages, ["AR122", "A", "ALSS", "AFPF 5", "AS1.23"])

    async def test_refuses_flow_above_the_device_full_scale(self) -> None:
        clients: list[MassFlowClient] = []

        def client(address: str, **_options):
            controller = MassFlowClient(address)
            clients.append(controller)
            return controller

        with (
            patch("alicat.driver.SerialClient", side_effect=client),
            self.assertRaisesRegex(DeviceError, "full scale of 2 SCCM"),
        ):
            await set_alicat_flow("/dev/mock", 2.01)

        messages = [
            call.args[0].decode().strip() for call in clients[0].writer.write.call_args_list
        ]
        self.assertEqual(messages, ["AR122", "A", "ALSS", "AFPF 5"])

    async def test_zero_flow_does_not_depend_on_the_full_scale_query(self) -> None:
        clients: list[MassFlowClient] = []

        def client(address: str, **_options):
            controller = MassFlowClient(address)
            clients.append(controller)
            return controller

        with patch("alicat.driver.SerialClient", side_effect=client):
            applied = await set_alicat_flow("/dev/mock", 0.0)

        messages = [
            call.args[0].decode().strip() for call in clients[0].writer.write.call_args_list
        ]
        self.assertEqual(applied, (0.0, None))
        self.assertEqual(messages, ["AR122", "A", "ALSS", "AS0.00"])

    async def test_refuses_an_analog_setpoint_source(self) -> None:
        clients: list[AnalogSetpointClient] = []

        def client(address: str, **_options):
            controller = AnalogSetpointClient(address)
            clients.append(controller)
            return controller

        with (
            patch("alicat.driver.SerialClient", side_effect=client),
            self.assertRaisesRegex(DeviceError, "source is analog"),
        ):
            await set_alicat_flow("/dev/mock", 1.0)

        messages = [
            call.args[0].decode().strip() for call in clients[0].writer.write.call_args_list
        ]
        self.assertEqual(messages, ["AR122", "A", "ALSS"])

    async def test_refuses_to_change_pressure_control_mode(self) -> None:
        clients: list[PressureClient] = []

        def client(address: str, **_options):
            controller = PressureClient(address)
            clients.append(controller)
            return controller

        with (
            patch("alicat.driver.SerialClient", side_effect=client),
            self.assertRaisesRegex(DeviceError, "Refusing"),
        ):
            await set_alicat_flow("/dev/mock", 1.0)

        messages = [
            call.args[0].decode().strip() for call in clients[0].writer.write.call_args_list
        ]
        self.assertEqual(messages, ["AR122", "A"])

    async def test_reports_that_an_unconfirmed_write_might_have_changed(self) -> None:
        clients: list[UnconfirmedWriteClient] = []

        def client(address: str, **_options):
            controller = UnconfirmedWriteClient(address)
            clients.append(controller)
            return controller

        with (
            patch("alicat.driver.SerialClient", side_effect=client),
            self.assertRaisesRegex(DeviceError, "might have changed"),
        ):
            await set_alicat_flow("/dev/mock", 1.0)

        self.assertEqual(clients[0].state["setpoint"], 1.0)

    async def test_rejects_nonfinite_flow_before_opening_a_connection(self) -> None:
        with (
            patch("alicat.driver.SerialClient") as client,
            self.assertRaisesRegex(DeviceError, "must be finite"),
        ):
            await set_alicat_flow("/dev/mock", float("nan"))

        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
