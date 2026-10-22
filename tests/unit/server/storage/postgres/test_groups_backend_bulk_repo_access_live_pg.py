"""Bulk repository access on real PostgreSQL, through the groups_backend path.

Gated by TEST_POSTGRES_DSN.  Each test gets a fresh database migrated by the
real MigrationRunner; failures are injected with a real plpgsql trigger.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterator, List, Tuple

import pytest

from code_indexer.server.services.audit_log_service import AuditLogService
from code_indexer.server.services.group_access_manager import GroupAccessManager
from tests.unit.server._audit_read_support import stored_audit_rows

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
_ACTOR = "example-admin"
_POISON = "example-poison"

# ^ perf each test runs every migration on a fresh database (~7 s, more under load)
pytestmark = [
    pytest.mark.skipif(
        not _DSN, reason="No PostgreSQL available (set TEST_POSTGRES_DSN to enable)"
    ),
    pytest.mark.timeout(60),
]


def _dsn_for(dbname: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(_DSN)
    params["dbname"] = dbname
    return make_conninfo(**params)  # type: ignore[arg-type]


@pytest.fixture()
def pg(tmp_path: Path) -> Iterator[Tuple[GroupAccessManager, Any, str, Path]]:
    import psycopg
    from psycopg_pool import ConnectionPool

    from code_indexer.server.storage.postgres.groups_backend import (
        GroupsPostgresBackend,
    )
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    name = f"bulk_access_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = _dsn_for(name)
    pool = None
    try:
        with MigrationRunner(dsn) as runner:
            runner.run()
        pool = ConnectionPool(dsn, min_size=1, max_size=2, open=True)
        manager = GroupAccessManager(
            tmp_path / "unused.db", storage_backend=GroupsPostgresBackend(pool)
        )
        manager.set_audit_service(AuditLogService(tmp_path / "audit.db"))
        group = manager.create_group("example-team", "")
        yield manager, group, dsn, tmp_path
    finally:
        if pool is not None:
            pool.close()
        with psycopg.connect(_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _poison(dsn: str, event: str, row: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE FUNCTION poison() RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN IF {row}.repo_name = '{_POISON}' THEN "
            "RAISE EXCEPTION 'injected'; END IF; "
            f"RETURN {row}; END $$"
        )
        conn.execute(
            f"CREATE TRIGGER poison BEFORE {event} ON repo_group_access "
            "FOR EACH ROW EXECUTE FUNCTION poison()"
        )


def _granted(manager: GroupAccessManager, group_id: int) -> List[str]:
    """Stored grants by name; get_group_repos always prepends cidx-meta."""
    return manager.get_group_repos(group_id)[1:]


def _rows(tmp: Path, action: str) -> List[Tuple[Any, ...]]:
    """(outcome, target_id, details) of *action* rows in write (id) order."""
    stored = stored_audit_rows(tmp / "audit.db")
    rows = sorted(
        (r for r in stored if r["action_type"] == action), key=lambda r: r["id"]
    )
    return [
        (
            r["outcome"],
            r["target_id"],
            json.loads(r["details"]) if r["details"] else None,
        )
        for r in rows
    ]


def test_bulk_grant_and_revoke_round_trip(pg) -> None:
    manager, group, _dsn, tmp = pg
    manager.grant_repo_access("example-a", group.id, "setup")
    names = ["example-a", "example-b", "example-c"]
    assert manager.grant_repos_access_audited(names, group.id, actor=_ACTOR) == [
        "example-b",
        "example-c",
    ]
    assert manager.grant_repos_access_audited(names, group.id, actor=_ACTOR) == []
    assert _granted(manager, group.id) == names
    assert _rows(tmp, "repo_access_grant") == [
        ("success", alias, {"repo": alias, "group": "example-team"})
        for alias in ("example-b", "example-c")
    ]
    revoked = manager.revoke_repos_access_audited_aliases(
        ["example-a", "example-c"], group.id, actor=_ACTOR
    )
    assert revoked == ["example-a", "example-c"]
    assert _granted(manager, group.id) == ["example-b"]
    revoke_targets = [r[1] for r in _rows(tmp, "repo_access_revoke")]
    assert revoke_targets == ["example-a", "example-c"]


def test_failed_bulk_grant_leaves_no_partial_grants(pg) -> None:
    import psycopg

    manager, group, dsn, tmp = pg
    _poison(dsn, "INSERT", "NEW")
    with pytest.raises(psycopg.Error):
        manager.grant_repos_access_audited(
            ["example-a", _POISON, "example-c"], group.id, actor=_ACTOR
        )
    assert _granted(manager, group.id) == []
    assert [r[0] for r in _rows(tmp, "repo_access_grant")] == ["failure"]


def test_failed_bulk_revoke_revokes_nothing(pg) -> None:
    import psycopg

    manager, group, dsn, tmp = pg
    for alias in ("example-a", _POISON, "example-c"):
        manager.grant_repo_access(alias, group.id, "setup")
    _poison(dsn, "DELETE", "OLD")
    with pytest.raises(psycopg.Error):
        manager.revoke_repos_access_audited(
            ["example-a", _POISON, "example-c"], group.id, actor=_ACTOR
        )
    assert _granted(manager, group.id) == ["example-a", "example-c", _POISON]
    assert [r[0] for r in _rows(tmp, "repo_access_revoke")] == ["failure"]
