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

ROUND 2 REMEDIATION (#1663 round 2)
------------------------------------
Code review confirmed the production fix is correct (write side pins the
session to UTC via ConnectionPool's `_configure_session`, and always
writes a tz-aware UTC value), but found two test-quality defects in this
module:

1. BLOCKING -- environment coupling: the original verification read in
   `test_persisted_run_at_is_aware_and_utc` used a BARE
   ``psycopg.connect(dsn)`` for its read-back, which does NOT go through
   this project's own ``ConnectionPool`` and therefore never gets the
   UTC session pin. Against a real PostgreSQL server configured with a
   non-UTC timezone (e.g. America/Chicago), psycopg renders the
   TIMESTAMPTZ value using THAT session's offset -- same instant,
   different (non-zero) `utcoffset()` -- so `assert run_at.utcoffset() ==
   timedelta(0)` failed deterministically, despite the write being
   perfectly correct. Reproduced live against a real non-UTC PostgreSQL
   16 container as part of this remediation
   (`datetime.timedelta(days=-1, seconds=68400)` observed, matching the
   reviewer's exact finding). Fixed by reading through this project's own
   ``ConnectionPool`` (Option A from the review), which IS pinned to UTC
   by the same `_configure_session` callback the production write path
   uses -- so the assertion now genuinely exercises the session pin this
   commit added, and is immune to the server's configured timezone by
   construction (the pool always issues `SET TIME ZONE 'UTC'` on
   connection open, regardless of what the server considers its default).

2. NON-BLOCKING (medium) -- missing disposable-database guard: this
   module's ``DROP TABLE IF EXISTS diagnostic_results`` ran against
   whatever ``TEST_POSTGRES_DSN`` named, with no safety guard on the
   database name. Fixed by adopting the SAME guard pattern already
   established in ``test_temporal_worker_lineage_live_pg_1533.py`` /
   ``test_fleet_migration_dedup_state_clear_all_live_pg_1589.py``:
   ``_refuse_unless_disposable`` FAILS (never silently skips) unless the
   database libpq actually RESOLVES from the DSN fully matches
   ``_DISPOSABLE_DB_NAME_REGEX`` -- re-confirmed against PostgreSQL's own
   ``SELECT current_database()`` after connecting, so neither a
   PGDATABASE/service-file default nor a later ``dbname=`` override can
   bypass it. ``TestDisposableDatabaseGuard`` proves the guard itself,
   without needing a live/reachable PostgreSQL server.
"""

from __future__ import annotations

import os
import re
from datetime import timedelta
from typing import Optional

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

# Guard 1 (round 2, #1663): the database name must FULLY MATCH this
# disposable format, so a DSN aimed at a real/shared database is refused
# rather than operated on. A FULL-STRING match, never substring
# containment -- see test_temporal_worker_lineage_live_pg_1533.py for the
# exact rationale (`production_cidx_test` and `cidx_test_prod` must both
# be refused). The optional suffix is DIGITS ONLY so no word like "prod"
# can ride along behind a marker. Matched with re.fullmatch in
# _refuse_unless_disposable.
_DISPOSABLE_DB_NAME_REGEX = r"(?:cidx_)?(?:test|tmp|scratch|sandbox)(?:_[0-9]+)?"


def _resolved_database_name(dsn: str) -> Optional[str]:
    """The database libpq will ACTUALLY connect to, or None if the DSN
    names none.

    Asks libpq's own parser rather than re-parsing the string -- a static
    re-parse is bypassable (a later ``dbname=`` in a key/value DSN
    overrides an earlier one, and a URI's own ``?dbname=`` query param
    overrides its path segment).
    """
    from psycopg.conninfo import conninfo_to_dict

    try:
        params = conninfo_to_dict(dsn)
    except Exception as exc:
        pytest.fail(
            f"TEST_POSTGRES_DSN could not be parsed by libpq ({exc}) -- "
            "refusing to run live-PostgreSQL tests against a DSN whose "
            "target cannot be determined."
        )
    dbname = params.get("dbname")
    return str(dbname) if dbname else None


def _refuse_unless_disposable(dsn: str) -> None:
    """FAIL (never silently skip) unless the database libpq RESOLVES from
    this DSN fully matches the disposable format."""
    resolved = _resolved_database_name(dsn)
    if not resolved:
        pytest.fail(
            "TEST_POSTGRES_DSN names no database, so the connection would "
            "inherit PGDATABASE or a service-file default that this guard "
            "cannot inspect -- refusing to run. Name the disposable database "
            "explicitly, e.g. cidx_test_1663."
        )
    db_name = resolved.lower()
    if re.fullmatch(_DISPOSABLE_DB_NAME_REGEX, db_name) is None:
        pytest.fail(
            f"TEST_POSTGRES_DSN points at database {db_name!r}, which does not "
            f"FULLY match the disposable format {_DISPOSABLE_DB_NAME_REGEX!r} "
            "(a name merely CONTAINING 'test' is deliberately not enough). "
            "Refusing to run: this module creates and drops the "
            "diagnostic_results table and must never be aimed at a real or "
            "shared database. Point TEST_POSTGRES_DSN at a disposable "
            "database, e.g. cidx_test_1663."
        )


@pytest.mark.skipif(
    not HAS_PSYCOPG_FOR_LIVE_PG,
    reason="psycopg not installed -- the guard's DSN parser needs the "
    "package, though never a live/reachable PostgreSQL server",
)
class TestDisposableDatabaseGuard:
    """The destruction-safety guard itself, tested WITHOUT a live/reachable
    PostgreSQL server (no ``TEST_POSTGRES_DSN`` needed). A guard exercised
    only when someone happens to have PostgreSQL wired up is not a guard.
    Mirrors TestDisposableDatabaseGuard in
    test_temporal_worker_lineage_live_pg_1533.py exactly.
    """

    REFUSED_NAMES = (
        "cidx_server",
        "cidx_production_lookalike",
        "production_cidx_test",
        "cidx_test_prod",
        "cidx_prod",
        "attestation",
        "",
    )

    ACCEPTED_NAMES = (
        "cidx_test_1663",
        "test",
        "cidx_tmp",
        "scratch",
        "cidx_sandbox_7",
    )

    @pytest.mark.parametrize("db_name", REFUSED_NAMES)
    def test_refuses_non_disposable_database_name(self, db_name: str) -> None:
        with pytest.raises(pytest.fail.Exception) as exc_info:
            _refuse_unless_disposable(f"postgresql://u@h:5432/{db_name}")
        assert "refus" in str(exc_info.value).lower()

    @pytest.mark.parametrize("db_name", ACCEPTED_NAMES)
    def test_accepts_disposable_database_name(self, db_name: str) -> None:
        _refuse_unless_disposable(f"postgresql://u@h:5432/{db_name}")

    def test_key_value_dsn_form_is_also_guarded(self) -> None:
        with pytest.raises(pytest.fail.Exception):
            _refuse_unless_disposable("host=h port=5432 dbname=cidx_server")

    BYPASS_DSNS = (
        "postgresql://u@h:5432/cidx_test?dbname=cidx_server",
        "host=h dbname=cidx_test dbname=cidx_server",
        "host=h user=u",
    )

    @pytest.mark.parametrize("dsn", BYPASS_DSNS)
    def test_refuses_dsn_whose_libpq_resolved_dbname_is_not_disposable(
        self, dsn: str
    ) -> None:
        with pytest.raises(pytest.fail.Exception):
            _refuse_unless_disposable(dsn)


@pytest.fixture(scope="module")
def pg_dsn_for_diagnostics_run_at():
    """Module-scoped DSN string for live-PG diagnostics run_at tests. Skips
    cleanly when no PostgreSQL is reachable, but FAILS (never silently
    skips) when a real server IS reachable and its database name does not
    match the disposable format -- this module creates/drops
    diagnostic_results in the public schema of whatever database the DSN
    names."""
    if not HAS_PSYCOPG_FOR_LIVE_PG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    # The name guard runs BEFORE any connection attempt, so a DSN aimed
    # somewhere forbidden can never be contacted, let alone skipped past.
    _refuse_unless_disposable(dsn)
    try:
        with psycopg.connect(dsn) as conn:
            row = conn.execute("SELECT current_database()").fetchone()
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    connected_db = row[0] if row else None
    if not connected_db:
        pytest.fail("PostgreSQL did not report current_database() -- refusing to run.")
    # Re-confirm against what PostgreSQL itself reports, not just what
    # libpq resolved locally -- neither a PGDATABASE/service-file default
    # nor a later dbname= override can slip past this second check.
    _refuse_unless_disposable(f"dbname={connected_db}")
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

        # Round 2 (#1663): read through this project's OWN ConnectionPool
        # rather than a bare psycopg.connect(dsn). The pool's
        # `_configure_session` callback pins every pooled connection's
        # session TimeZone to UTC (the same mechanism the production write
        # path relies on), so this read is immune to whatever timezone the
        # PostgreSQL server happens to be configured with by construction --
        # a bare connection would instead inherit the server/session
        # default and render the TIMESTAMPTZ value with THAT offset (same
        # instant, different -- and possibly non-zero -- utcoffset()),
        # which is exactly what made the original version of this test
        # fail deterministically against a real non-UTC PostgreSQL server.
        read_pool = ConnectionPool(dsn, name="bug1663-live-verify-read")
        try:
            with read_pool.connection() as conn:
                row = conn.execute(
                    "SELECT run_at FROM diagnostic_results WHERE category = %s",
                    (DiagnosticCategory.CLI_TOOLS.value,),
                ).fetchone()
        finally:
            read_pool.close()

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
