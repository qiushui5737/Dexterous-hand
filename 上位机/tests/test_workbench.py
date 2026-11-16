"""Real-only UI regressions. Serial is replaced with test doubles throughout."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hand_control import HandConfig, ROOT, main as control_main
from hand_gui import Console
from hand_motion import PRESETS, preset_pose


class PresetTests(unittest.TestCase):
    def test_manual_presets_stay_within_joint_and_differential_limits(self):
        for filename in ("index.json", "pinch.json", "full_hand.json"):
            config = HandConfig(ROOT / filename)
            for key in PRESETS:
                config.targets(preset_pose(config, key))

    def test_cli_cannot_start_a_simulated_server_or_demo(self):
        for arguments in (("serve",), ("demo",), ("client",)):
            with self.subTest(arguments=arguments), patch("sys.argv", ["hand_control.py", *arguments]), \
                    patch("hand_control.serial_bus") as serial, patch("sys.stderr"):
                with self.assertRaises(SystemExit) as exit_result:
                    control_main()
                self.assertEqual(exit_result.exception.code, 2)
                serial.assert_not_called()


class GuiFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.callback_errors = []
        cls.root.report_callback_exception = lambda *args: cls.callback_errors.append(args)
        with patch("hand_gui.serial_bus") as serial:
            cls.app = Console(cls.root)
            cls.startup_serial_calls = serial.call_count

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        if cls.callback_errors:
            raise AssertionError(cls.callback_errors)

    def setUp(self):
        self.app.stop()
        self.app.profile.set("整手 · 20 台")
        self.app.load_profile()
        self.root.geometry("1280x740+12+12")
        self.root.update()

    def pump(self, seconds=.15):
        self.root.after(max(1, int(seconds*1000)), self.root.quit)
        self.root.mainloop()

    def calibrated_config(self):
        data = self.app.config.to_data()
        data.update(calibrated=True, model_number=2570)
        self.app.apply_binding_data(data)
        self.app.port.set("COM_TEST")

    def test_only_requested_tabs_and_no_simulation_or_standalone_probe_controls(self):
        tabs = [self.app.tabs.tab(tab, "text").strip() for tab in self.app.tabs.tabs()]
        self.assertEqual(tabs, ["舵机监视", "目标 / 反馈曲线", "运行日志", "差动映射"])
        for attr in ("mode", "source_box", "mock", "player", "demo_button", "sequence_table",
                     "single_read_button", "read_button", "zero_button", "start_demo", "play_steps", "probe"):
            self.assertFalse(hasattr(self.app, attr), attr)
        self.assertEqual(self.root.bind("<F5>"), "")
        self.assertEqual(self.root.bind("<space>"), "")

    def test_startup_and_target_previews_never_open_serial_or_fabricate_feedback(self):
        self.assertEqual(self.startup_serial_calls, 0)
        with patch("hand_gui.serial_bus") as serial:
            self.app.nudge_joint("index_pip_flex", 5)
            self.app.apply_preset("half")
            self.app.home()
            self.app.preview_differential("combined")
            self.pump(.2)
            serial.assert_not_called()
        self.assertEqual(self.app.readings, {})
        self.assertFalse(self.app.samples)
        self.assertEqual(self.app.metric_vars["feedback"].get(), "等待实测")
        self.assertEqual(self.app.table.item("1", "values")[3], "—")

    def test_all_profiles_and_precise_manual_targets(self):
        for profile, count in (("食指 · 4 台", 4), ("对捏 · 8 台", 8), ("整手 · 20 台", 20)):
            self.app.profile.set(profile)
            self.app.load_profile()
            self.assertEqual(len(self.app.table.get_children()), count)
            self.app.nudge_joint("index_mcp_flex", .5)
            self.assertEqual(self.app.angles["index_mcp_flex"], .5)
            self.assertGreater(self.app.targets[1], 2048)
            self.assertGreater(self.app.targets[2], 2048)
            self.app.nudge_joint("index_mcp_abd", .5)
            self.assertGreater(self.app.targets[1], self.app.targets[2])

    def test_pose_save_load_and_bad_pose_leave_current_targets_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pose.json"
            self.app.apply_preset("half")
            expected = self.app.angles.copy()
            with patch("hand_gui.filedialog.asksaveasfilename", return_value=str(path)):
                self.app.save_pose()
            self.app.home()
            with patch("hand_gui.filedialog.askopenfilename", return_value=str(path)):
                self.app.load_pose()
            self.assertEqual(self.app.angles, expected)
            path.write_text(json.dumps({"format": "rapid-hand-pose/v1", "joints_deg": {"wrong": 1}}), encoding="utf-8")
            with patch("hand_gui.filedialog.askopenfilename", return_value=str(path)), patch("hand_gui.messagebox.showerror") as error:
                self.app.load_pose()
                error.assert_called_once()
            self.assertEqual(self.app.angles, expected)

    def test_preview_does_not_overwrite_measured_positions(self):
        self.app.readings = {1: {"id": 1, "position_tick": 2180, "status": 0, "voltage_v": 5.8}}
        self.app.last_feedback = time.monotonic()
        self.app.nudge_joint("index_mcp_flex", 5)
        self.pump()
        self.assertEqual(self.app.readings[1]["position_tick"], 2180)
        self.assertEqual(self.app.table.item("1", "values")[3], "2180")
        self.assertFalse(self.app.samples)

    def test_received_device_feedback_updates_chart_and_csv(self):
        for position in (2048, 2052):
            self.app.events.put(("reading", {"id": 1, "position_tick": position, "speed_rpm": 0,
                "motor_current_ma": 26, "voltage_v": 5.8, "temperature_c": 28, "status": 0}))
            self.pump(.08)
        self.app.tabs.select(1)
        self.root.update()
        self.app.draw_chart()
        self.assertGreater(len(self.app.chart.find_all()), 5)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.csv"
            with patch("hand_gui.filedialog.asksaveasfilename", return_value=str(path)):
                self.app.export()
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([int(row["position_tick"]) for row in rows], [2048, 2052])
            self.assertTrue(all(row["mode"] == "实机" and float(row["voltage_v"]) == 5.8 for row in rows))

    def test_compact_window_keeps_primary_controls_on_screen(self):
        self.root.update()
        for widget in (self.app.move_button, self.app.binding_button, self.app.stop_button, self.app.canvas, self.app.table):
            self.assertTrue(widget.winfo_ismapped())
            bottom = widget.winfo_rooty() - self.root.winfo_rooty() + widget.winfo_height()
            self.assertLessEqual(bottom, self.root.winfo_height())

    def test_mcp_and_cmc_preview_show_both_motor_targets(self):
        for name, ids in (("食指 · MCP", ("1", "2")), ("拇指 · CMC", ("5", "6"))):
            self.app.mapping_group.set(name)
            with patch("hand_gui.serial_bus") as serial:
                self.app.preview_differential("combined")
                serial.assert_not_called()
            self.assertEqual(self.app.mapping_table.get_children(), ids)
            a = self.app.mapping_table.item(ids[0], "values")
            b = self.app.mapping_table.item(ids[1], "values")
            self.assertEqual((a[3], b[3]), ("+15.00", "+5.00"))
            self.assertEqual((int(a[5]), int(b[5])), (2219, 2105))

    def test_port_failure_does_not_fall_back_to_generated_feedback(self):
        self.calibrated_config()
        with patch("hand_gui.messagebox.askokcancel", return_value=True), \
                patch("hand_gui.serial_bus", side_effect=OSError("port unavailable")), \
                patch("hand_gui.messagebox.showerror") as error:
            self.app.run_pose()
            self.app.worker.join(timeout=1)
            self.pump()
            error.assert_called_once()
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.readings)
        self.assertFalse(self.app.samples)

    def test_external_stream_opens_real_link_and_stop_exits_context(self):
        self.calibrated_config()
        started = threading.Event()
        calls = []

        def wait_for_stop(config, bus, tcp_port, *, stop_event, nearby_start, on_frame, notice):
            calls.append((tcp_port, nearby_start))
            started.set()
            stop_event.wait(2)

        with patch("hand_gui.serial_bus") as link, patch("hand_gui.serve", side_effect=wait_for_stop), \
                patch("hand_gui.messagebox.askokcancel", return_value=True), patch("hand_gui.messagebox.showerror") as error:
            self.app.start_stream()
            self.assertTrue(started.wait(1))
            link.assert_called_once_with("COM_TEST", 1000000)
            self.assertEqual(calls, [(8765, True)])
            self.app.stop()
            self.app.worker.join(timeout=1)
            self.pump()
            self.assertFalse(self.app.busy)
            link.return_value.__exit__.assert_called_once()
            error.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
