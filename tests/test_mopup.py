import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class MopupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "mopup fire", "description": "watch and rekindle",
             "severity": "high", "quantity": 5, "threshold": 10,
             "external_ref": "MOPUP-1"}, "creator", "field_commander")
        self.fid = self.item["id"]
        self.commander = TRANSITION_ROLES["controlled"][0]

    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()

    def _to_contained(self):
        current = self.service.get_item(self.fid, "viewer")
        for target in ("active", "contained"):
            current = self.service.transition(
                current["id"], target, current["version"], "ic", self.commander)
        return current

    def _register(self, ticket, line, temp, smoke="smoke", at="2026-09-26T08:00:00+00:00"):
        return self.service.register_hotspot(self.fid, {
            "ticket_no": ticket, "line_id": line, "detected_at": at,
            "surface_temperature": temp, "smoke_state": smoke},
            "patrol-a", "field_commander")

    def test_replay_returns_first_registration(self):
        first = self._register("T-1", "L1", 120)
        self.assertFalse(first["replay"])
        replay = self.service.register_hotspot(self.fid, {
            "ticket_no": "T-1", "line_id": "L1",
            "detected_at": "2026-09-26T09:00:00+00:00",
            "surface_temperature": 95, "smoke_state": "clear"},
            "patrol-a", "field_commander")
        self.assertTrue(replay["replay"])
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(replay["surface_temperature"], 120)
        self.assertEqual(len(self.service.list_hotspots(self.fid, "viewer")), 1)

    def test_cooling_requires_other_patrol_and_review(self):
        self._register("T-2", "L1", 120)
        payload = {"detected_at": "2026-09-26T10:00:00+00:00",
                   "temperature": 45, "smoke_state": "clear"}
        with self.assertRaises(ConflictError):
            self.service.add_cooling_observation(
                self.fid, "T-2", payload, "patrol-a", "field_commander")
        spot = self.service.add_cooling_observation(
            self.fid, "T-2", payload, "patrol-b", "field_commander")
        self.assertIsNone(spot["reviewed_by"])
        # 原登记记录保留：温度仍为120，且观测人不能自复核
        self.assertEqual(spot["surface_temperature"], 120)
        with self.assertRaises(ConflictError):
            self.service.review_hotspot(self.fid, "T-2", "patrol-b", "field_commander")
        reviewed = self.service.review_hotspot(
            self.fid, "T-2", "patrol-c", "field_commander")
        self.assertEqual(reviewed["reviewed_by"], "patrol-c")

    def test_cooling_must_be_later_and_cooler(self):
        self._register("T-3", "L1", 120)
        with self.assertRaises(ValidationError):
            self.service.add_cooling_observation(self.fid, "T-3", {
                "detected_at": "2026-09-26T07:00:00+00:00",
                "temperature": 40, "smoke_state": "clear"},
                "patrol-b", "field_commander")
        with self.assertRaises(ValidationError):
            self.service.add_cooling_observation(self.fid, "T-3", {
                "detected_at": "2026-09-26T10:00:00+00:00",
                "temperature": 130, "smoke_state": "clear"},
                "patrol-b", "field_commander")

    def test_rekindle_gate_blocks_controlled(self):
        current = self._to_contained()
        self._register("T-4", "L1", 120, "smoke")
        # 超温热点 + 烟点未复测
        with self.assertRaises(ConflictError):
            self.service.transition(self.fid, "controlled",
                                    current["version"], "ic", self.commander)
        status = self.service.line_status(self.fid, "viewer")
        self.assertFalse(status["can_declare_controlled"])
        self.assertEqual(status["lines"]["L1"]["max_temperature"], 120)
        self.assertEqual(status["lines"]["L1"]["pending_retest"], ["T-4"])
        # 降温但未复核：未复核结果不计入最高温判定，且未复核本身阻断
        self.service.add_cooling_observation(self.fid, "T-4", {
            "detected_at": "2026-09-26T10:00:00+00:00",
            "temperature": 50, "smoke_state": "clear"},
            "patrol-b", "field_commander")
        status = self.service.line_status(self.fid, "viewer")
        self.assertEqual(status["lines"]["L1"]["max_temperature"], 120)
        self.assertFalse(status["can_declare_controlled"])
        # 另一名巡线员复核后解除阻断
        self.service.review_hotspot(self.fid, "T-4", "patrol-c", "field_commander")
        status = self.service.line_status(self.fid, "viewer")
        self.assertTrue(status["can_declare_controlled"])
        self.assertEqual(status["lines"]["L1"]["max_temperature"], 50)
        self.assertEqual(status["lines"]["L1"]["pending_retest"], [])
        current = self.service.get_item(self.fid, "viewer")
        moved = self.service.transition(self.fid, "controlled",
                                        current["version"], "ic", self.commander)
        self.assertEqual(moved["status"], "controlled")

    def test_smoke_retest_pending_even_when_cool(self):
        self._to_contained()
        self._register("T-5", "L2", 60, "smoke")
        status = self.service.line_status(self.fid, "viewer")
        self.assertFalse(status["can_declare_controlled"])
        self.assertEqual(status["lines"]["L2"]["max_temperature"], 60)
        self.assertEqual(status["lines"]["L2"]["pending_retest"], ["T-5"])
        self.service.add_cooling_observation(self.fid, "T-5", {
            "detected_at": "2026-09-26T10:00:00+00:00",
            "temperature": 40, "smoke_state": "smoke"},
            "patrol-b", "field_commander")
        self.service.review_hotspot(self.fid, "T-5", "patrol-c", "field_commander")
        # 温度已降但仍见烟：继续待复测
        status = self.service.line_status(self.fid, "viewer")
        self.assertEqual(status["lines"]["L2"]["pending_retest"], ["T-5"])
        self.assertFalse(status["can_declare_controlled"])

    def test_correction_recomputes_line_summary(self):
        self._to_contained()
        self._register("T-6", "L1", 120, "smoke")
        self.service.add_cooling_observation(self.fid, "T-6", {
            "detected_at": "2026-09-26T10:00:00+00:00",
            "temperature": 45, "smoke_state": "clear"},
            "patrol-b", "field_commander")
        self.service.review_hotspot(self.fid, "T-6", "patrol-c", "field_commander")
        self.assertTrue(self.service.line_status(self.fid, "viewer")["can_declare_controlled"])
        # 误报更正为超温且见烟，相关火线按新值重算；旧降温复核作废须重新复测
        self.service.correct_hotspot(self.fid, "T-6", {
            "surface_temperature": 95, "smoke_state": "smoke"},
            "ic", "incident_commander")
        status = self.service.line_status(self.fid, "viewer")
        self.assertEqual(status["lines"]["L1"]["max_temperature"], 95)
        self.assertEqual(status["lines"]["L1"]["pending_retest"], ["T-6"])
        self.assertFalse(status["can_declare_controlled"])
        spot = self.service.list_hotspots(self.fid, "viewer")[0]
        self.assertIsNone(spot["reviewed_by"])
        self.assertIsNone(spot["cooling_observed_by"])
        # 重新降温观测、复核（烟点复测为无烟）后解除阻断
        self.service.add_cooling_observation(self.fid, "T-6", {
            "detected_at": "2026-09-26T12:00:00+00:00",
            "temperature": 50, "smoke_state": "clear"},
            "patrol-b", "field_commander")
        self.service.review_hotspot(self.fid, "T-6", "patrol-c", "field_commander")
        self.assertTrue(self.service.line_status(self.fid, "viewer")["can_declare_controlled"])


if __name__ == "__main__":
    unittest.main()
