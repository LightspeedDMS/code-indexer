"""
PostgreSQL backend for GroupAccessManager storage (GroupsBackend Protocol).

Story #415: PostgreSQL GroupsDB Backend Migration

Implements the GroupsBackend Protocol using psycopg v3 (sync mode).
All tables (groups, user_group_membership, repo_group_access, audit_logs)
live in the main PostgreSQL database.

Usage:
    from code_indexer.server.storage.postgres.groups_backend import GroupsPostgresBackend

    backend = GroupsPostgresBackend(pool)
    groups = backend.get_all_groups()
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from code_indexer.server.services.group_access_manager import (
    DefaultGroupCannotBeDeletedError,
    Group,
    GroupHasUsersError,
    GroupMembership,
    RepoGroupAccess,
)


logger = logging.getLogger(__name__)


def _dict_row_factory() -> Any:
    """Return psycopg v3 dict_row row factory, loaded lazily."""
    try:
        from psycopg.rows import dict_row

        return dict_row
    except ImportError as exc:
        raise ImportError(
            "psycopg (v3) is required for GroupsPostgresBackend. "
            "Install with: pip install psycopg"
        ) from exc


def _parse_dt(value: Any) -> datetime:
    """Convert a DB value to a timezone-aware datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return datetime.now(timezone.utc)


def _row_to_group(row: dict) -> Group:
    """Build a Group dataclass from a psycopg row dict."""
    return Group(
        id=row["id"],
        name=row["name"],
        description=row["description"] or "",
        is_default=bool(row["is_default"]),
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_membership(row: dict) -> GroupMembership:
    """Build a GroupMembership dataclass from a psycopg row dict."""
    return GroupMembership(
        user_id=row["user_id"],
        group_id=row["group_id"],
        assigned_at=_parse_dt(row["assigned_at"]),
        assigned_by=row["assigned_by"],
    )


def _row_to_repo_access(row: dict) -> RepoGroupAccess:
    """Build a RepoGroupAccess dataclass from a psycopg row dict."""
    return RepoGroupAccess(
        repo_name=row["repo_name"],
        group_id=row["group_id"],
        granted_at=_parse_dt(row["granted_at"]),
        granted_by=row["granted_by"],
    )


class GroupsPostgresBackend:
    """
    PostgreSQL implementation of the GroupsBackend Protocol.

    Manages groups, user memberships, repo-group access grants, and audit logs
    in the main PostgreSQL database via a psycopg v3 connection pool.
    """

    _SELECT_GROUP = "SELECT id, name, description, is_default, created_at FROM groups"

    def __init__(self, pool: Any) -> None:
        """
        Args:
            pool: An open psycopg v3 ConnectionPool instance.
        """
        self._pool = pool

    def _conn(self) -> Any:
        """Borrow a connection from the pool (context manager)."""
        return self._pool.connection()

    def get_all_groups(self) -> List[Group]:
        """Return all groups: default groups first, then alphabetically."""
        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(f"{self._SELECT_GROUP} ORDER BY is_default DESC, name ASC")
                return [_row_to_group(r) for r in cur.fetchall()]

    def get_group(self, group_id: int) -> Optional[Group]:
        """Return a group by primary key, or None."""
        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(f"{self._SELECT_GROUP} WHERE id = %s", (group_id,))
                row = cur.fetchone()
                return _row_to_group(row) if row else None

    def get_group_by_name(self, name: str) -> Optional[Group]:
        """Return a group by case-insensitive name, or None."""
        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(
                    f"{self._SELECT_GROUP} WHERE LOWER(name) = LOWER(%s)", (name,)
                )
                row = cur.fetchone()
                return _row_to_group(row) if row else None

    def create_group(self, name: str, description: str) -> Group:
        """
        Create a new custom group.

        Raises:
            ValueError: if a group with the same name already exists.
        """
        now = datetime.now(timezone.utc)
        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(
                    "SELECT id FROM groups WHERE LOWER(name) = LOWER(%s)", (name,)
                )
                if cur.fetchone():
                    raise ValueError(f"Group with name '{name}' already exists")
                cur.execute(
                    "INSERT INTO groups (name, description, is_default, created_at) "
                    "VALUES (%s, %s, FALSE, %s) RETURNING id",
                    (name, description, now),
                )
                row = cur.fetchone()
            conn.commit()

        assert row is not None
        group = self.get_group(row["id"])
        assert group is not None
        return group

    def update_group(
        self,
        group_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[Group]:
        """
        Update a custom group's name and/or description.

        Returns updated group or None if not found.

        Raises:
            ValueError: if targeting a default group or name already exists.
        """
        group = self.get_group(group_id)
        if group is None:
            return None
        if group.is_default:
            raise ValueError("Cannot update default groups")

        updates: List[str] = []
        params: List[Any] = []
        if name is not None:
            updates.append("name = %s")
            params.append(name)
        if description is not None:
            updates.append("description = %s")
            params.append(description)
        if not updates:
            return group

        params.append(group_id)
        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                if name is not None:
                    cur.execute(
                        "SELECT id FROM groups "
                        "WHERE LOWER(name) = LOWER(%s) AND id != %s",
                        (name, group_id),
                    )
                    if cur.fetchone():
                        raise ValueError(f"Group with name '{name}' already exists")
                cur.execute(
                    f"UPDATE groups SET {', '.join(updates)} WHERE id = %s", params
                )
            conn.commit()
        return self.get_group(group_id)

    def delete_group(self, group_id: int) -> bool:
        """
        Delete a custom group. Returns True if deleted, False if not found.

        Raises:
            DefaultGroupCannotBeDeletedError: if it is a default group.
            GroupHasUsersError: if the group still has members.
        """
        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(f"{self._SELECT_GROUP} WHERE id = %s", (group_id,))
                row = cur.fetchone()
                if row is None:
                    return False

                grp = _row_to_group(row)
                if grp.is_default:
                    raise DefaultGroupCannotBeDeletedError(
                        f"Cannot delete default group: {grp.name}"
                    )

                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM user_group_membership "
                    "WHERE group_id = %s",
                    (group_id,),
                )
                cnt_row = cur.fetchone()
                if cnt_row and cnt_row["cnt"] > 0:
                    raise GroupHasUsersError(
                        f"Cannot delete group with {cnt_row['cnt']} active user(s)"
                    )

                cur.execute(
                    "DELETE FROM repo_group_access WHERE group_id = %s", (group_id,)
                )
                cur.execute(
                    "DELETE FROM user_group_membership WHERE group_id = %s", (group_id,)
                )
                cur.execute("DELETE FROM groups WHERE id = %s", (group_id,))
            conn.commit()
        return True

    # ------------------------------------------------------------------
    # User membership
    # ------------------------------------------------------------------

    def assign_user_to_group(
        self, user_id: str, group_id: int, assigned_by: str
    ) -> None:
        """Assign (or re-assign) a user to a group, enforcing 1:1 via PK upsert."""
        now = datetime.now(timezone.utc)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO user_group_membership "
                    "(user_id, group_id, assigned_at, assigned_by) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (user_id) DO UPDATE SET "
                    "group_id = EXCLUDED.group_id, "
                    "assigned_at = EXCLUDED.assigned_at, "
                    "assigned_by = EXCLUDED.assigned_by",
                    (user_id, group_id, now, assigned_by),
                )
            conn.commit()

    def remove_user_from_group(self, user_id: str, group_id: int) -> bool:
        """Remove a user from a specific group. Idempotent — always returns True."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_group_membership "
                    "WHERE user_id = %s AND group_id = %s",
                    (user_id, group_id),
                )
            conn.commit()
        return True

    def get_user_group(self, user_id: str) -> Optional[Group]:
        """Return the group the user belongs to, or None."""
        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(
                    "SELECT g.id, g.name, g.description, g.is_default, g.created_at "
                    "FROM groups g "
                    "JOIN user_group_membership m ON g.id = m.group_id "
                    "WHERE m.user_id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
                return _row_to_group(row) if row else None

    def get_user_membership(self, user_id: str) -> Optional[GroupMembership]:
        """Return the full membership record for a user, or None."""
        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(
                    "SELECT user_id, group_id, assigned_at, assigned_by "
                    "FROM user_group_membership WHERE user_id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
                return _row_to_membership(row) if row else None

    def get_users_in_group(self, group_id: int) -> List[str]:
        """Return all user IDs in a group."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM user_group_membership WHERE group_id = %s",
                    (group_id,),
                )
                return [row[0] for row in cur.fetchall()]

    def get_user_count_in_group(self, group_id: int) -> int:
        """Return the number of users in a group."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM user_group_membership WHERE group_id = %s",
                    (group_id,),
                )
                row = cur.fetchone()
                return row[0] if row else 0

    # ------------------------------------------------------------------
    # Repository access
    # ------------------------------------------------------------------

    def grant_repo_access(self, repo_name: str, group_id: int, granted_by: str) -> bool:
        """
        Grant group access to a repo. Returns True if newly granted.

        Raises:
            ValueError: if the group does not exist.
        """
        if self.get_group(group_id) is None:
            raise ValueError(f"Group with ID {group_id} not found")

        now = datetime.now(timezone.utc)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO repo_group_access "
                    "(repo_name, group_id, granted_at, granted_by) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (repo_name, group_id) DO NOTHING",
                    (repo_name, group_id, now, granted_by),
                )
                newly_inserted: bool = cur.rowcount > 0
            conn.commit()
        return newly_inserted

    def revoke_repo_access(self, repo_name: str, group_id: int) -> bool:
        """
        Revoke a group's access to a repo. Returns True if revoked.

        Raises:
            CidxMetaCannotBeRevokedError: if repo_name is cidx-meta.
        """
        from code_indexer.server.services.group_access_manager import (
            CidxMetaCannotBeRevokedError,
        )
        from code_indexer.server.services.constants import CIDX_META_REPO

        if repo_name == CIDX_META_REPO:
            raise CidxMetaCannotBeRevokedError(
                f"{CIDX_META_REPO} access cannot be revoked from any group"
            )

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM repo_group_access "
                    "WHERE repo_name = %s AND group_id = %s",
                    (repo_name, group_id),
                )
                revoked: bool = cur.rowcount > 0
            conn.commit()
        return revoked

    def get_group_repos(self, group_id: int) -> List[str]:
        """Return repos accessible by a group. cidx-meta is always prepended."""
        from code_indexer.server.services.constants import CIDX_META_REPO

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT repo_name FROM repo_group_access "
                    "WHERE group_id = %s ORDER BY LOWER(repo_name)",
                    (group_id,),
                )
                repos = [row[0] for row in cur.fetchall()]
        return [CIDX_META_REPO] + repos

    def get_repo_groups(self, repo_name: str) -> List[Group]:
        """Return all groups that can access a repo (all groups for cidx-meta)."""
        from code_indexer.server.services.constants import CIDX_META_REPO

        if repo_name == CIDX_META_REPO:
            return self.get_all_groups()

        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(
                    "SELECT g.id, g.name, g.description, g.is_default, g.created_at "
                    "FROM groups g "
                    "JOIN repo_group_access rga ON g.id = rga.group_id "
                    "WHERE rga.repo_name = %s ORDER BY LOWER(g.name)",
                    (repo_name,),
                )
                return [_row_to_group(r) for r in cur.fetchall()]

    def get_repo_access(
        self, repo_name: str, group_id: int
    ) -> Optional[RepoGroupAccess]:
        """Return the access record for a repo-group combination, or None."""
        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(
                    "SELECT repo_name, group_id, granted_at, granted_by "
                    "FROM repo_group_access WHERE repo_name = %s AND group_id = %s",
                    (repo_name, group_id),
                )
                row = cur.fetchone()
                return _row_to_repo_access(row) if row else None

    def auto_assign_golden_repo(self, repo_name: str) -> None:
        """Auto-assign a new golden repo to admins and powerusers groups."""
        from code_indexer.server.services.constants import (
            DEFAULT_GROUP_ADMINS,
            DEFAULT_GROUP_POWERUSERS,
        )

        for group_name in (DEFAULT_GROUP_ADMINS, DEFAULT_GROUP_POWERUSERS):
            grp = self.get_group_by_name(group_name)
            if grp:
                self.grant_repo_access(repo_name, grp.id, "system:auto-assignment")

    # ------------------------------------------------------------------
    # Users with groups + existence check
    # ------------------------------------------------------------------

    def get_all_users_with_groups(
        self, limit: Optional[int] = None, offset: int = 0
    ) -> tuple:
        """Return (list_of_user_dicts, total_count) with group membership info."""
        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute("SELECT COUNT(*) AS cnt FROM user_group_membership")
                total_row = cur.fetchone()
                total = total_row["cnt"] if total_row else 0

                query = (
                    "SELECT m.user_id, m.group_id, g.name AS group_name, "
                    "m.assigned_at, m.assigned_by "
                    "FROM user_group_membership m "
                    "JOIN groups g ON m.group_id = g.id "
                    "ORDER BY m.user_id ASC"
                )
                params: List[Any] = []
                if limit is not None:
                    query += " LIMIT %s OFFSET %s"
                    params.extend([limit, offset])
                cur.execute(query, params)
                rows = cur.fetchall()

        return (
            [
                {
                    "user_id": r["user_id"],
                    "group_id": r["group_id"],
                    "group_name": r["group_name"],
                    "assigned_at": r["assigned_at"],
                    "assigned_by": r["assigned_by"],
                }
                for r in rows
            ],
            total,
        )

    def user_exists(self, user_id: str) -> bool:
        """Return True if the user has any group membership record."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM user_group_membership WHERE user_id = %s",
                    (user_id,),
                )
                return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # Audit logging (part of GroupsBackend protocol)
    # ------------------------------------------------------------------

    def log_audit(
        self,
        admin_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        details: Optional[str] = None,
    ) -> None:
        """Insert an audit log entry."""
        now = datetime.now(timezone.utc)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_logs "
                    "(timestamp, admin_id, action_type, target_type, target_id, details) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (now, admin_id, action_type, target_type, target_id, details),
                )
            conn.commit()

    def get_audit_logs(
        self,
        action_type: Optional[str] = None,
        target_type: Optional[str] = None,
        admin_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        exclude_target_type: Optional[str] = None,
    ) -> tuple:
        """Query audit log entries with filters. Returns (list, total_count)."""
        conditions: List[str] = []
        params: List[Any] = []

        if action_type:
            conditions.append("action_type = %s")
            params.append(action_type)
        if target_type:
            conditions.append("target_type = %s")
            params.append(target_type)
        if admin_id:
            conditions.append("admin_id = %s")
            params.append(admin_id)
        if date_from:
            conditions.append("timestamp >= %s")
            params.append(f"{date_from}T00:00:00")
        if date_to:
            conditions.append("timestamp <= %s")
            params.append(f"{date_to}T23:59:59")
        if exclude_target_type:
            conditions.append("target_type != %s")
            params.append(exclude_target_type)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM audit_logs {where}", params)
                cnt_row = cur.fetchone()
                total = cnt_row["cnt"] if cnt_row else 0

                query = (
                    f"SELECT id, timestamp, admin_id, action_type, "
                    f"target_type, target_id, details "
                    f"FROM audit_logs {where} ORDER BY timestamp DESC"
                )
                page_params = list(params)
                if limit is not None:
                    query += " LIMIT %s OFFSET %s"
                    page_params.extend([limit, offset])
                elif offset > 0:
                    query += " OFFSET %s"
                    page_params.append(offset)

                cur.execute(query, page_params)
                logs = [dict(r) for r in cur.fetchall()]

        return logs, total
