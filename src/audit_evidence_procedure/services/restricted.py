"""受限记录：打探干预、拒绝配合、跨机关协助。

三类记录：

- 各自独立的队列与访问权限，互不可见、互不相混；
- 任何更正都“新增版本”，原版本永久保留，无更新/删除路径；
- 跨机关协助记录创建即进入协助催办期限队列，到期催办、循环催办
  直至关闭（催办阶梯见 :class:`DeadlineService`）。
"""

from __future__ import annotations

import uuid

from ..constants import RESTRICTED_QUEUES, DeadlineKind, Permission
from ..errors import NotFound, PermissionDenied
from ..storage import Storage
from .access import AccessService
from .cases import CaseService
from .deadlines import DeadlineService

_WRITE_PERMISSION = {
    "interference": Permission.RESTRICTED_INTERFERENCE_WRITE,
    "refusal": Permission.RESTRICTED_REFUSAL_WRITE,
    "assistance": Permission.RESTRICTED_ASSISTANCE_WRITE,
}

# 队列访问权：打探干预最敏感（涉及对办案活动的外部影响），
# 只有机关负责人能查阅队列；经办人员可以登记/更正，但不能浏览队列。
_READ_PERMISSION = {
    "interference": Permission.RESTRICTED_READ_ALL,
    "refusal": Permission.RESTRICTED_REFUSAL_WRITE,
    "assistance": Permission.RESTRICTED_ASSISTANCE_WRITE,
}


class RestrictedRecordService:
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

    def create(
        self, case_id: str, kind: str, content: dict, created_by: str,
        urge_days: int | None = None,
    ) -> str:
        if kind not in RESTRICTED_QUEUES:
            raise ValueError(f"受限记录类别必须属于 {RESTRICTED_QUEUES}")
        if not isinstance(content, dict) or not content:
            raise ValueError("记录内容不能为空")
        self.access.require_permission(created_by, case_id, _WRITE_PERMISSION[kind])

        record_id = uuid.uuid4().hex
        version_id = uuid.uuid4().hex
        at = self.cases.clock.now().isoformat()
        with self.cases.batch(case_id) as batch:
            deadline_id = None
            if kind == "assistance":
                deadline_id = self.deadlines._create_in_conn(
                    batch.conn, case_id, DeadlineKind.ASSISTANCE_URGE.value,
                    "assistance", record_id, urge_days,
                )
            batch.conn.execute(
                """
                INSERT INTO restricted_records
                    (id, case_id, kind, current_version, status, urged_count,
                     created_by, created_at, deadline_id)
                VALUES (?, ?, ?, 1, 'open', 0, ?, ?, ?)
                """,
                (record_id, case_id, kind, created_by, at, deadline_id),
            )
            batch.conn.execute(
                """
                INSERT INTO restricted_versions
                    (id, record_id, version, content, correction_reason,
                     created_by, created_at)
                VALUES (?, ?, 1, ?, NULL, ?, ?)
                """,
                (version_id, record_id, self.storage.dumps(content), created_by, at),
            )
            batch.append_event(
                "restricted.created",
                {"record_id": record_id, "kind": kind, "deadline_id": deadline_id},
                created_by,
                Permission.RESTRICTED_READ_ALL.value,
            )
        return record_id

    def correct(
        self, case_id: str, record_id: str, content: dict, corrected_by: str,
        reason: str,
    ) -> int:
        """更正记录：新增一个版本并前推当前版本号；原版本不删除。"""

        if not reason or not reason.strip():
            raise ValueError("更正必须说明理由")
        record = self._record(case_id, record_id)
        kind = record["kind"]
        self.access.require_permission(corrected_by, case_id, _WRITE_PERMISSION[kind])
        if record["status"] == "closed":
            raise PermissionDenied("已关闭记录不得更正，如需更正应另行立案记录")

        at = self.cases.clock.now().isoformat()
        with self.cases.batch(case_id) as batch:
            next_version = int(record["current_version"]) + 1
            version_id = uuid.uuid4().hex
            batch.conn.execute(
                """
                INSERT INTO restricted_versions
                    (id, record_id, version, content, correction_reason,
                     created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (version_id, record_id, next_version, self.storage.dumps(content),
                 reason, corrected_by, at),
            )
            batch.conn.execute(
                "UPDATE restricted_records SET current_version = ? WHERE id = ?",
                (next_version, record_id),
            )
            batch.append_event(
                "restricted.corrected",
                {"record_id": record_id, "kind": kind,
                 "new_version": next_version, "reason": reason},
                corrected_by,
                Permission.RESTRICTED_READ_ALL.value,
            )
        return next_version

    def close(self, case_id: str, record_id: str, closed_by: str) -> None:
        record = self._record(case_id, record_id)
        self.access.require_permission(closed_by, case_id, _WRITE_PERMISSION[record["kind"]])
        at = self.cases.clock.now().isoformat()
        with self.cases.batch(case_id) as batch:
            batch.conn.execute(
                "UPDATE restricted_records SET status = 'closed' WHERE id = ?",
                (record_id,),
            )
            if record["deadline_id"]:
                self.deadlines._cancel_by_ref_in_conn(
                    batch.conn, case_id, "assistance", record_id
                )
            batch.append_event(
                "restricted.closed",
                {"record_id": record_id, "kind": record["kind"]},
                closed_by,
                Permission.RESTRICTED_READ_ALL.value,
            )

    # ---- 只读：三类队列严格分离 ----------------------------------

    def queue(self, case_id: str, kind: str, viewer_id: str) -> list[dict]:
        if kind not in RESTRICTED_QUEUES:
            raise ValueError(f"受限记录类别必须属于 {RESTRICTED_QUEUES}")
        # 三类队列各有独立访问权，互不可见
        self.access.require_permission(viewer_id, case_id, _READ_PERMISSION[kind])
        rows = self.storage.conn.execute(
            "SELECT * FROM restricted_records WHERE case_id = ? AND kind = ?"
            " ORDER BY created_at",
            (case_id, kind),
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, case_id: str, record_id: str, viewer_id: str) -> dict:
        record = self._record(case_id, record_id)
        self.access.require_permission(viewer_id, case_id, _READ_PERMISSION[record["kind"]])
        versions = [
            dict(r) for r in self.storage.conn.execute(
                "SELECT version, content, correction_reason, created_by, created_at"
                " FROM restricted_versions WHERE record_id = ? ORDER BY version",
                (record_id,),
            ).fetchall()
        ]
        for item in versions:
            item["content"] = self.storage.loads(item["content"])
        result = dict(record)
        result["versions"] = versions
        return result

    def _record(self, case_id: str, record_id: str) -> dict:
        row = self.storage.conn.execute(
            "SELECT * FROM restricted_records WHERE case_id = ? AND id = ?",
            (case_id, record_id),
        ).fetchone()
        if row is None:
            raise NotFound(f"受限记录不存在：{record_id}")
        return dict(row)
