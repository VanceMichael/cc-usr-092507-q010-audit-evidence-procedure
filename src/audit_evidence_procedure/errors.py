"""领域错误体系。

所有可预期的业务拒绝都抛出 ``DomainError`` 的子类，
便于调用方按程序语义处理，而不是依赖字符串匹配。
"""

from __future__ import annotations


class DomainError(Exception):
    """全部业务错误的基类。"""


class NotFound(DomainError):
    """引用的案件、人员或记录不存在。"""


class PermissionDenied(DomainError):
    """当前人员角色不具备执行该操作所需权限。"""


class NoApprovedVersion(DomainError):
    """计划、通知、范围或审计组尚无批准生效版本。"""


class InvalidDocument(DomainError):
    """批准版本内容不符合结构要求。"""


class ReviewerParticipated(DomainError):
    """复核人曾参与该事项的提出，不得复核本人或本方提出的事项。"""


class DuplicateConflict(DomainError):
    """同一交付号出现异文，需要转入证据争议，不能静默覆盖。"""


class MaterialAlreadyRestricted(DomainError):
    """材料已被封存限制，不能重复封存。"""


class AccountQueryGuardFailed(DomainError):
    """账户查询前置核验未全部通过（签发权限、双人执行、通知期限）。"""

    def __init__(self, message: str, checks: dict | None = None):
        super().__init__(message)
        self.checks = checks or {}


class MinimumNecessaryViolation(DomainError):
    """拟交付金融机构的信息超出完成协助所必需的范围。"""


class ReceiptConflict(DomainError):
    """回执试图反向改写查询申请；申请只允许追加回执，不允许被回执修改。"""


class StaleCaseWatermark(DomainError):
    """提交所依据的案件水位已过期，需要基于最新水位重新提交整批操作。"""


class DeadlineKindUnknown(DomainError):
    """期限类型未注册或不属于受管理的程序期限。"""
