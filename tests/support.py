"""测试共用：装配服务、登记人员、构造标准案件。"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audit_evidence_procedure import AuditEvidenceProcedureService, MutableClock  # noqa: E402

PERSONS = {
    "director": ("张局长", "director"),
    "auditor": ("李审计", "auditor"),
    "auditor2": ("王审计", "auditor"),
    "reviewer": ("赵复核", "reviewer"),
    "unit": ("周某（被审计单位）", "audited_unit"),
    "banker": ("陈某（金融机构）", "assisting_officer"),
}


def build_service(database: str = ":memory:"):
    clock = MutableClock()
    svc = AuditEvidenceProcedureService(database, clock=clock)
    for key, (name, role) in PERSONS.items():
        svc.register_person(key, name, role)
    return svc, clock


def open_standard_case(svc, case_id: str = "CASE-1", category: str = "fiscal"):
    return svc.open_case(case_id, category, "某财政收支审计项目", "director")


def approve_audit_notice(svc, case_id="CASE-1", days=90):
    """起草并批准一份覆盖当前时钟的审计通知书，返回其内容。"""

    clock = svc.clock
    content = {
        "notice_no": "SHENJITONGZHI-001",
        "audited_unit": "某单位",
        "valid_from": (clock.now() - timedelta(days=1)).isoformat(),
        "valid_until": (clock.now() + timedelta(days=days)).isoformat(),
    }
    version_id = svc.documents.draft(case_id, "audit_notice", content, "director")
    svc.documents.approve(case_id, version_id, "director")
    return content


def query_window(svc, days=30):
    clock = svc.clock
    return clock.now().isoformat(), (clock.now() + timedelta(days=days)).isoformat()
