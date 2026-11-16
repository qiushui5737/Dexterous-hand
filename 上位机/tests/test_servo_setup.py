"""EEPROM commissioning with immediate ID/baud changes; all devices are simulated."""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hand_control import HLS_BAUD_RATES, HlsBus
from hand_gui import Console
from hand_servo_setup import (CONFIG_BYTES, ServoSetupDialog, configure_servo,
                              read_servo_settings, scan_servo_ids)


class ConfigurationBus:
    def __init__(self, *, auto_alias=True):
        self.registers = {5: 1, 6: 0, 7: 253, 8: 0, 30: 2, 33: 1, 55: 1}
        self.host_baud = 1000000
        self.operations, self.writes = [], []
        self.torque = 0
        self.auto_alias = auto_alias
        self.other_ids = set()
        self.failed_write = None
        self.on_write = lambda *args: None
        self.port = SimpleNamespace(is_using=False, getBaudRate=lambda: self.host_baud,
            setBaudRate=self.set_baud,
            ser=SimpleNamespace(flush=lambda: None, write_timeout=.2))
        self.packet = SimpleNamespace(writeTxOnly=self.send, readTxRx=self.rx)

    def set_baud(self, baud):
        self.operations.append(("host_baud", baud))
        self.host_baud = baud
        return True

    def reachable(self, sid):
        return sid == self.registers[5] and self.host_baud == HLS_BAUD_RATES[self.registers[6]]

    def check(self, result, error, context):
        if result or error:
            raise RuntimeError(f"{context}: simulated communication failure {result}")

    def probe(self, sid):
        if not self.reachable(sid):
            raise RuntimeError("no status packet at this ID/baud")
        return dict(id=sid, primary_id=sid, secondary_id=self.registers[7],
            baud_code=self.registers[6], baud_rate=HLS_BAUD_RATES[self.registers[6]],
            model_number=2570, firmware_major=3, firmware_minor=43, endian=0,
            torque_enabled=self.torque, speed_rpm=0, position_tick=2048,
            voltage_v=5.8, temperature_c=28, status=0, motor_current_ma=0,
            mode=self.registers[33], resolution=self.registers[30], reply_level=self.registers[8])

    def read_int(self, sid, address, size):
        self.operations.append(("read", sid, address, self.host_baud))
        if not self.reachable(sid):
            raise RuntimeError("no status packet at this ID/baud")
        return self.registers[address]

    def rx(self, sid, address, size):
        if sid in self.other_ids:
            return [sid], 0, 0
        if self.reachable(sid):
            return [self.registers[address]], 0, 0
        return [], -6, 0

    def send(self, sid, address, size, data):
        if not self.reachable(sid):
            return -1001
        value = data[0]
        self.writes.append((sid, address, value, self.host_baud))
        self.operations.append(("write", sid, address, value, self.host_baud))
        if address != 55 and self.registers[55] != 0:
            raise AssertionError("EEPROM write while locked")
        self.registers[address] = value
        if address == 5 and self.auto_alias:
            self.registers[7] = value
        self.on_write(address, value)
        if (address, value) == self.failed_write:
            self.failed_write = None
            return -1001  # applied despite lost transmission confirmation
        return 0


def apply(bus, **kwargs):
    options = dict(current_id=1, new_id=2, new_baud=1000000,
                   isolated=True, settle=lambda _: None)
    options.update(kwargs)
    return configure_servo(bus, **options)


class ServoConfigurationTests(unittest.TestCase):
    def test_read_and_scan_do_not_write_or_require_a_joint_binding(self):
        bus = ConfigurationBus()
        self.assertEqual(read_servo_settings(bus, 1)["primary_id"], 1)
        found = scan_servo_ids(bus)
        self.assertEqual([info["primary_id"] for info in found], [1])
        self.assertEqual(bus.writes, [])

    def test_id_change_verifies_and_locks_at_new_id_without_waiting_for_old_reply(self):
        bus = ConfigurationBus(auto_alias=False)
        result = apply(bus)
        self.assertEqual(result["after"]["primary_id"], 2)
        self.assertEqual(result["after"]["secondary_id"], 2)
        self.assertEqual((bus.registers[33], bus.registers[30], bus.registers[8]), (0, 1, 1))
        self.assertEqual(bus.writes[-1][:3], (2, 55, 1))
        self.assertEqual(bus.torque, 0)
        self.assertTrue(all(address in CONFIG_BYTES and sid < 254 for sid, address, _, _ in bus.writes))
        self.assertTrue(all(address not in (40, 41, 42, 44, 46, 48) for _, address, _, _ in bus.writes))

    def test_id_and_baud_change_reconnects_before_relocking(self):
        bus = ConfigurationBus()
        result = apply(bus, new_baud=500000)
        self.assertEqual(result["after"]["baud_rate"], 500000)
        self.assertEqual(bus.host_baud, 500000)
        self.assertEqual(bus.writes[-1], (2, 55, 1, 500000))

    def test_unconfirmed_isolation_or_invalid_id_or_baud_never_writes(self):
        for options in (dict(isolated=False), dict(new_id=254), dict(new_id=-1), dict(new_baud=9600)):
            bus = ConfigurationBus()
            with self.subTest(options=options), self.assertRaises(ValueError):
                apply(bus, **options)
            self.assertEqual(bus.writes, [])

    def test_occupied_target_and_enabled_torque_abort_before_unlock(self):
        for occupied, torque in ((True, 0), (False, 1)):
            bus = ConfigurationBus()
            bus.other_ids = {2} if occupied else set()
            bus.torque = torque
            with self.subTest(occupied=occupied), self.assertRaises(ValueError):
                apply(bus)
            self.assertEqual(bus.writes, [])

    def test_changed_identification_invalidates_the_previous_read(self):
        bus = ConfigurationBus()
        expected = dict(read_servo_settings(bus, 1), firmware_minor=44)
        with self.assertRaisesRegex(ValueError, "重新读取"):
            apply(bus, expected=expected)
        self.assertEqual(bus.writes, [])

    def test_lost_confirmation_after_identity_or_baud_change_still_relocks(self):
        for address, value, expected_id, expected_baud in ((55, 0, 1, 1000000),
                (5, 2, 2, 1000000), (6, 1, 2, 500000)):
            bus = ConfigurationBus()
            bus.failed_write = address, value
            with self.subTest(address=address), self.assertRaisesRegex(RuntimeError, "回锁"):
                apply(bus, new_baud=500000)
            self.assertEqual(bus.registers[55], 1)
            self.assertEqual(bus.registers[5], expected_id)
            self.assertEqual(HLS_BAUD_RATES[bus.registers[6]], expected_baud)

    def test_cancellation_after_id_change_relocks_and_reports_new_id(self):
        bus = ConfigurationBus()
        stop = threading.Event()
        bus.on_write = lambda address, value: stop.set() if address == 5 else None
        with self.assertRaisesRegex(RuntimeError, "ID 2 / 1000000 回锁"):
            apply(bus, stop_event=stop)
        self.assertEqual(bus.registers[55], 1)
        self.assertEqual(bus.registers[5], 2)

    def test_cancellation_during_final_relock_does_not_silently_lose_new_endpoint(self):
        bus = ConfigurationBus()
        stop = threading.Event()
        bus.on_write = lambda address, value: stop.set() if (address, value) == (55, 1) else None
        with self.assertRaisesRegex(RuntimeError, "ID 2 / 1000000 回锁"):
            apply(bus, stop_event=stop)
        self.assertEqual(bus.registers[55], 1)

    def test_optional_initialization_can_preserve_operating_parameters(self):
        bus = ConfigurationBus()
        result = apply(bus, initialize=False)
        self.assertEqual((result["after"]["mode"], result["after"]["resolution"],
                          result["after"]["reply_level"]), (1, 2, 0))
        self.assertTrue(result["after"]["eeprom_locked"])


class ServoSetupGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.app = Console(cls.root)
        cls.errors = []
        cls.root.report_callback_exception = lambda *args: cls.errors.append(args)

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        if cls.errors:
            raise AssertionError(cls.errors)

    def setUp(self):
        self.app.port.set("COM_TEST")
        self.app.baud.set("1000000")
        self.dialog = ServoSetupDialog(self.app)
        self.app.servo_setup_dialog = self.dialog
        self.root.update()

    def tearDown(self):
        if self.dialog.window.winfo_exists():
            self.dialog.close()

    def join_job(self):
        self.app.worker.join(timeout=2)
        self.assertFalse(self.app.worker.is_alive())
        self.root.after(150, self.root.quit)
        self.root.mainloop()

    def test_unbound_servo_can_be_read_and_configured_without_changing_bindings(self):
        self.assertNotIn(31, self.app.config.motors)
        self.dialog.current_id.set("31")
        self.dialog.new_id.set("32")
        bus = ConfigurationBus()
        bus.registers[5] = 31
        before_motors = self.app.config.to_data()
        with patch("hand_gui.serial_bus") as link, patch("hand_servo_setup.messagebox.askokcancel", return_value=True), \
                patch("hand_gui.messagebox.showerror") as error:
            link.return_value.__enter__.return_value = bus
            self.dialog.read()
            self.join_job()
            self.assertEqual(self.dialog.observed["primary_id"], 31)
            self.dialog.isolated.set(True)
            with patch("hand_servo_setup.configure_servo", wraps=lambda *args, **kwargs:
                       configure_servo(*args, **kwargs, settle=lambda _: None)):
                self.dialog.apply()
                self.join_job()
            self.assertEqual(self.dialog.current_id.get(), "32")
            self.assertEqual(self.dialog.observed["secondary_id"], 32)
            self.assertEqual(self.app.config.to_data(), before_motors)
            error.assert_not_called()

    def test_no_isolation_or_changed_connection_or_cancel_never_writes(self):
        bus = ConfigurationBus()
        self.dialog.observed = read_servo_settings(bus, 1)
        self.dialog.read_endpoint = "COM_TEST", 1000000, 1
        with patch("hand_gui.serial_bus") as link, patch("hand_servo_setup.messagebox.showinfo"), \
                patch("hand_servo_setup.messagebox.askokcancel", return_value=False):
            self.dialog.apply()
            self.dialog.isolated.set(True)
            self.dialog.current_id.set("2")
            self.dialog.apply()
            self.dialog.current_id.set("1")
            self.dialog.apply()
            link.assert_not_called()

    def test_setup_entry_disabled_during_other_jobs(self):
        self.assertNotIn("disabled", self.app.servo_setup_button.state())
        self.app.busy=True
        try:
            self.app.refresh_controls()
            self.assertIn("disabled", self.app.servo_setup_button.state())
        finally:
            self.app.busy=False
            self.app.refresh_controls()

    def test_configuration_controls_remain_inside_the_dialog_at_minimum_size(self):
        self.dialog.window.geometry("700x620+20+20")
        self.root.update()
        for widget in (self.dialog.isolation_check, self.dialog.apply_button, self.dialog.read_button):
            self.assertTrue(widget.winfo_ismapped())
            bottom = widget.winfo_rooty() - self.dialog.window.winfo_rooty() + widget.winfo_height()
            self.assertLessEqual(bottom, self.dialog.window.winfo_height())


if __name__ == "__main__":
    unittest.main(verbosity=2)
