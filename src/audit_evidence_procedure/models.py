"""领域模型：案件、版本化文书、交付、账户查询、受限记录与期限。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class CaseDomain(str, Enum):
    """审计监督覆盖的统一案件领域。"""

    FISCAL = "财政收支"
    STATE_RESOURCES = "国有资源"
    PUBLIC_WORKS = "重大公共工程"


KNOWN_DOMAINS = frozenset(d.value for d in CaseDomain)


class DocumentKind(str, Enum):
    """只按批准版本生效的四类文书。"""

    ANNUAL_PLAN = "年度计划"
    NOTICE = "审计通知"
    SCOPE = "事项范围"
    TEAM = "审计组成员"


class DocumentState(str, Enum):
    SUBMITTED = "submitted"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class ExpansionState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RecordCategory(str, Enum):
    """三类受限记录，分别进入不同的期限队列。"""

    INTERFERENCE = "打探干预"
    REFUSAL = "拒绝配合"
    CROSS_AGENCY = "跨机关协助"


class DeadlineQueue(str, Enum):
    OPINION = "意见期限"
    ASSISTANCE = "协助催办"
    REVIEW = "复核工作"


class DeadlineStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"


class DisputeStatus(str, Enum):
    OPEN = "open"


@dataclass(frozen=True)
class Case:
    case_id: str
    title: str
    domains: tuple[str, ...]
    opened_by: str
    opened_at: datetime


@dataclass(frozen=True)
class DocumentVersion:
    case_id: str
    kind: DocumentKind
    version_no: int
    content: dict
    proposed_by: str
    proposed_at: datetime
    state: DocumentState = DocumentState.SUBMITTED
    approved_by: str | None = None
    approved_at: datetime | None = None


@dataclass(frozen=True)
class ScopeExpansion:
    """临时扩大范围请求：须说明法定依据，由未参与提出的人复核。"""

    request_id: str
    case_id: str
    proposed_by: str
    participants: tuple[str, ...]
    legal_basis: str
    added_items: tuple[dict, ...]
    state: ExpansionState
    requested_at: datetime
    reviewed_by: str | None
    review_note: str
    decided_at: datetime | None


@dataclass(frozen=True)
class Delivery:
    """一批电子资料交付：保存摘要、来源、承诺与到达时间。"""

    case_id: str
    delivery_no: str
    digest: str
    source: str
    commitment: str
    arrived_at: datetime
    batch_no: int | None
    registered_by: str
    water_level: int
    tx_id: str


@dataclass(frozen=True)
class EvidenceDispute:
    """相同交付号出现异文时转入的证据争议。"""

    dispute_id: str
    case_id: str
    delivery_no: str
    original_digest: str
    conflicting_digest: str
    reported_by: str
    opened_at: datetime
    status: DisputeStatus


@dataclass(frozen=True)
class DeliveryOutcome:
    delivery: Delivery
    is_retry: bool
    dispute: EvidenceDispute | None


@dataclass(frozen=True)
class Seal:
    seal_id: str
    case_id: str
    delivery_nos: tuple[str, ...]
    sealed_by: str
    reason: str
    sealed_at: datetime
    water_level: int


@dataclass(frozen=True)
class AccountQuery:
    """账户查询申请：签发后不可被回执改写。"""

    query_id: str
    case_id: str
    notice_version_no: int
    notice_no: str
    issuing_authority: str
    signed_by: str
    executors: tuple[tuple[str, str], ...]
    account_ids: tuple[str, ...]
    purpose: str
    legal_basis: str
    info_categories: tuple[str, ...]
    valid_until: datetime
    created_at: datetime
    water_level: int

    def institution_payload(self) -> dict:
        """交给金融机构的最小化信息，仅限完成协助所需内容。"""
        return {
            "query_id": self.query_id,
            "notice_no": self.notice_no,
            "issuing_authority": self.issuing_authority,
            "legal_basis": self.legal_basis,
            "account_ids": list(self.account_ids),
            "info_categories": list(self.info_categories),
            "executor_names": [name for _, name in self.executors],
            "valid_until": self.valid_until.isoformat(),
        }


@dataclass(frozen=True)
class QueryReceipt:
    """金融机构回执：独立事实，只能引用申请，不能改写申请。"""

    receipt_id: str
    query_id: str
    institution: str
    summary: str
    items_count: int
    received_at: datetime
    recorded_by: str


@dataclass(frozen=True)
class RecordVersion:
    """受限记录的一个版本：更正只新增版本，不删除原记录。"""

    version_no: int
    content: str
    recorded_by: str
    recorded_at: datetime
    reason: str


@dataclass(frozen=True)
class RestrictedRecord:
    record_id: str
    case_id: str
    category: RecordCategory
    registered_by: str
    opened_at: datetime
    deadline_at: datetime
    versions: tuple[RecordVersion, ...]


@dataclass(frozen=True)
class DeadlineItem:
    item_id: str
    queue: DeadlineQueue
    case_id: str
    ref_kind: str
    ref_id: str
    created_at: datetime
    due_at: datetime
    status: DeadlineStatus
    completed_at: datetime | None
    completed_by: str | None
    reminders: int


@dataclass(frozen=True)
class AccessEntry:
    """使用留痕：谁曾在何种权限下使用过案件材料。"""

    principal_id: str
    principal_name: str
    permission: str
    ref_kind: str
    ref_id: str
    case_id: str
    at: datetime


@dataclass(frozen=True)
class EvidenceExplanation:
    """授权查询的解释结果：证据如何取得、谁使用过、程序当前是否有效。"""

    case_id: str
    delivery_no: str
    acquisition: dict
    usage: tuple[AccessEntry, ...]
    procedure: dict
