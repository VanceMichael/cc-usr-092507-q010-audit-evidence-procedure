import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers
from audit_evidence_procedure import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from audit_evidence_procedure.models import (
    DeadlineStatus,
    DocumentKind,
    DocumentState,
    ExpansionState,
)


class PlanVersionTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = helpers.new_service()
        self.ps = helpers.make_principals()
        helpers.boot_case(self.service, self.ps)

    def test_only_approved_version_is_effective(self):
        s, ps = self.service, self.ps
        team1, reviewer = ps["team1"], ps["reviewer"]
        self.assertEqual(
            s.effective_document(helpers.CASE_ID, "事项范围").version_no, 1
        )
        # 提交未批准的版本 2：生效版本不变
        s.propose_document(
            team1,
            helpers.CASE_ID,
            "事项范围",
            {"items": [{"domain": "国有资源", "description": "土地出让"}]},
        )
        self.assertEqual(
            s.effective_document(helpers.CASE_ID, "事项范围").version_no, 1
        )
        # 批准后版本 2 生效，版本 1 被取代
        s.approve_document(reviewer, helpers.CASE_ID, "事项范围", 2)
        self.assertEqual(
            s.effective_document(helpers.CASE_ID, "事项范围").version_no, 2
        )
        old = s.state.documents[(helpers.CASE_ID, DocumentKind.SCOPE, 1)]
        self.assertEqual(old.state, DocumentState.SUPERSEDED)

    def test_approve_unknown_version_is_rejected(self):
        with self.assertRaises(NotFoundError):
            self.service.approve_document(
                self.ps["reviewer"], helpers.CASE_ID, "审计通知", 99
            )

    def test_approve_twice_is_rejected(self):
        with self.assertRaises(ConflictError):
            self.service.approve_document(
                self.ps["reviewer"], helpers.CASE_ID, "审计通知", 1
            )

    def test_propose_requires_permission(self):
        with self.assertRaises(PermissionDenied):
            self.service.propose_document(
                self.ps["project"],
                helpers.CASE_ID,
                "年度计划",
                {"year": 2027, "objectives": ["x"]},
            )

    def test_document_content_is_validated(self):
        with self.assertRaises(ValidationError):
            self.service.propose_document(
                self.ps["team1"], helpers.CASE_ID, "审计组成员", {"members": []}
            )
        with self.assertRaises(ValidationError):
            self.service.propose_document(
                self.ps["team1"],
                helpers.CASE_ID,
                "事项范围",
                {"items": [{"domain": "未知领域", "description": "x"}]},
            )


class ScopeExpansionTest(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = helpers.new_service()
        self.ps = helpers.make_principals()
        helpers.boot_case(self.service, self.ps)

    def test_expansion_requires_legal_basis(self):
        with self.assertRaisesRegex(ValidationError, "法定依据"):
            self.service.request_scope_expansion(
                self.ps["team1"],
                helpers.CASE_ID,
                [{"domain": "国有资源", "description": "矿业权"}],
                "   ",
            )

    def test_reviewer_must_not_be_proposer(self):
        hybrid = self.ps["hybrid"]
        expansion = self.service.request_scope_expansion(
            hybrid,
            helpers.CASE_ID,
            [{"domain": "国有资源", "description": "矿业权"}],
            "《审计法》第三十三条",
        )
        with self.assertRaisesRegex(PermissionDenied, "不得参与提出"):
            self.service.review_scope_expansion(
                hybrid, helpers.CASE_ID, expansion.request_id, approve=True
            )

    def test_reviewer_must_not_be_participant(self):
        expansion = self.service.request_scope_expansion(
            self.ps["team1"],
            helpers.CASE_ID,
            [{"domain": "国有资源", "description": "矿业权"}],
            "《审计法》第三十三条",
            participants=(self.ps["hybrid"].id,),
        )
        with self.assertRaisesRegex(PermissionDenied, "不得参与提出"):
            self.service.review_scope_expansion(
                self.ps["hybrid"], helpers.CASE_ID, expansion.request_id, approve=True
            )

    def test_approved_expansion_creates_new_effective_scope(self):
        s, ps = self.service, self.ps
        expansion = s.request_scope_expansion(
            ps["team1"],
            helpers.CASE_ID,
            [{"domain": "重大公共工程", "description": "轨道交通"}],
            "《审计法》第三十三条",
        )
        s.review_scope_expansion(
            ps["reviewer"], helpers.CASE_ID, expansion.request_id, approve=True,
            note="依据充分",
        )
        scope = s.effective_document(helpers.CASE_ID, "事项范围")
        self.assertEqual(scope.version_no, 2)
        descriptions = [item["description"] for item in scope.content["items"]]
        self.assertIn("预算执行", descriptions)  # 原有事项保留
        self.assertIn("轨道交通", descriptions)
        updated = s.state.expansions[expansion.request_id]
        self.assertEqual(updated.state, ExpansionState.APPROVED)
        self.assertEqual(updated.reviewed_by, ps["reviewer"].id)
        # 复核期限事项在同一事务中完成
        review_items = [
            item
            for item in s.state.deadline_items.values()
            if item.ref_id == expansion.request_id
        ]
        self.assertTrue(review_items)
        self.assertTrue(
            all(item.status == DeadlineStatus.DONE for item in review_items)
        )

    def test_rejected_expansion_keeps_scope_unchanged(self):
        s, ps = self.service, self.ps
        expansion = s.request_scope_expansion(
            ps["team1"],
            helpers.CASE_ID,
            [{"domain": "重大公共工程", "description": "轨道交通"}],
            "《审计法》第三十三条",
        )
        s.review_scope_expansion(
            ps["reviewer2"], helpers.CASE_ID, expansion.request_id,
            approve=False, note="依据不足",
        )
        self.assertEqual(
            s.effective_document(helpers.CASE_ID, "事项范围").version_no, 1
        )
        self.assertEqual(
            s.state.expansions[expansion.request_id].state, ExpansionState.REJECTED
        )
        # 已复核的请求不能再次复核
        with self.assertRaises(ConflictError):
            s.review_scope_expansion(
                ps["reviewer"], helpers.CASE_ID, expansion.request_id, approve=True
            )


if __name__ == "__main__":
    unittest.main()
