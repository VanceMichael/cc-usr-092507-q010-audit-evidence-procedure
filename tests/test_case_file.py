import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from audit_evidence_procedure import PermissionDenied


class CaseFileTest(unittest.TestCase):
    """统一案件脉络与受限栏目的权限隔离。"""

    def setUp(self):
        self.service, self.clock = helpers.new_service()
        self.ps = helpers.make_principals()
        helpers.boot_case(self.service, self.ps)
        s = self.service
        s.register_delivery(
            self.ps["entity"], helpers.CASE_ID, "DEL-1", "sha256:aaa", "来源", "承诺"
        )
        s.request_account_query(
            self.ps["leader"],
            helpers.CASE_ID,
            executors=[self.ps["team1"], self.ps["team2"]],
            account_ids=["ACC-001"],
            purpose="核查资金流向",
            legal_basis="《审计法》第三十三条",
            info_categories=["开户信息"],
        )
        s.register_record(self.ps["team1"], helpers.CASE_ID, "打探干预", "某人来电")

    def test_unified_case_thread_spans_three_domains(self):
        view = self.service.case_file(self.ps["reviewer"], helpers.CASE_ID)
        self.assertEqual(
            set(view.case.domains), {"财政收支", "国有资源", "重大公共工程"}
        )
        # 四类文书均有生效版本
        self.assertEqual(
            {kind for kind, doc in view.documents.items() if doc is not None},
            {"年度计划", "审计通知", "事项范围", "审计组成员"},
        )
        self.assertEqual(len(view.deliveries), 1)
        self.assertEqual(len(view.account_queries), 1)
        self.assertEqual(len(view.restricted_records), 1)
        self.assertEqual(view.omitted_sections, ())
        self.assertGreater(view.water_level, 0)

    def test_project_member_cannot_see_restricted_sections(self):
        view = self.service.case_file(self.ps["project"], helpers.CASE_ID)
        # 一般案件脉络可见
        self.assertIsNotNone(view.deliveries)
        self.assertEqual(len(view.deliveries), 1)
        # 金融账户查询材料与干预登记不可见
        self.assertIsNone(view.account_queries)
        self.assertIsNone(view.restricted_records)
        self.assertIn("金融账户查询材料", view.omitted_sections)
        self.assertIn("干预登记等受限记录", view.omitted_sections)

    def test_project_member_cannot_list_account_queries(self):
        with self.assertRaises(PermissionDenied):
            self.service.account_queries(self.ps["project"], helpers.CASE_ID)

    def test_case_file_requires_case_read(self):
        from audit_evidence_procedure import Principal

        stranger = Principal(id="U-X", name="无角色人员", roles=frozenset())
        with self.assertRaises(PermissionDenied):
            self.service.case_file(stranger, helpers.CASE_ID)


if __name__ == "__main__":
    unittest.main()
