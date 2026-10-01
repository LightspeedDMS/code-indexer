"""Real SQLite and real PostgreSQL stores for SIEM delivery tests.

SQLite: a temporary ``groups.db`` built by ``AuditLogService`` (audit +
SIEM tables in one file).  PostgreSQL: enabled by ``TEST_POSTGRES_DSN``;
the database name must FULLY match a disposable pattern, and every test
runs in its OWN schema (search_path) created by the real migrations and
dropped afterwards, so nothing in ``public`` is touched.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pytest

from code_indexer.server.services.siem_delivery.db import SiemDb

_DISPOSABLE_DB_NAME_REGEX = r"(?:cidx_)?(?:test|tmp|scratch|sandbox)(?:_[0-9]+)?"
BACKENDS = ("sqlite", "postgres")


def _dsn_with_search_path(dsn: str, schema: str) -> str:
    option = f"-csearch_path={schema}"
    if "://" in dsn:
        parts = urlparse(dsn)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "options"]
        query.append(("options", option))
        return urlunparse(parts._replace(query=urlencode(query)))
    return f"{dsn} options='{option}'"


def require_disposable_pg_dsn() -> str:
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    from psycopg.conninfo import conninfo_to_dict

    dbname = str(conninfo_to_dict(dsn).get("dbname") or "").lower()
    if re.fullmatch(_DISPOSABLE_DB_NAME_REGEX, dbname) is None:
        pytest.fail(
            f"TEST_POSTGRES_DSN database {dbname!r} is not a disposable name; "
            "refusing to create schemas in it"
        )
    return dsn


@dataclass
class SiemBackendHarness:
    name: str
    audit: Any  # an object with insert_events(events, *, siem_destinations=)
    db: SiemDb
    groups_db_path: Optional[Path] = None
    pool: Any = None

    def raw(self, sql: str, params: tuple = ()) -> None:
        """Run DDL/DML outside the code under test (fault injection)."""
        if self.name == "postgres":
            with self.pool.connection() as conn:
                if params:
                    conn.execute(sql.replace("?", "%s"), params)
                else:
                    conn.execute(sql)
            return
        import sqlite3

        assert self.groups_db_path is not None
        conn = sqlite3.connect(str(self.groups_db_path))
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def fail_queue_inserts(self, only_uuid: Optional[str] = None) -> None:
        if self.name == "postgres":
            cond = f"NEW.event_uuid = '{only_uuid}'" if only_uuid else "TRUE"
            self.raw(
                "CREATE OR REPLACE FUNCTION siem_test_fail() RETURNS trigger AS $$ "
                f"BEGIN IF {cond} THEN RAISE EXCEPTION 'injected'; END IF; "
                "RETURN NEW; END $$ LANGUAGE plpgsql"
            )
            self.raw(
                "CREATE TRIGGER siem_test_fail BEFORE INSERT ON siem_delivery_queue "
                "FOR EACH ROW EXECUTE FUNCTION siem_test_fail()"
            )
            return
        when = f"WHEN NEW.event_uuid = '{only_uuid}'" if only_uuid else ""
        self.raw(
            f"CREATE TRIGGER siem_test_fail BEFORE INSERT ON siem_delivery_queue {when} "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )

    def fail_audit_inserts(self, only_uuid: Optional[str] = None) -> None:
        if self.name == "postgres":
            cond = f"NEW.event_uuid = '{only_uuid}'" if only_uuid else "TRUE"
            self.raw(
                "CREATE OR REPLACE FUNCTION audit_test_fail() RETURNS trigger AS $$ "
                f"BEGIN IF {cond} THEN RAISE EXCEPTION 'injected'; END IF; "
                "RETURN NEW; END $$ LANGUAGE plpgsql"
            )
            self.raw(
                "CREATE TRIGGER audit_test_fail BEFORE INSERT ON audit_logs "
                "FOR EACH ROW EXECUTE FUNCTION audit_test_fail()"
            )
            return
        when = f"WHEN NEW.event_uuid = '{only_uuid}'" if only_uuid else ""
        self.raw(
            f"CREATE TRIGGER audit_test_fail BEFORE INSERT ON audit_logs {when} "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )

    def count(self, sql: str, params: tuple = ()) -> int:
        def _q(tx: Any) -> int:
            row = tx.one(sql, params)
            return int(list(row.values())[0]) if row else 0

        return int(self.db.read(_q))


def _sqlite_harness(tmp_path: Path) -> SiemBackendHarness:
    from code_indexer.server.services.audit_log_service import AuditLogService

    path = tmp_path / "groups.db"
    audit = AuditLogService(path)
    return SiemBackendHarness("sqlite", audit, SiemDb.sqlite(str(path)), path)


_PG_RESET = (
    "DROP TRIGGER IF EXISTS siem_test_fail ON siem_delivery_queue",
    "DROP TRIGGER IF EXISTS audit_test_fail ON audit_logs",
    "TRUNCATE siem_delivery_queue, siem_delivery_batches, siem_process_status, "
    "siem_destinations, siem_backlog_samples, audit_logs",
    "DELETE FROM siem_delivery_state",
    "INSERT INTO siem_delivery_state (id) VALUES (1)",
)


@pytest.fixture()
def pg_pool(_pg_session_pool: Any) -> Any:
    """The session's migrated schema, emptied for this test."""
    with _pg_session_pool.connection() as conn:
        for statement in _PG_RESET:
            conn.execute(statement)
    return _pg_session_pool


@pytest.fixture(scope="session")
def _pg_session_pool() -> Iterator[Any]:
    dsn = require_disposable_pg_dsn()
    import psycopg
    from psycopg_pool import ConnectionPool

    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    schema = f"cidx_siem_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    scoped = _dsn_with_search_path(dsn, schema)
    runner = MigrationRunner(scoped)
    try:
        runner.run()
    finally:
        runner.close()
    pool = ConnectionPool(scoped, min_size=1, max_size=8, open=True)
    try:
        yield pool
    finally:
        pool.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture(params=BACKENDS)
def siem_backend(request: Any, tmp_path: Path) -> Iterator[SiemBackendHarness]:
    if request.param == "sqlite":
        yield _sqlite_harness(tmp_path)
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        DatabaseConnectionManager.get_instance(str(tmp_path / "groups.db")).close_all()
        return
    pool = request.getfixturevalue("pg_pool")
    from code_indexer.server.storage.postgres.audit_log_backend import (
        AuditLogPostgresBackend,
    )

    yield SiemBackendHarness(
        "postgres", AuditLogPostgresBackend(pool), SiemDb.postgres(pool), pool=pool
    )
