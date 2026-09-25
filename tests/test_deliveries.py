import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from audit_evidence_procedure import DisputeStatus, PermissionDenied


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = helpers.new_service()
        self.ps = helpers.make_principals()
        helpers.boot_case(self.service, self.ps)

    def test_register_delivery_keeps_facts(self):
        outcome = self.service.register_delivery(
            self.ps["entity"],
            helpers.CASE_ID,
            "DEL-1",
            digest="sha256:aaa",
            source="被审计单位财务系统",
            commitment="数据真实完整承诺书",
            batch_no=1,
        )
        self.assertFalse(outcome.is_retry)
        self.assertIsNone(outcome.dispute)
        delivery = outcome.delivery
        self.assertEqual(delivery.digest, "sha256:aaa")
        self.assertEqual(delivery.source, "被审计单位财务系统")
        self.assertEqual(delivery.commitment, "数据真实完整承诺书")
        self.assertEqual(delivery.arrived_at, helpers.T0)
        self.assertEqual(delivery.batch_no, 1)

    def test_same_delivery_no_retry_reuses_original_facts(self):
        s = self.service
        first = s.register_delivery(
            self.ps["entity"], helpers.CASE_ID, "DEL-1", "sha256:aaa", "来源A", "承诺A"
        )
        self.clock.advance(timedelta(hours=2))
        level_before = s.water_level(helpers.CASE_ID)
        second = s.register_delivery(
            self.ps["entity"], helpers.CASE_ID, "DEL-1", "sha256:aaa", "来源A", "承诺A"
        )
        self.assertTrue(second.is_retry)
        self.assertIsNone(second.dispute)
        # 沿用原事实：到达时间不变，不产生新事件，水位不变
        self.assertEqual(second.delivery.arrived_at, first.delivery.arrived_at)
        self.assertEqual(s.water_level(helpers.CASE_ID), level_before)
        registered = [
            e for e in s.events(helpers.CASE_ID) if e.type == "delivery_registered"
        ]
        self.assertEqual(len(registered), 1)

    def test_conflicting_retry_opens_dispute_and_keeps_original(self):
        s = self.service
        s.register_delivery(
            self.ps["entity"], helpers.CASE_ID, "DEL-1", "sha256:aaa", "来源A", "承诺A"
        )
        conflict = s.register_delivery(
            self.ps["entity"], helpers.CASE_ID, "DEL-1", "sha256:bbb", "来源A", "承诺A"
        )
        self.assertFalse(conflict.is_retry)
        self.assertIsNotNone(conflict.dispute)
        # 原事实不被覆盖
        self.assertEqual(conflict.delivery.digest, "sha256:aaa")
        dispute = conflict.dispute
        self.assertEqual(dispute.original_digest, "sha256:aaa")
        self.assertEqual(dispute.conflicting_digest, "sha256:bbb")
        self.assertEqual(dispute.status, DisputeStatus.OPEN)
        self.assertEqual(dispute.delivery_no, "DEL-1")

    def test_delivery_requires_submit_permission(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_delivery(
                self.ps["project"], helpers.CASE_ID, "DEL-9", "sha256:x", "来源", "承诺"
            )

    def test_delivery_requires_digest(self):
        from audit_evidence_procedure import ValidationError

        with self.assertRaises(ValidationError):
            self.service.register_delivery(
                self.ps["entity"], helpers.CASE_ID, "DEL-9", "  ", "来源", "承诺"
            )


if __name__ == "__main__":
    unittest.main()
