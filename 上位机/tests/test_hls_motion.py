"""Regression cases for enabling an unloaded HLS servo and submitting its goal."""
from dataclasses import replace
import math
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hand_control import (GOAL_POSITION, TORQUE_ENABLE, HlsBus, HandConfig, ROOT,
                          pose_timeout_message, pose_timeout_seconds)


class SimulatedSync:
    def __init__(self, bus):
        self.bus = bus
        self.params = {}
        self.count = 0

    def addParam(self, sid, payload):
        self.params[sid] = int.from_bytes(payload, "little")
        return True

    def txPacket(self):
        self.count += 1
        self.bus.operations.append(("sync", self.params.copy()))
        for sid, target in self.params.items():
            self.bus.state[sid][GOAL_POSITION] = target
            if self.bus.auto_enable:
                self.bus.state[sid][TORQUE_ENABLE] = 1
            if self.bus.drop_torque:
                self.bus.state[sid][TORQUE_ENABLE] = 0
        return 0

    def clearParam(self):
        self.params.clear()


class FirmwareBus(HlsBus):
    """Emulate a firmware that latches current position when explicitly enabled."""
    def __init__(self, ids=(1,), *, auto_enable=False, drop_torque=False):
        self.success = 0
        self.auto_enable = auto_enable
        self.drop_torque = drop_torque
        self.operations = []
        self.enabled_ids = set()
        self.limits = {sid: (2118, 3256) for sid in ids}
        self.positions = {sid: 2687 for sid in ids}
        self.state = {sid: {TORQUE_ENABLE: 0, GOAL_POSITION: 2400} for sid in ids}
        self.sync = SimulatedSync(self)
        self.closed = False
        self.port = SimpleNamespace(is_using=False, closePort=lambda: setattr(self, "closed", True))
        self.after_write = lambda *args: None

    def status(self, sid):
        return dict(id=sid, position_tick=self.positions[sid], speed_rpm=0,
                    voltage_v=5.8, temperature_c=28, status=0, motor_current_ma=26)

    def read_int(self, sid, address, size):
        return self.state[sid][address]

    def write_verified(self, sid, address, size, value):
        self.operations.append(("write", sid, address, value))
        self.state[sid][address] = value
        if address == GOAL_POSITION and self.auto_enable:
            self.state[sid][TORQUE_ENABLE] = 1
        if address == GOAL_POSITION and value != self.positions[sid] and self.drop_torque:
            self.state[sid][TORQUE_ENABLE] = 0
        if address == TORQUE_ENABLE and value == 1:
            self.state[sid][GOAL_POSITION] = self.positions[sid]
        self.after_write(sid, address, value)


class MotionSubmissionTests(unittest.TestCase):
    def test_final_goal_follows_explicit_enable_even_if_enable_resets_old_goal(self):
        bus = FirmwareBus()
        bus.move({1: 3142})
        self.assertEqual(bus.state[1][GOAL_POSITION], 3142)
        self.assertEqual(bus.operations[:3], [
            ("write", 1, GOAL_POSITION, 2687), ("write", 1, TORQUE_ENABLE, 1),
            ("write", 1, GOAL_POSITION, 3142)])
        self.assertEqual(bus.sync.count, 0)
        bus.close()
        self.assertEqual(bus.state[1][TORQUE_ENABLE], 0)
        self.assertTrue(bus.closed)

    def test_auto_enable_firmware_also_gets_current_hold_before_final_goal(self):
        bus = FirmwareBus(auto_enable=True)
        bus.move({1: 3142})
        self.assertEqual(bus.operations, [("write", 1, GOAL_POSITION, 2687), ("write", 1, GOAL_POSITION, 3142)])
        self.assertEqual(bus.state[1][TORQUE_ENABLE], 1)

    def test_all_servo_enables_precede_the_one_final_batch(self):
        bus = FirmwareBus((1, 2))
        bus.move({1: 3142, 2: 2300})
        self.assertEqual(bus.operations[-1], ("sync", {1: 3142, 2: 2300}))
        self.assertEqual(bus.sync.count, 1)
        bus.move({1: 3100, 2: 2400})
        self.assertEqual(bus.operations[-1], ("sync", {1: 3100, 2: 2400}))
        self.assertEqual(sum(op[0] == "write" for op in bus.operations), 4)

    def test_outside_soft_limits_does_not_enable_or_send_anything(self):
        bus = FirmwareBus()
        with self.assertRaises(ValueError):
            bus.move({1: 3300})
        self.assertEqual(bus.operations, [])
        self.assertEqual(bus.enabled_ids, set())

    def test_cancel_after_hold_never_sends_final_goal_and_close_unloads(self):
        bus = FirmwareBus((1, 2), auto_enable=True)
        stop = threading.Event()
        bus.after_write = lambda *args: stop.set()
        bus.move({1: 3142, 2: 2300}, stop_event=stop)
        self.assertEqual(bus.sync.count, 0)
        self.assertEqual(bus.enabled_ids, {1})
        bus.close()
        self.assertEqual(bus.state[1][TORQUE_ENABLE], 0)
        self.assertEqual(bus.state[2][TORQUE_ENABLE], 0)

    def test_torque_dropped_during_final_send_is_not_automatically_reenabled(self):
        bus = FirmwareBus(drop_torque=True)
        with self.assertRaisesRegex(RuntimeError, "不自动重新使能"):
            bus.move({1: 3142})
        self.assertEqual(sum(op == ("write", 1, TORQUE_ENABLE, 1) for op in bus.operations), 1)
        bus.close()
        self.assertEqual(bus.state[1][TORQUE_ENABLE], 0)

    def test_timeout_accounts_for_acceleration_at_high_speed(self):
        motor = replace(HandConfig(ROOT / "index.json").motors[1], speed_rpm=30)
        timeout = pose_timeout_seconds({1: motor}, {1: 0}, {1: 4095})
        minimum_physical_time = 2 * math.sqrt((4095 * 360 / 4096) / (5 * 8.79))
        self.assertGreater(timeout, minimum_physical_time + 2)

    def test_no_movement_diagnostic_distinguishes_failure_from_angle_limit(self):
        reading = dict(FirmwareBus().status(1), torque_enabled=1, goal_tick=3142)
        message = pose_timeout_message({1: 2687}, {1: 3142}, {1: reading}, 5)
        self.assertIn("未观察到有效转动", message)
        self.assertIn("+39.99°", message)
        self.assertIn("扭矩：开启", message)
        self.assertIn("反馈 2687", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
