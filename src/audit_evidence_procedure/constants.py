"""领域常量：案件类别、文书种类、权限矩阵、期限类型。"""

from __future__ import annotations

from enum import Enum

# 统一案件脉络覆盖的三类监督事项
CASE_CATEGORY_FISCAL = "fiscal"          # 财政收支
CASE_CATEGORY_STATE_RESOURCE = "state_resource"  # 国有资源
CASE_CATEGORY_PUBLIC_PROJECT = "public_project"  # 重大公共工程
CASE_CATEGORIES = (
    CASE_CATEGORY_FISCAL,
    CASE_CATEGORY_STATE_RESOURCE,
    CASE_CATEGORY_PUBLIC_PROJECT,
)


class DocKind(str, Enum):
    """只按批准版本生效的四类文书。"""

    ANNUAL_PLAN = "annual_plan"          # 年度审计项目计划
    AUDIT_NOTICE = "audit_notice"        # 审计通知书
    SCOPE = "scope"                      # 审计事项范围
    TEAM = "team"                        # 审计组成员


# 人员角色
ROLE_DIRECTOR = "director"            # 审计机关负责人（签发账户查询通知）
ROLE_AUDITOR = "auditor"              # 审计组人员
ROLE_REVIEWER = "reviewer"            # 复核人员（须未参与提出）
ROLE_UNIT = "audited_unit"            # 被审计单位（交付资料）
ROLE_ASSISTING = "assisting_officer"  # 协助机关/金融机构经办

ROLES = (ROLE_DIRECTOR, ROLE_AUDITOR, ROLE_REVIEWER, ROLE_UNIT, ROLE_ASSISTING)


class RestrictedKind(str, Enum):
    """三类各有独立访问要求与期限队列的受限记录。"""

    INTERFERENCE = "interference"  # 打探、干预
    REFUSAL = "refusal"            # 拒绝配合
    ASSISTANCE = "assistance"      # 跨机关协助

# 受限记录各自独立队列（值与枚举保持一致，便于直接写库）
RESTRICTED_QUEUES = tuple(kind.value for kind in RestrictedKind)


class DeadlineKind(str, Enum):
    """受管理的程序期限：停机恢复后仍按持久化状态继续执行。"""

    COMMENT = "comment"                # 被审计单位意见期限
    ASSISTANCE_URGE = "assistance_urge"  # 协助事项催办
    REVIEW = "review"                  # 扩范围复核工作期限


DEADLINE_KINDS = tuple(kind.value for kind in DeadlineKind)

# 各类期限的默认时长（天）
DEFAULT_DEADLINE_DAYS = {
    DeadlineKind.COMMENT.value: 10,
    DeadlineKind.ASSISTANCE_URGE.value: 15,
    DeadlineKind.REVIEW.value: 5,
}

# 催办阶梯：到达期限未完成即催办，之后每隔该天数再次催办
URGE_INTERVAL_DAYS = 7


class Permission(str, Enum):
    """细粒度权限。普通项目角色绝不包含金融材料相关权限。"""

    PLAN_APPROVE = "plan.approve"
    DOC_DRAFT = "doc.draft"
    SCOPE_PROPOSE = "scope.propose"
    SCOPE_REVIEW = "scope.review"
    EVIDENCE_DELIVER = "evidence.deliver"
    EVIDENCE_COLLECT = "evidence.collect"
    EVIDENCE_SEAL = "evidence.seal"
    EVIDENCE_READ = "evidence.read"
    DISPUTE_RAISE = "dispute.raise"
    ACCOUNT_QUERY_REQUEST = "account_query.request"   # 提出查询申请
    ACCOUNT_QUERY_SIGN = "account_query.sign"         # 签发查询通知（机关负责人）
    ACCOUNT_QUERY_EXECUTE = "account_query.execute"   # 持通知执行（双人）
    ACCOUNT_MATERIAL_READ = "account_material.read"   # 查看金融账户查询材料
    ACCOUNT_MATERIAL_RECORD = "account_material.record"  # 登记/封存金融材料
    ASSISTANCE_PACKAGE_PREPARE = "assistance.package.prepare"
    RECEIPT_RECORD = "receipt.record"
    RESTRICTED_INTERFERENCE_WRITE = "restricted.interference.write"
    RESTRICTED_REFUSAL_WRITE = "restricted.refusal.write"
    RESTRICTED_ASSISTANCE_WRITE = "restricted.assistance.write"
    RESTRICTED_READ_ALL = "restricted.read_all"


# 角色 → 权限。注意：
# 1. 任何金融账户材料权限都不下放给普通审计项目角色的默认集合；
#    审计组人员仅可“提出/执行查询”，能否“查看材料”以逐案授权登记为准
#    （由机关负责人在签发时授予执行人员，见 services.account_queries）。
# 2. 登记（封存金融材料）只属于机关负责人序列的授权，普通项目无法干预。
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    ROLE_DIRECTOR: frozenset({
        Permission.PLAN_APPROVE,
        Permission.DOC_DRAFT,
        Permission.SCOPE_PROPOSE,
        Permission.SCOPE_REVIEW,
        Permission.EVIDENCE_COLLECT,
        Permission.EVIDENCE_SEAL,
        Permission.EVIDENCE_READ,
        Permission.ACCOUNT_QUERY_REQUEST,
        Permission.ACCOUNT_QUERY_SIGN,
        Permission.ACCOUNT_QUERY_EXECUTE,
        Permission.ACCOUNT_MATERIAL_READ,
        Permission.ACCOUNT_MATERIAL_RECORD,
        Permission.ASSISTANCE_PACKAGE_PREPARE,
        Permission.RECEIPT_RECORD,
        Permission.RESTRICTED_INTERFERENCE_WRITE,
        Permission.RESTRICTED_REFUSAL_WRITE,
        Permission.RESTRICTED_ASSISTANCE_WRITE,
        Permission.RESTRICTED_READ_ALL,
    }),
    ROLE_AUDITOR: frozenset({
        Permission.DOC_DRAFT,
        Permission.SCOPE_PROPOSE,
        Permission.EVIDENCE_COLLECT,
        Permission.EVIDENCE_READ,
        Permission.ACCOUNT_QUERY_REQUEST,
        Permission.ACCOUNT_QUERY_EXECUTE,
        Permission.ASSISTANCE_PACKAGE_PREPARE,
        Permission.RESTRICTED_INTERFERENCE_WRITE,
        Permission.RESTRICTED_REFUSAL_WRITE,
        Permission.RESTRICTED_ASSISTANCE_WRITE,
    }),
    ROLE_REVIEWER: frozenset({
        Permission.SCOPE_REVIEW,
        Permission.EVIDENCE_READ,
    }),
    ROLE_UNIT: frozenset({
        Permission.EVIDENCE_DELIVER,
        Permission.DISPUTE_RAISE,
    }),
    ROLE_ASSISTING: frozenset({
        Permission.RECEIPT_RECORD,
    }),
}
