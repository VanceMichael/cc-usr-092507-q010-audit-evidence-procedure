"""电子资料交付、证据争议与封存。

事实规则：

- 每个交付号首交即固定事实：摘要、来源、承诺、到达时间；
- 相同交付号重试且摘要一致 → 沿用原事实，仅登记重试轨迹，
  原到达时间等事实不被覆盖；
- 相同交付号但摘要不一致（异文）→ 原交付置争议态，异文转入
  证据争议队列，绝不静默覆盖；
- 封存使材料受限；重复封存被拒绝。所有动作以事件形式进入案件水位。
"""

from __future__ import annotations

import uuid

from ..constants import Permission
from ..errors import (
    DuplicateConflict,
    MaterialAlreadyRestricted,
    NotFound,
)
from ..storage import Storage
from .access import AccessService
from .cases import CaseService

REF_DELIVERY = "delivery"
REF_ACCOUNT_PACKAGE = "account_package"


class EvidenceService:
    def __init__(self, storage: Storage, access: AccessService, cases: CaseService):
        self.storage = storage
        self.access = access
        self.cases = cases

    # ---- 交付登记（支持独立事务与联合批处理） --------------------

    def register_delivery(
        self,
        case_id: str,
        delivery_no: str,
        content_hash: str,
        source: str,
        commitment: str,
        delivered_by: str,
        note: str = "",
    ) -> dict:
        """登记被审计单位分批交付的一批电子资料。

        返回原事实（首交）或重试判定结果。异文时先提交争议记录，
        再抛出 :class:`DuplicateConflict`。
        """

        self.access.require_permission(delivered_by, case_id, Permission.EVIDENCE_DELIVER)
        if not content_hash or not source or not commitment:
            raise ValueError("摘要、来源、真实性承诺均不能为空")
        with self.cases.batch(case_id) as batch:
            result = self._register_in_batch(
                batch, delivery_no, content_hash, source, commitment, delivered_by, note
            )
        if result["status"] == "disputed":
            raise DuplicateConflict(
                f"交付号 {delivery_no} 出现异文，已转入证据争议 {result['dispute_id']}"
            )
        return result

    def _register_in_batch(
        self, batch, delivery_no, content_hash, source, commitment, delivered_by, note
    ) -> dict:
        conn = batch.conn
        case_id = batch.case_id
        at = batch.now_iso()
        existing = conn.execute(
            "SELECT * FROM deliveries WHERE case_id = ? AND delivery_no = ?",
            (case_id, delivery_no),
        ).fetchone()

        if existing is None:
            delivery_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO deliveries
                    (id, case_id, delivery_no, content_hash, source, commitment,
                     arrived_at, retry_count, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'normal')
                """,
                (delivery_id, case_id, delivery_no, content_hash, source,
                 commitment, at),
            )
            batch.append_event(
                "evidence.delivered",
                {"delivery_id": delivery_id, "delivery_no": delivery_no,
                 "content_hash": content_hash, "source": source,
                 "arrived_at": at},
                delivered_by,
                Permission.EVIDENCE_READ.value,
            )
            return {
                "status": "first",
                "delivery_id": delivery_id,
                "delivery_no": delivery_no,
                "content_hash": content_hash,
                "source": source,
                "commitment": commitment,
                "arrived_at": at,
            }

        # 同号重试：以首交摘要为基准判定同文/异文
        same = existing["content_hash"] == content_hash
        conn.execute(
            """
            INSERT INTO delivery_retries
                (id, delivery_id, content_hash, source, commitment, arrived_at, same_fact)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex, existing["id"], content_hash, source,
             commitment, at, 1 if same else 0),
        )
        if same:
            conn.execute(
                "UPDATE deliveries SET retry_count = retry_count + 1 WHERE id = ?",
                (existing["id"],),
            )
            batch.append_event(
                "evidence.delivery_retried_same_fact",
                {"delivery_id": existing["id"], "delivery_no": delivery_no,
                 "retried_at": at},
                delivered_by,
                Permission.EVIDENCE_READ.value,
            )
            # 沿用原事实：返回首交的摘要/来源/承诺/到达时间
            return {
                "status": "replayed_same_fact",
                "delivery_id": existing["id"],
                "delivery_no": delivery_no,
                "content_hash": existing["content_hash"],
                "source": existing["source"],
                "commitment": existing["commitment"],
                "arrived_at": existing["arrived_at"],
                "retry_arrived_at": at,
            }

        # 异文：转入证据争议，原事实保持不变
        dispute_id = uuid.uuid4().hex
        conn.execute(
            "UPDATE deliveries SET status = 'disputed', retry_count = retry_count + 1"
            " WHERE id = ?",
            (existing["id"],),
        )
        conn.execute(
            """
            INSERT INTO disputes
                (id, case_id, delivery_no, original_hash, variant_hash,
                 variant_source, note, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (dispute_id, case_id, delivery_no, existing["content_hash"],
             content_hash, source, note, at),
        )
        batch.append_event(
            "evidence.dispute_opened",
            {"delivery_id": existing["id"], "delivery_no": delivery_no,
             "dispute_id": dispute_id, "original_hash": existing["content_hash"],
             "variant_hash": content_hash},
            delivered_by,
            Permission.EVIDENCE_READ.value,
        )
        return {
            "status": "disputed",
            "delivery_id": existing["id"],
            "dispute_id": dispute_id,
            "original": {
                "content_hash": existing["content_hash"],
                "source": existing["source"],
                "commitment": existing["commitment"],
                "arrived_at": existing["arrived_at"],
            },
        }

    # ---- 读取交付事实 --------------------------------------------

    def get_delivery(self, case_id: str, delivery_no: str, viewer_id: str) -> dict:
        self.access.require_permission(viewer_id, case_id, Permission.EVIDENCE_READ)
        row = self.storage.conn.execute(
            "SELECT * FROM deliveries WHERE case_id = ? AND delivery_no = ?",
            (case_id, delivery_no),
        ).fetchone()
        if row is None:
            raise NotFound(f"交付号不存在：{delivery_no}")
        return dict(row)

    def list_retries(self, case_id: str, delivery_no: str) -> list[dict]:
        rows = self.storage.conn.execute(
            """
            SELECT dr.content_hash, dr.source, dr.commitment, dr.arrived_at, dr.same_fact
            FROM delivery_retries dr JOIN deliveries d ON d.id = dr.delivery_id
            WHERE d.case_id = ? AND d.delivery_no = ? ORDER BY dr.arrived_at
            """,
            (case_id, delivery_no),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 争议处理 ------------------------------------------------

    def open_disputes(self, case_id: str) -> list[dict]:
        rows = self.storage.conn.execute(
            "SELECT * FROM disputes WHERE case_id = ? AND status = 'open'"
            " ORDER BY created_at",
            (case_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_dispute(self, case_id: str, dispute_id: str, resolver_id: str,
                        resolution_note: str) -> None:
        self.access.require_permission(resolver_id, case_id, Permission.EVIDENCE_COLLECT)
        at = self.cases.clock.now().isoformat()
        with self.cases.batch(case_id) as batch:
            cur = batch.conn.execute(
                "UPDATE disputes SET status = 'resolved' WHERE id = ? AND case_id = ?"
                " AND status = 'open'",
                (dispute_id, case_id),
            )
            if cur.rowcount == 0:
                raise NotFound(f"未决争议不存在：{dispute_id}")
            batch.append_event(
                "evidence.dispute_resolved",
                {"dispute_id": dispute_id, "note": resolution_note},
                resolver_id,
                Permission.EVIDENCE_READ.value,
            )

    # ---- 封存（受限） --------------------------------------------

    def seal_delivery(self, case_id: str, delivery_no: str, sealed_by: str) -> str:
        self.access.require_permission(sealed_by, case_id, Permission.EVIDENCE_SEAL)
        with self.cases.batch(case_id) as batch:
            delivery = batch.conn.execute(
                "SELECT id FROM deliveries WHERE case_id = ? AND delivery_no = ?",
                (case_id, delivery_no),
            ).fetchone()
            if delivery is None:
                raise NotFound(f"交付号不存在：{delivery_no}")
            return self._seal_in_batch(
                batch, REF_DELIVERY, delivery["id"], sealed_by,
                Permission.EVIDENCE_READ,
            )

    def _seal_in_batch(self, batch, ref_type: str, ref_id: str, sealed_by: str,
                       permission: Permission) -> str:
        conn = batch.conn
        case_id = batch.case_id
        existing = conn.execute(
            "SELECT id FROM sealed_items WHERE case_id = ? AND ref_type = ? AND ref_id = ?",
            (case_id, ref_type, ref_id),
        ).fetchone()
        if existing is not None:
            raise MaterialAlreadyRestricted(f"材料已封存：{ref_type}/{ref_id}")
        seal_id = uuid.uuid4().hex
        at = batch.now_iso()
        conn.execute(
            "INSERT INTO sealed_items (id, case_id, ref_type, ref_id, sealed_by, sealed_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (seal_id, case_id, ref_type, ref_id, sealed_by, at),
        )
        batch.append_event(
            "evidence.sealed",
            {"seal_id": seal_id, "ref_type": ref_type, "ref_id": ref_id,
             "sealed_at": at},
            sealed_by,
            permission.value,
        )
        return seal_id

    def is_sealed(self, case_id: str, ref_type: str, ref_id: str) -> bool:
        row = self.storage.conn.execute(
            "SELECT 1 FROM sealed_items WHERE case_id = ? AND ref_type = ? AND ref_id = ?",
            (case_id, ref_type, ref_id),
        ).fetchone()
        return row is not None

    def seal_ref(self, batch, ref_type: str, ref_id: str, sealed_by: str,
                 permission: Permission) -> str:
        """在给定联合批处理内封存任一材料引用（供账户材料登记复用）。"""

        return self._seal_in_batch(batch, ref_type, ref_id, sealed_by, permission)
