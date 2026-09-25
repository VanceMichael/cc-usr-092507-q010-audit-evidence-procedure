"""电子资料交付：摘要/来源/承诺/到达时间、同号幂等、异文争议、封存。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import unittest
from datetime import timedelta

from audit_evidence_procedure.errors import (
    DuplicateConflict,
    MaterialAlreadyRestricted,
    PermissionDenied,
)

from support import build_service, open_standard_case


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = build_service()
        open_standard_case(self.svc)

    def test_delivery_records_four_facts(self):
        result = self.svc.evidence.register_delivery(
            "CASE-1", "D-001", "hash-aaa", "被审计单位财务系统导出",
            "承诺资料真实完整", "unit",
        )
        self.assertEqual(result["status"], "first")
        self.assertTrue(result["arrived_at"])
        stored = self.svc.evidence.get_delivery("CASE-1", "D-001", "auditor")
        self.assertEqual(stored["content_hash"], "hash-aaa")
        self.assertEqual(stored["source"], "被审计单位财务系统导出")
        self.assertEqual(stored["commitment"], "承诺资料真实完整")
        self.assertEqual(stored["retry_count"], 0)

    def test_retry_same_hash_replays_original_facts(self):
        first = self.svc.evidence.register_delivery(
            "CASE-1", "D-002", "hash-bbb", "系统A", "承诺一", "unit",
        )
        self.clock.advance(timedelta(days=2))
        retry = self.svc.evidence.register_delivery(
            "CASE-1", "D-002", "hash-bbb", "系统A（重试）", "承诺一", "unit",
        )
        self.assertEqual(retry["status"], "replayed_same_fact")
        # 沿用原事实：原到达时间、来源、承诺均不被重试覆盖
        self.assertEqual(retry["arrived_at"], first["arrived_at"])
        self.assertEqual(retry["source"], "系统A")
        stored = self.svc.evidence.get_delivery("CASE-1", "D-002", "auditor")
        self.assertEqual(stored["retry_count"], 1)
        self.assertEqual(stored["source"], "系统A")
        retries = self.svc.evidence.list_retries("CASE-1", "D-002")
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]["same_fact"], 1)

    def test_variant_hash_becomes_dispute_and_original_is_kept(self):
        self.svc.evidence.register_delivery(
            "CASE-1", "D-003", "hash-original", "系统A", "承诺一", "unit",
        )
        with self.assertRaises(DuplicateConflict):
            self.svc.evidence.register_delivery(
                "CASE-1", "D-003", "hash-VARIANT", "系统A（修订版）", "承诺二",
                "unit", note="对方称此前导出版本有误",
            )
        # 原交付事实不变，状态转为 disputed
        stored = self.svc.evidence.get_delivery("CASE-1", "D-003", "auditor")
        self.assertEqual(stored["content_hash"], "hash-original")
        self.assertEqual(stored["status"], "disputed")
        disputes = self.svc.evidence.open_disputes("CASE-1")
        self.assertEqual(len(disputes), 1)
        self.assertEqual(disputes[0]["variant_hash"], "hash-VARIANT")
        # 争议可被处理关闭
        self.svc.evidence.resolve_dispute(
            "CASE-1", disputes[0]["id"], "director", "以现场核对版为准"
        )
        self.assertEqual(self.svc.evidence.open_disputes("CASE-1"), [])

    def test_unit_cannot_read_or_seal(self):
        self.svc.evidence.register_delivery(
            "CASE-1", "D-004", "hash-c", "src", "承诺", "unit",
        )
        with self.assertRaises(PermissionDenied):
            self.svc.evidence.get_delivery("CASE-1", "D-004", "unit")
        with self.assertRaises(PermissionDenied):
            self.svc.evidence.seal_delivery("CASE-1", "D-004", "unit")

    def test_seal_is_idempotently_rejected_and_persisted(self):
        self.svc.evidence.register_delivery(
            "CASE-1", "D-005", "hash-d", "src", "承诺", "unit",
        )
        seal_id = self.svc.evidence.seal_delivery("CASE-1", "D-005", "director")
        self.assertTrue(seal_id)
        with self.assertRaises(MaterialAlreadyRestricted):
            self.svc.evidence.seal_delivery("CASE-1", "D-005", "director")
        delivery_id = self.svc.evidence.get_delivery("CASE-1", "D-005", "auditor")["id"]
        self.assertTrue(self.svc.evidence.is_sealed("CASE-1", "delivery", delivery_id))


if __name__ == "__main__":
    unittest.main()
