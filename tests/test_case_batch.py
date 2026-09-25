"""案件水位联合原子批处理与按权限过滤的统一脉络。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import unittest

from audit_evidence_procedure.errors import (
    InvalidDocument,
    StaleCaseWatermark,
)

from support import build_service, open_standard_case


class CaseBatchTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = build_service()
        open_standard_case(self.svc)

    def _reviewed_expansion(self):
        version_id = self.svc.documents.draft(
            "CASE-1", "scope",
            {"items": ["预算执行", "重大公共工程标段"], "temporary_expansion": True},
            "auditor", participants=["auditor"], legal_basis="审计法第二十三条",
        )
        self.svc.documents.review("CASE-1", version_id, "reviewer", True)
        return version_id

    def test_evidence_seal_and_scope_change_commit_on_one_watermark(self):
        scope_v = self._reviewed_expansion()
        before = self.svc.cases.current_watermark("CASE-1")
        result = self.svc.submit_case_batch(
            "CASE-1",
            [
                {"op": "register_delivery", "delivery_no": "D-10",
                 "content_hash": "h10", "source": "财务系统",
                 "commitment": "承诺真实", "delivered_by": "unit"},
                {"op": "seal_delivery", "delivery_no": "D-10", "sealed_by": "director"},
                {"op": "approve_scope", "version_id": scope_v, "approver_id": "director"},
            ],
            expected_watermark=before,
        )
        self.assertEqual(result["watermark_before"], before)
        self.assertEqual(result["watermark_after"], before + 3)
        # 范围决定生效；材料同批受限
        self.assertEqual(self.svc.documents.effective("CASE-1", "scope")["version"], 1)
        delivery = self.svc.evidence.get_delivery("CASE-1", "D-10", "auditor")
        self.assertTrue(
            self.svc.evidence.is_sealed("CASE-1", "delivery", delivery["id"])
        )

    def test_failure_rolls_back_restriction_and_decision_together(self):
        # 扩范围版本未经复核，批准必然失败
        scope_v = self.svc.documents.draft(
            "CASE-1", "scope",
            {"items": ["新增事项"], "temporary_expansion": True},
            "auditor", participants=["auditor"], legal_basis="审计法第二十三条",
        )
        before = self.svc.cases.current_watermark("CASE-1")
        with self.assertRaises(InvalidDocument):
            self.svc.submit_case_batch(
                "CASE-1",
                [
                    {"op": "register_delivery", "delivery_no": "D-11",
                     "content_hash": "h11", "source": "财务系统",
                     "commitment": "承诺真实", "delivered_by": "unit"},
                    {"op": "seal_delivery", "delivery_no": "D-11",
                     "sealed_by": "director"},
                    {"op": "approve_scope", "version_id": scope_v,
                     "approver_id": "director"},
                ],
            )
        # 决定未生效，材料也绝不能已受限——整批回滚
        self.assertFalse(self.svc.documents.is_effective("CASE-1", "scope"))
        self.assertEqual(
            self.svc.storage.conn.execute(
                "SELECT COUNT(*) AS c FROM deliveries WHERE delivery_no = 'D-11'"
            ).fetchone()["c"],
            0,
        )
        self.assertEqual(self.svc.cases.current_watermark("CASE-1"), before)

    def test_stale_watermark_rejects_whole_batch(self):
        scope_v = self._reviewed_expansion()
        with self.assertRaises(StaleCaseWatermark):
            self.svc.submit_case_batch(
                "CASE-1",
                [
                    {"op": "register_delivery", "delivery_no": "D-12",
                     "content_hash": "h12", "source": "s", "commitment": "c",
                     "delivered_by": "unit"},
                    {"op": "approve_scope", "version_id": scope_v,
                     "approver_id": "director"},
                ],
                expected_watermark=999,
            )
        self.assertFalse(self.svc.documents.is_effective("CASE-1", "scope"))

    def test_disputed_delivery_cannot_ride_shared_batch(self):
        self.svc.evidence.register_delivery(
            "CASE-1", "D-13", "h13", "s", "c", "unit"
        )
        scope_v = self._reviewed_expansion()
        with self.assertRaises(Exception):
            self.svc.submit_case_batch(
                "CASE-1",
                [
                    {"op": "register_delivery", "delivery_no": "D-13",
                     "content_hash": "DIFFERENT", "source": "s2", "commitment": "c2",
                     "delivered_by": "unit"},
                    {"op": "approve_scope", "version_id": scope_v,
                     "approver_id": "director"},
                ],
            )
        # 异文导致整批失败：范围未生效（异文争议须单独处理）
        self.assertFalse(self.svc.documents.is_effective("CASE-1", "scope"))


class TimelinePermissionTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = build_service()
        self.svc.register_person("auditor3", "孙审计", "auditor")
        open_standard_case(self.svc, category="public_project")

    def test_timeline_filters_by_effective_permission(self):
        from support import approve_audit_notice, query_window

        approve_audit_notice(self.svc)
        # 普通取证事件
        self.svc.evidence.register_delivery(
            "CASE-1", "D-20", "h20", "工程台账系统", "承诺真实完整", "unit"
        )
        # 受限记录事件
        self.svc.restricted.create(
            "CASE-1", "interference", {"event": "过问评标调查"}, "auditor"
        )
        # 账户查询事件：仅两名执行人获逐案授权
        request_no = self.svc.account_queries.create_request(
            "CASE-1", "某工程发起单位", ["ACC-1"], "2026-01-01", "2026-02-01",
            ["开户信息"], "auditor",
        )
        vf, vu = query_window(self.svc)
        self.svc.account_queries.sign(
            "CASE-1", request_no, "director", ["auditor", "auditor2"], vf, vu
        )

        director_view = self.svc.cases.timeline("CASE-1", "director")
        executor_view = self.svc.cases.timeline("CASE-1", "auditor")
        outsider_view = self.svc.cases.timeline("CASE-1", "auditor3")

        types_director = {e["type"] for e in director_view}
        types_executor = {e["type"] for e in executor_view}
        types_outsider = {e["type"] for e in outsider_view}

        self.assertIn("evidence.delivered", types_director)
        self.assertIn("restricted.created", types_director)
        self.assertIn("account_query.signed", types_director)

        # 执行人可看取证与本次金融查询事件
        self.assertIn("evidence.delivered", types_executor)
        self.assertIn("account_query.signed", types_executor)
        # 但打探干预事件对普通项目人员不可见，连事件类型都不暴露
        self.assertNotIn("restricted.created", types_executor)

        # 未参与本次查询的第三名审计人员：看不到金融账户查询事件
        self.assertIn("evidence.delivered", types_outsider)
        self.assertNotIn("account_query.signed", types_outsider)

        # 水位序号连续
        seqs = [e["seq"] for e in director_view]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


if __name__ == "__main__":
    unittest.main()
