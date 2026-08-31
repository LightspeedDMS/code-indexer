"""
Story #1589: live-PostgreSQL round-trip tests for
GoldenRepoMetadataPostgresBackend.clear_all_dedup_states(reason) -- the
bulk "Clear All Dedup Warnings" action.

Per the project's "faithful DB mocks" lesson, this exercises a REAL
psycopg v3 connection -- not a mock.

DESTRUCTION SAFETY (two independent guards)
-------------------------------------------
The original version of this module ran ``DROP TABLE IF EXISTS
fleet_migration_dedup_state`` against whatever ``TEST_POSTGRES_DSN``
pointed at, with no disposable-database-name guard and no schema
isolation. CLAUDE.md's Bug #1533 section calls this exact pattern out by
name as a "KNOWN, still-outstanding hazard -- do not copy them": pointing
this suite's DSN at the staging clustered PostgreSQL (which the story's
own Testing Requirements ask testers to do) would DROP the real
``fleet_migration_dedup_state`` table, and because migration 045 is
already recorded in ``schema_migrations``, ``MigrationRunner`` would
never recreate it on the next start -- silently breaking the
fleet-migration dedup-audit mechanism in production.

Fixed by adopting the exact guarded pattern already established in
``test_temporal_worker_lineage_live_pg_1533.py`` (read that module for the
full rationale):

Guard 1 -- the database name must FULLY MATCH ``_DISPOSABLE_DB_NAME_REGEX``,
taken from libpq's OWN resolution (``conninfo_to_dict``) and re-confirmed
against ``SELECT current_database()`` before any DDL. A full-string match,
never substring containment (``production_cidx_test``/``cidx_test_prod``
must both be refused). Anything else FAILS loudly rather than skipping.
This guard itself is proven by ``TestDisposableDatabaseGuard`` below --
those tests require ``psycopg`` to be INSTALLED (it is a normal
dependency of this test module, imported by the guard's own DSN-parsing
helper) but need NO live/reachable PostgreSQL SERVER, so they still catch
a regression in the guard logic even when ``TEST_POSTGRES_DSN`` is unset.

Guard 2 -- the real protection: each test creates its OWN uniquely-named
schema, puts its ``fleet_migration_dedup_state`` table inside it via a
``search_path`` DSN option, and drops ONLY that schema; nothing in
``public`` is created, modified or dropped, proven read-only by
``test_private_schema_isolation_leaves_public_schema_intact``.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any, Dict, Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pytest


HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass

_CLEAR_ALL_REASON = "manually acknowledged via Diagnostics tab"

_DEFAULT_OUTCOME: Dict[str, Any] = {
    "duplicate_groups": 1,
    "records_before": 10,
    "records_deleted": 1,
    "winner_kept_groups": 1,
    "whole_group_deleted_groups": 0,
    "collection_total": 10,
}

# Guard 1: the database name must FULLY MATCH this disposable format, so a
# DSN aimed at a real/shared database is refused rather than operated on.
# A FULL-STRING match, never substring containment -- see
# test_temporal_worker_lineage_live_pg_1533.py for the exact rationale
# (`production_cidx_test` and `cidx_test_prod` must both be refused). The
# optional suffix is DIGITS ONLY so no word like "prod" can ride along
# behind a marker. Matched with re.fullmatch in _refuse_unless_disposable.
_DISPOSABLE_DB_NAME_REGEX = r"(?:cidx_)?(?:test|tmp|scratch|sandbox)(?:_[0-9]+)?"

_SCHEMA_PREFIX = "cidx_story1589_"


def _resolved_database_name(dsn: str) -> Optional[str]:
    """The database libpq will ACTUALLY connect to, or None if the DSN
    names none. Asks libpq's own parser rather than re-parsing the
    string -- a static re-parse is bypassable (a later ``dbname=`` in a
    key/value DSN overrides an earlier one). Requires psycopg to be
    INSTALLED (never a live server -- this is pure DSN parsing)."""
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
            "explicitly, e.g. cidx_test_1589."
        )
    db_name = resolved.lower()
    if re.fullmatch(_DISPOSABLE_DB_NAME_REGEX, db_name) is None:
        pytest.fail(
            f"TEST_POSTGRES_DSN points at database {db_name!r}, which does not "
            f"FULLY match the disposable format {_DISPOSABLE_DB_NAME_REGEX!r} "
            "(a name merely CONTAINING 'test' is deliberately not enough). "
            "Refusing to run: this module creates and drops schemas and must "
            "never be aimed at a real or shared database. Point "
            "TEST_POSTGRES_DSN at a disposable database, e.g. cidx_test_1589."
        )


@pytest.mark.skipif(
    not HAS_PSYCOPG_FOR_LIVE_PG,
    reason="psycopg not installed -- the guard's DSN parser needs the "
    "package, though never a live/reachable PostgreSQL server",
)
class TestDisposableDatabaseGuard:
    """The destruction-safety guard itself, tested WITHOUT a live/reachable
    PostgreSQL SERVER (no ``TEST_POSTGRES_DSN`` needed). ``psycopg`` must
    still be INSTALLED, since the guard's own DSN-parsing helper
    (``_resolved_database_name``) imports ``psycopg.conninfo`` to ask
    libpq how it resolves a DSN string -- that parsing is pure, local
    logic with no network I/O. A guard exercised only when someone
    happens to have a live PostgreSQL server wired up is not a guard.
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
        "cidx_test_1589",
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


def _dsn_with_search_path(dsn: str, schema: str) -> str:
    """The same DSN, resolving unqualified table names to *schema* first.

    Production code issues unqualified SQL (``FROM
    fleet_migration_dedup_state``), so the only way to redirect it into a
    throwaway schema without editing that code is the connection's own
    search_path.
    """
    option = f"-csearch_path={schema}"
    if "://" in dsn:
        parts = urlparse(dsn)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "options"]
        query.append(("options", option))
        return urlunparse(parts._replace(query=urlencode(query)))
    return f"{dsn} options='{option}'"


@pytest.fixture(scope="module")
def pg_dsn_for_dedup_state_clear_all() -> str:
    """Module-scoped DSN for the live-PG dedup-state clear-all tests.
    Skips when PostgreSQL is unavailable, but FAILS when it is present
    and pointed somewhere this module must not touch."""
    if not HAS_PSYCOPG_FOR_LIVE_PG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    # The name guard runs BEFORE any connection attempt, so a DSN aimed
    # somewhere forbidden can never be contacted, let alone skipped past.
    _refuse_unless_disposable(dsn)

    import psycopg

    connection_unavailable_markers = (
        "connection refused",
        "could not connect",
        "no such file or directory",
        "is the server running",
        "timeout expired",
    )
    try:
        with psycopg.connect(dsn) as conn:
            row = conn.execute("SELECT current_database()").fetchone()
    except psycopg.OperationalError as exc:
        if any(m in str(exc).lower() for m in connection_unavailable_markers):
            pytest.skip(f"PostgreSQL not reachable: {exc}")
        pytest.fail(
            f"PostgreSQL is reachable but the connection failed ({exc}). "
            "That is a misconfiguration, not an absent server -- refusing to "
            "skip past it."
        )
    except Exception as exc:
        pytest.fail(f"TEST_POSTGRES_DSN is misconfigured ({exc}) -- refusing to skip.")

    connected_db = row[0] if row else None
    if not connected_db:
        pytest.fail("PostgreSQL did not report current_database() -- refusing to run.")
    if connected_db != _resolved_database_name(dsn):
        pytest.fail(
            f"Connected to database {connected_db!r}, which is not the "
            f"{_resolved_database_name(dsn)!r} this DSN resolved to -- refusing to run."
        )
    _refuse_unless_disposable(f"dbname={connected_db}")
    return dsn


@pytest.fixture
def isolated_schema_dsn(pg_dsn_for_dedup_state_clear_all: str) -> Iterator[str]:
    """A DSN scoped to a private, uniquely-named schema holding a real
    ``fleet_migration_dedup_state`` table (matching migration 045 exactly).

    Only this schema is dropped, so nothing in ``public`` -- least of all
    a real ``fleet_migration_dedup_state`` -- can be affected.
    """
    import psycopg
    from psycopg import sql

    schema_name = f"{_SCHEMA_PREFIX}{uuid.uuid4().hex[:12]}"
    schema_ident = sql.Identifier(schema_name)

    with psycopg.connect(pg_dsn_for_dedup_state_clear_all, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema_ident))
    try:
        scoped_dsn = _dsn_with_search_path(
            pg_dsn_for_dedup_state_clear_all, schema_name
        )
        with psycopg.connect(scoped_dsn, autocommit=True) as conn:
            # Mirrors migration 045 (045_fleet_migration_dedup_state.sql)
            # exactly, created inside the private schema.
            conn.execute(
                sql.SQL(
                    """
                    CREATE TABLE {}.fleet_migration_dedup_state (
                        golden_alias                TEXT PRIMARY KEY,
                        duplicate_groups            INTEGER NOT NULL DEFAULT 0,
                        records_before               INTEGER NOT NULL DEFAULT 0,
                        records_deleted              INTEGER NOT NULL DEFAULT 0,
                        winner_kept_groups           INTEGER NOT NULL DEFAULT 0,
                        whole_group_deleted_groups   INTEGER NOT NULL DEFAULT 0,
                        collection_total             INTEGER NOT NULL DEFAULT 0,
                        first_dropped_at             TIMESTAMPTZ,
                        dropped_at                   TIMESTAMPTZ,
                        cleared_at                   TIMESTAMPTZ,
                        cleared_reason               TEXT
                    )
                    """
                ).format(schema_ident)
            )
        yield scoped_dsn
    finally:
        with psycopg.connect(pg_dsn_for_dedup_state_clear_all, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(schema_ident)
            )


@pytest.fixture
def backend_and_pool(isolated_schema_dsn):
    """A real GoldenRepoMetadataPostgresBackend against the live,
    schema-isolated table, with its ConnectionPool closed after the test
    regardless of outcome."""
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
        GoldenRepoMetadataPostgresBackend,
    )

    pool = ConnectionPool(isolated_schema_dsn, name="story1589-live-clear-all")
    backend = GoldenRepoMetadataPostgresBackend(pool)
    try:
        yield backend
    finally:
        pool.close()


def _record(backend, golden_alias: str, **overrides: Any) -> Dict[str, Any]:
    kwargs = {**_DEFAULT_OUTCOME, **overrides}
    return backend.record_dedup_outcome(golden_alias, **kwargs)  # type: ignore[no-any-return]


class TestClearAllDedupStatesLivePostgresHappyPath:
    def test_clears_every_active_row_and_returns_count_live(
        self, backend_and_pool
    ) -> None:
        backend = backend_and_pool
        _record(backend, "story1589-live-repo-a")
        _record(backend, "story1589-live-repo-b")

        cleared_count = backend.clear_all_dedup_states(_CLEAR_ALL_REASON)

        assert cleared_count == 2
        for alias in ("story1589-live-repo-a", "story1589-live-repo-b"):
            state = backend.get_dedup_state(alias)
            assert state is not None
            assert state["cleared_at"] is not None
            assert state["cleared_reason"] == _CLEAR_ALL_REASON


class TestClearAllDedupStatesLivePostgresSkipsAlreadyCleared:
    def test_already_cleared_row_is_not_touched_live(self, backend_and_pool) -> None:
        backend = backend_and_pool
        _record(backend, "story1589-live-already-cleared")
        backend.clear_dedup_state(
            "story1589-live-already-cleared", "successful full re-index"
        )
        original_state = backend.get_dedup_state("story1589-live-already-cleared")
        assert original_state is not None
        _record(backend, "story1589-live-still-active")

        cleared_count = backend.clear_all_dedup_states(_CLEAR_ALL_REASON)

        assert cleared_count == 1
        unchanged = backend.get_dedup_state("story1589-live-already-cleared")
        # Full-state equality: clear_all_dedup_states must leave every
        # field of an already-cleared row byte-identical, not merely the
        # two fields it writes for a NEWLY-cleared row.
        assert unchanged == original_state


class TestClearAllDedupStatesLivePostgresNoOp:
    def test_returns_zero_when_nothing_active_live(self, backend_and_pool) -> None:
        assert backend_and_pool.clear_all_dedup_states(_CLEAR_ALL_REASON) == 0


class TestClearAllDedupStatesLivePostgresValidation:
    def test_rejects_empty_reason_live(self, backend_and_pool) -> None:
        with pytest.raises(ValueError):
            backend_and_pool.clear_all_dedup_states("")


def _public_schema_table_identities(dsn: str) -> list:
    """Every table in ``public``, as (name, OID) pairs.

    The OID, not merely the name, so a drop-and-recreate -- which would
    silently destroy rows while leaving a same-named table behind -- is
    caught just as a plain create or drop is. Read-only: this inspects
    catalogs and writes nothing.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT c.relname, c.oid::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
            "ORDER BY c.relname"
        ).fetchall()
    return list(rows)


def test_private_schema_isolation_leaves_public_schema_intact(
    pg_dsn_for_dedup_state_clear_all: str, request: pytest.FixtureRequest
) -> None:
    """Destruction-safety regression guard for THIS module's fixtures.

    Compares the FULL identity of the ``public`` schema (every table with
    its OID) before and after the real fixture chain runs, which catches a
    create, a drop, and a drop-and-recreate alike. Deliberately READ-ONLY.
    """
    before = _public_schema_table_identities(pg_dsn_for_dedup_state_clear_all)

    # Runs the real fixture chain (schema creation, DDL, backend
    # construction) against the same database.
    request.getfixturevalue("backend_and_pool")

    assert (
        _public_schema_table_identities(pg_dsn_for_dedup_state_clear_all) == before
    ), (
        "the public schema changed while these fixtures ran -- they must "
        "operate ONLY inside their private schema"
    )
