"""
Bug #1701: live-PostgreSQL round-trip tests for
SelfMonitoringPostgresBackend's TIMESTAMPTZ-vs-str drift.

Root cause: self_monitoring_scans.started_at/.completed_at and
self_monitoring_issues.created_at are TIMESTAMPTZ columns (per the real
migration, storage/postgres/migrations/sql/001_initial_schema.sql).
psycopg deserializes TIMESTAMPTZ columns to native Python datetime objects,
never str -- but list_scans(), list_issues(), get_last_started_at(), and
fetch_stored_fingerprints() returned/used these values as if they were
str, with zero normalization (unlike the sibling
research_sessions_backend.py, which normalizes via pg_utils.sanitize_row()
/ to_iso() and was confirmed bug-free during the same investigation).

Fix: apply the SAME pg_utils.sanitize_row()/to_iso() normalization to all
affected read paths in self_monitoring_backend.py, so downstream callers
(web/routes.py's _add_scan_duration() and _calculate_next_scan_time(),
both of which call datetime.fromisoformat() on these values) get a
consistent ISO-8601 str regardless of storage backend.

Mirrors test_diagnostics_run_at_timezone_live_pg_1663.py's exact pattern:
gated by TEST_POSTGRES_DSN, skips cleanly when no PostgreSQL is available
(this project's existing posture -- these tests are not run in CI, only
locally against a real instance), creates the real self_monitoring_scans /
self_monitoring_issues tables matching migration 001_initial_schema.sql
(TIMESTAMPTZ columns), drops them afterward. Guarded by the same
disposable-database-name safety check established in
test_temporal_worker_lineage_live_pg_1533.py /
test_fleet_migration_dedup_state_clear_all_live_pg_1589.py /
test_diagnostics_run_at_timezone_live_pg_1663.py.

Per the project's "faithful DB mocks" lesson, this exercises a REAL
psycopg v3 connection through the real SelfMonitoringPostgresBackend --
not a mock -- so the TIMESTAMPTZ-to-datetime deserialization behavior a
mock would hide is genuinely exercised, and the actual downstream
web/routes.py helpers are called directly to prove the real production
symptom (TypeError from datetime.fromisoformat()) is gone.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
from code_indexer.server.storage.postgres.self_monitoring_backend import (
    SelfMonitoringPostgresBackend,
)

HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass

# Guard (#1701, mirrors #1663/#1589/#1533): the database name must FULLY
# MATCH this disposable format, so a DSN aimed at a real/shared database is
# refused rather than operated on. A FULL-STRING match, never substring
# containment. The optional suffix is DIGITS ONLY so no word like "prod"
# can ride along behind a marker. Matched with re.fullmatch in
# _refuse_unless_disposable.
_DISPOSABLE_DB_NAME_REGEX = r"(?:cidx_)?(?:test|tmp|scratch|sandbox)(?:_[0-9]+)?"


def _resolved_database_name(dsn: str) -> Optional[str]:
    """The database libpq will ACTUALLY connect to, or None if the DSN
    names none."""
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
            "explicitly, e.g. cidx_test_1701."
        )
    db_name = resolved.lower()
    if re.fullmatch(_DISPOSABLE_DB_NAME_REGEX, db_name) is None:
        pytest.fail(
            f"TEST_POSTGRES_DSN points at database {db_name!r}, which does not "
            f"FULLY match the disposable format {_DISPOSABLE_DB_NAME_REGEX!r} "
            "(a name merely CONTAINING 'test' is deliberately not enough). "
            "Refusing to run: this module creates and drops the "
            "self_monitoring_scans/self_monitoring_issues tables and must "
            "never be aimed at a real or shared database. Point "
            "TEST_POSTGRES_DSN at a disposable database, e.g. cidx_test_1701."
        )


@pytest.mark.skipif(
    not HAS_PSYCOPG_FOR_LIVE_PG,
    reason="psycopg not installed -- the guard's DSN parser needs the "
    "package, though never a live/reachable PostgreSQL server",
)
class TestDisposableDatabaseGuard:
    """The destruction-safety guard itself, tested WITHOUT a live/reachable
    PostgreSQL server (no TEST_POSTGRES_DSN needed)."""

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
        "cidx_test_1701",
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


@pytest.fixture(scope="module")
def pg_dsn_for_self_monitoring():
    """Module-scoped DSN string for live-PG self-monitoring tests. Skips
    cleanly when no PostgreSQL is reachable, but FAILS (never silently
    skips) when a real server IS reachable and its database name does not
    match the disposable format."""
    if not HAS_PSYCOPG_FOR_LIVE_PG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    _refuse_unless_disposable(dsn)
    try:
        with psycopg.connect(dsn) as conn:
            row = conn.execute("SELECT current_database()").fetchone()
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    connected_db = row[0] if row else None
    if not connected_db:
        pytest.fail("PostgreSQL did not report current_database() -- refusing to run.")
    _refuse_unless_disposable(f"dbname={connected_db}")
    return dsn


@pytest.fixture
def self_monitoring_tables(pg_dsn_for_self_monitoring):
    """Create the real self_monitoring_scans/self_monitoring_issues tables
    (matching 001_initial_schema.sql's TIMESTAMPTZ columns) before each
    test, dropped after, for isolation from any other schema/table on the
    target DB."""
    dsn = pg_dsn_for_self_monitoring
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS self_monitoring_issues")
        conn.execute("DROP TABLE IF EXISTS self_monitoring_scans")
        conn.execute(
            """
            CREATE TABLE self_monitoring_scans (
                scan_id TEXT PRIMARY KEY,
                started_at TIMESTAMPTZ NOT NULL,
                completed_at TIMESTAMPTZ,
                status TEXT NOT NULL,
                log_id_start INTEGER,
                log_id_end INTEGER,
                issues_created INTEGER,
                error_message TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE self_monitoring_issues (
                id SERIAL PRIMARY KEY,
                scan_id TEXT,
                github_issue_number INTEGER,
                github_issue_url TEXT,
                classification TEXT,
                error_codes TEXT,
                fingerprint TEXT,
                source_log_ids TEXT,
                source_files TEXT,
                title TEXT,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )
    yield dsn
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS self_monitoring_issues")
        conn.execute("DROP TABLE IF EXISTS self_monitoring_scans")


pytestmark = pytest.mark.skipif(
    not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available"
)


@pytest.fixture
def pg_backend_factory():
    """Yields a factory that builds a FRESH ConnectionPool/backend per call
    (proving state is genuinely persisted server-side, not cached
    in-process), and closes every pool it created on teardown so live-PG
    tests do not leak connections."""
    created_pools: list = []

    def _make(dsn: str, name: str) -> SelfMonitoringPostgresBackend:
        pool = ConnectionPool(dsn, name=name)
        created_pools.append(pool)
        return SelfMonitoringPostgresBackend(pool)

    yield _make

    for pool in created_pools:
        pool.close()


def _seed_scan(
    backend: SelfMonitoringPostgresBackend,
    scan_id: str,
    started_iso: str,
    completed_iso: Optional[str] = None,
) -> None:
    """Insert a scan record, optionally completing it immediately."""
    backend.create_scan_record(scan_id, started_iso, log_id_start=1)
    if completed_iso is not None:
        backend.update_scan_record(
            scan_id, "SUCCESS", completed_iso, log_id_end=2, issues_created=0
        )


def _seed_issue(
    backend: SelfMonitoringPostgresBackend,
    scan_id: str,
    created_iso: str,
    fingerprint: str = "fp1",
) -> None:
    """Insert an issue-metadata record tied to scan_id."""
    backend.store_issue_metadata(
        scan_id,
        None,
        None,
        "bug",
        "title1",
        "E1",
        fingerprint,
        "1",
        "f.py",
        created_iso,
    )


class TestListScansLivePg1701:
    """list_scans() must return started_at/completed_at as ISO-8601 str."""

    def test_returns_iso_string_timestamps(
        self, self_monitoring_tables, pg_backend_factory
    ) -> None:
        dsn = self_monitoring_tables
        now = datetime.now(timezone.utc).isoformat()
        _seed_scan(
            pg_backend_factory(dsn, "bug1701-write-list-scans"), "scan1", now, now
        )

        scans = pg_backend_factory(dsn, "bug1701-read-list-scans").list_scans()

        assert len(scans) == 1
        assert isinstance(scans[0]["started_at"], str)
        assert isinstance(scans[0]["completed_at"], str)
        # Must not raise -- the real production symptom.
        datetime.fromisoformat(scans[0]["started_at"])
        datetime.fromisoformat(scans[0]["completed_at"])


class TestListIssuesLivePg1701:
    """list_issues() must return created_at as ISO-8601 str."""

    def test_returns_iso_string_created_at(
        self, self_monitoring_tables, pg_backend_factory
    ) -> None:
        dsn = self_monitoring_tables
        now = datetime.now(timezone.utc).isoformat()
        write_backend = pg_backend_factory(dsn, "bug1701-write-list-issues")
        _seed_scan(write_backend, "scan1", now)
        _seed_issue(write_backend, "scan1", now)

        issues = pg_backend_factory(dsn, "bug1701-read-list-issues").list_issues()

        assert len(issues) == 1
        assert isinstance(issues[0]["created_at"], str)
        datetime.fromisoformat(issues[0]["created_at"])


class TestGetLastStartedAtLivePg1701:
    """get_last_started_at() must return an ISO-8601 str."""

    def test_returns_iso_string(
        self, self_monitoring_tables, pg_backend_factory
    ) -> None:
        dsn = self_monitoring_tables
        now = datetime.now(timezone.utc).isoformat()
        _seed_scan(pg_backend_factory(dsn, "bug1701-write-last-started"), "scan1", now)

        last_started = pg_backend_factory(
            dsn, "bug1701-read-last-started"
        ).get_last_started_at()

        assert isinstance(last_started, str)
        datetime.fromisoformat(last_started)


class TestFetchStoredFingerprintsLivePg1701:
    """fetch_stored_fingerprints()'s created_at (5th tuple element) must be
    an ISO-8601 str."""

    def test_returns_iso_string_created_at(
        self, self_monitoring_tables, pg_backend_factory
    ) -> None:
        dsn = self_monitoring_tables
        now = datetime.now(timezone.utc).isoformat()
        write_backend = pg_backend_factory(dsn, "bug1701-write-fingerprints")
        _seed_scan(write_backend, "scan1", now)
        _seed_issue(write_backend, "scan1", now)

        fingerprints = pg_backend_factory(
            dsn, "bug1701-read-fingerprints"
        ).fetch_stored_fingerprints(retention_days=365)

        assert len(fingerprints) == 1
        created_at = fingerprints[0][4]
        assert isinstance(created_at, str)
        datetime.fromisoformat(created_at)


class TestCleanupOrphanedScansLivePg1701:
    """cleanup_orphaned_scans() takes a str cutoff and compares it against
    the TIMESTAMPTZ column -- PostgreSQL implicitly casts the str
    parameter, so no normalization change was needed here (unlike the
    other 4 methods). This regression test locks that pre-existing correct
    behavior."""

    def test_marks_orphans_failure(
        self, self_monitoring_tables, pg_backend_factory
    ) -> None:
        dsn = self_monitoring_tables
        backend = pg_backend_factory(dsn, "bug1701-cleanup-orphaned")
        old_time = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        _seed_scan(backend, "scan_orphan", old_time)

        cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        count = backend.cleanup_orphaned_scans(cutoff_iso)
        assert count == 1

        scans = backend.list_scans()
        assert scans[0]["status"] == "FAILURE"


class TestWebRoutesHelpersLivePg1701:
    """The actual user-visible contract: web/routes.py's
    _add_scan_duration() and _calculate_next_scan_time() must not raise
    TypeError and must not silently degrade to 'N/A' when the backend is a
    real PostgreSQL TIMESTAMPTZ-backed store."""

    def test_add_scan_duration_computes_real_duration(
        self, self_monitoring_tables, pg_backend_factory
    ) -> None:
        from code_indexer.server.web.routes import _add_scan_duration

        dsn = self_monitoring_tables
        backend = pg_backend_factory(dsn, "bug1701-routes-duration")
        started = datetime.now(timezone.utc)
        completed = started + timedelta(minutes=3, seconds=15)
        _seed_scan(backend, "scan1", started.isoformat(), completed.isoformat())

        scans = backend.list_scans()
        _add_scan_duration(scans)

        assert scans[0]["duration"] == "3m 15s", (
            f"expected a real computed duration, got {scans[0]['duration']!r} "
            "-- this is the exact production symptom: TypeError from "
            "datetime.fromisoformat() on a native datetime silently caught "
            "and degraded to 'N/A'"
        )

    def test_calculate_next_scan_time_computes_real_time(
        self, self_monitoring_tables, pg_backend_factory
    ) -> None:
        from code_indexer.server.web.routes import _calculate_next_scan_time

        dsn = self_monitoring_tables
        backend = pg_backend_factory(dsn, "bug1701-routes-next-scan")
        started = datetime.now(timezone.utc)
        _seed_scan(backend, "scan1", started.isoformat())

        last_started = backend.get_last_started_at()
        next_scan = _calculate_next_scan_time(last_started, cadence_minutes=60)

        assert next_scan is not None, (
            "expected a real computed next-scan time -- this is the exact "
            "production symptom: TypeError from datetime.fromisoformat() on "
            "a native datetime, caught by a broad except Exception that "
            "logs a spurious ERROR (WEB-SELF-MONITORING-003) and returns "
            "None ('N/A' in the UI)"
        )
        expected = started + timedelta(minutes=60)
        assert datetime.fromisoformat(next_scan) == expected
