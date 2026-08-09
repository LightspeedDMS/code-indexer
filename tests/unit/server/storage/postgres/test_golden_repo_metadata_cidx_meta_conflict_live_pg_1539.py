"""Live PostgreSQL proof for Bug #1539's cidx-meta conflict quarantine
(Codex round-3 item (e)): GoldenRepoMetadataPostgresBackend's
record/reset/get_cidx_meta_conflict_failure against a REAL, unmocked
PostgreSQL connection.

TEST_POSTGRES_DSN is not set in this development environment as of this
writing -- the entire module is skip-gated and will report "skipped"
rather than "passed" here, exactly like
test_golden_repo_metadata_temporal_options_live_pg_1414.py and
test_fleet_migration_quarantine_concurrency_1477.py. It is designed to
run for real the moment a developer points TEST_POSTGRES_DSN at an
actual PostgreSQL instance.

Isolation: this module deliberately does NOT copy
test_golden_repo_metadata_temporal_options_live_pg_1414.py's own
`DROP TABLE IF EXISTS <fixed name>` pattern against whatever the DSN
points at -- CLAUDE.md flags that as a known, still-outstanding hazard.
Instead it follows test_temporal_worker_lineage_live_pg_1533.py's SAFER
private-per-test-SCHEMA pattern: a uniquely-named schema is created, the
table lives ONLY inside it (via a `search_path` DSN option), and ONLY
that schema is dropped afterward -- `public` is never touched. The
disposable-database-name guard is duplicated here deliberately (small,
self-contained, matches this codebase's existing per-file convention for
live-PG safety checks) rather than imported cross-module.
"""

import os
import re
import uuid
from typing import Iterator

import pytest

HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass

# Guard: the configured database name must FULLY match this disposable
# format -- substring containment (e.g. "test" in "production_cidx_test")
# is NOT acceptable, matching test_temporal_worker_lineage_live_pg_1533.py.
_DISPOSABLE_DB_NAME_REGEX = r"(?:cidx_)?(?:test|tmp|scratch|sandbox)(?:_[0-9]+)?"


def _require_disposable_database(dsn: str) -> None:
    """Bug #1539 (Codex round-4 finding 4): an unsafe/unparseable DSN must
    FAIL the test (pytest.fail), never skip -- a skip lets the suite
    report green with zero safety validation ever having run, matching
    the established convention in
    test_temporal_worker_lineage_live_pg_1533.py. "PostgreSQL not
    configured at all" (missing TEST_POSTGRES_DSN, handled by the
    fixture BEFORE this function is ever called) remains a legitimate
    skip -- only "a DSN IS present but fails this safety check" fails.
    """
    match = re.search(r"/([^/?]+)(?:\?.*)?$", dsn)
    db_name = match.group(1) if match else ""
    if re.fullmatch(_DISPOSABLE_DB_NAME_REGEX, db_name) is None:
        pytest.fail(
            f"TEST_POSTGRES_DSN database name {db_name!r} does not FULLY "
            f"match the disposable format {_DISPOSABLE_DB_NAME_REGEX!r} "
            f"-- refusing to run live-PG schema-creation tests against a "
            f"database that might not be disposable"
        )


class TestDisposableDatabaseGuard:
    """The destruction-safety guard itself, tested WITHOUT any database
    (Bug #1539 Codex round-4 finding 4) -- runs unconditionally so the
    guard is exercised on every test run, not only when someone happens
    to have PostgreSQL wired up. Mirrors
    test_temporal_worker_lineage_live_pg_1533.py's own
    TestDisposableDatabaseGuard class."""

    REFUSED_NAMES = (
        "cidx_server",
        "production_cidx_test",
        "cidx_test_prod",
        "cidx_prod",
        "attestation",
        "",
    )

    ACCEPTED_NAMES = (
        "cidx_test_1539",
        "test",
        "cidx_tmp",
        "scratch",
        "cidx_sandbox_7",
    )

    @pytest.mark.parametrize("db_name", REFUSED_NAMES)
    def test_refuses_non_disposable_database_name(self, db_name: str) -> None:
        with pytest.raises(pytest.fail.Exception):
            _require_disposable_database(f"postgresql://u@h:5432/{db_name}")

    @pytest.mark.parametrize("db_name", ACCEPTED_NAMES)
    def test_accepts_disposable_database_name(self, db_name: str) -> None:
        _require_disposable_database(
            f"postgresql://u@h:5432/{db_name}"
        )  # must not raise


@pytest.fixture(scope="module")
def pg_dsn_for_conflict_quarantine():
    if not HAS_PSYCOPG_FOR_LIVE_PG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    _require_disposable_database(dsn)
    try:
        import psycopg

        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"PostgreSQL unreachable: {exc}")
    return dsn


@pytest.fixture
def isolated_schema_dsn(pg_dsn_for_conflict_quarantine: str) -> Iterator[str]:
    """Create a uniquely-named private schema, apply the Bug #1539 table
    DDL inside it via a search_path DSN option, yield the scoped DSN, and
    drop ONLY that schema afterward."""
    import psycopg
    from psycopg import sql

    schema_name = f"cidx_test_1539_{uuid.uuid4().hex[:12]}"
    schema_ident = sql.Identifier(schema_name)

    with psycopg.connect(pg_dsn_for_conflict_quarantine) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema_ident))
        conn.commit()

    scoped_dsn = (
        f"{pg_dsn_for_conflict_quarantine} options='-csearch_path={schema_name}'"
    )

    with psycopg.connect(scoped_dsn) as conn:
        conn.execute(
            """
            CREATE TABLE cidx_meta_conflict_quarantine_state (
                golden_alias                TEXT PRIMARY KEY,
                consecutive_failure_count   INTEGER NOT NULL DEFAULT 0,
                last_target_sha             TEXT,
                last_detail                 TEXT,
                first_failed_at             TIMESTAMPTZ,
                last_failed_at              TIMESTAMPTZ,
                updated_at                  TIMESTAMPTZ
            )
            """
        )
        conn.commit()

    try:
        yield scoped_dsn
    finally:
        with psycopg.connect(pg_dsn_for_conflict_quarantine) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(schema_ident)
            )
            conn.commit()


class TestLivePostgresConflictQuarantine:
    def test_record_same_target_sha_increments(self, isolated_schema_dsn):
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(isolated_schema_dsn)
        backend = GoldenRepoMetadataPostgresBackend(pool)
        try:
            assert (
                backend.record_cidx_meta_conflict_failure(
                    "cidx-meta-global", "sha-a", "d1"
                )
                == 1
            )
            assert (
                backend.record_cidx_meta_conflict_failure(
                    "cidx-meta-global", "sha-a", "d2"
                )
                == 2
            )
        finally:
            backend.close()

    def test_record_different_target_sha_resets_to_one(self, isolated_schema_dsn):
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(isolated_schema_dsn)
        backend = GoldenRepoMetadataPostgresBackend(pool)
        try:
            backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d1")
            backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d2")
            assert (
                backend.record_cidx_meta_conflict_failure(
                    "cidx-meta-global", "sha-b", "d3"
                )
                == 1
            )
        finally:
            backend.close()

    def test_reset_clears_state(self, isolated_schema_dsn):
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(isolated_schema_dsn)
        backend = GoldenRepoMetadataPostgresBackend(pool)
        try:
            backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d1")
            backend.reset_cidx_meta_conflict_failure("cidx-meta-global")
            assert (
                backend.get_cidx_meta_conflict_failure_state("cidx-meta-global") is None
            )
        finally:
            backend.close()

    def test_get_state_reflects_last_target_sha_and_count(self, isolated_schema_dsn):
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool = ConnectionPool(isolated_schema_dsn)
        backend = GoldenRepoMetadataPostgresBackend(pool)
        try:
            backend.record_cidx_meta_conflict_failure(
                "cidx-meta-global", "sha-a", "detail-1"
            )
            backend.record_cidx_meta_conflict_failure(
                "cidx-meta-global", "sha-a", "detail-2"
            )
            state = backend.get_cidx_meta_conflict_failure_state("cidx-meta-global")
            assert state["consecutive_failure_count"] == 2
            assert state["last_target_sha"] == "sha-a"
            assert state["last_detail"] == "detail-2"
        finally:
            backend.close()
