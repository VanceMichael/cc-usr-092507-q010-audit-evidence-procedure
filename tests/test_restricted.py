"""三类受限记录：独立队列、版本更正、协助催办队列。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import unittest
from datetime import timedelta

from audit_evidence_procedure.errors import PermissionDenied

from support import build_service, open_standard_case


class RestrictedRecordTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = build_service()
        open_standard_case(self.svc)

    def test_three_kinds_have_separate_queues_and_access(self):
        iid = self.svc.restricted.create(
            "CASE-1", "interference", {"event": "有人打电话过问案情"}, "auditor",
        )
        rid = self.svc.restricted.create(
            "CASE-1", "refusal", {"event": "被审计单位拒绝提供台账"}, "auditor",
        )
        aid = self.svc.restricted.create(
            "CASE-1", "assistance", {"event": "请金融机构协助冻结资料"}, "auditor",
        )
        # 审计人员可看拒绝配合、协助队列，但打探干预队列仅机关负责人可阅
        self.assertEqual(len(self.svc.restricted.queue("CASE-1", "refusal", "auditor")), 1)
        self.assertEqual(len(self.svc.restricted.queue("CASE-1", "assistance", "auditor")), 1)
        with self.assertRaises(PermissionDenied):
            self.svc.restricted.queue("CASE-1", "interference", "auditor")
        interference = self.svc.restricted.queue("CASE-1", "interference", "director")
        self.assertEqual([r["id"] for r in interference], [iid])
        # 三类队列互不相混：拒绝队列中没有打探/协助记录
        refusal = self.svc.restricted.queue("CASE-1", "refusal", "auditor")
        self.assertEqual([r["id"] for r in refusal], [rid])
        self.assertNotIn(aid, [r["id"] for r in refusal])

    def test_correction_adds_version_never_deletes_original(self):
        record_id = self.svc.restricted.create(
            "CASE-1", "refusal",
            {"event": "拒绝提供台账", "date": "2026-01-11"}, "auditor",
        )
        new_version = self.svc.restricted.correct(
            "CASE-1", record_id,
            {"event": "拒绝提供台账及合同", "date": "2026-01-11"},
            "auditor", reason="补充合同一项，原事实保留",
        )
        self.assertEqual(new_version, 2)
        detail = self.svc.restricted.get("CASE-1", record_id, "auditor")
        self.assertEqual(detail["current_version"], 2)
        self.assertEqual(len(detail["versions"]), 2)
        v1, v2 = detail["versions"]
        self.assertIsNone(v1["correction_reason"])
        self.assertEqual(v2["correction_reason"], "补充合同一项，原事实保留")
        # 原版本内容原样保留
        self.assertEqual(v1["content"]["event"], "拒绝提供台账")

    def test_correction_requires_reason(self):
        record_id = self.svc.restricted.create(
            "CASE-1", "refusal", {"event": "x"}, "auditor"
        )
        with self.assertRaises(ValueError):
            self.svc.restricted.correct(
                "CASE-1", record_id, {"event": "y"}, "auditor", reason="   "
            )

    def test_assistance_gets_urge_deadline_and_recurring_urging(self):
        record_id = self.svc.restricted.create(
            "CASE-1", "assistance", {"event": "协助查询"}, "auditor", urge_days=3,
        )
        pending = self.svc.deadlines.pending("CASE-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "assistance_urge")

        # 到期前不动作
        self.clock.advance(timedelta(days=2))
        self.assertEqual(self.svc.deadlines.pump(), [])

        # 到期：首次催办，并排入下一周期
        self.clock.advance(timedelta(days=2))
        actions = self.svc.deadlines.pump()
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["urge_count"], 1)
        detail = self.svc.restricted.get("CASE-1", record_id, "auditor")
        self.assertEqual(detail["status"], "urged")

        # 再一个催办周期：第二次催办
        self.clock.advance(timedelta(days=7))
        actions = self.svc.deadlines.pump()
        self.assertEqual(actions[0]["urge_count"], 2)

        # 关闭记录后，后续催办期限被取消，不再产生动作
        self.svc.restricted.close("CASE-1", record_id, "auditor")
        self.clock.advance(timedelta(days=10))
        self.assertEqual(self.svc.deadlines.pump(), [])

    def test_unit_cannot_create_restricted_records(self):
        with self.assertRaises(PermissionDenied):
            self.svc.restricted.create(
                "CASE-1", "refusal", {"event": "x"}, "unit"
            )


if __name__ == "__main__":
    unittest.main()
