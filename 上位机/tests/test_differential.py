"""验证双舵机映射、独立自由度和越界时的整帧拒绝。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hand_control import CommandSession, HandConfig, ROOT
from device_doubles import FakeBus


class RecordingBus(FakeBus):
    def __init__(self, motors):
        super().__init__(motors)
        self.prepares = 0
        self.moves = []

    def prepare(self, *args, **kwargs):
        self.prepares += 1
        return super().prepare(*args, **kwargs)

    def move(self, targets, **kwargs):
        self.moves.append(targets.copy())
        super().move(targets, **kwargs)


class DifferentialTests(unittest.TestCase):
    def config_from(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return HandConfig(path)

    def source(self):
        return json.loads((ROOT / "index.json").read_text(encoding="utf-8-sig"))

    def test_all_five_pairs_for_both_axes_and_combined_motion(self):
        config = HandConfig(ROOT / "full_hand.json")
        self.assertEqual(len(config.differential_pairs), 5)
        cases = ((10, 0, 2162, 2162), (0, 5, 2105, 1991),
                 (0, -5, 1991, 2105), (10, 5, 2219, 2105))
        for pair in config.differential_pairs.values():
            for flex, side, expected_a, expected_b in cases:
                with self.subTest(pair=pair.name, flex=flex, side=side):
                    pose = dict.fromkeys(config.joints, 0)
                    pose.update({pair.flex_joint: flex, pair.side_joint: side})
                    targets = config.targets(pose)
                    self.assertEqual(tuple(targets[sid] for sid in pair.motor_ids), (expected_a, expected_b))
                    for sid in config.motors.keys() - set(pair.motor_ids):
                        self.assertEqual(targets[sid], 2048)

    def test_profile_pairs_are_derived_from_mapping_not_fixed_ids(self):
        for filename, expected in (("index.json", 1), ("pinch.json", 2), ("full_hand.json", 5)):
            self.assertEqual(len(HandConfig(ROOT / filename).differential_pairs), expected)
        data = self.source()
        data["motors"][0]["id"] = 31
        data["motors"][1]["id"] = 42
        config = self.config_from(data)
        self.assertEqual(config.differential_pairs["index_mcp"].motor_ids, (31, 42))

    def test_unequal_ratios_directions_and_independent_zero_offsets(self):
        data = self.source()
        data["motors"][0].update(zero_tick=2100, min_tick=1000, max_tick=3000,
                                 weights={"index_mcp_flex": 2, "index_mcp_abd": -1})
        data["motors"][1].update(zero_tick=1900, min_tick=1000, max_tick=3000,
                                 weights={"index_mcp_flex": -.5, "index_mcp_abd": -3})
        config = self.config_from(data)
        pose = dict.fromkeys(config.joints, 0)
        pose.update(index_mcp_flex=10, index_mcp_abd=5)
        targets = config.targets(pose)
        self.assertEqual((targets[1], targets[2]), (2271, 1672))
        detail = config.describe_mapping(pose)
        self.assertEqual(detail[1]["contributions_deg"], {"index_mcp_flex": 20, "index_mcp_abd": -5})
        self.assertEqual(detail[2]["motor_deg"], -20)

    def test_degenerate_or_incomplete_pair_configuration_is_rejected(self):
        data = self.source()
        data["motors"][1]["weights"] = {"index_mcp_flex": 2, "index_mcp_abd": 2}
        with self.assertRaisesRegex(ValueError, "退化"):
            self.config_from(data)
        data = self.source()
        del data["motors"][1]["weights"]["index_mcp_abd"]
        with self.assertRaisesRegex(ValueError, "每台都须包含"):
            self.config_from(data)

    def test_motor_limit_violation_rejects_whole_frame_before_any_write(self):
        data = self.source()
        data["motors"][0]["max_tick"] = 2200
        config = self.config_from(data)
        bus = RecordingBus(config.motors)
        session = CommandSession(config, bus)
        pose = dict.fromkeys(config.joints, 0)
        pose.update(index_mcp_flex=10, index_mcp_abd=5)
        report = config.describe_mapping(pose)
        self.assertFalse(report[1]["within_limits"])
        self.assertTrue(report[2]["within_limits"])
        with self.assertRaises(ValueError):
            session.set_joint_positions(pose)
        self.assertEqual(bus.prepares, 0)
        self.assertEqual(bus.moves, [])
        self.assertEqual(bus.positions[1], 2048)
        self.assertEqual(bus.positions[2], 2048)

    def test_joint_frame_produces_one_complete_motor_update_and_diagnostics(self):
        config = HandConfig(ROOT / "full_hand.json")
        bus = RecordingBus(config.motors)
        pose = dict.fromkeys(config.joints, 0)
        pose.update(index_mcp_flex=10, index_mcp_abd=5, thumb_cmc_flex=10, thumb_cmc_roll=5)
        reply = CommandSession(config, bus).set_joint_positions(pose)
        self.assertEqual(len(bus.moves), 1)
        self.assertEqual(len(bus.moves[0]), 20)
        for a, b in ((1, 2), (5, 6)):
            self.assertEqual((reply["motor_target_deg"][a], reply["motor_target_deg"][b]), (15, 5))
            self.assertEqual((bus.moves[0][a], bus.moves[0][b]), (2219, 2105))

    def test_missing_second_joint_parameter_is_not_invented(self):
        config = HandConfig(ROOT / "index.json")
        pose = dict.fromkeys(config.joints, 0)
        del pose["index_mcp_abd"]
        with self.assertRaisesRegex(ValueError, "index_mcp_abd"):
            config.targets(pose)


if __name__ == "__main__":
    unittest.main(verbosity=2)
