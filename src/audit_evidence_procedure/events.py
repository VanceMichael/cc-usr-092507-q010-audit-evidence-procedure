"""事件定义：案件的全部事实以事件形式追加，永不改写。"""

from __future__ import annotations

from dataclasses import dataclass

# 案件
CASE_OPENED = "case_opened"
# 年度计划、审计通知、事项范围、审计组成员的版本化文书
DOCUMENT_PROPOSED = "document_proposed"
DOCUMENT_APPROVED = "document_approved"
# 临时扩大范围
SCOPE_EXPANSION_REQUESTED = "scope_expansion_requested"
SCOPE_EXPANSION_REVIEWED = "scope_expansion_reviewed"
# 电子资料交付与证据争议
DELIVERY_REGISTERED = "delivery_registered"
DELIVERY_DISPUTE_OPENED = "delivery_dispute_opened"
# 封存
MATERIALS_SEALED = "materials_sealed"
# 账户查询与金融机构回执
ACCOUNT_QUERY_ISSUED = "account_query_issued"
ACCOUNT_QUERY_RECEIPT = "account_query_receipt"
# 受限记录（打探干预、拒绝配合、跨机关协助）
RESTRICTED_RECORD_REGISTERED = "restricted_record_registered"
RESTRICTED_RECORD_CORRECTED = "restricted_record_corrected"
# 期限队列
DEADLINE_ITEM_CREATED = "deadline_item_created"
DEADLINE_ITEM_COMPLETED = "deadline_item_completed"
ASSISTANCE_REMINDER_SENT = "assistance_reminder_sent"
# 使用留痕
ACCESS_LOGGED = "access_logged"


@dataclass(frozen=True)
class DraftEvent:
    """待提交的事件草稿，由存储层分配序号与水位。"""

    type: str
    payload: dict


@dataclass(frozen=True)
class Event:
    """已提交事件：同一事务内的事件共享 tx_id 与案件水位。"""

    seq: int
    tx_id: str
    case_id: str
    water_level: int
    type: str
    payload: dict
    at: str
