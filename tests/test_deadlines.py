"""程序期限队列与停机恢复。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from audit_evidence_procedure import MutableClock
from audit_evidence_procedure.service import AuditEvidenceProcedureService

from support import PERSONS, approve_audit_notice, open_standard_case


class DeadlineRecoveryTest(unittest.TestCase):
    def _fresh(self, database=":memory:"):
        clock = MutableClock()
        svc = AuditEvidenceProcedureService(database, clock=clock)
        for key, (name, role) in PERSONS.items():
            svc.register_person(key, name, role)
        open_standard_case(svc)
        return svc, clock

    def test_comment_period_fires_after_downtime(self):
        svc, clock = self._fresh()
        svc.open_comment_period("CASE-1", days=10)
        # 模拟停机：不调用任何处理，只拨时钟
        clock.advance(timedelta(days=11))
        actions = svc.recover()["actions"]
        self.assertEqual([a["kind"] for a in actions], ["comment"])
        # 幂等：再次恢复不重复执行
        self.assertEqual(svc.recover()["actions"], [])

    def test_all_three_deadline_kinds_resume_from_persistent_store(self):
        database_dir = tempfile.TemporaryDirectory()
        db_path = str(Path(database_dir.name) / "audit.db")
        svc, clock = self._fresh(db_path)
        approve_audit_notice(svc)
        svc.open_comment_period("CASE-1", days=2)
        svc.restricted.create(
            "CASE-1", "assistance", {"event": "协助事项"}, "auditor", urge_days=2,
        )
        # 扩范围复核期限
        scope_v = svc.documents.draft(
            "CASE-1", "scope",
            {"items": ["预算执行", "国有资源处置"], "temporary_expansion": True},
            "auditor", participants=["auditor"], legal_basis="审计法第二十三条",
        )
        svc.documents.review("CASE-1", scope_v, "reviewer", True)
        svc.close()

        # “换机重启”：新建服务实例指向同一数据库，时钟也已跨过期限
        clock2 = MutableClock(clock.now() + timedelta(days=10))
        svc2 = AuditEvidenceProcedureService(db_path, clock=clock2)
        result = svc2.recover()
        kinds = {a["kind"] for a in result["actions"]}
        self.assertEqual(kinds, {"comment", "assistance_urge", "review"})
        # 协助事项未关闭，恢复后仍存在下一轮催办期限
        pending_kinds = {d["kind"] for d in svc2.deadlines.pending("CASE-1")}
        self.assertIn("assistance_urge", pending_kinds)
        svc2.close()
        database_dir.cleanup()

    def test_review_work_deadline_completes_when_scope_approved(self):
        svc, clock = self._fresh()
        scope_v = svc.documents.draft(
            "CASE-1", "scope",
            {"items": ["重大公共工程追加"], "temporary_expansion": True},
            "auditor", participants=["auditor"], legal_basis="审计法第二十三条",
        )
        svc.documents.review("CASE-1", scope_v, "reviewer", True)
        pending = svc.deadlines.pending("CASE-1")
        self.assertEqual([d["kind"] for d in pending], ["review"])
        svc.documents.approve("CASE-1", scope_v, "director")
        # 批准即完成复核期限，不再到期触发
        self.assertEqual(svc.deadlines.pending("CASE-1"), [])
        clock.advance(timedelta(days=30))
        self.assertEqual(svc.deadlines.pump(), [])


if __name__ == "__main__":
    unittest.main()
