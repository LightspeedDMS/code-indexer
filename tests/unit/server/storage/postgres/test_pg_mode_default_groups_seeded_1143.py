"""
Tests for Bug #1143: Default groups not seeded in PostgreSQL/cluster mode.

ROOT CAUSE:
  GroupAccessManager.__init__ (group_access_manager.py:126-128) returns early
  when a storage_backend (PG mode) is provided, calling neither _ensure_schema()
  nor _bootstrap_default_groups(). GroupsPostgresBackend.__init__ (groups_backend.py:98-103)
  only stores the pool — never inserts the default groups rows.

FIX:
  GroupsPostgresBackend must expose bootstrap_default_groups() (idempotent
  INSERT ... ON CONFLICT DO NOTHING). GroupAccessManager.__init__ must call it
  when storage_backend is provided instead of returning immediately.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from code_indexer.server.services.constants import (
    DEFAULT_GROUP_ADMINS,
    DEFAULT_GROUP_POWERUSERS,
    DEFAULT_GROUP_USERS,
)
from code_indexer.server.services.group_access_manager import (
    DEFAULT_GROUPS,
    Group,
    GroupAccessManager,
    GroupMembership,
    RepoGroupAccess,
)


# ---------------------------------------------------------------------------
# Minimal real backend fake (SQLite in-memory — no mocks)
#
# Implements only the GroupsBackend protocol methods exercised by:
#   - GroupAccessManager.__init__ (bootstrap_default_groups)
#   - Test assertions (get_all_groups, get_group_by_name)
#
# All other protocol methods are stubs — they satisfy the Protocol interface
# but are not exercised by constructor or test assertions.
# ---------------------------------------------------------------------------


class _MinimalGroupsBackend:
    """
    Minimal in-memory SQLite fake that implements the GroupsBackend protocol.

    Mirrors the broken GroupsPostgresBackend: __init__ stores only the
    connection and does NOT seed default groups, so tests can verify that
    GroupAccessManager triggers the bootstrap explicitly.
    """

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE groups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL UNIQUE,
                description TEXT    NOT NULL DEFAULT '',
                is_default  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL
            );
            CREATE TABLE user_group_membership (
                user_id     TEXT    PRIMARY KEY,
                group_id    INTEGER NOT NULL REFERENCES groups(id),
                assigned_at TEXT    NOT NULL,
                assigned_by TEXT    NOT NULL
            );
            CREATE TABLE repo_group_access (
                repo_name   TEXT    NOT NULL,
                group_id    INTEGER NOT NULL REFERENCES groups(id),
                granted_at  TEXT    NOT NULL,
                granted_by  TEXT,
                PRIMARY KEY (repo_name, group_id)
            );
            CREATE TABLE audit_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                admin_id    TEXT    NOT NULL,
                action_type TEXT    NOT NULL,
                target_type TEXT    NOT NULL,
                target_id   TEXT    NOT NULL,
                details     TEXT
            );
            """
        )
        self._conn.commit()
        # Intentionally NOT seeding default groups — mirrors the broken PG backend.

    def close(self) -> None:
        self._conn.close()

    # --- active methods: bootstrapping and test assertions ---

    def bootstrap_default_groups(self) -> None:
        """Idempotent seed of admins/powerusers/users rows."""
        now = datetime.now(timezone.utc).isoformat()
        for group_def in DEFAULT_GROUPS:
            self._conn.execute(
                "INSERT OR IGNORE INTO groups "
                "(name, description, is_default, created_at) VALUES (?, ?, 1, ?)",
                (group_def["name"], group_def["description"], now),
            )
        self._conn.commit()

    def get_all_groups(self) -> List[Group]:
        cur = self._conn.execute(
            "SELECT id, name, description, is_default, created_at "
            "FROM groups ORDER BY is_default DESC, name ASC"
        )
        return [self._row_to_group(r) for r in cur.fetchall()]

    def get_group_by_name(self, name: str) -> Optional[Group]:
        cur = self._conn.execute(
            "SELECT id, name, description, is_default, created_at "
            "FROM groups WHERE LOWER(name) = LOWER(?)",
            (name,),
        )
        row = cur.fetchone()
        return self._row_to_group(row) if row else None

    # --- protocol stubs (satisfy GroupsBackend interface, not exercised here) ---

    def _row_to_group(self, row: Any) -> Group:
        return Group(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            is_default=bool(row["is_default"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def get_group(self, group_id: int) -> Optional[Group]:
        cur = self._conn.execute(
            "SELECT id, name, description, is_default, created_at FROM groups WHERE id = ?",
            (group_id,),
        )
        row = cur.fetchone()
        return self._row_to_group(row) if row else None

    def create_group(self, name: str, description: str) -> Group:
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "INSERT INTO groups (name, description, is_default, created_at) VALUES (?, ?, 0, ?)",
            (name, description, now),
        )
        self._conn.commit()
        assert cur.lastrowid is not None
        result = self.get_group(cur.lastrowid)
        assert result is not None
        return result

    def update_group(
        self,
        group_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[Group]:
        return self.get_group(group_id)

    def delete_group(self, group_id: int) -> bool:
        return True

    def assign_user_to_group(
        self, user_id: str, group_id: int, assigned_by: str
    ) -> None:
        pass

    def remove_user_from_group(self, user_id: str, group_id: int) -> bool:
        return True

    def get_user_group(self, user_id: str) -> Optional[Group]:
        return None

    def get_user_membership(self, user_id: str) -> Optional[GroupMembership]:
        return None

    def get_users_in_group(self, group_id: int) -> List[str]:
        return []

    def get_user_count_in_group(self, group_id: int) -> int:
        return 0

    def grant_repo_access(self, repo_name: str, group_id: int, granted_by: str) -> bool:
        return True

    def revoke_repo_access(self, repo_name: str, group_id: int) -> bool:
        return True

    def get_group_repos(self, group_id: int) -> List[str]:
        return []

    def get_repo_groups(self, repo_name: str) -> list:
        return []

    def get_repo_access(
        self, repo_name: str, group_id: int
    ) -> Optional[RepoGroupAccess]:
        return None

    def auto_assign_golden_repo(self, repo_name: str) -> None:
        pass

    def get_all_users_with_groups(
        self, limit: Optional[int] = None, offset: int = 0
    ) -> tuple:
        return [], 0

    def user_exists(self, user_id: str) -> bool:
        return False

    def log_audit(
        self,
        admin_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        details: Optional[str] = None,
    ) -> None:
        pass

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
        return [], 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGroupsPostgresBackendHasBootstrapMethod:
    """GroupsPostgresBackend must expose bootstrap_default_groups() (Bug #1143)."""

    def test_bootstrap_default_groups_method_exists(self) -> None:
        """
        GIVEN the GroupsPostgresBackend class
        THEN it must have a callable bootstrap_default_groups().
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        assert hasattr(GroupsPostgresBackend, "bootstrap_default_groups") and callable(
            getattr(GroupsPostgresBackend, "bootstrap_default_groups")
        ), (
            "Bug #1143: GroupsPostgresBackend is missing bootstrap_default_groups(). "
            "Add an idempotent INSERT ... ON CONFLICT DO NOTHING for the three "
            "default groups (admins/powerusers/users)."
        )


class TestPgModeDefaultGroupsSeeded:
    """
    Bug #1143: GroupAccessManager must seed default groups when constructed
    with a PG-mode backend.

    Uses _MinimalGroupsBackend (real SQLite in-memory, no mocks) to exercise
    the PG-mode constructor path without requiring a live PostgreSQL server.
    """

    def test_pg_mode_constructor_seeds_all_three_default_groups(
        self, tmp_path: Path
    ) -> None:
        """
        GIVEN a fresh backend with no groups (mirrors a fresh PG deployment)
        WHEN GroupAccessManager is constructed with that backend (PG mode)
        THEN all three default groups exist in the backend.
        """
        backend = _MinimalGroupsBackend()
        try:
            assert backend.get_group_by_name(DEFAULT_GROUP_ADMINS) is None, (
                "Precondition: backend must start empty."
            )

            GroupAccessManager(db_path=tmp_path / "unused.db", storage_backend=backend)

            group_names = {g.name for g in backend.get_all_groups()}
            assert DEFAULT_GROUP_ADMINS in group_names, (
                "Bug #1143: 'admins' group not seeded in PG mode."
            )
            assert DEFAULT_GROUP_POWERUSERS in group_names, (
                "Bug #1143: 'powerusers' group not seeded in PG mode."
            )
            assert DEFAULT_GROUP_USERS in group_names, (
                "Bug #1143: 'users' group not seeded in PG mode."
            )
        finally:
            backend.close()

    def test_pg_mode_bootstrap_is_idempotent(self, tmp_path: Path) -> None:
        """
        GIVEN a backend that already has the default groups (from a previous boot)
        WHEN GroupAccessManager is constructed again with the same backend
        THEN exactly three default groups exist (no duplicates).
        """
        backend = _MinimalGroupsBackend()
        try:
            GroupAccessManager(db_path=tmp_path / "unused.db", storage_backend=backend)
            GroupAccessManager(db_path=tmp_path / "unused.db", storage_backend=backend)

            default_groups = [g for g in backend.get_all_groups() if g.is_default]
            assert len(default_groups) == 3, (
                f"Bug #1143: Expected exactly 3 default groups after two constructions "
                f"(idempotency check), got {len(default_groups)}: "
                f"{[g.name for g in default_groups]}"
            )
        finally:
            backend.close()

    def test_sqlite_mode_bootstrap_unaffected(self, tmp_path: Path) -> None:
        """
        Anti-regression: SQLite-mode GroupAccessManager still seeds default groups.
        """
        manager = GroupAccessManager(db_path=tmp_path / "groups.db")

        group_names = {g.name for g in manager.get_all_groups()}
        assert DEFAULT_GROUP_ADMINS in group_names, (
            "Regression: SQLite-mode 'admins' group missing after PG-mode fix."
        )
        assert DEFAULT_GROUP_POWERUSERS in group_names, (
            "Regression: SQLite-mode 'powerusers' group missing after PG-mode fix."
        )
        assert DEFAULT_GROUP_USERS in group_names, (
            "Regression: SQLite-mode 'users' group missing after PG-mode fix."
        )
