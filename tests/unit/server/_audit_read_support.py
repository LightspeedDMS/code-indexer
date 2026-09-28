"""Shared fixtures for the audit read-path tests (SQLite and PostgreSQL).

Every store is REAL: an ``AuditLogService`` on a fresh SQLite file (both
directly and in the solo-mode shape where one service wraps another on the
same file), and an ``AuditLogService`` delegating to
``AuditLogPostgresBackend`` on a fresh PostgreSQL database when
``TEST_POSTGRES_DSN`` is set.  Rows are written through the real single write
function (``insert_events``).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Iterator, List, Optional

import pytest

from code_indexer.server.services.audit_events import AuditEvent
from code_indexer.server.services.audit_log_service import AuditLogService

PG_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

STORE_KINDS = ("sqlite", "sqlite_solo", "postgres")


def make_event(
    *,
    ts: str,
    action_type: str = "user_deleted",
    target_type: str = "user",
    target_id: str = "bob",
    actor: str = "alice",
    outcome: Optional[str] = "success",
    source: Optional[str] = "web",
    ip_address: Optional[str] = "192.0.2.10",
    correlation_id: Optional[str] = None,
    node_id: Optional[str] = None,
    auth_method: Optional[str] = "web_session",
    actor_is_system: bool = False,
    details_json: Optional[str] = None,
) -> AuditEvent:
    """One fully attributed row with an explicit timestamp."""
    return AuditEvent(
        event_uuid=str(uuid.uuid4()),
        occurred_at=ts,
        actor=actor,
        actor_is_system=actor_is_system,
        action_type=action_type,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        source=source,
        ip_address=ip_address,
        correlation_id=correlation_id or f"evt-{uuid.uuid4()}",
        node_id=node_id,
        auth_method=auth_method,
        details_json=details_json,
    )


def _pg_dsn_for(dbname: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(PG_DSN)
    params["dbname"] = dbname
    return make_conninfo(**params)  # type: ignore[arg-type]


def _pg_store() -> Iterator[AuditLogService]:
    import psycopg

    from code_indexer.server.storage.postgres.audit_log_backend import (
        AuditLogPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    name = f"audit_read_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = _pg_dsn_for(name)
    pool: Any = None
    try:
        with MigrationRunner(dsn) as runner:
            runner.run()
        pool = ConnectionPool(dsn, min_size=1, max_size=2)
        yield AuditLogService(
            Path("/nonexistent-unused"),
            storage_backend=AuditLogPostgresBackend(pool),
        )
    finally:
        if pool is not None:
            pool.close()
        with psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def build_store(kind: str, tmp_path: Path) -> Iterator[AuditLogService]:
    """Yield a real audit store of the given kind."""
    if kind == "sqlite":
        yield AuditLogService(tmp_path / "groups.db")
    elif kind == "sqlite_solo":
        db_path = tmp_path / "groups.db"
        inner = AuditLogService(db_path)
        yield AuditLogService(db_path, storage_backend=inner)
    elif kind == "postgres":
        if not PG_DSN:
            pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
        yield from _pg_store()
    else:  # pragma: no cover - programming error in a parametrize list
        raise AssertionError(f"unknown store kind {kind}")


def seed(store: AuditLogService, events: List[AuditEvent]) -> None:
    store.insert_events(events)


def ids_of(page: Any) -> List[int]:
    return [row.id for row in page.rows]
