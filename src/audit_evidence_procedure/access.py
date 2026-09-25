"""角色与权限：普通项目权限不得查看金融账户查询材料与干预登记。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import PermissionDenied


class Permission(str, Enum):
    CASE_READ = "case:read"
    CASE_MANAGE = "case:manage"
    PLAN_PROPOSE = "plan:propose"
    PLAN_APPROVE = "plan:approve"
    SCOPE_REVIEW = "scope:review"
    EVIDENCE_SUBMIT = "evidence:submit"
    EVIDENCE_READ = "evidence:read"
    EVIDENCE_SEAL = "evidence:seal"
    FINANCIAL_QUERY_SIGN = "financial:query:sign"
    FINANCIAL_QUERY_EXECUTE = "financial:query:execute"
    FINANCIAL_QUERY_READ = "financial:query:read"
    RESTRICTED_RECORD_WRITE = "restricted:write"
    RESTRICTED_RECORD_READ = "restricted:read"
    DEADLINE_MANAGE = "deadline:manage"
    EXPLAIN_AUTHORIZED = "explain:authorized"


class Role(str, Enum):
    AUDIT_TEAM = "审计组人员"
    AUDITED_ENTITY = "被审计单位"
    REVIEWER = "审计机关复核人员"
    AGENCY_LEADER = "审计机关负责人"
    ASSISTING_AGENCY = "协助机关人员"
    PROJECT_MEMBER = "普通项目人员"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.AUDIT_TEAM: frozenset({
        Permission.CASE_READ,
        Permission.PLAN_PROPOSE,
        Permission.EVIDENCE_SUBMIT,
        Permission.EVIDENCE_READ,
        Permission.EVIDENCE_SEAL,
        Permission.FINANCIAL_QUERY_EXECUTE,
        Permission.FINANCIAL_QUERY_READ,
        Permission.RESTRICTED_RECORD_WRITE,
        Permission.RESTRICTED_RECORD_READ,
        Permission.DEADLINE_MANAGE,
    }),
    Role.AUDITED_ENTITY: frozenset({
        Permission.CASE_READ,
        Permission.EVIDENCE_SUBMIT,
    }),
    Role.REVIEWER: frozenset({
        Permission.CASE_READ,
        Permission.EVIDENCE_READ,
        Permission.PLAN_APPROVE,
        Permission.SCOPE_REVIEW,
        Permission.FINANCIAL_QUERY_READ,
        Permission.RESTRICTED_RECORD_READ,
        Permission.DEADLINE_MANAGE,
        Permission.EXPLAIN_AUTHORIZED,
    }),
    Role.AGENCY_LEADER: frozenset({
        Permission.CASE_READ,
        Permission.CASE_MANAGE,
        Permission.EVIDENCE_READ,
        Permission.FINANCIAL_QUERY_SIGN,
        Permission.FINANCIAL_QUERY_READ,
        Permission.RESTRICTED_RECORD_READ,
        Permission.EXPLAIN_AUTHORIZED,
    }),
    Role.ASSISTING_AGENCY: frozenset({
        Permission.CASE_READ,
    }),
    # 普通项目权限：只能看一般案件脉络，看不到金融账户查询材料与干预登记。
    Role.PROJECT_MEMBER: frozenset({
        Permission.CASE_READ,
        Permission.EVIDENCE_READ,
    }),
}


@dataclass(frozen=True)
class Principal:
    """操作主体：一个 id 加一组角色。"""

    id: str
    name: str
    roles: frozenset[Role]

    @property
    def permissions(self) -> frozenset[Permission]:
        granted: set[Permission] = set()
        for role in self.roles:
            granted |= ROLE_PERMISSIONS[role]
        return frozenset(granted)

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions


def require(principal: Principal, permission: Permission) -> None:
    """缺少权限时拒绝操作。"""
    if not principal.has(permission):
        raise PermissionDenied(
            f"{principal.name}（{principal.id}）缺少权限：{permission.value}"
        )
