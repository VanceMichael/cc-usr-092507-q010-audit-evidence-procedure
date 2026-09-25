"""身份、角色与权限解析。"""

from __future__ import annotations

import uuid

from ..constants import ROLE_PERMISSIONS, ROLES, Permission
from ..errors import NotFound, PermissionDenied
from ..storage import Storage


class AccessService:
    def __init__(self, storage: Storage):
        self.storage = storage

    # ---- 人员登记 -------------------------------------------------

    def register_person(self, person_id: str, name: str, role: str) -> str:
        if role not in ROLES:
            raise ValueError(f"未知角色：{role}")
        with self.storage.tx() as conn:
            conn.execute(
                "INSERT INTO persons (id, name, role) VALUES (?, ?, ?)",
                (person_id, name, role),
            )
        return person_id

    def person(self, person_id: str) -> dict:
        row = self.storage.conn.execute(
            "SELECT id, name, role FROM persons WHERE id = ?", (person_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"人员不存在：{person_id}")
        return dict(row)

    def require_role(self, person_id: str, role: str) -> None:
        if self.person(person_id)["role"] != role:
            raise PermissionDenied(f"需要角色 {role}")

    # ---- 权限解析 -------------------------------------------------

    def effective_permissions(self, person_id: str, case_id: str) -> frozenset[Permission]:
        """角色权限 ∪ 逐案授权。逐案授权是查看金融材料等敏感权限的唯一下放途径。"""

        role = self.person(person_id)["role"]
        perms = set(ROLE_PERMISSIONS[role])
        rows = self.storage.conn.execute(
            "SELECT permission FROM case_grants WHERE case_id = ? AND person_id = ?",
            (case_id, person_id),
        ).fetchall()
        perms.update(Permission(row["permission"]) for row in rows)
        return frozenset(perms)

    def grant_case_permission(
        self, case_id: str, person_id: str, permission: Permission,
        granted_by: str, at: str,
    ) -> None:
        self.person(person_id)  # 人员必须存在
        with self.storage.tx() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO case_grants
                    (case_id, person_id, permission, granted_by, granted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (case_id, person_id, permission.value, granted_by, at),
            )

    def has_permission(
        self, person_id: str, case_id: str, permission: Permission
    ) -> bool:
        return permission in self.effective_permissions(person_id, case_id)

    def require_permission(
        self, person_id: str, case_id: str, permission: Permission
    ) -> frozenset[Permission]:
        perms = self.effective_permissions(person_id, case_id)
        if permission not in perms:
            raise PermissionDenied(
                f"人员 {person_id} 缺少权限 {permission.value}（普通项目权限不得查看金融账户"
                "查询材料或干预登记）"
            )
        return perms

    def new_id(self) -> str:
        return uuid.uuid4().hex
