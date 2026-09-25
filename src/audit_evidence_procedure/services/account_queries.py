"""金融账户查询授权链。

流程：申请（固定快照摘要）→ 签发（三道前置核验 + 双人逐案授权）→
最小必要协助包 → 双人执行 → 金融机构回执（只追加、不可改写申请）→
材料登记封存。

每次使用都写入使用记录；:meth:`explain` 可解释证据如何取得、
谁曾在何种权限下使用、程序当前是否有效。
"""

from __future__ import annotations

import uuid

from ..clock import parse_iso
from ..constants import Permission
from ..errors import (
    AccountQueryGuardFailed,
    MinimumNecessaryViolation,
    NoApprovedVersion,
    NotFound,
    PermissionDenied,
    ReceiptConflict,
)
from ..storage import Storage
from .access import AccessService
from .cases import CaseService
from .documents import DocumentService
from .evidence import REF_ACCOUNT_PACKAGE, EvidenceService

# 允许出现在协助包中的字段白名单——交付金融机构的信息以此为上限
ALLOWED_PACKAGE_FIELDS = frozenset({
    "target_subject",       # 被查询单位/个人名称
    "subject_id_type",      # 身份证件种类
    "subject_id_no",        # 证件号码（完成身份识别所需）
    "account_ids",          # 具体账户标识（不得超范围给全部账户）
    "period_from",          # 查询起始日
    "period_until",         # 查询截止日
    "inquiry_items",        # 查询事项（如开户信息、交易流水）
    "notice_no",            # 协助查询通知书编号
    "assisting_institution",  # 受理的金融机构
})


class AccountQueryService:
    def __init__(
        self,
        storage: Storage,
        access: AccessService,
        cases: CaseService,
        documents: DocumentService,
        evidence: EvidenceService,
    ):
        self.storage = storage
        self.access = access
        self.cases = cases
        self.documents = documents
        self.evidence = evidence

    # ---- 申请 ----------------------------------------------------

    def create_request(
        self,
        case_id: str,
        target_subject: str,
        account_ids: list[str],
        period_from: str,
        period_until: str,
        inquiry_items: list[str],
        applicant: str,
    ) -> str:
        """提出对发起单位或个人相关账户的查询申请，返回申请编号。"""

        self.access.require_permission(applicant, case_id, Permission.ACCOUNT_QUERY_REQUEST)
        if not account_ids or not inquiry_items:
            raise ValueError("账户标识与查询事项不能为空")
        if period_from > period_until:
            raise ValueError("查询期间起止无效")
        request_no = f"AQ-{uuid.uuid4().hex[:12].upper()}"
        scope = {
            "account_ids": sorted(account_ids),
            "period_from": period_from,
            "period_until": period_until,
            "inquiry_items": sorted(inquiry_items),
        }
        at = self.cases.clock.now().isoformat()
        query_id = uuid.uuid4().hex
        with self.storage.tx() as conn:
            conn.execute(
                """
                INSERT INTO account_queries
                    (id, case_id, request_no, applicant, target_subject, account_scope,
                     status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)
                """,
                (query_id, case_id, request_no, applicant, target_subject,
                 self.storage.dumps(scope), at),
            )
            conn.execute(
                "UPDATE account_queries SET application_hash = ? WHERE id = ?",
                (self._application_hash({
                    "request_no": request_no, "case_id": case_id,
                    "applicant": applicant, "target_subject": target_subject,
                    "account_scope": scope, "created_at": at,
                }), query_id),
            )
        return request_no

    # ---- 签发（三道前置核验） ------------------------------------

    def sign(
        self,
        case_id: str,
        request_no: str,
        signer: str,
        executors: list[str],
        valid_from: str,
        valid_until: str,
    ) -> dict:
        """签发协助查询通知书。

        核验：签发权限 / 两名执行人员（不同人、均具执行权）/ 通知书期限
        （审计通知已批准生效，查询通知有效期落在审计通知期限内且未届满）。
        核验全部通过才生效，并仅向两名执行人逐案授予材料查看权。
        """

        query = self._query(case_id, request_no)
        self.access.require_permission(signer, case_id, Permission.ACCOUNT_QUERY_SIGN)
        checks = self._sign_checks(case_id, query, signer, executors, valid_from, valid_until)
        if not all(checks.values()):
            raise AccountQueryGuardFailed("账户查询签发前置核验未通过", checks)

        at = self.cases.clock.now().isoformat()
        with self.cases.batch(case_id) as batch:
            notice_doc_id = uuid.uuid4().hex
            batch.conn.execute(
                """
                UPDATE account_queries
                SET status = 'signed', signer = ?, signed_at = ?, notice_doc_id = ?,
                    notice_valid_from = ?, notice_valid_until = ?, executors = ?
                WHERE id = ?
                """,
                (signer, at, notice_doc_id, valid_from, valid_until,
                 self.storage.dumps(executors), query["id"]),
            )
            # 逐案授予两名执行人查看本次查询材料的权限——敏感权限的唯一下放路径
            for executor in executors:
                batch.conn.execute(
                    """
                    INSERT OR IGNORE INTO case_grants
                        (case_id, person_id, permission, granted_by, granted_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (case_id, executor, Permission.ACCOUNT_MATERIAL_READ.value,
                     signer, at),
                )
                self._log_in_conn(
                    batch.conn, query["id"], executor,
                    Permission.ACCOUNT_MATERIAL_READ.value,
                    "grant_on_sign", {"signer": signer, "request_no": request_no}, at,
                )
            self._log_in_conn(
                batch.conn, query["id"], signer,
                Permission.ACCOUNT_QUERY_SIGN.value, "sign",
                {"request_no": request_no, "executors": executors,
                 "valid_from": valid_from, "valid_until": valid_until}, at,
            )
            batch.append_event(
                "account_query.signed",
                {"request_no": request_no, "signer": signer,
                 "executors": executors, "valid_until": valid_until,
                 "checks": checks},
                signer,
                Permission.ACCOUNT_MATERIAL_READ.value,
            )
        return {"request_no": request_no, "notice_doc_id": notice_doc_id, "checks": checks}

    def _sign_checks(self, case_id, query, signer, executors, valid_from, valid_until) -> dict:
        checks = {
            "signer_has_authority": False,
            "two_executors": False,
            "notice_period_valid": False,
            "request_is_draft": query["status"] == "draft",
        }
        checks["signer_has_authority"] = self.access.has_permission(
            signer, case_id, Permission.ACCOUNT_QUERY_SIGN
        )
        if len(executors) == 2 and executors[0] != executors[1]:
            checks["two_executors"] = all(
                self.access.has_permission(e, case_id, Permission.ACCOUNT_QUERY_EXECUTE)
                for e in executors
            )
        try:
            notice = self.documents.effective(case_id, "audit_notice")
            notice_content = notice["content"]
            now = self.cases.clock.now()
            within_audit_notice = (
                parse_iso(notice_content["valid_from"]) <= parse_iso(valid_from)
                and parse_iso(valid_until) <= parse_iso(notice_content["valid_until"])
            )
            checks["notice_period_valid"] = (
                within_audit_notice
                and parse_iso(valid_from) <= now <= parse_iso(valid_until)
            )
        except (NoApprovedVersion, KeyError):
            checks["notice_period_valid"] = False
        return checks

    # ---- 最小必要协助包 ------------------------------------------

    def prepare_assistance_package(
        self, case_id: str, request_no: str, fields: dict, prepared_by: str
    ) -> int:
        """组装交金融机构的协助信息包。

        字段白名单 + 逐项不得超出申请范围；多余字段一律拒绝，
        防止把与协助无关的账户信息带出去。返回包版本号。
        """

        query = self._query(case_id, request_no)
        self.access.require_permission(
            prepared_by, case_id, Permission.ASSISTANCE_PACKAGE_PREPARE
        )
        self._require_currently_valid(query)
        extra = sorted(set(fields) - ALLOWED_PACKAGE_FIELDS)
        if extra:
            raise MinimumNecessaryViolation(
                f"协助包包含完成协助所不需要的字段：{extra}"
            )
        scope = self.storage.loads(query["account_scope"])
        self._check_fields_within_scope(fields, query, scope)

        at = self.cases.clock.now().isoformat()
        with self.cases.batch(case_id) as batch:
            last = batch.conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM assistance_packages"
                " WHERE query_id = ?",
                (query["id"],),
            ).fetchone()["v"]
            version = last + 1
            package_id = uuid.uuid4().hex
            batch.conn.execute(
                """
                INSERT INTO assistance_packages
                    (id, query_id, version, fields, prepared_by, prepared_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (package_id, query["id"], version, self.storage.dumps(fields),
                 prepared_by, at),
            )
            self._log_in_conn(
                batch.conn, query["id"], prepared_by,
                Permission.ASSISTANCE_PACKAGE_PREPARE.value,
                "package_prepared", {"version": version, "fields": sorted(fields)}, at,
            )
            batch.append_event(
                "account_query.package_prepared",
                {"request_no": request_no, "package_id": package_id,
                 "version": version, "fields": sorted(fields)},
                prepared_by,
                Permission.ACCOUNT_MATERIAL_READ.value,
            )
        return version

    def _check_fields_within_scope(self, fields, query, scope) -> None:
        if "target_subject" in fields and fields["target_subject"] != query["target_subject"]:
            raise MinimumNecessaryViolation("协助包被查询主体与申请不一致")
        if "account_ids" in fields:
            extra_accounts = sorted(set(fields["account_ids"]) - set(scope["account_ids"]))
            if extra_accounts:
                raise MinimumNecessaryViolation(f"协助包账户超出批准范围：{extra_accounts}")
        if "period_from" in fields and fields["period_from"] < scope["period_from"]:
            raise MinimumNecessaryViolation("协助包起始日早于申请期间")
        if "period_until" in fields and fields["period_until"] > scope["period_until"]:
            raise MinimumNecessaryViolation("协助包截止日晚于申请期间")
        if "inquiry_items" in fields:
            extra_items = sorted(set(fields["inquiry_items"]) - set(scope["inquiry_items"]))
            if extra_items:
                raise MinimumNecessaryViolation(f"协助包查询事项超出申请：{extra_items}")
        for key, value in fields.items():
            if value in (None, "", []):
                raise MinimumNecessaryViolation(f"协助包字段 {key} 为空值，应予剔除而非报送")

    # ---- 执行 ----------------------------------------------------

    def execute(self, case_id: str, request_no: str, executor_id: str) -> None:
        """两名执行人各自在通知有效期内持通知执行登记。

        状态置为 executed 对两人均幂等；每次执行都独立留痕，
        从而可核验查询确由两名授权人员共同实施。
        """

        query = self._query(case_id, request_no)
        executors = self.storage.loads(query["executors"]) or []
        if executor_id not in executors:
            raise PermissionDenied("执行人必须是签发时登记的两名执行人员之一")
        self.access.require_permission(executor_id, case_id, Permission.ACCOUNT_QUERY_EXECUTE)
        self._require_currently_valid(query)
        if query["status"] not in ("signed", "executed"):
            raise AccountQueryGuardFailed(
                "查询通知尚未签发或已终结", {"status": query["status"]}
            )
        at = self.cases.clock.now().isoformat()
        with self.cases.batch(case_id) as batch:
            batch.conn.execute(
                "UPDATE account_queries SET status = 'executed' WHERE id = ?",
                (query["id"],),
            )
            self._log_in_conn(
                batch.conn, query["id"], executor_id,
                Permission.ACCOUNT_QUERY_EXECUTE.value, "execute",
                {"request_no": request_no}, at,
            )
            batch.append_event(
                "account_query.executed",
                {"request_no": request_no, "executor": executor_id},
                executor_id,
                Permission.ACCOUNT_MATERIAL_READ.value,
            )

    # ---- 回执（只追加，绝不改写申请） ----------------------------

    def record_receipt(
        self, case_id: str, request_no: str, payload_hash: str,
        summary: str, recorded_by: str,
    ) -> int:
        query = self._query(case_id, request_no)
        self.access.require_permission(recorded_by, case_id, Permission.RECEIPT_RECORD)
        # 锚点校验：申请现状必须仍与签发时快照一致，回执路径无任何申请写操作
        before = self._current_application_hash(query)
        if before != query["application_hash"]:
            raise ReceiptConflict("申请已被改动，回执不能建立在被改写的申请之上")

        at = self.cases.clock.now().isoformat()
        with self.cases.batch(case_id) as batch:
            seq_row = batch.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM receipts WHERE query_id = ?",
                (query["id"],),
            ).fetchone()
            seq = seq_row["s"] + 1
            receipt_id = uuid.uuid4().hex
            batch.conn.execute(
                """
                INSERT INTO receipts
                    (id, query_id, seq, payload_hash, summary, recorded_by,
                     recorded_at, app_hash_before, app_hash_after)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (receipt_id, query["id"], seq, payload_hash, summary, recorded_by,
                 at, before, before),
            )
            if query["status"] in ("signed", "executed"):
                batch.conn.execute(
                    "UPDATE account_queries SET status = 'material_returned' WHERE id = ?",
                    (query["id"],),
                )
            self._log_in_conn(
                batch.conn, query["id"], recorded_by,
                Permission.RECEIPT_RECORD.value, "receipt_recorded",
                {"seq": seq, "payload_hash": payload_hash}, at,
            )
            batch.append_event(
                "account_query.receipt_recorded",
                {"request_no": request_no, "seq": seq,
                 "payload_hash": payload_hash, "summary": summary,
                 "application_unchanged": True},
                recorded_by,
                Permission.ACCOUNT_MATERIAL_READ.value,
            )
        return seq

    # ---- 材料查看与封存 ------------------------------------------

    def read_material(self, case_id: str, request_no: str, reader_id: str) -> dict:
        """查看金融账户查询材料：逐案授权 + 程序当前有效，双条件同时满足。"""

        query = self._query(case_id, request_no)
        self.access.require_permission(reader_id, case_id, Permission.ACCOUNT_MATERIAL_READ)
        at = self.cases.clock.now().isoformat()
        with self.storage.tx() as conn:
            self._log_in_conn(
                conn, query["id"], reader_id,
                Permission.ACCOUNT_MATERIAL_READ.value, "material_read",
                {"request_no": request_no}, at,
            )
        packages = [
            dict(r) for r in self.storage.conn.execute(
                "SELECT version, fields, prepared_by, prepared_at FROM assistance_packages"
                " WHERE query_id = ? ORDER BY version",
                (query["id"],),
            ).fetchall()
        ]
        receipts = [
            dict(r) for r in self.storage.conn.execute(
                "SELECT seq, payload_hash, summary, recorded_by, recorded_at,"
                " app_hash_before, app_hash_after FROM receipts"
                " WHERE query_id = ? ORDER BY seq",
                (query["id"],),
            ).fetchall()
        ]
        return {"packages": packages, "receipts": receipts}

    def seal_account_package(
        self, case_id: str, request_no: str, sealed_by: str
    ) -> str:
        """登记/封存金融机构返回材料；普通项目权限无法干预此登记。"""

        query = self._query(case_id, request_no)
        self.access.require_permission(
            sealed_by, case_id, Permission.ACCOUNT_MATERIAL_RECORD
        )
        with self.cases.batch(case_id) as batch:
            # 事件可见权与登记操作权分离：执行人能看到材料已封存的事实
            seal_id = self.evidence.seal_ref(
                batch, REF_ACCOUNT_PACKAGE, query["id"], sealed_by,
                Permission.ACCOUNT_MATERIAL_READ,
            )
        return seal_id

    # ---- 使用记录与授权解释 --------------------------------------

    def usage_log(self, case_id: str, request_no: str) -> list[dict]:
        query = self._query(case_id, request_no)
        rows = self.storage.conn.execute(
            "SELECT person_id, permission, action, detail, at FROM query_access_log"
            " WHERE query_id = ? ORDER BY id",
            (query["id"],),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = self.storage.loads(item["detail"])
            result.append(item)
        return result

    def explain(self, case_id: str, request_no: str, viewer_id: str) -> dict:
        """授权查询解释：证据链、使用史（何人何权限）、程序当前有效性。"""

        query = self._query(case_id, request_no)
        # 能看到解释的人：机关负责人或本次逐案授权人
        if not (
            self.access.has_permission(viewer_id, case_id, Permission.ACCOUNT_QUERY_SIGN)
            or self.access.has_permission(viewer_id, case_id, Permission.ACCOUNT_MATERIAL_READ)
        ):
            raise PermissionDenied("无权查看账户查询授权解释")
        validity = self.validity(case_id, request_no)
        packages = [
            {"version": r["version"], "fields": self.storage.loads(r["fields"]),
             "prepared_by": r["prepared_by"], "prepared_at": r["prepared_at"]}
            for r in self.storage.conn.execute(
                "SELECT version, fields, prepared_by, prepared_at FROM assistance_packages"
                " WHERE query_id = ? ORDER BY version",
                (query["id"],),
            ).fetchall()
        ]
        receipts = [
            {"seq": r["seq"], "payload_hash": r["payload_hash"],
             "recorded_by": r["recorded_by"], "recorded_at": r["recorded_at"]}
            for r in self.storage.conn.execute(
                "SELECT seq, payload_hash, recorded_by, recorded_at FROM receipts"
                " WHERE query_id = ? ORDER BY seq",
                (query["id"],),
            ).fetchall()
        ]
        usages = []
        for row in self.storage.conn.execute(
            "SELECT person_id, permission, action, detail, at FROM query_access_log"
            " WHERE query_id = ? ORDER BY id",
            (query["id"],),
        ).fetchall():
            person = self.access.person(row["person_id"])
            usages.append({
                "person_id": row["person_id"],
                "person_name": person["name"],
                "role": person["role"],
                "under_permission": row["permission"],
                "action": row["action"],
                "detail": self.storage.loads(row["detail"]),
                "at": row["at"],
            })
        return {
            "request_no": request_no,
            "target_subject": query["target_subject"],
            "scope": self.storage.loads(query["account_scope"]),
            "evidence_chain": {
                "how_obtained": (
                    "审计组依批准生效的审计通知书提出申请，机关负责人签发协助查询通知书，"
                    "两名授权执行人持通知向金融机构提交最小必要信息包，"
                    "金融机构回执以只追加方式登记，申请快照摘要全程不变"
                ),
                "application_hash": query["application_hash"],
                "application_intact": query["application_hash"] == self._current_application_hash(query),
                "packages": packages,
                "receipts": receipts,
            },
            "usage_history": usages,
            "procedure_valid_now": validity["valid"],
            "validity_checks": validity["checks"],
        }

    def validity(self, case_id: str, request_no: str) -> dict:
        """程序当前是否有效：重放全部前置条件并检验申请完整性、通知期限。"""

        query = self._query(case_id, request_no)
        executors = self.storage.loads(query["executors"]) or []
        checks: dict[str, object] = {
            "status": query["status"],
            "signed": query["status"] in ("signed", "executed", "material_returned"),
            "signer_has_authority": False,
            "two_executors": False,
            "audit_notice_effective": False,
            "within_notice_period": False,
            "application_intact": query["application_hash"]
            == self._current_application_hash(query),
            "sealed": self.evidence.is_sealed(case_id, REF_ACCOUNT_PACKAGE, query["id"]),
        }
        if query["signer"]:
            checks["signer_has_authority"] = self.access.has_permission(
                query["signer"], case_id, Permission.ACCOUNT_QUERY_SIGN
            )
        if len(executors) == 2 and executors[0] != executors[1]:
            checks["two_executors"] = all(
                self.access.has_permission(e, case_id, Permission.ACCOUNT_QUERY_EXECUTE)
                for e in executors
            )
        try:
            notice = self.documents.effective(case_id, "audit_notice")
            checks["audit_notice_effective"] = True
            now = self.cases.clock.now()
            if query["notice_valid_from"] and query["notice_valid_until"]:
                checks["within_notice_period"] = (
                    parse_iso(query["notice_valid_from"]) <= now
                    <= parse_iso(query["notice_valid_until"])
                    and parse_iso(notice["content"]["valid_from"])
                    <= parse_iso(query["notice_valid_from"])
                    and parse_iso(query["notice_valid_until"])
                    <= parse_iso(notice["content"]["valid_until"])
                )
        except NoApprovedVersion:
            pass
        gate_keys = (
            "signed", "signer_has_authority", "two_executors",
            "audit_notice_effective", "within_notice_period", "application_intact",
        )
        return {"valid": all(checks[k] for k in gate_keys), "checks": checks}

    # ---- 内部 ----------------------------------------------------

    def _require_currently_valid(self, query) -> None:
        validity = self.validity(query["case_id"], query["request_no"])
        if not validity["valid"]:
            raise AccountQueryGuardFailed(
                "查询程序当前无效或通知期限已届满", validity["checks"]
            )

    def _query(self, case_id: str, request_no: str) -> dict:
        row = self.storage.conn.execute(
            "SELECT * FROM account_queries WHERE case_id = ? AND request_no = ?",
            (case_id, request_no),
        ).fetchone()
        if row is None:
            raise NotFound(f"查询申请不存在：{request_no}")
        return dict(row)

    def _current_application_hash(self, query: dict) -> str:
        return self._application_hash({
            "request_no": query["request_no"],
            "case_id": query["case_id"],
            "applicant": query["applicant"],
            "target_subject": query["target_subject"],
            "account_scope": self.storage.loads(query["account_scope"]),
            "created_at": query["created_at"],
        })

    def _application_hash(self, snapshot: dict) -> str:
        import hashlib

        canonical = self.storage.dumps(snapshot)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _log_in_conn(self, conn, query_id, person_id, permission, action, detail, at) -> None:
        conn.execute(
            """
            INSERT INTO query_access_log
                (query_id, person_id, permission, action, detail, at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (query_id, person_id, permission, action, self.storage.dumps(detail), at),
        )
