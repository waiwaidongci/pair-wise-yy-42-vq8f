import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import (CONTROLLED_STATE, MOPUP_STATE, fireline_summary,
                       guard_blockers, mop_up_blockers, smoke_retest_pending,
                       effective_surface_temp)


class MopUpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        item = self.service.create_item(
            {"title": "mopup fire", "description": "guard phase", "severity": "high",
             "quantity": 4, "threshold": 10, "external_ref": "WF-MP-1"},
            "creator", "field_commander")
        self.item = self.service.transition(
            item["id"], "active", item["version"], "ic", "incident_commander")
        self.item = self.service.transition(
            self.item["id"], MOPUP_STATE, self.item["version"], "ic",
            "incident_commander")

    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()

    def _register(self, ticket, line, temp, smoke, at, actor="patrol-a"):
        return self.service.register_hotspot(self.item["id"], {
            "ticket_no": ticket, "fireline": line, "detected_at": at,
            "surface_temp": temp, "smoke_status": smoke}, actor, "field_commander")

    def test_replay_same_ticket_returns_first(self):
        first = self._register("T-1", "L1", 95.0, "smoking", "2026-09-26T01:00:00+00:00")
        replay = self.service.register_hotspot(self.item["id"], {
            "ticket_no": "T-1", "fireline": "L1", "detected_at": "2026-09-26T02:00:00+00:00",
            "surface_temp": 120.0, "smoke_status": "clear"}, "patrol-b", "field_commander")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(replay["surface_temp"], 95.0)  # 首条记录不被覆盖
        self.assertEqual(len(self.service.list_hotspots(self.item["id"], "viewer")["hotspots"]), 1)

    def test_review_must_be_another_officer_and_keeps_original(self):
        h = self._register("T-2", "L1", 110.0, "clear", "2026-09-26T01:00:00+00:00")
        self.service.record_cooling(self.item["id"], "T-2", {
            "observed_at": "2026-09-26T03:00:00+00:00", "surface_temp": 60.0},
            "patrol-b", "field_commander")
        with self.assertRaises(ConflictError):  # 登记人不能复核
            self.service.review_cooling(self.item["id"], "T-2", {
                "review_at": "2026-09-26T04:00:00+00:00", "confirmed_temp": 60.0},
                "patrol-a", "field_commander")
        with self.assertRaises(ConflictError):  # 降温观测人也不能复核
            self.service.review_cooling(self.item["id"], "T-2", {
                "review_at": "2026-09-26T04:00:00+00:00", "confirmed_temp": 60.0},
                "patrol-b", "field_commander")
        reviewed = self.service.review_cooling(self.item["id"], "T-2", {
            "review_at": "2026-09-26T04:00:00+00:00", "confirmed_temp": 60.0},
            "patrol-c", "field_commander")
        self.assertEqual(reviewed["surface_temp"], 110.0)  # 原登记保留
        self.assertEqual(reviewed["effective_temp"], 60.0)  # 以复核降温值为准
        with self.assertRaises(ConflictError):  # 降温观测不可覆盖
            self.service.record_cooling(self.item["id"], "T-2", {
                "observed_at": "2026-09-26T05:00:00+00:00", "surface_temp": 40.0},
                "patrol-b", "field_commander")

    def test_blockers_keep_fire_in_original_state(self):
        self._register("T-3", "L1", 95.0, "smoking", "2026-09-26T01:00:00+00:00")
        current = self.service.get_item(self.item["id"], "viewer")
        with self.assertRaises(ConflictError):  # 超温 + 烟点未复测
            self.service.transition(current["id"], CONTROLLED_STATE,
                                    current["version"], "ic", "incident_commander")
        self.service.record_cooling(self.item["id"], "T-3", {
            "observed_at": "2026-09-26T03:00:00+00:00", "surface_temp": 70.0},
            "patrol-b", "field_commander")
        current = self.service.get_item(self.item["id"], "viewer")
        with self.assertRaises(ConflictError):  # 降温未复核 + 烟点未复测
            self.service.transition(current["id"], CONTROLLED_STATE,
                                    current["version"], "ic", "incident_commander")
        self.service.review_cooling(self.item["id"], "T-3", {
            "review_at": "2026-09-26T04:00:00+00:00", "confirmed_temp": 70.0},
            "patrol-c", "field_commander")
        current = self.service.get_item(self.item["id"], "viewer")
        with self.assertRaises(ConflictError):  # 仅剩烟点未复测
            self.service.transition(current["id"], CONTROLLED_STATE,
                                    current["version"], "ic", "incident_commander")
        self.service.retest_smoke(self.item["id"], "T-3", {
            "retest_at": "2026-09-26T05:00:00+00:00", "smoke_status": "clear"},
            "patrol-a", "field_commander")
        current = self.service.get_item(self.item["id"], "viewer")
        done = self.service.transition(current["id"], CONTROLLED_STATE,
                                       current["version"], "ic", "incident_commander")
        self.assertEqual(done["status"], CONTROLLED_STATE)

    def test_correction_recalculates_fireline_totals(self):
        self._register("T-4", "L1", 90.0, "clear", "2026-09-26T01:00:00+00:00")
        self._register("T-5", "L2", 85.0, "smoking", "2026-09-26T01:30:00+00:00", "patrol-b")
        summary = {f["fireline"]: f for f in
                   self.service.get_item(self.item["id"], "viewer")["firelines"]}
        self.assertEqual(summary["L1"]["max_temp"], 90.0)
        self.assertEqual(summary["L2"]["pending_retest"], 1)
        self.service.correct_hotspot(self.item["id"], "T-5", {
            "surface_temp": 50.0, "smoke_status": "clear", "fireline": "L1"},
            "patrol-a", "field_commander")
        summary = {f["fireline"]: f for f in
                   self.service.get_item(self.item["id"], "viewer")["firelines"]}
        self.assertEqual(summary["L1"]["max_temp"], 90.0)  # 归入L1但仍低于原90
        self.assertEqual(summary["L1"]["pending_retest"], 0)
        self.assertNotIn("L2", summary)
        corrected = self.repo.get_hotspot_by_ticket(self.item["id"], "T-5")
        self.assertEqual(corrected["fireline"], "L1")

    def test_validation_and_role_guards(self):
        with self.assertRaises(ValidationError):
            self.service.register_hotspot(self.item["id"], {
                "ticket_no": "T-9", "fireline": "L1",
                "detected_at": "2026-09-26T01:00:00", "surface_temp": 90,
                "smoke_status": "smoking"}, "p", "field_commander")  # 无时区
        with self.assertRaises(ValidationError):
            self._register("T-9", "L1", 90, "burning", "2026-09-26T01:00:00+00:00")
        with self.assertRaises(PermissionDenied):
            self.service.register_hotspot(self.item["id"], {
                "ticket_no": "T-9", "fireline": "L1",
                "detected_at": "2026-09-26T01:00:00+00:00", "surface_temp": 90,
                "smoke_status": "clear"}, "log", "logistics")
        self.assertTrue(self.repo.verify_audit_chain())


class RuleUnitTest(unittest.TestCase):
    def test_pure_rules(self):
        hs = [{"ticket_no": "a", "fireline": "L1", "surface_temp": 120,
               "smoke_status": "clear", "cooling_temp": None, "review_by": None,
               "smoke_retest_status": None},
              {"ticket_no": "b", "fireline": "L1", "surface_temp": 110,
               "smoke_status": "smoking", "cooling_temp": 70, "review_by": "p2",
               "smoke_retest_status": None}]
        self.assertTrue(smoke_retest_pending(hs[1]))
        self.assertEqual(effective_surface_temp(hs[1]), 70)
        blockers = mop_up_blockers(hs)
        self.assertEqual(blockers[0], "火线L1仍有超过80℃的热点")
        self.assertTrue(any("现场单号b" in b for b in blockers))
        summary = {f["fireline"]: f for f in fireline_summary(hs)}
        self.assertEqual(summary["L1"]["max_temp"], 120.0)
        self.assertEqual(summary["L1"]["pending_retest"], 1)
        self.assertEqual(len(guard_blockers("controlled", [])), 0)
        self.assertEqual(len(guard_blockers("closed", hs)), 0)


if __name__ == "__main__":
    unittest.main()
