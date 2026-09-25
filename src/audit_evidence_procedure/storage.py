"""SQLite 存储层。

设计要点：

- 全部业务状态与事件日志持久化在同一数据库，停机后重建期限队列；
- 写入通过 ``BEGIN IMMEDIATE`` 串行化，案件以单调递增的 ``watermark``
  （= 已提交事件序号）作为乐观并发水位；
- 服务层只在本模块写 SQL，便于统一审计列与不可变约束；
- 受限记录的“更正”永远是插入新版本，不存在 UPDATE/DELETE 原事实的路径。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    opened_by TEXT,
    opened_at TEXT NOT NULL,
    watermark INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS case_grants (
    case_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    permission TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY (case_id, person_id, permission)
);

CREATE TABLE IF NOT EXISTS docs (
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 0,
    current_id TEXT,
    PRIMARY KEY (case_id, kind)
);

CREATE TABLE IF NOT EXISTS doc_versions (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,                 -- draft | approved | rejected
    content TEXT NOT NULL,                -- JSON
    basis TEXT,                           -- 临时扩范围的法定依据
    participants TEXT,                    -- JSON: 提出方参与人（复核回避名单）
    proposed_by TEXT,
    proposed_at TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    approved_by TEXT,
    approved_at TEXT,
    UNIQUE (case_id, kind, version)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    delivery_no TEXT NOT NULL,
    content_hash TEXT NOT NULL,          -- 首交摘要（异文判定基准，永不改变）
    source TEXT NOT NULL,
    commitment TEXT NOT NULL,            -- 真实性/完整性承诺
    arrived_at TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'normal' -- normal | disputed
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_delivery_no
    ON deliveries(case_id, delivery_no);

CREATE TABLE IF NOT EXISTS delivery_retries (
    id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,          -- 本次重试携带的摘要
    source TEXT NOT NULL,
    commitment TEXT NOT NULL,
    arrived_at TEXT NOT NULL,
    same_fact INTEGER NOT NULL           -- 1=沿用原事实 0=异文（另入争议表）
);

CREATE TABLE IF NOT EXISTS disputes (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    delivery_no TEXT NOT NULL,
    original_hash TEXT NOT NULL,
    variant_hash TEXT NOT NULL,
    variant_source TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'open', -- open | resolved
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sealed_items (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    ref_type TEXT NOT NULL,              -- delivery | account_package
    ref_id TEXT NOT NULL,
    sealed_by TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    UNIQUE (case_id, ref_type, ref_id)
);

CREATE TABLE IF NOT EXISTS account_queries (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    request_no TEXT NOT NULL UNIQUE,
    applicant TEXT NOT NULL,
    target_subject TEXT NOT NULL,       -- 发起单位或个人
    account_scope TEXT NOT NULL,        -- JSON: 账户/期间/事项范围
    status TEXT NOT NULL DEFAULT 'draft', -- draft|signed|executed|material_returned|void
    signer TEXT,
    signed_at TEXT,
    notice_doc_id TEXT,
    notice_valid_from TEXT,
    notice_valid_until TEXT,
    executors TEXT,                     -- JSON: 两名执行人员
    application_hash TEXT,              -- 申请快照摘要（回执不得改写的锚点）
    voided_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS query_access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    permission TEXT NOT NULL,           -- 当时依据的权限
    action TEXT NOT NULL,
    detail TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assistance_packages (
    id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    fields TEXT NOT NULL,               -- JSON: 最小必要字段集
    prepared_by TEXT NOT NULL,
    prepared_at TEXT NOT NULL,
    UNIQUE (query_id, version)
);

CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    summary TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    app_hash_before TEXT NOT NULL,
    app_hash_after TEXT NOT NULL,       -- 必须恒等于 before，证明申请未被回执改写
    UNIQUE (query_id, seq)
);

CREATE TABLE IF NOT EXISTS restricted_records (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- interference | refusal | assistance
    current_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', -- open | urged | closed
    urged_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deadline_id TEXT
);

CREATE TABLE IF NOT EXISTS restricted_versions (
    id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,              -- JSON
    correction_reason TEXT,             -- NULL 表示首版而非更正
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (record_id, version)
);

CREATE TABLE IF NOT EXISTS deadlines (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- comment | assistance_urge | review
    ref_type TEXT NOT NULL,             -- case | assistance | scope_version
    ref_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | done | cancelled
    urged_count INTEGER NOT NULL DEFAULT 0,
    last_urged_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_deadlines_due ON deadlines(status, due_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,             -- JSON
    actor TEXT,
    required_permission TEXT,          -- 非空时只有具备该权限者能在时间线看到
    at TEXT NOT NULL,
    UNIQUE (case_id, seq)
);
"""


class Storage:
    """持有一个 SQLite 连接，提供事务与基础读写助手。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """立即写锁事务；异常即回滚，保证“要么全成要么全不成”。"""

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ---- 通用助手 -------------------------------------------------

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def loads(value: str | None) -> Any:
        return json.loads(value) if value is not None else None

    def watermark(self, conn: sqlite3.Connection, case_id: str) -> int:
        row = conn.execute(
            "SELECT watermark FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        if row is None:
            from .errors import NotFound

            raise NotFound(f"案件不存在：{case_id}")
        return int(row["watermark"])

    def append_event(
        self,
        conn: sqlite3.Connection,
        case_id: str,
        event_type: str,
        payload: dict,
        actor: str | None,
        required_permission: str | None = None,
        at: str | None = None,
    ) -> int:
        """在当前事务内追加事件并前推案件水位，返回事件序号。"""

        next_seq = self.watermark(conn, case_id) + 1
        conn.execute(
            """
            INSERT INTO events
                (case_id, seq, type, payload, actor, required_permission, at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_id,
                next_seq,
                event_type,
                self.dumps(payload),
                actor,
                required_permission,
                at,
            ),
        )
        conn.execute(
            "UPDATE cases SET watermark = ? WHERE id = ?", (next_seq, case_id)
        )
        return next_seq
