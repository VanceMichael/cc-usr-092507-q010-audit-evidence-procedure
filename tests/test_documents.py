"""批准版本生效与临时扩范围复核。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import unittest

from audit_evidence_procedure.errors import (
    InvalidDocument,
    NoApprovedVersion,
    PermissionDenied,
    ReviewerParticipated,
)

from support import build_service, open_standard_case


class DocumentVersionTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = build_service()
        open_standard_case(self.svc)

    def test_draft_is_not_effective(self):
        self.svc.documents.draft(
            "CASE-1", "annual_plan", {"year": 2026, "items": ["财政收支审计"]},
            "director",
        )
        self.assertFalse(self.svc.documents.is_effective("CASE-1", "annual_plan"))
        with self.assertRaises(NoApprovedVersion):
            self.svc.documents.effective("CASE-1", "annual_plan")

    def test_only_approved_version_takes_effect_and_supersedes(self):
        v1 = self.svc.documents.draft(
            "CASE-1", "team", {"members": ["auditor"]}, "director"
        )
        self.assertEqual(self.svc.documents.approve("CASE-1", v1, "director"), 1)
        self.assertEqual(
            self.svc.documents.effective("CASE-1", "team")["content"]["members"],
            ["auditor"],
        )
        v2 = self.svc.documents.draft(
            "CASE-1", "team", {"members": ["auditor", "auditor2"]}, "director"
        )
        # v2 仅为草稿时，生效版本仍是 v1
        self.assertEqual(
            self.svc.documents.effective("CASE-1", "team")["version"], 1
        )
        self.svc.documents.approve("CASE-1", v2, "director")
        effective = self.svc.documents.effective("CASE-1", "team")
        self.assertEqual(effective["version"], 2)
        self.assertEqual(effective["content"]["members"], ["auditor", "auditor2"])
        # 旧版本仍然保留，可追溯
        self.assertEqual(
            [v["status"] for v in self.svc.documents.list_versions("CASE-1", "team")],
            ["approved", "approved"],
        )

    def test_reviewer_cannot_approve(self):
        v1 = self.svc.documents.draft(
            "CASE-1", "annual_plan", {"year": 2026, "items": ["x"]}, "director"
        )
        with self.assertRaises(PermissionDenied):
            self.svc.documents.approve("CASE-1", v1, "reviewer")

    def test_temporary_expansion_requires_legal_basis(self):
        content = {"items": ["预算执行"], "temporary_expansion": True}
        with self.assertRaises(InvalidDocument):
            self.svc.documents.draft(
                "CASE-1", "scope", content, "auditor",
                participants=["auditor"],
            )
        version_id = self.svc.documents.draft(
            "CASE-1", "scope", content, "auditor",
            participants=["auditor", "director"], legal_basis="审计法第二十三条",
        )
        # 未经未参与者复核，不得批准
        with self.assertRaises(InvalidDocument):
            self.svc.documents.approve("CASE-1", version_id, "director")
        # 无复核权的提出人被权限拦截
        with self.assertRaises(PermissionDenied):
            self.svc.documents.review("CASE-1", version_id, "auditor", True)
        # 有复核权但曾参与提出的机关负责人同样被回避规则拦截
        with self.assertRaises(ReviewerParticipated):
            self.svc.documents.review("CASE-1", version_id, "director", True)
        # 未参与提出的复核人复核通过
        self.svc.documents.review("CASE-1", version_id, "reviewer", True, "依据充分")
        # 复核与批准分离：复核人无批准权
        with self.assertRaises(PermissionDenied):
            self.svc.documents.approve("CASE-1", version_id, "reviewer")
        version_no = self.svc.documents.approve("CASE-1", version_id, "director")
        self.assertEqual(version_no, 1)
        effective = self.svc.documents.effective("CASE-1", "scope")
        self.assertEqual(effective["legal_basis"], "审计法第二十三条")
        self.assertEqual(effective["reviewed_by"], "reviewer")

    def test_review_rejection_blocks_approval_but_keeps_old_version(self):
        normal = self.svc.documents.draft(
            "CASE-1", "scope", {"items": ["预算执行"]}, "director"
        )
        self.svc.documents.approve("CASE-1", normal, "director")
        expansion = self.svc.documents.draft(
            "CASE-1", "scope",
            {"items": ["预算执行", "国资处置"], "temporary_expansion": True},
            "auditor", participants=["auditor"], legal_basis="审计法第二十三条",
        )
        self.svc.documents.review("CASE-1", expansion, "reviewer", False, "依据不足")
        with self.assertRaises(InvalidDocument):
            self.svc.documents.approve("CASE-1", expansion, "director")
        # 驳回不影响既有生效版本
        self.assertEqual(
            self.svc.documents.effective("CASE-1", "scope")["version"], 1
        )

    def test_non_expansion_scope_does_not_require_review(self):
        version_id = self.svc.documents.draft(
            "CASE-1", "scope", {"items": ["预算执行"]}, "director"
        )
        self.assertEqual(self.svc.documents.approve("CASE-1", version_id, "director"), 1)


if __name__ == "__main__":
    unittest.main()
