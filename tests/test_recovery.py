import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from audit_evidence_procedure import (
    AuditService,
    DeadlineQueue,
    DeadlineStatus,
)


class RecoveryTest(unittest.TestCase):
    """停机恢复后，意见期限、协助催办和复核工作继续执行。"""

    def test_deadline_work_continues_after_downtime(self):
        service, clock = helpers.new_service()
        ps = helpers.make_principals()
        helpers.boot_case(service, ps)
        team1, reviewer = ps["team1"], ps["reviewer"]
        # 三类期限来源：拒绝配合 → 意见期限；跨机关协助 → 协助催办；范围扩大 → 复核工作
        service.register_record(team1, helpers.CASE_ID, "拒绝配合", "拒绝提供账簿")
        service.register_record(team1, helpers.CASE_ID, "跨机关协助", "请税务协助")
        expansion = service.request_scope_expansion(
            team1,
            helpers.CASE_ID,
            [{"domain": "国有资源", "description": "土地储备"}],
            "《审计法》第三十三条",
        )
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "events.json"
            service.store.dump(snapshot)
            level_before = service.water_level(helpers.CASE_ID)
            events_before = len(service.events())
            # 停机 11 天：三类期限全部逾期
            clock.advance(timedelta(days=11))
            restored = AuditService.recover(snapshot, clock=clock)
            # 恢复后案件事实完整：水位与事件数量与停机前一致
            self.assertEqual(restored.water_level(helpers.CASE_ID), level_before)
            self.assertEqual(len(restored.events()), events_before)
            overdue = restored.overdue_items(reviewer)
            queues = {item.queue for item in overdue}
            self.assertIn(DeadlineQueue.OPINION, queues)
            self.assertIn(DeadlineQueue.ASSISTANCE, queues)
            self.assertIn(DeadlineQueue.REVIEW, queues)
            # 协助催办继续执行
            assist_item = next(
                item for item in overdue if item.queue == DeadlineQueue.ASSISTANCE
            )
            restored.send_assistance_reminder(team1, assist_item.item_id, "请尽快反馈")
            self.assertEqual(
                restored.state.deadline_items[assist_item.item_id].reminders, 1
            )
            # 复核工作继续执行
            restored.review_scope_expansion(
                reviewer, helpers.CASE_ID, expansion.request_id, approve=True
            )
            self.assertEqual(
                restored.effective_document(helpers.CASE_ID, "事项范围").version_no, 2
            )
            review_items = [
                item
                for item in restored.state.deadline_items.values()
                if item.ref_id == expansion.request_id
            ]
            self.assertTrue(
                all(item.status == DeadlineStatus.DONE for item in review_items)
            )
            # 意见期限继续执行
            opinion_item = next(
                item
                for item in restored.state.deadline_items.values()
                if item.queue == DeadlineQueue.OPINION
            )
            restored.complete_deadline_item(
                team1, opinion_item.item_id, "已收到被审计单位意见"
            )
            self.assertEqual(
                restored.state.deadline_items[opinion_item.item_id].status,
                DeadlineStatus.DONE,
            )

    def test_recovered_state_matches_original(self):
        service, clock = helpers.new_service()
        ps = helpers.make_principals()
        helpers.boot_case(service, ps)
        service.register_delivery(
            ps["entity"], helpers.CASE_ID, "DEL-1", "sha256:aaa", "来源", "承诺"
        )
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "events.json"
            service.store.dump(snapshot)
            restored = AuditService.recover(snapshot, clock=clock)
            delivery = restored.state.deliveries[(helpers.CASE_ID, "DEL-1")]
            self.assertEqual(delivery.digest, "sha256:aaa")
            self.assertEqual(delivery.arrived_at, helpers.T0)


if __name__ == "__main__":
    unittest.main()
