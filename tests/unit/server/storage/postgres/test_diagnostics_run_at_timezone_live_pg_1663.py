"""
Bug #1663: live-PostgreSQL round-trip tests for diagnostics_service.py's
run_at write/read normalization.

Root cause: `_save_results_to_db()` wrote `datetime.now().isoformat()` -- a
NAIVE, LOCAL-timezone string -- into `diagnostic_results.run_at`, a
TIMESTAMPTZ column on PostgreSQL. If the PostgreSQL session's configured
timezone ever differs from the app process's local timezone,
`get_status()`'s freshness/TTL comparison (`datetime.now() -
cached_timestamp`) is skewed by the offset difference.

Fix: write run_at as a timezone-AWARE UTC value
(`datetime.now(timezone.utc)`), and normalize `_coerce_run_at()`'s str
branch to also strip/convert tzinfo (matching the pre-existing
isinstance(raw, datetime) branch), since the SQLite TEXT column now stores
an offset-bearing ISO string too.

Mirrors test_wiki_cache_backend_view_counts_datetime_live_pg_1669.py's and
test_fleet_migration_dedup_state_live_pg_1560.py's exact pattern: gated by
TEST_POSTGRES_DSN, skips cleanly when no PostgreSQL is available (this
project's existing posture -- these tests are not run in CI, only locally
against a real instance), creates the real diagnostic_results table
matching migration 001_initial_schema.sql exactly (results_json JSONB,
run_at TIMESTAMPTZ), drops it afterward.

Per the project's "faithful DB mocks" lesson, this exercises a REAL
psycopg v3 connection through the real DiagnosticsPostgresBackend -- not a
mock -- so the TIMESTAMPTZ-to-datetime deserialization behavior a mock
would hide is genuinely exercised.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from code_indexer.server.services.diagnostics_service import (
    DiagnosticCategory,
    DiagnosticResult,
    DiagnosticStatus,
    DiagnosticsService,
)

# Neither module below imports psycopg at module level (connection_pool.py
# guards it behind TYPE_CHECKING; psycopg_pool is only imported lazily on
# first real ConnectionPool construction), so these imports are safe
# unconditionally -- a genuine failure here surfaces as a real error
# instead of being masked by the psycopg-only guard below.
from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
from code_indexer.server.storage.postgres.diagnostics_backend import (
    DiagnosticsPostgresBackend,
)

HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass

_TIMESTAMP_TOLERANCE = timedelta(minutes=1)


@pytest.fixture(scope="module")
def pg_dsn_for_diagnostics_run_at():
    """Module-scoped DSN string for live-PG diagnostics run_at tests. Skips
    if unavailable (matches pg_dsn_for_dedup_state in
    test_fleet_migration_dedup_state_live_pg_1560.py)."""
    if not HAS_PSYCOPG_FOR_LIVE_PG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    return dsn


@pytest.fixture
def diagnostic_results_table(pg_dsn_for_diagnostics_run_at):
    """Create a real diagnostic_results table (matching
    001_initial_schema.sql exactly) before each test, dropped after, for
    isolation from any other schema/table on the target DB."""
    dsn = pg_dsn_for_diagnostics_run_at
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS diagnostic_results")
        conn.execute(
            """
            CREATE TABLE diagnostic_results (
                category        TEXT    PRIMARY KEY,
                results_json    JSONB   NOT NULL,
                run_at          TIMESTAMPTZ NOT NULL
            )
            """
        )
    yield dsn
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS diagnostic_results")


pytestmark = pytest.mark.skipif(
    not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available"
)


def _sample_results() -> list:
    return [
        DiagnosticResult(
            name="PG Sample Tool",
            status=DiagnosticStatus.WORKING,
            message="ok",
            details={},
        )
    ]


def _service_with_pg_backend(dsn: str, name: str) -> DiagnosticsService:
    """Build a real DiagnosticsService wired to a real
    DiagnosticsPostgresBackend against *dsn*. A FRESH pool/service per call
    proves state is genuinely persisted server-side, not cached
    in-process."""
    pool = ConnectionPool(dsn, name=name)
    backend = DiagnosticsPostgresBackend(pool)
    return DiagnosticsService(storage_backend=backend)


class TestSaveResultsToDbAwareUtcLivePg1663:
    """Bug #1663: write side must persist a genuinely tz-aware UTC value
    that PostgreSQL/psycopg round-trips correctly."""

    def test_persisted_run_at_is_aware_and_utc(self, diagnostic_results_table) -> None:
        dsn = diagnostic_results_table
        write_service = _service_with_pg_backend(dsn, "bug1663-live-write")
        write_service._save_results_to_db(
            DiagnosticCategory.CLI_TOOLS, _sample_results()
        )

        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT run_at FROM diagnostic_results WHERE category = %s",
                (DiagnosticCategory.CLI_TOOLS.value,),
            ).fetchone()

        assert row is not None, (
            "expected row to have been written by _save_results_to_db"
        )
        run_at = row[0]
        assert run_at.tzinfo is not None, (
            "psycopg must return a tz-aware datetime for the TIMESTAMPTZ "
            "column -- a naive value here would mean the write-side fix "
            "regressed back to naive-local"
        )
        assert run_at.utcoffset() == timedelta(0)


class TestReadCoercionRoundTripLivePg1663:
    """Bug #1663: _read_category_from_db()/_coerce_run_at() must accept the
    real psycopg TIMESTAMPTZ shape and normalize it to a naive local
    datetime close to 'now'."""

    def test_read_category_from_db_returns_naive_datetime(
        self, diagnostic_results_table
    ) -> None:
        from datetime import datetime

        dsn = diagnostic_results_table
        write_service = _service_with_pg_backend(dsn, "bug1663-live-write2")
        write_service._save_results_to_db(
            DiagnosticCategory.CLI_TOOLS, _sample_results()
        )

        read_service = _service_with_pg_backend(dsn, "bug1663-live-read")
        loaded = read_service._read_category_from_db(DiagnosticCategory.CLI_TOOLS)

        assert loaded is not None
        results, run_at_dt = loaded
        assert any(r.name == "PG Sample Tool" for r in results)
        assert run_at_dt.tzinfo is None, (
            "the coerced run_at must be naive -- get_status() compares it "
            "against a naive datetime.now()"
        )
        assert abs(run_at_dt - datetime.now()) < _TIMESTAMP_TOLERANCE


class TestGetStatusRepeatedCallLivePg1663:
    """The actual user-visible contract: two successive get_status() calls
    against a real PostgreSQL-backed service must not raise -- this is
    exactly where a tz-aware/naive mismatch would surface (the freshness
    comparison `now - cached_timestamps[category] < cache_ttl`)."""

    def test_second_get_status_call_does_not_raise(
        self, diagnostic_results_table
    ) -> None:
        dsn = diagnostic_results_table
        write_service = _service_with_pg_backend(dsn, "bug1663-live-write3")
        write_service._save_results_to_db(
            DiagnosticCategory.CLI_TOOLS, _sample_results()
        )

        read_service = _service_with_pg_backend(dsn, "bug1663-live-read2")

        status_first = read_service.get_status()
        assert any(
            r.name == "PG Sample Tool"
            for r in status_first[DiagnosticCategory.CLI_TOOLS]
        )

        # Must not raise TypeError: can't subtract offset-naive and
        # offset-aware datetimes.
        status_second = read_service.get_status()
        assert any(
            r.name == "PG Sample Tool"
            for r in status_second[DiagnosticCategory.CLI_TOOLS]
        )
