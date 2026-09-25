import dataclasses
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from audit_evidence_procedure import (
    PermissionDenied,
    Principal,
    Role,
    ValidationError,
)


class AccountQueryTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = helpers.new_service()
        self.ps = helpers.make_principals()
        helpers.boot_case(self.service, self.ps)

    def _issue(self, signer=None, **overrides):
        params = {
            "executors": [self.ps["team1"], self.ps["team2"]],
            "account_ids": ["ACC-001", "ACC-002"],
            "purpose": "核查资金流向",
            "legal_basis": "《审计法》第三十三条",
            "info_categories": ["开户信息", "交易流水"],
        }
        params.update(overrides)
        return self.service.request_account_query(
            signer or self.ps["leader"], helpers.CASE_ID, **params
        )

    def test_issue_requires_sign_permission(self):
        with self.assertRaises(PermissionDenied):
            self._issue(signer=self.ps["team1"])

    def test_requires_two_distinct_executors(self):
        with self.assertRaisesRegex(ValidationError, "两名执行人员"):
            self._issue(executors=[self.ps["team1"]])
        with self.assertRaisesRegex(ValidationError, "同一人"):
            self._issue(executors=[self.ps["team1"], self.ps["team1"]])

    def test_executor_must_have_execute_permission(self):
        with self.assertRaises(PermissionDenied):
            self._issue(executors=[self.ps["team1"], self.ps["project"]])

    def test_executor_must_be_on_approved_team(self):
        outsider = Principal(
            id="U-OUT", name="外部人员", roles=frozenset({Role.AUDIT_TEAM})
        )
        with self.assertRaisesRegex(ValidationError, "审计组成员"):
            self._issue(executors=[self.ps["team1"], outsider])

    def test_notice_validity_enforced(self):
        self.clock.advance(timedelta(days=31))
        with self.assertRaisesRegex(ValidationError, "通知书期限"):
            self._issue()

    def test_query_requires_approved_notice(self):
        self.service.propose_document(
            self.ps["team1"],
            helpers.CASE_ID,
            "审计通知",
            {
                "notice_no": "审通〔2026〕2号",
                "valid_from": helpers.T0.isoformat(),
                "valid_until": (helpers.T0 + timedelta(days=10)).isoformat(),
                "scope_summary": "未批准",
            },
        )
        with self.assertRaisesRegex(ValidationError, "批准版本"):
            self._issue(notice_version_no=2)

    def test_institution_payload_is_minimized(self):
        query = self._issue()
        payload = query.institution_payload()
        # 只含完成协助所需内容
        self.assertEqual(
            set(payload),
            {
                "query_id",
                "notice_no",
                "issuing_authority",
                "legal_basis",
                "account_ids",
                "info_categories",
                "executor_names",
                "valid_until",
            },
        )
        self.assertEqual(payload["executor_names"], ["审计组员丁", "审计组员戊"])
        self.assertEqual(payload["notice_no"], "审通〔2026〕1号")
        # 不含案件脉络、目的叙述等内部信息
        for leaked in ("purpose", "case_id", "case_title", "scope_items", "domains"):
            self.assertNotIn(leaked, payload)
        # 与合同模式一致
        schema = json.loads(
            Path("contracts/institution_payload.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(schema["required"]) - set(payload), set())
        if schema.get("additionalProperties") is False:
            self.assertEqual(set(payload) - set(schema["properties"]), set())

    def test_receipt_cannot_rewrite_application(self):
        s = self.service
        query = self._issue()
        before = dataclasses.asdict(query)
        receipt = s.record_receipt(
            self.ps["team1"], query.query_id, "某银行", "已提供流水", 128
        )
        after = dataclasses.asdict(s.state.queries[query.query_id])
        # 回执登记后申请保持原样
        self.assertEqual(before, after)
        # 回执是引用申请的独立事实
        self.assertEqual(receipt.query_id, query.query_id)
        self.assertEqual(receipt.items_count, 128)
        self.assertIn(receipt.receipt_id, s.state.receipts)
        issued = [e for e in s.events() if e.type == "account_query_issued"]
        self.assertEqual(len(issued), 1)

    def test_receipt_requires_existing_query(self):
        from audit_evidence_procedure import NotFoundError

        with self.assertRaises(NotFoundError):
            self.service.record_receipt(
                self.ps["team1"], "Q-UNKNOWN", "某银行", "回执", 1
            )


if __name__ == "__main__":
    unittest.main()
