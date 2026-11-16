"""Sparse bindings and the manual-send UI, without opening physical serial ports."""
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hand_bindings import BindingEditor
from hand_control import (CommandSession, HandConfig, HlsBus, ROOT,
                          calibrate_bindings, hardware_limits)
from hand_gui import Console
from device_doubles import FakeBus


def sparse_data():
    data = HandConfig(ROOT / "full_hand.json").to_data()
    data.update(calibrated=False, allow_unbound_joints=True, model_number=None)
    data["motors"] = [{"id": 1, "zero_tick": 2048, "min_tick": 1365, "max_tick": 2731,
                       "weights": {"index_pip_flex": 1}, "current_ma": 250, "speed_rpm": 5}]
    return data


def feedback(sid=1, position=2153):
    return dict(id=sid, position_tick=position, speed_rpm=0.0, voltage_v=5.8,
                temperature_c=27, status=0, motor_current_ma=0, model_number=2570,
                mode=0, resolution=1, min_tick=0, max_tick=0, torque_enabled=0,
                primary_id=sid, secondary_id=253, reply_level=1,
                firmware_major=3, firmware_minor=43, endian=0, full_angle_feedback=True)


class SparseBindingTests(unittest.TestCase):
    def test_unbound_joint_changes_produce_no_motor_target(self):
        config = HandConfig.from_data(sparse_data())
        pose = dict.fromkeys(config.joints, 0)
        expected = config.targets(pose)
        pose.update(index_mcp_flex=20, thumb_ip_flex=20)
        self.assertEqual(config.targets(pose), expected)
        pose["index_pip_flex"] = 10
        self.assertEqual(config.targets(pose), {1: 2162})
        bus = FakeBus(config.motors)
        bus.move = Mock(wraps=bus.move)
        CommandSession(config, bus).set_joint_positions(pose)
        bus.move.assert_called_once()
        self.assertEqual(bus.move.call_args.args, ({1: 2162},))

    def test_no_binding_frame_never_prepares_or_writes(self):
        data = sparse_data()
        data["motors"] = []
        config = HandConfig.from_data(data)
        bus = Mock()
        with self.assertRaisesRegex(ValueError, "没有已绑定"):
            CommandSession(config, bus).set_joint_positions(dict.fromkeys(config.joints, 0))
        self.assertFalse(bus.mock_calls)

    def test_legacy_profiles_still_require_complete_differential_mapping(self):
        data = sparse_data()
        data.pop("allow_unbound_joints")
        with self.assertRaises(ValueError):
            HandConfig.from_data(data)

    def test_read_only_calibration_preserves_relative_limits_and_direction(self):
        data = sparse_data()
        data["motors"][0]["weights"] = {"index_pip_flex": -2}
        original = HandConfig.from_data(data)
        config = calibrate_bindings(original, {1: feedback()})
        self.assertTrue(config.calibrated)
        self.assertEqual(config.model_number, 2570)
        self.assertEqual(config.motors[1].zero_tick, 2153)
        self.assertEqual((config.motors[1].min_tick, config.motors[1].max_tick), (1470, 2836))
        self.assertEqual(config.motors[1].weights, {"index_pip_flex": -2})
        self.assertTrue(config.motors[1].allow_multiturn)
        self.assertEqual(original.motors[1].zero_tick, 2048)
        self.assertEqual(HandConfig.from_data(config.to_data()).to_data(), config.to_data())

    def test_bad_calibration_never_changes_existing_configuration(self):
        config = HandConfig.from_data(sparse_data())
        original = config.to_data()
        for key, value in (("torque_enabled", 1), ("mode", 1), ("full_angle_feedback", False),
                           ("position_tick", 4080), ("status", 1), ("voltage_v", 4.4)):
            with self.subTest(key=key), self.assertRaises((ValueError, RuntimeError)):
                calibrate_bindings(config, {1: dict(feedback(), **{key: value})})
        with self.assertRaises(ValueError):
            calibrate_bindings(config, {})
        self.assertEqual(config.to_data(), original)

    def test_multiturn_requires_opt_in_full_feedback_and_still_uses_soft_limits(self):
        info = feedback()
        with self.assertRaises(RuntimeError):
            hardware_limits(info)
        self.assertEqual(hardware_limits(info, True), (0, 4095))
        config = calibrate_bindings(HandConfig.from_data(sparse_data()), {1: info})
        bus = object.__new__(HlsBus)
        bus.limits = {}
        bus.probe = Mock(return_value=info)
        bus.write_verified = Mock()
        self.assertEqual(bus.prepare(config.motors, 2570), {1: 2153})
        self.assertEqual(bus.limits, {1: (1470, 2836)})
        bus.probe.return_value = dict(info, full_angle_feedback=False)
        bus.write_verified.reset_mock()
        with self.assertRaises(RuntimeError):
            bus.prepare(config.motors, 2570)
        bus.write_verified.assert_not_called()


class BindingGuiTests(unittest.TestCase):
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
        self.app.stop()
        self.app.profile.set("整手 · 20 台")
        self.app.load_profile()
        self.root.geometry("1280x740+0+0")
        self.root.update()

    def pump(self, seconds=.15):
        self.root.after(max(1, int(seconds * 1000)), self.root.quit)
        self.root.mainloop()

    def join_job(self):
        self.app.worker.join(timeout=2)
        self.assertFalse(self.app.worker.is_alive())
        self.pump()

    def test_send_and_binding_buttons_stay_visible_without_scrolling(self):
        self.root.update()
        for widget in (self.app.move_button, self.app.binding_button, self.app.binding_zero_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertGreaterEqual(widget.winfo_rooty(), self.root.winfo_rooty())
            self.assertLessEqual(widget.winfo_rooty() - self.root.winfo_rooty() + widget.winfo_height(), self.root.winfo_height())

    def test_editor_can_replace_template_with_one_binding_without_serial(self):
        with patch("hand_gui.serial_bus") as serial:
            editor = BindingEditor(self.app, "index_pip_flex")
            editor.clear()
            editor.fields["low"].set("-60")
            editor.fields["high"].set("60")
            editor.upsert()
            self.assertEqual(len(self.app.config.motors), 20)  # changes stay in the dialog until applied
            self.assertEqual(len(editor.data["motors"]), 1)
            editor.apply()
            self.assertEqual(set(self.app.config.motors), {1})
            self.assertEqual(self.app.config.motors[1].weights, {"index_pip_flex": 1})
            self.assertEqual(len(self.app.config.joints), 20)
            self.assertIn("19 个关节不发送", self.app.binding_status.get())
            serial.assert_not_called()

    def test_capture_then_send_only_addresses_bound_servo(self):
        self.app.apply_binding_data(sparse_data())
        self.app.port.set("COM_TEST")
        bus = Mock(spec=["probe", "prepare", "move", "status", "check_health", "limits"])
        bus.probe.return_value = feedback()
        with patch("hand_gui.serial_bus") as serial, patch("hand_gui.messagebox.askokcancel", return_value=True), \
                patch("hand_gui.messagebox.showerror") as error:
            serial.return_value.__enter__.return_value = bus
            self.app.capture_binding_zero()
            self.join_job()
            bus.probe.assert_called_once_with(1)
            bus.prepare.assert_not_called()
            bus.move.assert_not_called()
            self.assertTrue(self.app.config.calibrated)
            self.app.nudge_joint("index_pip_flex", 10)
            self.app.nudge_joint("thumb_ip_flex", 20)  # unbound, does not add an ID
            self.assertEqual(self.app.targets, {1: 2267})
            bus.prepare.return_value = {1: 2153}
            bus.limits = {1: (1470, 2836)}
            bus.status.return_value = dict(feedback(position=2267), torque_enabled=1)
            self.app.move_button.invoke()
            self.join_job()
            self.assertEqual(set(bus.prepare.call_args.args[0]), {1})
            self.assertEqual(bus.move.call_args.args, ({1: 2267},))
            self.assertTrue(all(call.args == (1,) for call in bus.status.call_args_list))
            self.assertEqual(serial.call_count, 2)
            error.assert_not_called()

    def test_uncalibrated_and_empty_bindings_never_open_serial(self):
        self.app.apply_binding_data(sparse_data())
        with patch("hand_gui.serial_bus") as serial, patch("hand_gui.messagebox.showinfo"):
            self.app.run_pose()
            self.app.start_stream()
            data = sparse_data()
            data["motors"] = []
            self.app.apply_binding_data(data)
            self.root.update()
            self.app.run_pose()
            self.app.start_stream()
            serial.assert_not_called()
        self.assertEqual(self.app.table.get_children(), ())

    def test_timeout_between_servo_reads_reports_failure_and_exits_serial_context(self):
        data = sparse_data()
        data.update(calibrated=True, model_number=2570)
        data["motors"].append(dict(data["motors"][0], id=2, weights={"index_dip_flex": 1}))
        self.app.apply_binding_data(data)
        self.app.port.set("COM_TEST")
        bus = Mock(spec=["prepare", "move", "status", "check_health", "limits"])
        bus.prepare.return_value = {1: 2048, 2: 2048}
        bus.limits = {1: (1365, 2731), 2: (1365, 2731)}
        clock = [100.0]

        def slow_status(sid):
            clock[0] += 10
            return dict(feedback(sid, 2000), torque_enabled=1)

        bus.status.side_effect = slow_status
        with patch("hand_gui.serial_bus") as serial, patch("hand_gui.messagebox.askokcancel", return_value=True), \
                patch("hand_gui.messagebox.showerror") as error:
            serial.return_value.__enter__.return_value = bus
            gui_clock = SimpleNamespace(monotonic=lambda: clock[0], time=time.time, strftime=time.strftime)
            with patch("hand_gui.time", gui_clock):
                self.app.run_pose()
                self.app.worker.join(timeout=2)
            self.pump()
            self.assertFalse(self.app.busy)
            error.assert_called_once()
            self.assertIn("未在预计时间内到位", error.call_args.args[1])
            serial.return_value.__exit__.assert_called_once()

    def test_runtime_unload_and_lost_goal_stop_without_automatic_retry(self):
        for torque, goal, expected in ((0, 2048, "运行中扭矩已关闭"), (1, 2000, "指令目标未保持")):
            with self.subTest(torque=torque, goal=goal):
                data = sparse_data()
                data.update(calibrated=True, model_number=2570)
                self.app.apply_binding_data(data)
                self.app.port.set("COM_TEST")
                bus = Mock(spec=["prepare", "move", "status", "check_health", "limits"])
                bus.prepare.return_value = {1: 2048}
                bus.limits = {1: (1365, 2731)}
                bus.status.return_value = dict(feedback(position=2048), torque_enabled=torque, goal_tick=goal)
                with patch("hand_gui.serial_bus") as serial, patch("hand_gui.messagebox.askokcancel", return_value=True), \
                        patch("hand_gui.messagebox.showerror") as error:
                    serial.return_value.__enter__.return_value = bus
                    self.app.run_pose()
                    self.join_job()
                    error.assert_called_once()
                    self.assertIn(expected, error.call_args.args[1])
                    self.assertEqual(bus.move.call_count, 1)
                    self.assertEqual(bus.prepare.call_count, 1)
                    serial.return_value.__exit__.assert_called_once()

    def test_saving_bindings_preserves_pose_without_serial(self):
        self.app.apply_binding_data(sparse_data())
        self.app.nudge_joint("index_pip_flex", .5)
        pose=self.app.angles.copy()
        with tempfile.TemporaryDirectory() as directory, patch("hand_gui.serial_bus") as serial:
            path=Path(directory)/"bindings.json"
            with patch("hand_gui.filedialog.asksaveasfilename",return_value=str(path)):
                self.app.save_bindings()
            self.assertEqual(self.app.angles,pose)
            config=HandConfig(path)
            self.assertEqual(set(config.motors),{1})
            self.assertTrue(config.allow_unbound_joints)
            serial.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
