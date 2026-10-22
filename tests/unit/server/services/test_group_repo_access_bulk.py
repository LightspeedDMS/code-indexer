"""Bulk repository access for one group: audited grant and all-or-nothing revoke.

Real SQLite groups store, real AuditLogService on its own file.  Database
failures are injected with real SQLite triggers (RAISE(ABORT)), never mocks.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from code_indexer.server.services.audit_log_service import AuditLogService
from code_indexer.server.services.group_access_manager import (
    GroupAccessManager,
    GroupNotFoundError,
)
from tests.unit.server._audit_read_support import stored_audit_rows

_ACTOR = "example-admin"
_POISON = "example-poison"


def _manager(tmp_path: Path, name: str = "groups.db") -> GroupAccessManager:
    manager = GroupAccessManager(tmp_path / name)
    manager.set_audit_service(AuditLogService(tmp_path / "audit.db"))
    return manager


def _rows(tmp_path: Path, action: str) -> List[Tuple[Any, ...]]:
    """(actor, outcome, target_id, details) of *action* rows, oldest first.

    stored_audit_rows returns newest first; reversing gives write order.
    """
    stored = stored_audit_rows(tmp_path / "audit.db")
    rows = [r for r in stored if r["action_type"] == action]
    rows.reverse()
    return [
        (
            r["admin_id"],
            r["outcome"],
            r["target_id"],
            json.loads(r["details"]) if r["details"] else None,
        )
        for r in rows
    ]


def _poison(db: Path, statement: str) -> None:
    """A real trigger that aborts the statement touching the poison name."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _granted(manager: GroupAccessManager, group_id: int) -> List[str]:
    """Granted names in get_group_repos' name order, without cidx-meta."""
    return manager.get_group_repos(group_id)[1:]


@pytest.fixture()
def env(tmp_path: Path) -> Dict[str, Any]:
    manager = _manager(tmp_path)
    group = manager.create_group("example-team", "")
    return {"m": manager, "g": group, "tmp": tmp_path}


class TestBulkGrant:
    def test_grants_only_new_names_and_audits_each(self, env) -> None:
        m, g, tmp = env["m"], env["g"], env["tmp"]
        m.grant_repo_access("example-a", g.id, "setup")
        names = ["example-a", "example-b", "example-c", "example-b"]
        granted = m.grant_repos_access_audited(names, g.id, actor=_ACTOR)
        assert granted == ["example-b", "example-c"]
        assert _granted(m, g.id) == ["example-a", "example-b", "example-c"]
        assert _rows(tmp, "repo_access_grant") == [
            (_ACTOR, "success", alias, {"repo": alias, "group": "example-team"})
            for alias in ("example-b", "example-c")
        ]

    def test_all_noop_grant_changes_nothing_and_writes_no_row(self, env) -> None:
        m, g, tmp = env["m"], env["g"], env["tmp"]
        m.grant_repo_access("example-a", g.id, "setup")
        assert m.grant_repos_access_audited(["example-a"], g.id, actor=_ACTOR) == []
        assert m.grant_repos_access_audited([], g.id, actor=_ACTOR) == []
        assert _rows(tmp, "repo_access_grant") == []

    def test_cidx_meta_is_never_granted(self, env) -> None:
        m, g, tmp = env["m"], env["g"], env["tmp"]
        granted = m.grant_repos_access_audited(
            ["cidx-meta", "example-a"], g.id, actor=_ACTOR
        )
        assert granted == ["example-a"]
        assert [r[2] for r in _rows(tmp, "repo_access_grant")] == ["example-a"]

    def test_unknown_group_raises_and_records_one_failure(self, env) -> None:
        m, tmp = env["m"], env["tmp"]
        with pytest.raises(GroupNotFoundError):
            m.grant_repos_access_audited(["example-a"], 987654, actor=_ACTOR)
        assert [r[1] for r in _rows(tmp, "repo_access_grant")] == ["failure"]

    def test_database_failure_rolls_back_every_grant(self, env) -> None:
        m, g, tmp = env["m"], env["g"], env["tmp"]
        _poison(
            tmp / "groups.db",
            "CREATE TRIGGER poison BEFORE INSERT ON repo_group_access "
            f"WHEN NEW.repo_name = '{_POISON}' BEGIN SELECT RAISE(ABORT, 'x'); END",
        )
        names = ["example-a", _POISON, "example-c"]
        with pytest.raises(sqlite3.DatabaseError):
            m.grant_repos_access_audited(names, g.id, actor=_ACTOR)
        assert _granted(m, g.id) == []
        assert [r[1] for r in _rows(tmp, "repo_access_grant")] == ["failure"]


class TestBulkRevoke:
    def test_revokes_granted_names_returns_aliases_and_audits(self, env) -> None:
        m, g, tmp = env["m"], env["g"], env["tmp"]
        for alias in ("example-a", "example-b", "example-c"):
            m.grant_repo_access(alias, g.id, "setup")
        names = ["cidx-meta", "example-a", "example-c"]
        revoked = m.revoke_repos_access_audited_aliases(names, g.id, actor=_ACTOR)
        assert revoked == ["example-a", "example-c"]
        assert _granted(m, g.id) == ["example-b"]
        assert _rows(tmp, "repo_access_revoke") == [
            (_ACTOR, "success", alias, {"repo": alias, "group": "example-team"})
            for alias in ("example-a", "example-c")
        ]

    def test_names_not_in_group_write_one_summary_failure_row(self, env) -> None:
        m, g, tmp = env["m"], env["g"], env["tmp"]
        m.grant_repo_access("example-a", g.id, "setup")
        names = ["example-a", "example-x", "example-y"]
        assert m.revoke_repos_access_audited(names, g.id, actor=_ACTOR) == 1
        assert _rows(tmp, "repo_access_revoke")[1:] == [
            (
                _ACTOR,
                "failure",
                "unresolved",
                {"group": "example-team", "not_in_group_count": 2},
            )
        ]

    def test_database_failure_mid_batch_revokes_nothing(self, env) -> None:
        m, g, tmp = env["m"], env["g"], env["tmp"]
        for alias in ("example-a", _POISON, "example-c"):
            m.grant_repo_access(alias, g.id, "setup")
        _poison(
            tmp / "groups.db",
            "CREATE TRIGGER poison BEFORE DELETE ON repo_group_access "
            f"WHEN OLD.repo_name = '{_POISON}' BEGIN SELECT RAISE(ABORT, 'x'); END",
        )
        names = ["example-a", _POISON, "example-c"]
        with pytest.raises(sqlite3.DatabaseError):
            m.revoke_repos_access_audited(names, g.id, actor=_ACTOR)
        assert _granted(m, g.id) == ["example-a", "example-c", _POISON]
        assert [r[1] for r in _rows(tmp, "repo_access_revoke")] == ["failure"]


class TestSoloBackendShape:
    def test_outer_manager_delegates_the_batch_to_its_backend(
        self, tmp_path: Path
    ) -> None:
        inner = GroupAccessManager(tmp_path / "groups.db")
        outer = GroupAccessManager(tmp_path / "unused.db", storage_backend=inner)
        outer.set_audit_service(AuditLogService(tmp_path / "audit.db"))
        group = outer.create_group("example-team", "")
        names = ["example-a", "example-b"]
        assert outer.grant_repos_access_audited(names, group.id, actor=_ACTOR) == names
        assert _granted(inner, group.id) == names
        revoked = outer.revoke_repos_access_audited_aliases(
            names, group.id, actor=_ACTOR
        )
        assert revoked == names
        assert _granted(inner, group.id) == []
