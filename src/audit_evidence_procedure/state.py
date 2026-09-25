"""案件状态投影：从事件流重建全部索引，支撑停机恢复。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from . import events as ev
from .models import (
    AccessEntry,
    AccountQuery,
    Case,
    DeadlineItem,
    DeadlineQueue,
    DeadlineStatus,
    Delivery,
    DisputeStatus,
    DocumentKind,
    DocumentState,
    DocumentVersion,
    EvidenceDispute,
    ExpansionState,
    QueryReceipt,
    RecordCategory,
    RecordVersion,
    RestrictedRecord,
    ScopeExpansion,
    Seal,
)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class State:
    """全部案件索引。只通过 apply 事件演进，不直接改写。"""

    def __init__(self) -> None:
        self.cases: dict[str, Case] = {}
        self.documents: dict[tuple[str, DocumentKind, int], DocumentVersion] = {}
        self.expansions: dict[str, ScopeExpansion] = {}
        self.deliveries: dict[tuple[str, str], Delivery] = {}
        self.disputes: dict[str, EvidenceDispute] = {}
        self.seals: dict[str, Seal] = {}
        self.delivery_seal: dict[tuple[str, str], str] = {}
        self.queries: dict[str, AccountQuery] = {}
        self.receipts: dict[str, QueryReceipt] = {}
        self.query_receipts: dict[str, list[str]] = {}
        self.records: dict[str, RestrictedRecord] = {}
        self.deadline_items: dict[str, DeadlineItem] = {}
        self.access_log: list[AccessEntry] = []
        self.water_levels: dict[str, int] = {}

    # ---- 查询辅助 ----

    def water_level(self, case_id: str) -> int:
        return self.water_levels.get(case_id, 0)

    def next_version(self, case_id: str, kind: DocumentKind) -> int:
        versions = [
            version_no
            for (cid, k, version_no) in self.documents
            if cid == case_id and k == kind
        ]
        return max(versions, default=0) + 1

    def effective_document(
        self, case_id: str, kind: DocumentKind
    ) -> DocumentVersion | None:
        """只按批准版本生效：返回最新已批准版本。"""
        approved = [
            doc
            for (cid, k, _), doc in self.documents.items()
            if cid == case_id and k == kind and doc.state == DocumentState.APPROVED
        ]
        return max(approved, key=lambda d: d.version_no, default=None)

    # ---- 事件演进 ----

    def apply(self, event: ev.Event) -> None:
        self.water_levels[event.case_id] = max(
            self.water_level(event.case_id), event.water_level
        )
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is not None:
            handler(event)

    def _on_case_opened(self, event: ev.Event) -> None:
        p = event.payload
        self.cases[event.case_id] = Case(
            case_id=event.case_id,
            title=p["title"],
            domains=tuple(p["domains"]),
            opened_by=p["opened_by"],
            opened_at=_dt(event.at),
        )

    def _on_document_proposed(self, event: ev.Event) -> None:
        p = event.payload
        kind = DocumentKind(p["kind"])
        self.documents[(event.case_id, kind, p["version_no"])] = DocumentVersion(
            case_id=event.case_id,
            kind=kind,
            version_no=p["version_no"],
            content=dict(p["content"]),
            proposed_by=p["proposed_by"],
            proposed_at=_dt(event.at),
        )

    def _on_document_approved(self, event: ev.Event) -> None:
        p = event.payload
        kind = DocumentKind(p["kind"])
        key = (event.case_id, kind, p["version_no"])
        doc = self.documents.get(key)
        if doc is None:
            doc = DocumentVersion(
                case_id=event.case_id,
                kind=kind,
                version_no=p["version_no"],
                content={},
                proposed_by=p["approved_by"],
                proposed_at=_dt(event.at),
            )
        self.documents[key] = replace(
            doc,
            state=DocumentState.APPROVED,
            approved_by=p["approved_by"],
            approved_at=_dt(event.at),
        )
        for (cid, k, version_no), other in list(self.documents.items()):
            if (
                cid == event.case_id
                and k == kind
                and version_no != p["version_no"]
                and other.state == DocumentState.APPROVED
            ):
                self.documents[(cid, k, version_no)] = replace(
                    other, state=DocumentState.SUPERSEDED
                )

    def _on_scope_expansion_requested(self, event: ev.Event) -> None:
        p = event.payload
        self.expansions[p["request_id"]] = ScopeExpansion(
            request_id=p["request_id"],
            case_id=event.case_id,
            proposed_by=p["proposed_by"],
            participants=tuple(p["participants"]),
            legal_basis=p["legal_basis"],
            added_items=tuple(dict(item) for item in p["added_items"]),
            state=ExpansionState.PENDING,
            requested_at=_dt(event.at),
            reviewed_by=None,
            review_note="",
            decided_at=None,
        )

    def _on_scope_expansion_reviewed(self, event: ev.Event) -> None:
        p = event.payload
        expansion = self.expansions[p["request_id"]]
        self.expansions[p["request_id"]] = replace(
            expansion,
            state=ExpansionState.APPROVED if p["approved"] else ExpansionState.REJECTED,
            reviewed_by=p["reviewed_by"],
            review_note=p.get("note", ""),
            decided_at=_dt(event.at),
        )

    def _on_delivery_registered(self, event: ev.Event) -> None:
        p = event.payload
        self.deliveries[(event.case_id, p["delivery_no"])] = Delivery(
            case_id=event.case_id,
            delivery_no=p["delivery_no"],
            digest=p["digest"],
            source=p["source"],
            commitment=p["commitment"],
            arrived_at=_dt(event.at),
            batch_no=p.get("batch_no"),
            registered_by=p["registered_by"],
            water_level=event.water_level,
            tx_id=event.tx_id,
        )

    def _on_delivery_dispute_opened(self, event: ev.Event) -> None:
        p = event.payload
        self.disputes[p["dispute_id"]] = EvidenceDispute(
            dispute_id=p["dispute_id"],
            case_id=event.case_id,
            delivery_no=p["delivery_no"],
            original_digest=p["original_digest"],
            conflicting_digest=p["conflicting_digest"],
            reported_by=p["reported_by"],
            opened_at=_dt(event.at),
            status=DisputeStatus.OPEN,
        )

    def _on_materials_sealed(self, event: ev.Event) -> None:
        p = event.payload
        self.seals[p["seal_id"]] = Seal(
            seal_id=p["seal_id"],
            case_id=event.case_id,
            delivery_nos=tuple(p["delivery_nos"]),
            sealed_by=p["sealed_by"],
            reason=p.get("reason", ""),
            sealed_at=_dt(event.at),
            water_level=event.water_level,
        )
        for delivery_no in p["delivery_nos"]:
            self.delivery_seal[(event.case_id, delivery_no)] = p["seal_id"]

    def _on_account_query_issued(self, event: ev.Event) -> None:
        p = event.payload
        self.queries[p["query_id"]] = AccountQuery(
            query_id=p["query_id"],
            case_id=event.case_id,
            notice_version_no=p["notice_version_no"],
            notice_no=p["notice_no"],
            issuing_authority=p.get("issuing_authority", "审计机关"),
            signed_by=p["signed_by"],
            executors=tuple((e["id"], e["name"]) for e in p["executors"]),
            account_ids=tuple(p["account_ids"]),
            purpose=p["purpose"],
            legal_basis=p["legal_basis"],
            info_categories=tuple(p["info_categories"]),
            valid_until=_dt(p["valid_until"]),
            created_at=_dt(event.at),
            water_level=event.water_level,
        )

    def _on_account_query_receipt(self, event: ev.Event) -> None:
        p = event.payload
        self.receipts[p["receipt_id"]] = QueryReceipt(
            receipt_id=p["receipt_id"],
            query_id=p["query_id"],
            institution=p["institution"],
            summary=p["summary"],
            items_count=p["items_count"],
            received_at=_dt(event.at),
            recorded_by=p["recorded_by"],
        )
        self.query_receipts.setdefault(p["query_id"], []).append(p["receipt_id"])

    def _on_restricted_record_registered(self, event: ev.Event) -> None:
        p = event.payload
        first = RecordVersion(
            version_no=1,
            content=p["content"],
            recorded_by=p["registered_by"],
            recorded_at=_dt(event.at),
            reason="登记",
        )
        self.records[p["record_id"]] = RestrictedRecord(
            record_id=p["record_id"],
            case_id=event.case_id,
            category=RecordCategory(p["category"]),
            registered_by=p["registered_by"],
            opened_at=_dt(event.at),
            deadline_at=_dt(p["deadline_at"]),
            versions=(first,),
        )

    def _on_restricted_record_corrected(self, event: ev.Event) -> None:
        p = event.payload
        record = self.records[p["record_id"]]
        version = RecordVersion(
            version_no=p["version_no"],
            content=p["content"],
            recorded_by=p["corrected_by"],
            recorded_at=_dt(event.at),
            reason=p.get("reason", ""),
        )
        self.records[p["record_id"]] = replace(
            record, versions=record.versions + (version,)
        )

    def _on_deadline_item_created(self, event: ev.Event) -> None:
        p = event.payload
        self.deadline_items[p["item_id"]] = DeadlineItem(
            item_id=p["item_id"],
            queue=DeadlineQueue(p["queue"]),
            case_id=event.case_id,
            ref_kind=p["ref_kind"],
            ref_id=p["ref_id"],
            created_at=_dt(event.at),
            due_at=_dt(p["due_at"]),
            status=DeadlineStatus.PENDING,
            completed_at=None,
            completed_by=None,
            reminders=0,
        )

    def _on_deadline_item_completed(self, event: ev.Event) -> None:
        p = event.payload
        item = self.deadline_items[p["item_id"]]
        self.deadline_items[p["item_id"]] = replace(
            item,
            status=DeadlineStatus.DONE,
            completed_at=_dt(event.at),
            completed_by=p["completed_by"],
        )

    def _on_assistance_reminder_sent(self, event: ev.Event) -> None:
        p = event.payload
        item = self.deadline_items[p["item_id"]]
        self.deadline_items[p["item_id"]] = replace(item, reminders=item.reminders + 1)

    def _on_access_logged(self, event: ev.Event) -> None:
        p = event.payload
        self.access_log.append(
            AccessEntry(
                principal_id=p["principal_id"],
                principal_name=p["principal_name"],
                permission=p["permission"],
                ref_kind=p["ref_kind"],
                ref_id=p["ref_id"],
                case_id=event.case_id,
                at=_dt(event.at),
            )
        )
