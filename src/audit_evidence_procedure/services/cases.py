"""统一案件脉络：财政收支、国有资源、重大公共工程进入同一案件。

案件以单调水位（已提交事件序号）支持乐观并发；时间线按查看者的
有效权限过滤，普通项目权限无法看到金融账户查询材料类事件。
"""

from __future__ import annotations

import sys

from ..clock import Clock
from ..constants import CASE_CATEGORIES
from ..errors import NotFound, StaleCaseWatermark
from ..storage import Storage
from .access import AccessService


class CaseService:
    def __init__(self, storage: Storage, clock: Clock, access: AccessService):
        self.storage = storage
        self.clock = clock
        self.access = access

    def open_case(self, case_id: str, category: str, title: str, opened_by: str) -> dict:
        if category not in CASE_CATEGORIES:
            raise ValueError(f"案件类别必须属于 {CASE_CATEGORIES}")
        self.access.person(opened_by)
        at = self.clock.now().isoformat()
        with self.storage.tx() as conn:
            conn.execute(
                "INSERT INTO cases (id, category, title, opened_by, opened_at, watermark)"
                " VALUES (?, ?, ?, ?, ?, 0)",
                (case_id, category, title, opened_by, at),
            )
        return self.case(case_id)

    def case(self, case_id: str) -> dict:
        row = self.storage.conn.execute(
            "SELECT id, category, title, opened_by, opened_at, watermark FROM cases"
            " WHERE id = ?",
            (case_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"案件不存在：{case_id}")
        return dict(row)

    def require_case(self, case_id: str) -> dict:
        return self.case(case_id)

    def current_watermark(self, case_id: str) -> int:
        return self.storage.watermark(self.storage.conn, case_id)

    # ---- 原子批处理 ----------------------------------------------

    def batch(self, case_id: str, expected_watermark: int | None = None):
        """返回一个批处理上下文。

        - ``expected_watermark`` 非空时，进入事务即校验案件水位；
        - 取证、封存、范围变更等不同决定必须挂在同一个批内，
          提交时统一推进水位；
        - 任一操作失败 → 整个事务回滚：不会出现“材料已受限而决定尚未生效”。
        """

        return _CaseBatch(self, case_id, expected_watermark)

    # ---- 统一脉络时间线 ------------------------------------------

    def timeline(self, case_id: str, viewer_id: str) -> list[dict]:
        """按查看者权限过滤的案件事件脉络。"""

        self.require_case(case_id)
        perms = self.access.effective_permissions(viewer_id, case_id)
        perm_values = {p.value for p in perms}
        rows = self.storage.conn.execute(
            "SELECT seq, type, payload, actor, required_permission, at"
            " FROM events WHERE case_id = ? ORDER BY seq",
            (case_id,),
        ).fetchall()
        result = []
        for row in rows:
            required = row["required_permission"]
            if required is not None and required not in perm_values:
                # 不暴露事件类型本身，避免侧写受限活动
                continue
            result.append(
                {
                    "seq": row["seq"],
                    "type": row["type"],
                    "payload": self.storage.loads(row["payload"]),
                    "actor": row["actor"],
                    "at": row["at"],
                }
            )
        return result


class _CaseBatch:
    """单事务批处理；同一批内的事件连续占用水位序号。"""

    def __init__(self, service: CaseService, case_id: str, expected_watermark: int | None):
        self.service = service
        self.case_id = case_id
        self.expected_watermark = expected_watermark
        self._cm = None
        self.conn = None
        self.base_watermark: int | None = None
        self.appended = 0

    def __enter__(self):
        self._cm = self.service.storage.tx()
        self.conn = self._cm.__enter__()
        try:
            current = self.service.storage.watermark(self.conn, self.case_id)
            if self.expected_watermark is not None and current != self.expected_watermark:
                raise StaleCaseWatermark(
                    f"案件水位已变化：期望 {self.expected_watermark}，当前 {current}；"
                    "请基于最新水位重新提交整批操作"
                )
        except BaseException:
            # 水位校验失败也要终结已开启的写事务，避免事务悬挂
            self._cm.__exit__(*sys.exc_info())
            raise
        self.base_watermark = current
        return self

    def append_event(
        self, event_type: str, payload: dict, actor: str | None,
        required_permission: str | None = None,
    ) -> int:
        seq = self.service.storage.append_event(
            self.conn, self.case_id, event_type, payload, actor,
            required_permission, self.service.clock.now().isoformat(),
        )
        self.appended += 1
        return seq

    def now_iso(self) -> str:
        return self.service.clock.now().isoformat()

    def __exit__(self, exc_type, exc, tb):
        return self._cm.__exit__(exc_type, exc, tb)
