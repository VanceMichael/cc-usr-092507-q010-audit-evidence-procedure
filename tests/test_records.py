import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from audit_evidence_procedure import (
    DeadlineQueue,
    PermissionDenied,
    RecordCategory,
)


class RestrictedRecordTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = helpers.new_service()
        self.ps = helpers.make_principals()
        helpers.boot_case(self.service, self.ps)

    def test_categories_enter_distinct_queues(self):
        s, ps = self.service, self.ps
        s.register_record(ps["team1"], helpers.CASE_ID, "打探干预", "某人来电询问案情")
        s.register_record(ps["team1"], helpers.CASE_ID, "拒绝配合", "拒绝提供账簿")
        s.register_record(ps["team1"], helpers.CASE_ID, "跨机关协助", "请税务协助调取申报数据")
        opinion = s.deadline_queue(ps["reviewer"], "意见期限")
        assistance = s.deadline_queue(ps["reviewer"], "协助催办")
        review = s.deadline_queue(ps["reviewer"], "复核工作")
        self.assertEqual(len(opinion), 1)
        self.assertEqual(len(assistance), 1)
        self.assertEqual(len(review), 1)
        # 不同队列对应不同法定期限
        self.assertEqual(opinion[0].due_at, helpers.T0 + timedelta(days=10))
        self.assertEqual(assistance[0].due_at, helpers.T0 + timedelta(days=7))
        self.assertEqual(review[0].due_at, helpers.T0 + timedelta(days=3))
        # 记录与队列正确关联
        self.assertEqual(opinion[0].queue, DeadlineQueue.OPINION)
        record = s.state.records[opinion[0].ref_id]
        self.assertEqual(record.category, RecordCategory.REFUSAL)

    def test_correction_appends_version_without_deleting(self):
        s, ps = self.service, self.ps
        record = s.register_record(
            ps["team1"], helpers.CASE_ID, "打探干预", "初次记录内容"
        )
        s.correct_record(ps["team1"], record.record_id, "更正后的内容", reason="补充细节")
        history = s.record_history(ps["reviewer"], record.record_id)
        self.assertEqual(len(history), 2)
        # 原记录保留，更正只是新增版本
        self.assertEqual(history[0].version_no, 1)
        self.assertEqual(history[0].content, "初次记录内容")
        self.assertEqual(history[1].version_no, 2)
        self.assertEqual(history[1].content, "更正后的内容")
        self.assertEqual(history[1].reason, "补充细节")

    def test_project_member_cannot_read_restricted_records(self):
        record = self.service.register_record(
            self.ps["team1"], helpers.CASE_ID, "打探干预", "内容"
        )
        with self.assertRaises(PermissionDenied):
            self.service.record_history(self.ps["project"], record.record_id)

    def test_register_requires_write_permission(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_record(
                self.ps["reviewer"], helpers.CASE_ID, "拒绝配合", "内容"
            )


if __name__ == "__main__":
    unittest.main()
