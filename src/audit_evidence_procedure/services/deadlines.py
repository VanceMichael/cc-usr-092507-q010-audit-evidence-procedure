"""程序期限队列：意见期限、协助催办、复核工作。

- 期限行持久化在案件库中，进程重启后 :meth:`pump` 依据 ``due_at``
  继续执行，不依赖内存定时器；
- 协助催办采用阶梯：到期未关闭即催办并排入下一周期，直至记录关闭；
- 期限只允许被“完成/取消”，不允许删除——与全量留痕要求一致。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from ..clock import Clock
from ..constants import (
    DEFAULT_DEADLINE_DAYS,
    URGE_INTERVAL_DAYS,
    DEADLINE_KINDS,
    Permission,
)
from ..errors import DeadlineKindUnknown
from ..storage import Storage
from .cases import CaseService


class DeadlineService:
    def __init__(self, storage: Storage, clock: Clock, cases: CaseService):
        self.storage = storage
        self.clock = clock
        self.cases = cases

    def _due_at(self, kind: str, days: int | None = None) -> str:
        days = DEFAULT_DEADLINE_DAYS[kind] if days is None else days
        return (self.clock.now() + timedelta(days=days)).isoformat()

    def _create_in_conn(
        self,
        conn,
        case_id: str,
        kind: str,
        ref_type: str,
        ref_id: str,
        days: int | None = None,
    ) -> str:
        if kind not in DEADLINE_KINDS:
            raise DeadlineKindUnknown(f"未知期限类型：{kind}")
        deadline_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO deadlines
                (id, case_id, kind, ref_type, ref_id, due_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deadline_id, case_id, kind, ref_type, ref_id,
                self._due_at(kind, days), self.clock.now().isoformat(),
            ),
        )
        return deadline_id

    def schedule(
        self, case_id: str, kind: str, ref_type: str, ref_id: str,
        days: int | None = None,
    ) -> str:
        with self.storage.tx() as conn:
            return self._create_in_conn(conn, case_id, kind, ref_type, ref_id, days)

    def schedule_comment_period(self, case_id: str, days: int | None = None) -> str:
        return self.schedule(case_id, "comment", "case", case_id, days)

    def _complete_by_ref_in_conn(
        self, conn, case_id: str, ref_type: str, ref_id: str, at: str
    ) -> None:
        conn.execute(
            "UPDATE deadlines SET status = 'done', completed_at = ?"
            " WHERE case_id = ? AND ref_type = ? AND ref_id = ? AND status = 'pending'",
            (at, case_id, ref_type, ref_id),
        )

    def _cancel_by_ref_in_conn(
        self, conn, case_id: str, ref_type: str, ref_id: str
    ) -> None:
        conn.execute(
            "UPDATE deadlines SET status = 'cancelled'"
            " WHERE case_id = ? AND ref_type = ? AND ref_id = ? AND status = 'pending'",
            (case_id, ref_type, ref_id),
        )

    def complete(self, case_id: str, kind: str, ref_id: str) -> None:
        at = self.clock.now().isoformat()
        with self.storage.tx() as conn:
            conn.execute(
                "UPDATE deadlines SET status = 'done', completed_at = ?"
                " WHERE case_id = ? AND kind = ? AND ref_id = ? AND status = 'pending'",
                (at, case_id, kind, ref_id),
            )

    def pending(self, case_id: str | None = None) -> list[dict]:
        if case_id is None:
            rows = self.storage.conn.execute(
                "SELECT * FROM deadlines WHERE status = 'pending' ORDER BY due_at"
            ).fetchall()
        else:
            rows = self.storage.conn.execute(
                "SELECT * FROM deadlines WHERE status = 'pending' AND case_id = ?"
                " ORDER BY due_at",
                (case_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def pump(self) -> list[dict]:
        """推进所有到期期限。停机恢复后调用即可继续执行；可重复调用、幂等。"""

        now = self.clock.now().isoformat()
        rows = self.storage.conn.execute(
            "SELECT * FROM deadlines WHERE status = 'pending' AND due_at <= ?"
            " ORDER BY due_at",
            (now,),
        ).fetchall()
        actions: list[dict] = []
        for row in rows:
            item = dict(row)
            with self.cases.batch(item["case_id"]) as batch:
                at = batch.now_iso()
                if item["kind"] == "assistance_urge":
                    record = batch.conn.execute(
                        "SELECT id, status, urged_count FROM restricted_records"
                        " WHERE id = ? AND case_id = ?",
                        (item["ref_id"], item["case_id"]),
                    ).fetchone()
                    if record is None or record["status"] == "closed":
                        batch.conn.execute(
                            "UPDATE deadlines SET status = 'cancelled' WHERE id = ?",
                            (item["id"],),
                        )
                        continue
                    new_count = int(record["urged_count"]) + 1
                    batch.conn.execute(
                        "UPDATE restricted_records SET status = 'urged',"
                        " urged_count = ? WHERE id = ?",
                        (new_count, record["id"]),
                    )
                    batch.conn.execute(
                        "UPDATE deadlines SET status = 'done', urged_count = ?,"
                        " last_urged_at = ?, completed_at = ? WHERE id = ?",
                        (new_count, at, at, item["id"]),
                    )
                    batch.append_event(
                        "restricted.urge_due",
                        {"record_id": record["id"], "urge_count": new_count},
                        None,
                        Permission.RESTRICTED_READ_ALL.value,
                    )
                    # 排入下一催办周期；记录关闭时由取消逻辑终结
                    self._create_in_conn(
                        batch.conn, item["case_id"], "assistance_urge",
                        "assistance", record["id"], days=URGE_INTERVAL_DAYS,
                    )
                    actions.append(
                        {"kind": "assistance_urge", "case_id": item["case_id"],
                         "record_id": record["id"], "urge_count": new_count}
                    )
                elif item["kind"] == "comment":
                    batch.conn.execute(
                        "UPDATE deadlines SET status = 'done', completed_at = ?"
                        " WHERE id = ?",
                        (at, item["id"]),
                    )
                    batch.append_event(
                        "deadline.comment_due", {"deadline_id": item["id"]}, None
                    )
                    actions.append(
                        {"kind": "comment", "case_id": item["case_id"],
                         "deadline_id": item["id"]}
                    )
                elif item["kind"] == "review":
                    batch.conn.execute(
                        "UPDATE deadlines SET status = 'done', completed_at = ?"
                        " WHERE id = ?",
                        (at, item["id"]),
                    )
                    batch.append_event(
                        "deadline.review_due",
                        {"deadline_id": item["id"], "ref_id": item["ref_id"]},
                        None,
                        Permission.SCOPE_REVIEW.value,
                    )
                    actions.append(
                        {"kind": "review", "case_id": item["case_id"],
                         "deadline_id": item["id"]}
                    )
        return actions
