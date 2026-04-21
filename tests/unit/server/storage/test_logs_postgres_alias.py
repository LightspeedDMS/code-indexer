"""
Tests for Story #876 Phase C: LogsPostgresBackend must carry `alias` kwarg.

The PostgreSQL logs backend is the cluster-mode counterpart of
LogsSqliteBackend. Its insert_log() signature must match the Protocol
contract (Optional[str] alias kwarg) and must persist + return the value
so lifecycle-runner ERROR rows carry the repo tag in multi-node setups.

Signature tests run without a live database; the round-trip test is skipped
only when psycopg is absent or TEST_POSTGRES_DSN is unset (expressed via
@pytest.mark.skipif). Any other failure propagates loudly.

Typing note: `ConnectionPool` is imported under TYPE_CHECKING so test
collection succeeds on machines without psycopg installed, yet the fixtures
retain strong static types.
"""

from __future__ import annotations

import inspect
import os
import uuid
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Iterator,
    Optional,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

import pytest

if TYPE_CHECKING:  # pragma: no cover — only used by static type checkers
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

try:
    import psycopg  # noqa: F401

    HAS_PSYCOPG = True
except ImportError:
    HAS_PSYCOPG = False


_TEST_DSN = os.environ.get("TEST_POSTGRES_DSN", "")
_PG_AVAILABLE = HAS_PSYCOPG and bool(_TEST_DSN)


# ---------------------------------------------------------------------------
# Signature test (no DB required)
# ---------------------------------------------------------------------------


def test_logs_postgres_backend_insert_log_declares_alias_parameter() -> None:
    """LogsPostgresBackend.insert_log must declare `alias: Optional[str] = None`."""
    from code_indexer.server.storage.postgres.logs_backend import LogsPostgresBackend

    sig = inspect.signature(LogsPostgresBackend.insert_log)
    params = sig.parameters
    assert "alias" in params, (
        "LogsPostgresBackend.insert_log must declare `alias` "
        "(Story #876 Phase C). "
        f"Current parameters: {list(params.keys())}"
    )

    alias_param = params["alias"]
    assert alias_param.default is None, (
        "`alias` must default to None so existing callers still work; "
        f"got default={alias_param.default!r}"
    )

    hints = get_type_hints(LogsPostgresBackend.insert_log)
    assert "alias" in hints, (
        "Type hints for insert_log must include `alias`; none resolved."
    )
    resolved = hints["alias"]
    expected = Optional[str]
    assert resolved == expected or (
        get_origin(resolved) is Union and set(get_args(resolved)) == {str, type(None)}
    ), f"`alias` must be typed as Optional[str]; got {resolved!r}"


# ---------------------------------------------------------------------------
# Fixtures for the round-trip test (each has a single responsibility)
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_pool() -> Iterator["ConnectionPool"]:
    """Yield a live ConnectionPool; close it on teardown. No error swallowing."""
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    pool = ConnectionPool(_TEST_DSN)
    # Smoke-test the connection — failures propagate loudly.
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture
def lifecycle_alias_source(pg_pool: "ConnectionPool") -> Iterator[str]:
    """Yield a unique source string; delete matching rows on teardown."""
    unique_source = f"test-alias-{uuid.uuid4().hex[:8]}"
    try:
        yield unique_source
    finally:
        with pg_pool.connection() as conn:
            conn.execute("DELETE FROM logs WHERE source = %s", (unique_source,))
            conn.commit()


# ---------------------------------------------------------------------------
# Round-trip test (requires live PG — skipped when PG is not configured)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _PG_AVAILABLE,
    reason="psycopg not installed or TEST_POSTGRES_DSN not set",
)
def test_logs_postgres_backend_persists_alias_roundtrip(
    pg_pool: "ConnectionPool",
    lifecycle_alias_source: str,
) -> None:
    """Inserting a log row with alias='x' must round-trip via query_logs."""
    from code_indexer.server.storage.postgres.logs_backend import LogsPostgresBackend

    backend = LogsPostgresBackend(pg_pool)
    ts = datetime.now(timezone.utc).isoformat()

    backend.insert_log(
        timestamp=ts,
        level="ERROR",
        source=lifecycle_alias_source,
        message="write_meta_md failed",
        alias="my-repo-876c",
    )

    rows, total = backend.query_logs(source=lifecycle_alias_source, limit=10)
    assert total >= 1
    matching = [r for r in rows if r["source"] == lifecycle_alias_source]
    assert matching, f"Inserted row not returned; rows={rows}"
    assert matching[0]["alias"] == "my-repo-876c"
