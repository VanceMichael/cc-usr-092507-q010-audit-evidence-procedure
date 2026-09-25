"""审计取证与程序控制服务。

统一案件脉络覆盖财政收支、国有资源和重大公共工程；全部事实经事件
落账，取证、封存和范围变更可在同一案件水位原子提交。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import events as ev
from .access import Permission, Principal, require
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import (
    KNOWN_DOMAINS,
    AccountQuery,
    Case,
    DeadlineItem,
    DeadlineQueue,
    DeadlineStatus,
    Delivery,
    DeliveryOutcome,
    DocumentKind,
    DocumentState,
    DocumentVersion,
    EvidenceExplanation,
    ExpansionState,
    QueryReceipt,
    RecordCategory,
    RestrictedRecord,
    ScopeExpansion,
    Seal,
)
from .state import State
from .store import EventStore

# 各类受限记录与复核工作的法定期限
OPINION_PERIOD = timedelta(days=10)       # 拒绝配合 → 意见期限
ASSISTANCE_PERIOD = timedelta(days=7)     # 跨机关协助 → 协助催办
INTERFERENCE_PERIOD = timedelta(days=3)   # 打探干预 → 复核工作
REVIEW_PERIOD = timedelta(days=5)         # 临时扩大范围 → 复核工作

CATEGORY_QUEUE = {
    RecordCategory.INTERFERENCE: DeadlineQueue.REVIEW,
    RecordCategory.REFUSAL: DeadlineQueue.OPINION,
    RecordCategory.CROSS_AGENCY: DeadlineQueue.ASSISTANCE,
}
CATEGORY_PERIOD = {
    RecordCategory.INTERFERENCE: INTERFERENCE_PERIOD,
    RecordCategory.REFUSAL: OPINION_PERIOD,
    RecordCategory.CROSS_AGENCY: ASSISTANCE_PERIOD,
}


@dataclass(frozen=True)
class Operation:
    """案件事务中的一个操作。"""

    kind: str
    params: dict


def op_register_evidence(
    delivery_no: str,
    digest: str,
    source: str,
    commitment: str,
    batch_no: int | None = None,
) -> Operation:
    return Operation(
        "register_evidence",
        {
            "delivery_no": delivery_no,
            "digest": digest,
            "source": source,
            "commitment": commitment,
            "batch_no": batch_no,
        },
    )


def op_seal_materials(delivery_nos: list[str], reason: str = "") -> Operation:
    return Operation(
        "seal_materials", {"delivery_nos": list(delivery_nos), "reason": reason}
    )


def op_apply_scope_version(version_no: int) -> Operation:
    return Operation("apply_scope_version", {"version_no": int(version_no)})


@dataclass(frozen=True)
class CommitResult:
    tx_id: str | None
    water_level: int
    events: tuple[ev.Event, ...]
    outcomes: tuple[dict, ...] = ()
    no_op: bool = False


@dataclass(frozen=True)
class CaseFileView:
    """统一案件脉络视图；受限栏目按权限省略。"""

    case: Case
    water_level: int
    documents: dict
    deliveries: tuple[Delivery, ...] | None
    seals: tuple[Seal, ...] | None
    account_queries: tuple[AccountQuery, ...] | None
    restricted_records: tuple[RestrictedRecord, ...] | None
    pending_deadlines: tuple[DeadlineItem, ...]
    omitted_sections: tuple[str, ...]


def _new_id() -> str:
    return uuid.uuid4().hex


class AuditService:
    """审计取证与程序控制服务入口。"""

    def __init__(
        self, store: EventStore | None = None, clock: Clock | None = None
    ) -> None:
        self.store = store or EventStore()
        self.clock = clock or SystemClock()
        self.state = State()
        for event in self.store.events:
            self.state.apply(event)

    @classmethod
    def recover(
        cls, path: str | Path, clock: Clock | None = None
    ) -> "AuditService":
        """停机恢复：重放事件快照，期限队列与复核工作继续执行。"""
        return cls(store=EventStore.load(path), clock=clock)

    # ---- 基础设施 ----

    def water_level(self, case_id: str) -> int:
        return self.state.water_level(case_id)

    def events(self, case_id: str | None = None) -> tuple[ev.Event, ...]:
        if case_id is None:
            return self.store.events
        return tuple(e for e in self.store.events if e.case_id == case_id)

    def _case_or_raise(self, case_id: str) -> Case:
        case = self.state.cases.get(case_id)
        if case is None:
            raise NotFoundError(f"案件不存在：{case_id}")
        return case

    def _commit(self, case_id: str, drafts: list[ev.DraftEvent]) -> CommitResult:
        """整批落账：同一事务共享新的案件水位；任何校验失败都不会走到这里。"""
        if not drafts:
            raise ValidationError("空事务不允许提交")
        tx_id = _new_id()
        level = self.state.water_level(case_id) + 1
        events = self.store.append(
            case_id,
            drafts,
            water_level=level,
            tx_id=tx_id,
            at=self.clock.now().isoformat(),
        )
        for event in events:
            self.state.apply(event)
        return CommitResult(tx_id=tx_id, water_level=level, events=tuple(events))

    def _log_access(
        self,
        principal: Principal,
        permission: Permission,
        case_id: str,
        ref_kind: str,
        ref_id: str,
    ) -> None:
        """使用留痕：记录谁在何种权限下使用了什么，不推进案件水位。"""
        draft = ev.DraftEvent(
            ev.ACCESS_LOGGED,
            {
                "principal_id": principal.id,
                "principal_name": principal.name,
                "permission": permission.value,
                "ref_kind": ref_kind,
                "ref_id": ref_id,
            },
        )
        events = self.store.append(
            case_id,
            [draft],
            water_level=self.state.water_level(case_id),
            tx_id=_new_id(),
            at=self.clock.now().isoformat(),
        )
        for event in events:
            self.state.apply(event)

    # ---- 案件 ----

    def open_case(
        self,
        principal: Principal,
        case_id: str,
        title: str,
        domains: list[str],
    ) -> Case:
        require(principal, Permission.CASE_MANAGE)
        if case_id in self.state.cases:
            raise ConflictError(f"案件已存在：{case_id}")
        if not domains:
            raise ValidationError("案件必须覆盖至少一个监督领域")
        unknown = sorted(set(domains) - KNOWN_DOMAINS)
        if unknown:
            raise ValidationError(f"未知监督领域：{unknown}")
        self._commit(
            case_id,
            [
                ev.DraftEvent(
                    ev.CASE_OPENED,
                    {
                        "title": title,
                        "domains": list(domains),
                        "opened_by": principal.id,
                    },
                )
            ],
        )
        return self.state.cases[case_id]

    def case_file(self, principal: Principal, case_id: str) -> CaseFileView:
        """统一案件脉络：普通项目权限看不到金融账户查询材料与干预登记。"""
        require(principal, Permission.CASE_READ)
        case = self._case_or_raise(case_id)
        omitted: list[str] = []
        if principal.has(Permission.EVIDENCE_READ):
            deliveries: tuple[Delivery, ...] | None = tuple(
                d for (cid, _), d in self.state.deliveries.items() if cid == case_id
            )
            seals: tuple[Seal, ...] | None = tuple(
                s for s in self.state.seals.values() if s.case_id == case_id
            )
        else:
            deliveries, seals = None, None
            omitted.append("电子资料交付")
        if principal.has(Permission.FINANCIAL_QUERY_READ):
            queries: tuple[AccountQuery, ...] | None = tuple(
                q for q in self.state.queries.values() if q.case_id == case_id
            )
        else:
            queries = None
            omitted.append("金融账户查询材料")
        if principal.has(Permission.RESTRICTED_RECORD_READ):
            records: tuple[RestrictedRecord, ...] | None = tuple(
                r for r in self.state.records.values() if r.case_id == case_id
            )
        else:
            records = None
            omitted.append("干预登记等受限记录")
        documents = {
            kind.value: self.state.effective_document(case_id, kind)
            for kind in DocumentKind
        }
        pending = tuple(
            item
            for item in self.state.deadline_items.values()
            if item.case_id == case_id and item.status == DeadlineStatus.PENDING
        )
        return CaseFileView(
            case=case,
            water_level=self.state.water_level(case_id),
            documents=documents,
            deliveries=deliveries,
            seals=seals,
            account_queries=queries,
            restricted_records=records,
            pending_deadlines=pending,
            omitted_sections=tuple(omitted),
        )

    # ---- 版本化文书：只按批准版本生效 ----

    def propose_document(
        self, principal: Principal, case_id: str, kind: str, content: dict
    ) -> DocumentVersion:
        require(principal, Permission.PLAN_PROPOSE)
        self._case_or_raise(case_id)
        kind = DocumentKind(kind)
        self._validate_document_content(kind, content)
        version_no = self.state.next_version(case_id, kind)
        self._commit(
            case_id,
            [
                ev.DraftEvent(
                    ev.DOCUMENT_PROPOSED,
                    {
                        "kind": kind.value,
                        "version_no": version_no,
                        "content": dict(content),
                        "proposed_by": principal.id,
                    },
                )
            ],
        )
        return self.state.documents[(case_id, kind, version_no)]

    def approve_document(
        self, principal: Principal, case_id: str, kind: str, version_no: int
    ) -> DocumentVersion:
        require(principal, Permission.PLAN_APPROVE)
        self._case_or_raise(case_id)
        kind = DocumentKind(kind)
        doc = self.state.documents.get((case_id, kind, version_no))
        if doc is None:
            raise NotFoundError(f"{kind.value}版本不存在：{version_no}")
        if doc.state != DocumentState.SUBMITTED:
            raise ConflictError("只有待批准的版本可以批准生效")
        self._commit(
            case_id,
            [
                ev.DraftEvent(
                    ev.DOCUMENT_APPROVED,
                    {
                        "kind": kind.value,
                        "version_no": version_no,
                        "approved_by": principal.id,
                    },
                )
            ],
        )
        return self.state.documents[(case_id, kind, version_no)]

    def effective_document(
        self, case_id: str, kind: str
    ) -> DocumentVersion | None:
        return self.state.effective_document(case_id, DocumentKind(kind))

    def _validate_document_content(self, kind: DocumentKind, content: dict) -> None:
        if kind == DocumentKind.NOTICE:
            for key in ("notice_no", "valid_from", "valid_until"):
                if key not in content:
                    raise ValidationError(f"审计通知缺少字段：{key}")
            try:
                valid_from = datetime.fromisoformat(str(content["valid_from"]))
                valid_until = datetime.fromisoformat(str(content["valid_until"]))
            except ValueError as exc:
                raise ValidationError("通知书期限格式无效") from exc
            if valid_from >= valid_until:
                raise ValidationError("通知书期限无效：起始不得晚于截止")
        elif kind == DocumentKind.TEAM:
            if not content.get("members"):
                raise ValidationError("审计组成员不能为空")
        elif kind == DocumentKind.SCOPE:
            self._validate_scope_items(content.get("items"))
        elif kind == DocumentKind.ANNUAL_PLAN:
            if not content.get("year"):
                raise ValidationError("年度计划缺少年度")
            if not content.get("objectives"):
                raise ValidationError("年度计划缺少目标")

    def _validate_scope_items(self, items: object) -> None:
        if not items:
            raise ValidationError("事项范围不能为空")
        for item in items:
            if item.get("domain") not in KNOWN_DOMAINS:
                raise ValidationError(f"未知监督领域：{item.get('domain')}")
            if not item.get("description"):
                raise ValidationError("事项缺少描述")

    # ---- 临时扩大范围：法定依据 + 未参与提出的人复核 ----

    def request_scope_expansion(
        self,
        principal: Principal,
        case_id: str,
        added_items: list[dict],
        legal_basis: str,
        participants: tuple[str, ...] = (),
    ) -> ScopeExpansion:
        require(principal, Permission.PLAN_PROPOSE)
        self._case_or_raise(case_id)
        if not legal_basis or not str(legal_basis).strip():
            raise ValidationError("临时扩大范围必须说明法定依据")
        self._validate_scope_items(added_items)
        request_id = _new_id()
        now = self.clock.now()
        drafts = [
            ev.DraftEvent(
                ev.SCOPE_EXPANSION_REQUESTED,
                {
                    "request_id": request_id,
                    "proposed_by": principal.id,
                    "participants": list(participants),
                    "legal_basis": legal_basis,
                    "added_items": [dict(item) for item in added_items],
                },
            ),
            ev.DraftEvent(
                ev.DEADLINE_ITEM_CREATED,
                {
                    "item_id": _new_id(),
                    "queue": DeadlineQueue.REVIEW.value,
                    "ref_kind": "scope_expansion",
                    "ref_id": request_id,
                    "due_at": (now + REVIEW_PERIOD).isoformat(),
                },
            ),
        ]
        self._commit(case_id, drafts)
        return self.state.expansions[request_id]

    def review_scope_expansion(
        self,
        principal: Principal,
        case_id: str,
        request_id: str,
        approve: bool,
        note: str = "",
    ) -> ScopeExpansion:
        require(principal, Permission.SCOPE_REVIEW)
        self._case_or_raise(case_id)
        expansion = self.state.expansions.get(request_id)
        if expansion is None or expansion.case_id != case_id:
            raise NotFoundError(f"扩大范围请求不存在：{request_id}")
        if expansion.state != ExpansionState.PENDING:
            raise ConflictError("该请求复核已完成")
        if (
            principal.id == expansion.proposed_by
            or principal.id in expansion.participants
        ):
            raise PermissionDenied("复核人不得参与提出该扩大范围请求")
        drafts = [
            ev.DraftEvent(
                ev.SCOPE_EXPANSION_REVIEWED,
                {
                    "request_id": request_id,
                    "reviewed_by": principal.id,
                    "approved": bool(approve),
                    "note": note,
                },
            )
        ]
        if approve:
            current = self.state.effective_document(case_id, DocumentKind.SCOPE)
            items = list(current.content["items"]) if current else []
            items.extend(dict(item) for item in expansion.added_items)
            version_no = self.state.next_version(case_id, DocumentKind.SCOPE)
            drafts.append(
                ev.DraftEvent(
                    ev.DOCUMENT_PROPOSED,
                    {
                        "kind": DocumentKind.SCOPE.value,
                        "version_no": version_no,
                        "content": {"items": items},
                        "proposed_by": expansion.proposed_by,
                    },
                )
            )
            drafts.append(
                ev.DraftEvent(
                    ev.DOCUMENT_APPROVED,
                    {
                        "kind": DocumentKind.SCOPE.value,
                        "version_no": version_no,
                        "approved_by": principal.id,
                    },
                )
            )
        for item in self.state.deadline_items.values():
            if (
                item.ref_kind == "scope_expansion"
                and item.ref_id == request_id
                and item.status == DeadlineStatus.PENDING
            ):
                drafts.append(
                    ev.DraftEvent(
                        ev.DEADLINE_ITEM_COMPLETED,
                        {
                            "item_id": item.item_id,
                            "completed_by": principal.id,
                            "note": "范围扩大复核完成",
                        },
                    )
                )
        self._commit(case_id, drafts)
        return self.state.expansions[request_id]

    # ---- 电子资料交付：幂等沿用原事实，异文转入证据争议 ----

    def register_delivery(
        self,
        principal: Principal,
        case_id: str,
        delivery_no: str,
        digest: str,
        source: str,
        commitment: str,
        batch_no: int | None = None,
    ) -> DeliveryOutcome:
        result = self.commit(
            principal,
            case_id,
            [op_register_evidence(delivery_no, digest, source, commitment, batch_no)],
        )
        outcome = result.outcomes[0]
        delivery = self.state.deliveries[(case_id, delivery_no)]
        dispute = (
            self.state.disputes.get(outcome["dispute_id"])
            if outcome.get("dispute_id")
            else None
        )
        return DeliveryOutcome(
            delivery=delivery, is_retry=outcome["kind"] == "retry", dispute=dispute
        )

    def get_delivery(
        self, principal: Principal, case_id: str, delivery_no: str
    ) -> Delivery:
        require(principal, Permission.EVIDENCE_READ)
        delivery = self.state.deliveries.get((case_id, delivery_no))
        if delivery is None:
            raise NotFoundError(f"交付不存在：{delivery_no}")
        self._log_access(
            principal, Permission.EVIDENCE_READ, case_id, "delivery", delivery_no
        )
        return delivery

    # ---- 案件事务：同一水位原子提交 ----

    def commit(
        self, principal: Principal, case_id: str, operations: list[Operation]
    ) -> CommitResult:
        """取证、封存和范围变更同时发生时，基于同一案件水位提交。

        先逐操作校验并模拟演进（事务内后续操作可见前面操作产生的
        事实），全部通过才落账；任一失败则整体回滚，不会出现材料
        已受限而决定尚未生效的中间状态。
        """
        self._case_or_raise(case_id)
        if not operations:
            raise ValidationError("空事务不允许提交")
        handlers = {
            "register_evidence": self._op_register_evidence,
            "seal_materials": self._op_seal_materials,
            "apply_scope_version": self._op_apply_scope_version,
        }
        drafts: list[ev.DraftEvent] = []
        outcomes: list[dict] = []
        try:
            for operation in operations:
                handler = handlers.get(operation.kind)
                if handler is None:
                    raise ValidationError(f"未知事务操作：{operation.kind}")
                produced, outcome = handler(principal, case_id, operation.params)
                self._project_tentative(case_id, produced)
                drafts.extend(produced)
                outcomes.append(outcome)
        except Exception:
            self._rebuild_state()
            raise
        self._rebuild_state()
        if not drafts:
            return CommitResult(
                tx_id=None,
                water_level=self.state.water_level(case_id),
                events=(),
                outcomes=tuple(outcomes),
                no_op=True,
            )
        result = self._commit(case_id, drafts)
        return CommitResult(
            tx_id=result.tx_id,
            water_level=result.water_level,
            events=result.events,
            outcomes=tuple(outcomes),
        )

    def _project_tentative(
        self, case_id: str, drafts: list[ev.DraftEvent]
    ) -> None:
        """把事务内已产生的事件临时投影到状态，供后续操作校验。"""
        tentative_level = self.state.water_level(case_id) + 1
        for offset, draft in enumerate(drafts):
            self.state.apply(
                ev.Event(
                    seq=-1 - offset,
                    tx_id="tentative",
                    case_id=case_id,
                    water_level=tentative_level,
                    type=draft.type,
                    payload=draft.payload,
                    at=self.clock.now().isoformat(),
                )
            )

    def _rebuild_state(self) -> None:
        """从事务日志重建状态：模拟投影的回滚手段。"""
        self.state = State()
        for event in self.store.events:
            self.state.apply(event)

    def _op_register_evidence(
        self, principal: Principal, case_id: str, params: dict
    ) -> tuple[list[ev.DraftEvent], dict]:
        require(principal, Permission.EVIDENCE_SUBMIT)
        delivery_no = params["delivery_no"]
        digest = params["digest"]
        if not digest or not str(digest).strip():
            raise ValidationError("交付必须提供摘要")
        existing = self.state.deliveries.get((case_id, delivery_no))
        if existing is not None:
            if existing.digest == digest:
                # 相同交付号重试：沿用原事实，不产生新事件
                return [], {"kind": "retry", "dispute_id": None}
            # 异文：转入证据争议，原事实保持不变
            dispute_id = _new_id()
            return [
                ev.DraftEvent(
                    ev.DELIVERY_DISPUTE_OPENED,
                    {
                        "dispute_id": dispute_id,
                        "delivery_no": delivery_no,
                        "original_digest": existing.digest,
                        "conflicting_digest": digest,
                        "reported_by": principal.id,
                    },
                )
            ], {"kind": "dispute", "dispute_id": dispute_id}
        return [
            ev.DraftEvent(
                ev.DELIVERY_REGISTERED,
                {
                    "delivery_no": delivery_no,
                    "digest": digest,
                    "source": params["source"],
                    "commitment": params["commitment"],
                    "batch_no": params.get("batch_no"),
                    "registered_by": principal.id,
                },
            )
        ], {"kind": "registered", "dispute_id": None}

    def _op_seal_materials(
        self, principal: Principal, case_id: str, params: dict
    ) -> tuple[list[ev.DraftEvent], dict]:
        require(principal, Permission.EVIDENCE_SEAL)
        delivery_nos = list(params["delivery_nos"])
        if not delivery_nos:
            raise ValidationError("封存必须指定交付")
        for delivery_no in delivery_nos:
            if (case_id, delivery_no) not in self.state.deliveries:
                raise NotFoundError(f"交付不存在：{delivery_no}")
            if (case_id, delivery_no) in self.state.delivery_seal:
                raise ConflictError(f"交付已封存：{delivery_no}")
        seal_id = _new_id()
        return [
            ev.DraftEvent(
                ev.MATERIALS_SEALED,
                {
                    "seal_id": seal_id,
                    "delivery_nos": delivery_nos,
                    "sealed_by": principal.id,
                    "reason": params.get("reason", ""),
                },
            )
        ], {"seal_id": seal_id}

    def _op_apply_scope_version(
        self, principal: Principal, case_id: str, params: dict
    ) -> tuple[list[ev.DraftEvent], dict]:
        require(principal, Permission.PLAN_APPROVE)
        version_no = int(params["version_no"])
        doc = self.state.documents.get(
            (case_id, DocumentKind.SCOPE, version_no)
        )
        if doc is None:
            raise NotFoundError(f"事项范围版本不存在：{version_no}")
        if doc.state != DocumentState.SUBMITTED:
            raise ConflictError("只有待批准版本可以生效")
        return [
            ev.DraftEvent(
                ev.DOCUMENT_APPROVED,
                {
                    "kind": DocumentKind.SCOPE.value,
                    "version_no": version_no,
                    "approved_by": principal.id,
                },
            )
        ], {"version_no": version_no}

    def seal_materials(
        self,
        principal: Principal,
        case_id: str,
        delivery_nos: list[str],
        reason: str = "",
    ) -> Seal:
        result = self.commit(
            principal, case_id, [op_seal_materials(delivery_nos, reason)]
        )
        return self.state.seals[result.outcomes[0]["seal_id"]]

    # ---- 账户查询：签发权限 + 两名执行人员 + 通知书期限 ----

    def request_account_query(
        self,
        principal: Principal,
        case_id: str,
        executors: list[Principal],
        account_ids: list[str],
        purpose: str,
        legal_basis: str,
        info_categories: list[str],
        notice_version_no: int | None = None,
        issuing_authority: str = "审计机关",
    ) -> AccountQuery:
        require(principal, Permission.FINANCIAL_QUERY_SIGN)
        self._case_or_raise(case_id)
        if notice_version_no is None:
            notice = self.state.effective_document(case_id, DocumentKind.NOTICE)
            if notice is None:
                raise ValidationError("尚无生效的审计通知")
        else:
            notice = self.state.documents.get(
                (case_id, DocumentKind.NOTICE, notice_version_no)
            )
            if notice is None:
                raise NotFoundError(f"审计通知版本不存在：{notice_version_no}")
            if notice.state != DocumentState.APPROVED:
                raise ValidationError("审计通知只按批准版本生效")
        now = self.clock.now()
        valid_from = datetime.fromisoformat(str(notice.content["valid_from"]))
        valid_until = datetime.fromisoformat(str(notice.content["valid_until"]))
        if not valid_from <= now <= valid_until:
            raise ValidationError("超出通知书期限，不得发起账户查询")
        if len(executors) != 2:
            raise ValidationError("账户查询必须由两名执行人员办理")
        if executors[0].id == executors[1].id:
            raise ValidationError("两名执行人员不得为同一人")
        team = self.state.effective_document(case_id, DocumentKind.TEAM)
        if team is None:
            raise ValidationError("尚无生效的审计组成员版本")
        members = set(team.content["members"])
        for executor in executors:
            if not executor.has(Permission.FINANCIAL_QUERY_EXECUTE):
                raise PermissionDenied(
                    f"执行人员缺少账户查询执行权限：{executor.name}"
                )
            if executor.id not in members:
                raise ValidationError(
                    f"执行人员不在批准的审计组成员中：{executor.name}"
                )
        if not account_ids:
            raise ValidationError("必须指定查询账户")
        if not legal_basis or not str(legal_basis).strip():
            raise ValidationError("账户查询必须说明法定依据")
        if not info_categories:
            raise ValidationError("必须指定查询信息类别")
        query_id = _new_id()
        self._commit(
            case_id,
            [
                ev.DraftEvent(
                    ev.ACCOUNT_QUERY_ISSUED,
                    {
                        "query_id": query_id,
                        "notice_version_no": notice.version_no,
                        "notice_no": notice.content["notice_no"],
                        "issuing_authority": issuing_authority,
                        "signed_by": principal.id,
                        "executors": [
                            {"id": e.id, "name": e.name} for e in executors
                        ],
                        "account_ids": list(account_ids),
                        "purpose": purpose,
                        "legal_basis": legal_basis,
                        "info_categories": list(info_categories),
                        "valid_until": valid_until.isoformat(),
                    },
                )
            ],
        )
        return self.state.queries[query_id]

    def record_receipt(
        self,
        principal: Principal,
        query_id: str,
        institution: str,
        summary: str,
        items_count: int,
    ) -> QueryReceipt:
        """登记金融机构回执：独立事实，不能反向改写申请。"""
        require(principal, Permission.FINANCIAL_QUERY_EXECUTE)
        query = self.state.queries.get(query_id)
        if query is None:
            raise NotFoundError(f"账户查询不存在：{query_id}")
        if items_count < 0:
            raise ValidationError("回执条目数不能为负")
        receipt_id = _new_id()
        self._commit(
            query.case_id,
            [
                ev.DraftEvent(
                    ev.ACCOUNT_QUERY_RECEIPT,
                    {
                        "receipt_id": receipt_id,
                        "query_id": query_id,
                        "institution": institution,
                        "summary": summary,
                        "items_count": int(items_count),
                        "recorded_by": principal.id,
                    },
                )
            ],
        )
        return self.state.receipts[receipt_id]

    def account_queries(
        self, principal: Principal, case_id: str
    ) -> tuple[AccountQuery, ...]:
        require(principal, Permission.FINANCIAL_QUERY_READ)
        self._case_or_raise(case_id)
        self._log_access(
            principal, Permission.FINANCIAL_QUERY_READ, case_id, "case", case_id
        )
        return tuple(
            q for q in self.state.queries.values() if q.case_id == case_id
        )

    # ---- 受限记录：不同队列，更正只新增版本 ----

    def register_record(
        self,
        principal: Principal,
        case_id: str,
        category: str,
        content: str,
        deadline_at: datetime | None = None,
    ) -> RestrictedRecord:
        require(principal, Permission.RESTRICTED_RECORD_WRITE)
        self._case_or_raise(case_id)
        category = RecordCategory(category)
        if not content or not str(content).strip():
            raise ValidationError("受限记录内容不能为空")
        now = self.clock.now()
        due = deadline_at or (now + CATEGORY_PERIOD[category])
        record_id = _new_id()
        drafts = [
            ev.DraftEvent(
                ev.RESTRICTED_RECORD_REGISTERED,
                {
                    "record_id": record_id,
                    "category": category.value,
                    "content": content,
                    "registered_by": principal.id,
                    "deadline_at": due.isoformat(),
                },
            ),
            ev.DraftEvent(
                ev.DEADLINE_ITEM_CREATED,
                {
                    "item_id": _new_id(),
                    "queue": CATEGORY_QUEUE[category].value,
                    "ref_kind": "restricted_record",
                    "ref_id": record_id,
                    "due_at": due.isoformat(),
                },
            ),
        ]
        self._commit(case_id, drafts)
        return self.state.records[record_id]

    def correct_record(
        self,
        principal: Principal,
        record_id: str,
        content: str,
        reason: str = "",
    ) -> RestrictedRecord:
        """更正受限记录：新增版本，原记录保留。"""
        require(principal, Permission.RESTRICTED_RECORD_WRITE)
        record = self.state.records.get(record_id)
        if record is None:
            raise NotFoundError(f"受限记录不存在：{record_id}")
        if not content or not str(content).strip():
            raise ValidationError("更正内容不能为空")
        version_no = len(record.versions) + 1
        self._commit(
            record.case_id,
            [
                ev.DraftEvent(
                    ev.RESTRICTED_RECORD_CORRECTED,
                    {
                        "record_id": record_id,
                        "version_no": version_no,
                        "content": content,
                        "corrected_by": principal.id,
                        "reason": reason,
                    },
                )
            ],
        )
        return self.state.records[record_id]

    def record_history(
        self, principal: Principal, record_id: str
    ) -> tuple:
        require(principal, Permission.RESTRICTED_RECORD_READ)
        record = self.state.records.get(record_id)
        if record is None:
            raise NotFoundError(f"受限记录不存在：{record_id}")
        self._log_access(
            principal,
            Permission.RESTRICTED_RECORD_READ,
            record.case_id,
            "restricted_record",
            record_id,
        )
        return record.versions

    # ---- 期限队列 ----

    def deadline_queue(
        self,
        principal: Principal,
        queue: str,
        include_completed: bool = False,
    ) -> tuple[DeadlineItem, ...]:
        require(principal, Permission.DEADLINE_MANAGE)
        queue = DeadlineQueue(queue)
        items = [
            item
            for item in self.state.deadline_items.values()
            if item.queue == queue
            and (include_completed or item.status == DeadlineStatus.PENDING)
        ]
        return tuple(sorted(items, key=lambda item: item.due_at))

    def overdue_items(
        self, principal: Principal, queue: str | None = None
    ) -> tuple[DeadlineItem, ...]:
        require(principal, Permission.DEADLINE_MANAGE)
        now = self.clock.now()
        items = [
            item
            for item in self.state.deadline_items.values()
            if item.status == DeadlineStatus.PENDING and item.due_at < now
        ]
        if queue is not None:
            queue = DeadlineQueue(queue)
            items = [item for item in items if item.queue == queue]
        return tuple(sorted(items, key=lambda item: item.due_at))

    def complete_deadline_item(
        self, principal: Principal, item_id: str, note: str = ""
    ) -> DeadlineItem:
        require(principal, Permission.DEADLINE_MANAGE)
        item = self.state.deadline_items.get(item_id)
        if item is None:
            raise NotFoundError(f"期限事项不存在：{item_id}")
        if item.status == DeadlineStatus.DONE:
            raise ConflictError("期限事项已完成")
        self._commit(
            item.case_id,
            [
                ev.DraftEvent(
                    ev.DEADLINE_ITEM_COMPLETED,
                    {
                        "item_id": item_id,
                        "completed_by": principal.id,
                        "note": note,
                    },
                )
            ],
        )
        return self.state.deadline_items[item_id]

    def send_assistance_reminder(
        self, principal: Principal, item_id: str, note: str = ""
    ) -> DeadlineItem:
        """协助催办：向协助机关发出催办，事项保持待办直至完成。"""
        require(principal, Permission.DEADLINE_MANAGE)
        item = self.state.deadline_items.get(item_id)
        if item is None:
            raise NotFoundError(f"期限事项不存在：{item_id}")
        if item.queue != DeadlineQueue.ASSISTANCE:
            raise ValidationError("仅协助催办队列可以发送催办")
        self._commit(
            item.case_id,
            [
                ev.DraftEvent(
                    ev.ASSISTANCE_REMINDER_SENT,
                    {
                        "item_id": item_id,
                        "sent_by": principal.id,
                        "note": note,
                    },
                )
            ],
        )
        return self.state.deadline_items[item_id]

    # ---- 授权解释 ----

    def explain_evidence(
        self, principal: Principal, case_id: str, delivery_no: str
    ) -> EvidenceExplanation:
        """解释证据如何取得、谁曾在何种权限下使用、程序当前是否有效。"""
        require(principal, Permission.EXPLAIN_AUTHORIZED)
        self._case_or_raise(case_id)
        delivery = self.state.deliveries.get((case_id, delivery_no))
        if delivery is None:
            raise NotFoundError(f"交付不存在：{delivery_no}")
        self._log_access(
            principal, Permission.EXPLAIN_AUTHORIZED, case_id, "delivery", delivery_no
        )
        seal_id = self.state.delivery_seal.get((case_id, delivery_no))
        seal = self.state.seals.get(seal_id) if seal_id else None
        acquisition = {
            "digest": delivery.digest,
            "source": delivery.source,
            "commitment": delivery.commitment,
            "arrived_at": delivery.arrived_at.isoformat(),
            "batch_no": delivery.batch_no,
            "registered_by": delivery.registered_by,
            "water_level": delivery.water_level,
            "tx_id": delivery.tx_id,
            "sealed": seal is not None,
            "seal_id": seal.seal_id if seal else None,
        }
        usage = tuple(
            entry
            for entry in self.state.access_log
            if entry.case_id == case_id
            and entry.ref_kind == "delivery"
            and entry.ref_id == delivery_no
        )
        documents = {
            kind: self.state.effective_document(case_id, kind)
            for kind in DocumentKind
        }
        missing = [kind.value for kind, doc in documents.items() if doc is None]
        open_disputes = [
            dispute
            for dispute in self.state.disputes.values()
            if dispute.case_id == case_id
            and dispute.delivery_no == delivery_no
            and dispute.status.value == "open"
        ]
        notice = documents[DocumentKind.NOTICE]
        now = self.clock.now()
        notice_valid = False
        if notice is not None:
            valid_from = datetime.fromisoformat(str(notice.content["valid_from"]))
            valid_until = datetime.fromisoformat(str(notice.content["valid_until"]))
            notice_valid = valid_from <= now <= valid_until
        reasons: list[str] = []
        if missing:
            reasons.append("缺少生效版本：" + "、".join(missing))
        if open_disputes:
            reasons.append(f"存在未解决的证据争议：{len(open_disputes)} 项")
        if not notice_valid:
            reasons.append("审计通知不在有效期限内")
        procedure = {
            "effective_versions": {
                kind.value: doc.version_no if doc else None
                for kind, doc in documents.items()
            },
            "notice_within_validity": notice_valid,
            "open_disputes": len(open_disputes),
            "valid": not reasons,
            "reasons": reasons,
        }
        return EvidenceExplanation(
            case_id=case_id,
            delivery_no=delivery_no,
            acquisition=acquisition,
            usage=usage,
            procedure=procedure,
        )
