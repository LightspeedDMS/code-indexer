"""Bug #1533: live-PostgreSQL proof that the temporal lineage lookup reads the
SHARED metadata store.

This is the cluster fact pattern verbatim, against a REAL psycopg connection
and REAL tables -- no mock of the registry, per this project's "faithful DB
mocks" lesson (an unfaithful fake can certify a silent no-op as passing):

    PG activated rows: [('e2e1529mine', 'e2e1529', 'admin')]
    node-local sqlite activated_repos: 0

The activation row is inserted ONLY into PostgreSQL, and NO node-local JSON
metadata file is written. The activation's clone DIRECTORY does exist, exactly
as on a real node with shared storage -- so the ONLY thing distinguishing a
correct read from the pre-fix one is which metadata store is consulted.

DESTRUCTION SAFETY (two independent guards)
-------------------------------------------
An earlier version ran ``DROP TABLE IF EXISTS activated_repos`` against
whatever ``TEST_POSTGRES_DSN`` pointed at -- a misconfigured runner or
copy-pasted DSN would have destroyed real activation metadata. Other live-PG
modules here (e.g. test_migration_runner.py's ``isolated_schema``, dropping
``schema_migrations``) still do this; that cleanup is flagged separately.

Guard 1 -- the database name must FULLY MATCH ``_DISPOSABLE_DB_NAME_REGEX``,
taken from libpq's OWN resolution (``conninfo_to_dict``) and re-confirmed
against ``SELECT current_database()`` before any DDL. Both details were real
bypasses: substring containment accepted `production_cidx_test`, and
re-parsing the DSN missed that a later ``dbname=`` overrides the URI path
(`postgresql://u@h/cidx_test?dbname=cidx_server` reads disposable, connects to
the real database). Anything else FAILS loudly rather than skipping.

Guard 2 -- the real protection: each test creates its OWN uniquely-named
schema, puts its tables inside it via a ``search_path`` DSN option, and drops
ONLY that schema; nothing in ``public`` is created, modified or dropped, proven
read-only by ``test_private_schema_isolation_leaves_public_schema_intact``.
(Older live-PG modules here still drop fixed production-shaped tables -- an
outstanding hazard, never a pattern to copy.)
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Iterator, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.services.temporal_worker import (
    _resolve_golden_temporal_context,
)
from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)

HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass

USERNAME = "admin"
ACTIVATED_ALIAS = "e2e1533mine"
GOLDEN_ALIAS = "e2e1533"
CLONE_BRANCH = "master"
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 2

# Guard 1: the database name must FULLY MATCH this disposable format, so a DSN
# aimed at a real/shared database is refused rather than operated on.
#
# A FULL-STRING match, never substring containment. `marker in db_name` accepts
# production-shaped names that merely contain a marker somewhere --
# `production_cidx_test`, `cidx_test_prod` and `attestation` all slipped through
# that check, which is the weakness Codex's review of this module found. The
# optional suffix is DIGITS ONLY, so no word like "prod" can ride along behind a
# marker. Matched with re.fullmatch in _refuse_unless_disposable.
_DISPOSABLE_DB_NAME_REGEX = r"(?:cidx_)?(?:test|tmp|scratch|sandbox)(?:_[0-9]+)?"

_SCHEMA_PREFIX = "cidx_bug1533_"


class _WorkerInput:
    """Only the attributes the lineage resolver reads."""

    def __init__(self, repo_path: str) -> None:
        self.username = USERNAME
        self.repository_alias = ACTIVATED_ALIAS
        self.repo_path = repo_path


def _resolved_database_name(dsn: str) -> Optional[str]:
    """The database libpq will ACTUALLY connect to, or None if the DSN names
    none.

    Asks libpq's own parser rather than re-parsing the string. A static
    re-parse is not the point of truth and was genuinely bypassable: libpq
    lets a later ``dbname=`` override the URI path, so
    ``postgresql://u@h/cidx_test?dbname=cidx_server`` reads as disposable but
    connects to ``cidx_server``. Both bypass shapes are covered by
    TestDisposableDatabaseGuard.BYPASS_DSNS.
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
    """FAIL (never silently skip) unless the database libpq RESOLVES from this
    DSN fully matches the disposable format."""
    import re

    resolved = _resolved_database_name(dsn)
    if not resolved:
        pytest.fail(
            "TEST_POSTGRES_DSN names no database, so the connection would "
            "inherit PGDATABASE or a service-file default that this guard "
            "cannot inspect -- refusing to run. Name the disposable database "
            "explicitly, e.g. cidx_test_1533."
        )
    db_name = resolved.lower()
    if re.fullmatch(_DISPOSABLE_DB_NAME_REGEX, db_name) is None:
        pytest.fail(
            f"TEST_POSTGRES_DSN points at database {db_name!r}, which does not "
            f"FULLY match the disposable format {_DISPOSABLE_DB_NAME_REGEX!r} "
            "(a name merely CONTAINING 'test' is deliberately not enough). "
            "Refusing to run: this module creates and drops schemas and must "
            "never be aimed at a real or shared database. Point "
            "TEST_POSTGRES_DSN at a disposable database, e.g. cidx_test_1533."
        )


class TestDisposableDatabaseGuard:
    """The destruction-safety guard itself, tested WITHOUT any database.

    Runs unconditionally (no TEST_POSTGRES_DSN, no psycopg needed), because a
    guard exercised only when someone happens to have PostgreSQL wired up is
    not a guard. Codex's review found the original check was substring
    containment (``marker in db_name``), which accepts production-shaped names
    that merely contain "test" anywhere -- ``production_cidx_test`` and
    ``cidx_test_prod`` both slipped through. The rule is now a FULL-STRING
    match against an explicit disposable format.
    """

    # Names that must NEVER be operated on. cidx_server is this project's real
    # production database name; attestation merely contains "test".
    REFUSED_NAMES = (
        "cidx_server",
        "cidx_production_lookalike",
        "production_cidx_test",
        "cidx_test_prod",
        "cidx_prod",
        "attestation",
        "",
    )

    # The disposable format: optional cidx_ prefix, a disposable marker, and an
    # optional NUMERIC suffix (digits only, so no word like "prod" rides along).
    ACCEPTED_NAMES = (
        "cidx_test_1533",
        "test",
        "cidx_tmp",
        "scratch",
        "cidx_sandbox_7",
    )

    @pytest.mark.parametrize("db_name", REFUSED_NAMES)
    def test_refuses_non_disposable_database_name(self, db_name: str) -> None:
        with pytest.raises(pytest.fail.Exception) as exc_info:
            _refuse_unless_disposable(f"postgresql://u@h:5432/{db_name}")
        # Matched case-insensitively on "refus" so the assertion pins the
        # REASON (the guard tripped) rather than exact prose -- the
        # no-database path and the bad-format path word it differently.
        assert "refus" in str(exc_info.value).lower()

    @pytest.mark.parametrize("db_name", ACCEPTED_NAMES)
    def test_accepts_disposable_database_name(self, db_name: str) -> None:
        _refuse_unless_disposable(f"postgresql://u@h:5432/{db_name}")

    def test_key_value_dsn_form_is_also_guarded(self) -> None:
        """libpq accepts key=value DSNs too; the guard must not be bypassable
        by using that form."""
        with pytest.raises(pytest.fail.Exception):
            _refuse_unless_disposable("host=h port=5432 dbname=cidx_server")

    # DSNs whose libpq-RESOLVED database is not disposable, even though a
    # naive re-parse of the string sees a disposable-looking name. libpq lets
    # a later `dbname=` override the URI path, and a DSN naming no database at
    # all silently inherits PGDATABASE/the service file -- so the connection
    # would land somewhere the guard never inspected.
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

    Production code issues unqualified SQL (``FROM activated_repos``), so the
    only way to redirect it into a throwaway schema without editing that code
    is the connection's own search_path.
    """
    option = f"-csearch_path={schema}"
    if "://" in dsn:
        parts = urlparse(dsn)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "options"]
        query.append(("options", option))
        return urlunparse(parts._replace(query=urlencode(query)))
    return f"{dsn} options='{option}'"


@pytest.fixture(scope="module")
def pg_dsn_for_lineage() -> str:
    """Module-scoped DSN for the live-PG lineage test. Skips when PostgreSQL is
    unavailable, but FAILS when it is present and pointed somewhere this module
    must not touch."""
    if not HAS_PSYCOPG_FOR_LIVE_PG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    # The name guard runs BEFORE any connection attempt, so a DSN aimed
    # somewhere forbidden can never be contacted, let alone skipped past.
    _refuse_unless_disposable(dsn)

    import psycopg

    # Only a genuinely unreachable server is a legitimate skip. An auth
    # failure, a missing database or a malformed DSN is MISCONFIGURATION and
    # must fail loudly -- skipping those silently turns a broken setup into a
    # green run.
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

    # Point of truth: the SERVER's own answer, checked before any DDL runs.
    # libpq resolution is what actually decides the target, so validate what
    # we are genuinely connected to -- not what the DSN string looked like.
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
def isolated_schema_dsn(pg_dsn_for_lineage: str) -> Iterator[str]:
    """A DSN scoped to a private, uniquely-named schema holding real
    ``activated_repos`` + ``golden_repos_metadata`` tables.

    Only this schema is dropped, so nothing in ``public`` -- least of all a
    real ``activated_repos`` -- can be affected.
    """
    import psycopg
    from psycopg import sql

    schema_name = f"{_SCHEMA_PREFIX}{uuid.uuid4().hex[:12]}"
    schema_ident = sql.Identifier(schema_name)

    with psycopg.connect(pg_dsn_for_lineage, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema_ident))
    try:
        scoped_dsn = _dsn_with_search_path(pg_dsn_for_lineage, schema_name)
        with psycopg.connect(scoped_dsn, autocommit=True) as conn:
            # Mirrors migrations 011 + 039 (activated_repos) and 001
            # (golden_repos_metadata), created inside the private schema.
            conn.execute(
                sql.SQL(
                    """
                    CREATE TABLE {}.activated_repos (
                        id SERIAL PRIMARY KEY,
                        username TEXT NOT NULL,
                        user_alias TEXT NOT NULL,
                        golden_repo_alias TEXT,
                        repo_path TEXT NOT NULL,
                        current_branch TEXT DEFAULT 'main',
                        activated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                        last_accessed TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                        git_committer_email TEXT,
                        ssh_key_used BOOLEAN DEFAULT FALSE,
                        is_composite BOOLEAN DEFAULT FALSE,
                        wiki_enabled BOOLEAN DEFAULT FALSE,
                        metadata_json JSONB,
                        activation_id TEXT,
                        UNIQUE(username, user_alias)
                    )
                    """
                ).format(schema_ident)
            )
            conn.execute(
                sql.SQL(
                    """
                    CREATE TABLE {}.golden_repos_metadata (
                        alias                   TEXT        PRIMARY KEY NOT NULL,
                        repo_url                TEXT        NOT NULL,
                        default_branch          TEXT        NOT NULL,
                        clone_path              TEXT        NOT NULL,
                        created_at              TIMESTAMPTZ NOT NULL,
                        enable_temporal         BOOLEAN     NOT NULL DEFAULT FALSE,
                        temporal_options        JSONB,
                        wiki_enabled            BOOLEAN     DEFAULT FALSE,
                        category_id             INTEGER,
                        category_auto_assigned  BOOLEAN     DEFAULT FALSE
                    )
                    """
                ).format(schema_ident)
            )
        yield scoped_dsn
    finally:
        with psycopg.connect(pg_dsn_for_lineage, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(schema_ident)
            )


def _insert_shared_activation_row(dsn: str, repo_path: Path) -> None:
    """Write the activation to the SHARED store ONLY -- no JSON file anywhere,
    which is precisely a cluster node's view of a repo activated elsewhere."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activated_repos "
            "(username, user_alias, golden_repo_alias, repo_path, "
            "current_branch) VALUES (%s, %s, %s, %s, %s)",
            (USERNAME, ACTIVATED_ALIAS, GOLDEN_ALIAS, str(repo_path), CLONE_BRANCH),
        )


@pytest.fixture
def cluster_node_managers(
    isolated_schema_dsn: str, tmp_path: Path
) -> Iterator[Tuple[ActivatedRepoManager, ActivatedRepoManager, Path]]:
    """Two REAL managers over the SAME data dir, differing only in which
    metadata stores they read: one FULLY wired to real PostgreSQL (activation
    pool AND an injected PG golden-metadata backend -- the server's DI-wired
    instance on a cluster node), one not wired at all (what the worker used to
    construct for itself). The clone directory exists for both.

    The pool is closed in ``finally`` so a failure anywhere in the remaining
    setup cannot leak it.
    """
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
        GoldenRepoMetadataPostgresBackend,
    )

    data_dir = tmp_path / "cluster-node" / "data"
    pool = ConnectionPool(
        isolated_schema_dsn, min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE
    )
    try:
        pg_backed = ActivatedRepoManager(
            data_dir=str(data_dir),
            golden_repo_manager=GoldenRepoManager(
                data_dir=str(data_dir),
                storage_backend=GoldenRepoMetadataPostgresBackend(pool),
            ),
        )
        pg_backed.set_connection_pool(pool)

        node_local = ActivatedRepoManager(data_dir=str(data_dir))

        clone_dir = Path(pg_backed.get_activated_repo_path(USERNAME, ACTIVATED_ALIAS))
        clone_dir.mkdir(parents=True, exist_ok=True)
        _insert_shared_activation_row(isolated_schema_dsn, clone_dir)

        yield pg_backed, node_local, clone_dir
    finally:
        pool.close()


def test_shared_store_manager_resolves_the_lineage(cluster_node_managers) -> None:
    """The row lives only in PostgreSQL, so only the shared-store manager can
    answer -- and it must yield the golden repo's FIXED temporal root."""
    pg_backed, _node_local, clone_dir = cluster_node_managers

    assert pg_backed.uses_shared_metadata_stores() is True

    ctx = _resolve_golden_temporal_context(
        _WorkerInput(str(clone_dir)),
        "job-pg-1533",
        activated_repo_manager=pg_backed,
    )

    assert ctx.alias == GOLDEN_ALIAS
    assert ctx.temporal_index_dir == server_temporal_index_root(
        Path(pg_backed.activated_repos_dir).parent / "golden-repos", GOLDEN_ALIAS
    )


def test_node_local_manager_cannot_see_the_shared_row(cluster_node_managers) -> None:
    """Discriminating counterpart: the same lookup through a node-local
    manager finds NOTHING for a genuinely-activated repo.

    Pre-fix that None became an all-None context, the read fell back to the
    activation's own CoW clone (empty of temporal data since Bug #1529), and
    the query answered HTTP 200 with zero results.
    """
    _pg_backed, node_local, _clone_dir = cluster_node_managers

    assert node_local.uses_shared_metadata_stores() is False
    assert node_local.get_repository(USERNAME, ACTIVATED_ALIAS, touch=False) is None


def _public_schema_table_identities(dsn: str) -> list:
    """Every table in ``public``, as (name, OID) pairs.

    The OID, not merely the name, so a drop-and-recreate -- which would
    silently destroy rows while leaving a same-named table behind -- is caught
    just as a plain create or drop is. ``relkind IN ('r', 'p')`` covers
    ordinary AND partitioned tables. Read-only: this inspects catalogs and
    writes nothing.
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
    pg_dsn_for_lineage: str, request: pytest.FixtureRequest
) -> None:
    """Destruction-safety regression guard for THIS module's fixtures.

    Compares the FULL identity of the ``public`` schema (every table with its
    OID) before and after the real fixture chain runs, which catches a create,
    a drop, and a drop-and-recreate alike.

    Deliberately READ-ONLY. An earlier version planted a sentinel table in
    ``public`` to prove rows survived; that contradicted this module's own
    "nothing in public is created, modified or dropped" claim and could leak
    the table if the insert raised before its try block. Comparing catalog
    identity proves the same property without writing anything.
    """
    before = _public_schema_table_identities(pg_dsn_for_lineage)

    # Runs the real fixture chain (schema creation, DDL, row insert, manager
    # construction) against the same database.
    request.getfixturevalue("cluster_node_managers")

    assert _public_schema_table_identities(pg_dsn_for_lineage) == before, (
        "the public schema changed while these fixtures ran -- they must "
        "operate ONLY inside their private schema"
    )
