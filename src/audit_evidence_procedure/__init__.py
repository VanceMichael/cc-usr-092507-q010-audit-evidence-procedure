"""审计取证授权与程序留痕服务。"""

from .context import load_context
from .service import AuditEvidenceProcedureService
from .clock import MutableClock, SystemClock
from .constants import (
    CASE_CATEGORIES,
    DeadlineKind,
    DocKind,
    Permission,
    RestrictedKind,
)
from .errors import (
    AccountQueryGuardFailed,
    DomainError,
    DuplicateConflict,
    InvalidDocument,
    MaterialAlreadyRestricted,
    MinimumNecessaryViolation,
    NoApprovedVersion,
    NotFound,
    PermissionDenied,
    ReceiptConflict,
)

__all__ = [
    "load_context",
    "AuditEvidenceProcedureService",
    "MutableClock",
    "SystemClock",
    "CASE_CATEGORIES",
    "DeadlineKind",
    "DocKind",
    "Permission",
    "RestrictedKind",
    "DomainError",
    "AccountQueryGuardFailed",
    "DuplicateConflict",
    "InvalidDocument",
    "MaterialAlreadyRestricted",
    "MinimumNecessaryViolation",
    "NoApprovedVersion",
    "NotFound",
    "PermissionDenied",
    "ReceiptConflict",
]
