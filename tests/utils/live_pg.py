"""Live-PostgreSQL test plumbing with the two destruction-safety guards.

The same guards as ``test_temporal_worker_lineage_live_pg_1533.py`` and its
three copies (read that module for the full rationale), in one importable
place:

Guard 1 -- the database libpq RESOLVES from ``TEST_POSTGRES_DSN`` must FULLY
match ``DISPOSABLE_DB_NAME_REGEX``; anything else FAILS (never skips).
Guard 2 -- each test works in its own uniquely named schema, selected through
the connection's ``search_path``, and drops only that schema.

With no ``TEST_POSTGRES_DSN`` (or no reachable server) the tests skip.
"""

from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pytest

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "code_indexer"
    / "server"
    / "storage"
    / "postgres"
    / "migrations"
    / "sql"
)

DISPOSABLE_DB_NAME_REGEX = r"(?:cidx_)?(?:test|tmp|scratch|sandbox)(?:_[0-9]+)?"

_UNREACHABLE_MARKERS = (
    "connection refused",
    "could not connect",
    "no such file or directory",
    "is the server running",
    "timeout expired",
)


def _resolved_database_name(dsn: str) -> Optional[str]:
    from psycopg.conninfo import conninfo_to_dict

    try:
        params = conninfo_to_dict(dsn)
    except Exception as exc:
        pytest.fail(f"TEST_POSTGRES_DSN could not be parsed by libpq ({exc})")
    dbname = params.get("dbname")
    return str(dbname) if dbname else None


def refuse_unless_disposable(dsn: str) -> None:
    """FAIL unless the libpq-resolved database name is disposable."""
    resolved = _resolved_database_name(dsn)
    if not resolved or not re.fullmatch(DISPOSABLE_DB_NAME_REGEX, resolved.lower()):
        pytest.fail(
            f"TEST_POSTGRES_DSN resolves to database {resolved!r}, which does not "
            f"fully match {DISPOSABLE_DB_NAME_REGEX!r} -- refusing to run tests "
            "that create and drop schemas against it."
        )


def live_pg_dsn() -> str:
    """The guarded DSN, or skip when PostgreSQL is not configured/reachable."""
    try:
        import psycopg
    except ImportError:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    refuse_unless_disposable(dsn)
    try:
        with psycopg.connect(dsn) as conn:
            row = conn.execute("SELECT current_database()").fetchone()
    except psycopg.OperationalError as exc:
        if any(marker in str(exc).lower() for marker in _UNREACHABLE_MARKERS):
            pytest.skip(f"PostgreSQL not reachable: {exc}")
        pytest.fail(f"PostgreSQL reachable but the connection failed: {exc}")
    connected = row[0] if row else None
    if connected != _resolved_database_name(dsn):
        pytest.fail(f"Connected to {connected!r}, not the DSN's database")
    refuse_unless_disposable(f"dbname={connected}")
    return dsn


def dsn_with_search_path(dsn: str, schema: str) -> str:
    option = f"-csearch_path={schema}"
    if "://" in dsn:
        parts = urlparse(dsn)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "options"]
        query.append(("options", option))
        return urlunparse(parts._replace(query=urlencode(query)))
    return f"{dsn} options='{option}'"


@contextmanager
def private_schema(dsn: str, prefix: str, ddl: str) -> Iterator[str]:
    """A DSN bound to a fresh schema holding *ddl*; the schema is dropped
    afterwards. Nothing in ``public`` is touched."""
    import psycopg

    schema = f"{prefix}{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    scoped = dsn_with_search_path(dsn, schema)
    try:
        with psycopg.connect(scoped, autocommit=True) as conn:
            conn.execute(ddl)
        yield scoped
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def migration_ddl(*names: str) -> str:
    """The SQL of the named PostgreSQL migration files, in order."""
    return "\n".join((_MIGRATIONS_DIR / name).read_text() for name in names)
