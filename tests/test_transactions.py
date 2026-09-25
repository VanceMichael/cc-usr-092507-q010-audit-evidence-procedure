import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from audit_evidence_procedure import (
    NotFoundError,
    Principal,
    Role,
    op_apply_scope_version,
    op_register_evidence,
    op_seal_materials,
)


class CaseTransactionTest(unittest.TestCase):
    """取证、封存和范围变更同时发生时，基于同一案件水位原子提交。"""

    def setUp(self):
        self.service, self.clock = helpers.new_service()
        self.ps = helpers.make_principals()
        helpers.boot_case(self.service, self.ps)
        # 待批准的事项范围版本 2
        self.service.propose_document(
            self.ps["team1"],
            helpers.CASE_ID,
            "事项范围",
            {"items": [{"domain": "国有资源", "description": "土地出让"}]},
        )
        self.operator = Principal(
            id="U-OP",
            name="取证复核人员",
            roles=frozenset({Role.AUDIT_TEAM, Role.REVIEWER}),
        )

    def test_atomic_commit_shares_water_level(self):
        s = self.service
        before = s.water_level(helpers.CASE_ID)
        result = s.commit(
            self.operator,
            helpers.CASE_ID,
            [
                op_register_evidence("DEL-1", "sha256:aaa", "财务系统", "承诺书"),
                op_seal_materials(["DEL-1"], "防止篡改"),
                op_apply_scope_version(2),
            ],
        )
        self.assertEqual(result.water_level, before + 1)
        self.assertTrue(result.events)
        # 同一事务：全部事件共享 tx_id 与案件水位
        self.assertTrue(all(e.tx_id == result.tx_id for e in result.events))
        self.assertTrue(
            all(e.water_level == result.water_level for e in result.events)
        )
        # 三类事实同时生效
        self.assertIn((helpers.CASE_ID, "DEL-1"), s.state.deliveries)
        self.assertEqual(len(s.state.seals), 1)
        self.assertEqual(
            s.effective_document(helpers.CASE_ID, "事项范围").version_no, 2
        )

    def test_failed_commit_leaves_no_partial_state(self):
        s = self.service
        before = s.water_level(helpers.CASE_ID)
        with self.assertRaises(NotFoundError):
            s.commit(
                self.operator,
                helpers.CASE_ID,
                [
                    op_register_evidence("DEL-1", "sha256:aaa", "财务系统", "承诺书"),
                    op_seal_materials(["DEL-1"], "防止篡改"),
                    op_apply_scope_version(99),  # 不存在 → 整个事务失败
                ],
            )
        # 不得出现材料已受限而决定尚未生效的中间状态
        self.assertNotIn((helpers.CASE_ID, "DEL-1"), s.state.deliveries)
        self.assertEqual(len(s.state.seals), 0)
        self.assertEqual(s.water_level(helpers.CASE_ID), before)
        self.assertEqual(
            s.effective_document(helpers.CASE_ID, "事项范围").version_no, 1
        )

    def test_failed_seal_aborts_evidence_registration(self):
        s = self.service
        with self.assertRaises(NotFoundError):
            s.commit(
                self.operator,
                helpers.CASE_ID,
                [
                    op_register_evidence("DEL-2", "sha256:ccc", "财务系统", "承诺书"),
                    op_seal_materials(["DEL-UNKNOWN"], "封存不存在的交付"),
                ],
            )
        self.assertNotIn((helpers.CASE_ID, "DEL-2"), s.state.deliveries)
        self.assertEqual(len(s.state.seals), 0)

    def test_sealed_delivery_cannot_be_sealed_again(self):
        from audit_evidence_procedure import ConflictError

        s = self.service
        s.register_delivery(
            self.ps["entity"], helpers.CASE_ID, "DEL-1", "sha256:aaa", "来源", "承诺"
        )
        s.seal_materials(self.operator, helpers.CASE_ID, ["DEL-1"], "首次封存")
        with self.assertRaises(ConflictError):
            s.seal_materials(self.operator, helpers.CASE_ID, ["DEL-1"], "重复封存")


if __name__ == "__main__":
    unittest.main()
