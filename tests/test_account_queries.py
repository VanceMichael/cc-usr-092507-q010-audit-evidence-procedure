"""金融账户查询：签发三核验、双人执行、最小必要、回执不改写、授权解释。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import unittest
from datetime import timedelta

from audit_evidence_procedure.errors import (
    AccountQueryGuardFailed,
    MinimumNecessaryViolation,
    NoApprovedVersion,
    PermissionDenied,
    ReceiptConflict,
)

from support import approve_audit_notice, build_service, open_standard_case, query_window


class AccountQueryTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = build_service()
        open_standard_case(self.svc)
        approve_audit_notice(self.svc)

    def _request(self, request_no=None):
        return self.svc.account_queries.create_request(
            "CASE-1",
            target_subject="某发起单位",
            account_ids=["ACC-1", "ACC-2"],
            period_from="2026-01-01",
            period_until="2026-03-31",
            inquiry_items=["开户信息", "交易流水"],
            applicant="auditor",
        )

    def test_full_authorized_chain_and_explanation(self):
        request_no = self._request()
        vf, vu = query_window(self.svc)
        signed = self.svc.account_queries.sign(
            "CASE-1", request_no, "director", ["auditor", "auditor2"], vf, vu
        )
        self.assertTrue(all(signed["checks"].values()))

        package_fields = {
            "target_subject": "某发起单位",
            "account_ids": ["ACC-1"],
            "period_from": "2026-01-01",
            "period_until": "2026-03-31",
            "inquiry_items": ["交易流水"],
            "notice_no": signed["notice_doc_id"],
        }
        version = self.svc.account_queries.prepare_assistance_package(
            "CASE-1", request_no, package_fields, "auditor"
        )
        self.assertEqual(version, 1)

        # 两名执行人各自持通知执行登记（双人执行）
        self.svc.account_queries.execute("CASE-1", request_no, "auditor")
        self.svc.account_queries.execute("CASE-1", request_no, "auditor2")

        # 金融机构回执
        seq = self.svc.account_queries.record_receipt(
            "CASE-1", request_no, "rcpt-hash-1", "账户流水电子件 1 份", "banker"
        )
        self.assertEqual(seq, 1)

        # 执行人经逐案授权可查看材料；普通项目角色（另一审计组人员）不行
        material = self.svc.account_queries.read_material(
            "CASE-1", request_no, "auditor"
        )
        self.assertEqual(material["receipts"][0]["app_hash_before"],
                         material["receipts"][0]["app_hash_after"])

        # 机关负责人登记/封存金融材料
        self.svc.account_queries.seal_account_package("CASE-1", request_no, "director")

        explanation = self.svc.account_queries.explain(
            "CASE-1", request_no, "director"
        )
        self.assertTrue(explanation["procedure_valid_now"])
        self.assertTrue(explanation["evidence_chain"]["application_intact"])
        actions = {u["action"] for u in explanation["usage_history"]}
        self.assertIn("sign", actions)
        self.assertIn("grant_on_sign", actions)
        self.assertEqual(
            sorted(u["person_id"] for u in explanation["usage_history"]
                   if u["action"] == "execute"),
            ["auditor", "auditor2"],
        )
        grant = next(u for u in explanation["usage_history"]
                     if u["action"] == "grant_on_sign")
        self.assertEqual(grant["under_permission"], "account_material.read")

    def test_sign_requires_authority_two_executors_and_notice_period(self):
        request_no = self._request()
        vf, vu = query_window(self.svc)

        # 无签发权的审计组人员不能签发
        with self.assertRaises(PermissionDenied):
            self.svc.account_queries.sign(
                "CASE-1", request_no, "auditor", ["auditor", "auditor2"], vf, vu
            )
        # 只有一名执行人
        try:
            self.svc.account_queries.sign(
                "CASE-1", request_no, "director", ["auditor"], vf, vu
            )
        except AccountQueryGuardFailed as exc:
            self.assertFalse(exc.checks["two_executors"])
        else:
            self.fail("应当因执行人员不足被拒绝")
        # 两人为同一人
        with self.assertRaises(AccountQueryGuardFailed):
            self.svc.account_queries.sign(
                "CASE-1", request_no, "director", ["auditor", "auditor"], vf, vu
            )
        # 查询通知期限超出审计通知期限
        with self.assertRaises(AccountQueryGuardFailed) as caught:
            self.svc.account_queries.sign(
                "CASE-1", request_no, "director", ["auditor", "auditor2"],
                vf, (self.clock.now() + timedelta(days=3650)).isoformat(),
            )
        self.assertFalse(caught.exception.checks["notice_period_valid"])

    def test_no_effective_audit_notice_blocks_signing(self):
        self.svc, self.clock = build_service()
        open_standard_case(self.svc)  # 不批准审计通知书
        request_no = self._request()
        vf, vu = query_window(self.svc)
        with self.assertRaises(AccountQueryGuardFailed) as caught:
            self.svc.account_queries.sign(
                "CASE-1", request_no, "director", ["auditor", "auditor2"], vf, vu
            )
        self.assertFalse(caught.exception.checks["notice_period_valid"])

    def test_expired_notice_invalidates_execution_and_validity(self):
        request_no = self._request()
        vf, vu = query_window(self.svc, days=10)
        self.svc.account_queries.sign(
            "CASE-1", request_no, "director", ["auditor", "auditor2"], vf, vu
        )
        self.clock.advance(timedelta(days=11))
        validity = self.svc.account_queries.validity("CASE-1", request_no)
        self.assertFalse(validity["valid"])
        self.assertFalse(validity["checks"]["within_notice_period"])
        with self.assertRaises(AccountQueryGuardFailed):
            self.svc.account_queries.execute("CASE-1", request_no, "auditor")
        with self.assertRaises(AccountQueryGuardFailed):
            self.svc.account_queries.prepare_assistance_package(
                "CASE-1", request_no,
                {"target_subject": "某发起单位"}, "auditor",
            )

    def test_minimum_necessary_package_is_enforced(self):
        request_no = self._request()
        vf, vu = query_window(self.svc)
        self.svc.account_queries.sign(
            "CASE-1", request_no, "director", ["auditor", "auditor2"], vf, vu
        )
        # 白名单外字段
        with self.assertRaises(MinimumNecessaryViolation):
            self.svc.account_queries.prepare_assistance_package(
                "CASE-1", request_no,
                {"target_subject": "某发起单位", "家庭住址": "某处"}, "auditor",
            )
        # 账户超出申请范围
        with self.assertRaises(MinimumNecessaryViolation):
            self.svc.account_queries.prepare_assistance_package(
                "CASE-1", request_no,
                {"target_subject": "某发起单位", "account_ids": ["ACC-9"]},
                "auditor",
            )
        # 查询事项扩大
        with self.assertRaises(MinimumNecessaryViolation):
            self.svc.account_queries.prepare_assistance_package(
                "CASE-1", request_no,
                {"target_subject": "某发起单位",
                 "inquiry_items": ["开户信息", "贷款记录"]},
                "auditor",
            )

    def test_non_executor_cannot_execute_or_read_material(self):
        request_no = self._request()
        vf, vu = query_window(self.svc)
        self.svc.account_queries.sign(
            "CASE-1", request_no, "director", ["auditor", "auditor2"], vf, vu
        )
        # reviewer 不是执行人，也没有逐案材料授权
        with self.assertRaises(PermissionDenied):
            self.svc.account_queries.execute("CASE-1", request_no, "reviewer")
        with self.assertRaises(PermissionDenied):
            self.svc.account_queries.read_material("CASE-1", request_no, "reviewer")
        # 同为普通项目人员但未列入本次执行的人，同样不能查看金融账户材料
        self.svc.register_person("auditor3", "孙审计", "auditor")
        with self.assertRaises(PermissionDenied):
            self.svc.account_queries.read_material("CASE-1", request_no, "auditor3")
        # 登记金融材料仅限机关负责人序列
        with self.assertRaises(PermissionDenied):
            self.svc.account_queries.seal_account_package(
                "CASE-1", request_no, "auditor"
            )

    def test_receipt_cannot_rewrite_application(self):
        request_no = self._request()
        vf, vu = query_window(self.svc)
        self.svc.account_queries.sign(
            "CASE-1", request_no, "director", ["auditor", "auditor2"], vf, vu
        )
        self.svc.account_queries.record_receipt(
            "CASE-1", request_no, "h1", "首批回执", "banker"
        )
        # 模拟异常篡改申请事实：回执路径必须拒绝在被改写的申请上继续登记
        self.svc.storage.conn.execute(
            "UPDATE account_queries SET applicant = ? WHERE request_no = ?",
            ("intruder", request_no),
        )
        with self.assertRaises(ReceiptConflict):
            self.svc.account_queries.record_receipt(
                "CASE-1", request_no, "h2", "第二批回执", "banker"
            )
        explanation = self.svc.account_queries.explain(
            "CASE-1", request_no, "director"
        )
        self.assertFalse(explanation["evidence_chain"]["application_intact"])
        self.assertFalse(explanation["procedure_valid_now"])

    def test_draft_request_is_not_a_valid_procedure(self):
        request_no = self._request()
        validity = self.svc.account_queries.validity("CASE-1", request_no)
        self.assertFalse(validity["valid"])
        self.assertFalse(validity["checks"]["signed"])


if __name__ == "__main__":
    unittest.main()
