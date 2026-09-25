"""测试共用引导：主体、时钟与一个已批准全部版本文书的案件。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from audit_evidence_procedure import (  # noqa: E402
    AuditService,
    ManualClock,
    Principal,
    Role,
)

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

CASE_ID = "CASE-2026-001"
DOMAINS = ["财政收支", "国有资源", "重大公共工程"]


def make_principals() -> dict[str, Principal]:
    def p(pid: str, name: str, *roles: Role) -> Principal:
        return Principal(id=pid, name=name, roles=frozenset(roles))

    return {
        "leader": p("U-LEAD", "负责人甲", Role.AGENCY_LEADER),
        "reviewer": p("U-REV-1", "复核人乙", Role.REVIEWER),
        "reviewer2": p("U-REV-2", "复核人丙", Role.REVIEWER),
        "team1": p("U-TEAM-1", "审计组员丁", Role.AUDIT_TEAM),
        "team2": p("U-TEAM-2", "审计组员戊", Role.AUDIT_TEAM),
        "entity": p("U-ENT", "被审计单位联系人", Role.AUDITED_ENTITY),
        "project": p("U-PROJ", "普通项目人员", Role.PROJECT_MEMBER),
        "assistant": p("U-AST", "协助机关人员", Role.ASSISTING_AGENCY),
        # 身兼审计组与复核角色，用于验证“复核人不得参与提出”
        "hybrid": p("U-HYB", "复合型人员", Role.AUDIT_TEAM, Role.REVIEWER),
    }


def new_service() -> tuple[AuditService, ManualClock]:
    clock = ManualClock(T0)
    return AuditService(clock=clock), clock


def boot_case(
    service: AuditService,
    ps: dict[str, Principal],
    notice_valid_days: int = 30,
) -> str:
    """开立统一案件，并批准年度计划、审计通知、事项范围、审计组成员四类版本。"""
    leader, reviewer = ps["leader"], ps["reviewer"]
    team1, team2 = ps["team1"], ps["team2"]
    service.open_case(leader, CASE_ID, "统一案件", DOMAINS)
    service.propose_document(
        team1, CASE_ID, "年度计划", {"year": 2026, "objectives": ["覆盖三大领域"]}
    )
    service.approve_document(reviewer, CASE_ID, "年度计划", 1)
    service.propose_document(
        team1,
        CASE_ID,
        "审计通知",
        {
            "notice_no": "审通〔2026〕1号",
            "valid_from": (T0 - timedelta(days=1)).isoformat(),
            "valid_until": (T0 + timedelta(days=notice_valid_days)).isoformat(),
            "scope_summary": "财政收支等",
        },
    )
    service.approve_document(reviewer, CASE_ID, "审计通知", 1)
    service.propose_document(
        team1,
        CASE_ID,
        "事项范围",
        {"items": [{"domain": "财政收支", "description": "预算执行"}]},
    )
    service.approve_document(reviewer, CASE_ID, "事项范围", 1)
    service.propose_document(
        team1, CASE_ID, "审计组成员", {"members": [team1.id, team2.id]}
    )
    service.approve_document(reviewer, CASE_ID, "审计组成员", 1)
    return CASE_ID
