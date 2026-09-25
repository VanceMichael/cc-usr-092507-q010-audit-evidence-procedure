"""审计取证与程序控制服务门面。

组装访问控制、案件脉络、批准文书、电子取证、账户查询授权、
受限记录与期限队列七个子服务，并提供：

- :meth:`submit_case_batch`：取证、封存、范围变更基于同一案件水位
  的联合原子提交；
- :meth:`recover`：停机恢复后重建期限处理（意见期限、协助催办、复核工作）。
"""

from __future__ import annotations

from pathlib import Path

from .clock import Clock, SystemClock
from .constants import Permission
from .errors import DomainError, InvalidDocument, NotFound, StaleCaseWatermark
from .storage import Storage
from .services.access import AccessService
from .services.account_queries import AccountQueryService
from .services.cases import CaseService
from .services.deadlines import DeadlineService
from .services.documents import DocumentService
from .services.evidence import EvidenceService
from .services.restricted import RestrictedRecordService


class AuditEvidenceProcedureService:
    def __init__(self, database: str | Path = ":memory:", clock: Clock | None = None):
        self.clock = clock or SystemClock()
        self.storage = Storage(database)
        self.access = AccessService(self.storage)
        self.deadlines = DeadlineService(self.storage, self.clock, cases=None)  # type: ignore[arg-type]
        self.cases = CaseService(self.storage, self.clock, self.access)
        # 期限服务需要案件服务（联合批处理），补齐循环依赖
        self.deadlines.cases = self.cases
        self.documents = DocumentService(
            self.storage, self.access, self.cases, self.deadlines
        )
        self.evidence = EvidenceService(self.storage, self.access, self.cases)
        self.account_queries = AccountQueryService(
            self.storage, self.access, self.cases, self.documents, self.evidence
        )
        self.restricted = RestrictedRecordService(
            self.storage, self.access, self.cases, self.deadlines
        )

    def close(self) -> None:
        self.storage.close()

    def __enter__(self) -> "AuditEvidenceProcedureService":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 人员与案件快捷入口 --------------------------------------

    def register_person(self, person_id: str, name: str, role: str) -> str:
        return self.access.register_person(person_id, name, role)

    def open_case(self, case_id: str, category: str, title: str, opened_by: str) -> dict:
        return self.cases.open_case(case_id, category, title, opened_by)

    def grant_case_permission(self, case_id, person_id, permission, granted_by):
        return self.access.grant_case_permission(
            case_id, person_id, permission, granted_by, self.clock.now().isoformat()
        )

    def open_comment_period(self, case_id: str, days: int | None = None) -> str:
        return self.deadlines.schedule_comment_period(case_id, days)

    # ---- 联合原子批处理 ------------------------------------------

    def submit_case_batch(
        self, case_id: str, operations: list[dict],
        expected_watermark: int | None = None,
    ) -> dict:
        """把多个决定作为同一案件水位下的一个事务提交。

        支持的操作：

        - ``{"op": "register_delivery", ...}`` 参数同
          :meth:`EvidenceService.register_delivery`；
        - ``{"op": "seal_delivery", "delivery_no", "sealed_by"}``；
        - ``{"op": "approve_scope", "version_id", "approver_id"}``
          （批准一个已经复核通过的临时扩范围版本）。

        任一操作失败 → 整批回滚，不会留下半成品状态。
        异文交付属于需要单独成案的争议，不得混入联合批处理。
        """

        results: list[dict] = []
        try:
            with self.cases.batch(case_id, expected_watermark) as batch:
                base = batch.base_watermark
                for operation in operations:
                    kind = operation.get("op")
                    if kind == "register_delivery":
                        self.access.require_permission(
                            operation["delivered_by"], case_id,
                            Permission.EVIDENCE_DELIVER,
                        )
                        if not operation["content_hash"] or not operation["source"] \
                                or not operation["commitment"]:
                            raise ValueError("摘要、来源、真实性承诺均不能为空")
                        result = self.evidence._register_in_batch(
                            batch,
                            operation["delivery_no"],
                            operation["content_hash"],
                            operation["source"],
                            operation["commitment"],
                            operation["delivered_by"],
                            operation.get("note", ""),
                        )
                        if result["status"] == "disputed":
                            raise DomainError(
                                "异文交付必须单独转入证据争议，不能与其他决定同批提交"
                            )
                    elif kind == "seal_delivery":
                        self.access.require_permission(
                            operation["sealed_by"], case_id, Permission.EVIDENCE_SEAL
                        )
                        row = batch.conn.execute(
                            "SELECT id FROM deliveries WHERE case_id = ? AND delivery_no = ?",
                            (case_id, operation["delivery_no"]),
                        ).fetchone()
                        if row is None:
                            raise NotFound(f"交付号不存在：{operation['delivery_no']}")
                        seal_id = self.evidence.seal_ref(
                            batch, "delivery", row["id"],
                            operation["sealed_by"], Permission.EVIDENCE_READ,
                        )
                        result = {"seal_id": seal_id, "delivery_no": operation["delivery_no"]}
                    elif kind == "approve_scope":
                        version = self.documents._version(case_id, operation["version_id"])
                        if version["kind"] != "scope":
                            raise InvalidDocument("联合批处理仅支持范围（scope）版本批准")
                        self.access.require_permission(
                            operation["approver_id"], case_id, Permission.PLAN_APPROVE,
                        )
                        version_no = self.documents._approve_in_batch(
                            batch, version, operation["approver_id"], batch.now_iso()
                        )
                        result = {"scope_version": version_no}
                    else:
                        raise ValueError(f"未知批处理操作：{kind!r}")
                    results.append(result)
        except StaleCaseWatermark:
            raise
        return {
            "case_id": case_id,
            "watermark_before": base,
            "watermark_after": self.cases.current_watermark(case_id),
            "results": results,
        }

    # ---- 停机恢复 ------------------------------------------------

    def recover(self) -> dict:
        """进程重启后调用：推进全部到期期限，返回本次执行的动作。

        - 意见期限：到期即闭合并留痕；
        - 协助催办：对未关闭的跨机关协助记录执行催办并排入下一周期；
        - 复核工作：到期留痕提醒。
        重复调用幂等，状态全部来自持久化存储。
        """

        actions = self.deadlines.pump()
        return {"actions": actions, "pending": self.deadlines.pending()}
