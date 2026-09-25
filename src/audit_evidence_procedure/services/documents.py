"""年度计划、审计通知、事项范围、审计组成员：只按批准版本生效。

约束：

- 草稿不生效；任何读取“当前生效内容”的调用都只返回 approved 版本；
- 临时扩大审计事项范围必须在版本上写明法定依据；
- 扩范围版本须由未参与提出的人复核（复核回避名单 = 提出方参与人），
  复核通过后方可批准；
- 驳回只改变版本状态，旧批准版本继续有效。
"""

from __future__ import annotations

import uuid

from ..constants import DeadlineKind, Permission
from ..errors import (
    InvalidDocument,
    NoApprovedVersion,
    ReviewerParticipated,
)
from ..storage import Storage
from .access import AccessService
from .cases import CaseService
from .deadlines import DeadlineService


class DocumentService:
    def __init__(
        self,
        storage: Storage,
        access: AccessService,
        cases: CaseService,
        deadlines: DeadlineService,
    ):
        self.storage = storage
        self.access = access
        self.cases = cases
        self.deadlines = deadlines

    # ---- 起草新版本 ----------------------------------------------

    def draft(
        self,
        case_id: str,
        kind: str,
        content: dict,
        proposed_by: str,
        participants: list[str] | None = None,
        legal_basis: str | None = None,
    ) -> str:
        """起草一个新版本。返回版本 ID。

        临时扩大范围（kind=scope 且声明 ``content["temporary_expansion"]``）
        必须提供 ``legal_basis``（法定依据）。
        """

        self._validate_content(kind, content)
        self.access.require_permission(proposed_by, case_id, Permission.DOC_DRAFT)
        participants = list(dict.fromkeys(participants or [proposed_by]))
        if proposed_by not in participants:
            participants.append(proposed_by)

        if kind == "scope" and content.get("temporary_expansion"):
            if not legal_basis or not legal_basis.strip():
                raise InvalidDocument("临时扩大审计事项范围必须说明法定依据")

        version_id = uuid.uuid4().hex
        at = self.cases.clock.now().isoformat()
        with self.storage.tx() as conn:
            row = conn.execute(
                "SELECT current_version FROM docs WHERE case_id = ? AND kind = ?",
                (case_id, kind),
            ).fetchone()
            next_version = (row["current_version"] if row else 0) + 1
            if row is None:
                conn.execute(
                    "INSERT INTO docs (case_id, kind, current_version, current_id)"
                    " VALUES (?, ?, 0, NULL)",
                    (case_id, kind),
                )
            conn.execute(
                """
                INSERT INTO doc_versions
                    (id, case_id, kind, version, status, content, basis, participants,
                     proposed_by, proposed_at)
                VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)
                """,
                (
                    version_id, case_id, kind, next_version,
                    self.storage.dumps(content), legal_basis,
                    self.storage.dumps(participants), proposed_by, at,
                ),
            )
        return version_id

    # ---- 复核（扩范围强制；其他文书可选走复核） ------------------

    def review(
        self, case_id: str, version_id: str, reviewer_id: str,
        approve_review: bool, note: str = "",
    ) -> None:
        version = self._version(case_id, version_id)
        self.access.require_permission(reviewer_id, case_id, Permission.SCOPE_REVIEW)
        participants = self.storage.loads(version["participants"])
        if reviewer_id in participants:
            raise ReviewerParticipated(
                "复核人曾参与该事项的提出，不得复核：须由未参与提出的人复核"
            )
        if version["status"] != "draft":
            raise InvalidDocument(f"版本当前状态为 {version['status']}，不可复核")

        at = self.cases.clock.now().isoformat()
        with self.storage.tx() as conn:
            conn.execute(
                "UPDATE doc_versions SET reviewed_by = ?, reviewed_at = ?,"
                " review_note = ?, status = CASE WHEN ? = 1 THEN 'reviewed' ELSE 'rejected' END"
                " WHERE id = ? AND case_id = ?",
                (reviewer_id, at, note, 1 if approve_review else 0,
                 version_id, case_id),
            )
            if not approve_review:
                # 驳回即终结该版本，无需再安排批准
                return
            if version["kind"] == "scope" and self.storage.loads(version["content"]).get(
                "temporary_expansion"
            ):
                # 扩范围复核工作进入期限队列，停机后继续催办至批准完成
                self.deadlines._create_in_conn(
                    conn, case_id, DeadlineKind.REVIEW.value,
                    "scope_version", version_id,
                )

    # ---- 批准生效 ------------------------------------------------

    def approve(
        self, case_id: str, version_id: str, approver_id: str
    ) -> int:
        """批准版本并使其成为唯一当前生效版本，返回版本号。"""

        version = self._version(case_id, version_id)
        # 批准权统一属于机关负责人；复核人员只负责复核，实现复核与批准分离。
        self.access.require_permission(approver_id, case_id, Permission.PLAN_APPROVE)

        content = self.storage.loads(version["content"])
        needs_review = (
            version["kind"] == "scope" and content.get("temporary_expansion")
        )
        if needs_review:
            if version["status"] != "reviewed":
                raise InvalidDocument(
                    "临时扩大范围版本须先经未参与提出的人复核通过，方可批准"
                )
            if version["reviewed_by"] == approver_id:
                raise ReviewerParticipated("批准人不得与复核人为同一人")
        elif version["status"] not in ("draft", "reviewed"):
            raise InvalidDocument(f"版本状态 {version['status']} 不可批准")

        at = self.cases.clock.now().isoformat()
        with self.cases.batch(case_id) as batch:
            self._approve_in_batch(batch, version, approver_id, at)
        return int(version["version"])

    def _approve_in_batch(self, batch, version: dict, approver_id: str, at: str) -> int:
        """在既有批处理事务内批准版本（供取证/封存/范围变更联合提交复用）。"""

        case_id = version["case_id"]
        content = self.storage.loads(version["content"])
        needs_review = (
            version["kind"] == "scope" and content.get("temporary_expansion")
        )
        if needs_review:
            if version["status"] != "reviewed":
                raise InvalidDocument(
                    "临时扩大范围版本须先经未参与提出的人复核通过，方可批准"
                )
            if version["reviewed_by"] == approver_id:
                raise ReviewerParticipated("批准人不得与复核人为同一人")
        elif version["status"] not in ("draft", "reviewed"):
            raise InvalidDocument(f"版本状态 {version['status']} 不可批准")

        batch.conn.execute(
            "UPDATE doc_versions SET status = 'approved', approved_by = ?,"
            " approved_at = ? WHERE id = ? AND case_id = ?",
            (approver_id, at, version["id"], case_id),
        )
        batch.conn.execute(
            "UPDATE docs SET current_version = ?, current_id = ?"
            " WHERE case_id = ? AND kind = ?",
            (version["version"], version["id"], case_id, version["kind"]),
        )
        batch.append_event(
            "document.approved",
            {"kind": version["kind"], "version": version["version"],
             "version_id": version["id"], "legal_basis": version["basis"]},
            approver_id,
        )
        if needs_review:
            self.deadlines._complete_by_ref_in_conn(
                batch.conn, case_id, "scope_version", version["id"], at
            )
        return int(version["version"])

    # ---- 只读查询 ------------------------------------------------

    def effective(self, case_id: str, kind: str) -> dict:
        """返回当前唯一生效（approved）版本；没有则拒绝，绝不回退用草稿。"""

        row = self.storage.conn.execute(
            """
            SELECT dv.id, dv.version, dv.content, dv.approved_by, dv.approved_at,
                   dv.basis, dv.reviewed_by
            FROM docs d JOIN doc_versions dv ON dv.id = d.current_id
            WHERE d.case_id = ? AND d.kind = ?
            """,
            (case_id, kind),
        ).fetchone()
        if row is None:
            raise NoApprovedVersion(f"{kind} 尚无批准生效版本")
        return {
            "version_id": row["id"],
            "version": row["version"],
            "content": self.storage.loads(row["content"]),
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
            "legal_basis": row["basis"],
            "reviewed_by": row["reviewed_by"],
        }

    def is_effective(self, case_id: str, kind: str) -> bool:
        try:
            self.effective(case_id, kind)
            return True
        except NoApprovedVersion:
            return False

    def list_versions(self, case_id: str, kind: str) -> list[dict]:
        rows = self.storage.conn.execute(
            "SELECT id, version, status, basis, proposed_by, reviewed_by, approved_by,"
            " proposed_at, approved_at FROM doc_versions"
            " WHERE case_id = ? AND kind = ? ORDER BY version",
            (case_id, kind),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 内部 ----------------------------------------------------

    def _version(self, case_id: str, version_id: str) -> dict:
        row = self.storage.conn.execute(
            "SELECT * FROM doc_versions WHERE id = ? AND case_id = ?",
            (version_id, case_id),
        ).fetchone()
        if row is None:
            from ..errors import NotFound

            raise NotFound(f"文书版本不存在：{version_id}")
        return dict(row)

    @staticmethod
    def _validate_content(kind: str, content: dict) -> None:
        if not isinstance(content, dict) or not content:
            raise InvalidDocument("文书内容不能为空")
        required = {
            "annual_plan": ("year", "items"),
            "audit_notice": ("notice_no", "audited_unit", "valid_from", "valid_until"),
            "scope": ("items",),
            "team": ("members",),
        }[kind]
        missing = [f for f in required if f not in content]
        if missing:
            raise InvalidDocument(f"{kind} 内容缺少字段：{missing}")
        if kind == "team" and not content["members"]:
            raise InvalidDocument("审计组成员不能为空")
