import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from audit_evidence_procedure import PermissionDenied


class ExplainTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = helpers.new_service()
        self.ps = helpers.make_principals()
        helpers.boot_case(self.service, self.ps)
        self.service.register_delivery(
            self.ps["entity"],
            helpers.CASE_ID,
            "DEL-1",
            "sha256:aaa",
            "被审计单位财务系统",
            "数据真实完整承诺书",
        )

    def test_explain_shows_acquisition_usage_and_validity(self):
        s, ps = self.service, self.ps
        # 复核人先读取一次，留下使用记录
        s.get_delivery(ps["reviewer"], helpers.CASE_ID, "DEL-1")
        explanation = s.explain_evidence(ps["leader"], helpers.CASE_ID, "DEL-1")
        # 证据如何取得
        acquisition = explanation.acquisition
        self.assertEqual(acquisition["digest"], "sha256:aaa")
        self.assertEqual(acquisition["source"], "被审计单位财务系统")
        self.assertEqual(acquisition["commitment"], "数据真实完整承诺书")
        self.assertIn("arrived_at", acquisition)
        self.assertGreaterEqual(acquisition["water_level"], 1)
        self.assertFalse(acquisition["sealed"])
        # 谁曾在何种权限下使用
        usage = {(u.principal_id, u.permission) for u in explanation.usage}
        self.assertIn((ps["reviewer"].id, "evidence:read"), usage)
        self.assertIn((ps["leader"].id, "explain:authorized"), usage)
        # 程序当前是否有效
        self.assertTrue(explanation.procedure["valid"])
        self.assertEqual(explanation.procedure["open_disputes"], 0)
        self.assertTrue(explanation.procedure["notice_within_validity"])
        self.assertEqual(
            explanation.procedure["effective_versions"],
            {"年度计划": 1, "审计通知": 1, "事项范围": 1, "审计组成员": 1},
        )

    def test_open_dispute_marks_procedure_invalid(self):
        s, ps = self.service, self.ps
        # 异文重试 → 证据争议
        s.register_delivery(
            ps["entity"], helpers.CASE_ID, "DEL-1", "sha256:bbb", "来源", "承诺"
        )
        explanation = s.explain_evidence(ps["leader"], helpers.CASE_ID, "DEL-1")
        self.assertFalse(explanation.procedure["valid"])
        self.assertEqual(explanation.procedure["open_disputes"], 1)
        self.assertTrue(any("争议" in reason for reason in explanation.procedure["reasons"]))

    def test_explain_requires_authorization(self):
        with self.assertRaises(PermissionDenied):
            self.service.explain_evidence(
                self.ps["team1"], helpers.CASE_ID, "DEL-1"
            )
        with self.assertRaises(PermissionDenied):
            self.service.explain_evidence(
                self.ps["project"], helpers.CASE_ID, "DEL-1"
            )


if __name__ == "__main__":
    unittest.main()
