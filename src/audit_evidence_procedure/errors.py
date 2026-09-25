"""领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """领域错误基类。"""


class PermissionDenied(DomainError):
    """当前主体缺少所需权限，或违反分离职责约束。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class ValidationError(DomainError):
    """输入或当前状态不满足程序要求。"""


class ConflictError(DomainError):
    """当前状态与请求冲突。"""
