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
An earlier version of this module ran ``DROP TABLE IF EXISTS activated_repos``
against whatever ``TEST_POSTGRES_DSN`` pointed at. That is unacceptable: a
misconfigured runner or a copy-pasted DSN would destroy real activation
metadata. Note that this project's OTHER live-PG modules (e.g.
test_migration_runner.py's ``isolated_schema``, which drops
``schema_migrations``) share that hazard on a fixed, production-shaped table
name -- they predate this module and are NOT a pattern to copy; the wider
cleanup is flagged separately rather than done here.

Guard 1 -- the target database's name must FULLY MATCH the disposable format
``_DISPOSABLE_DB_NAME_REGEX`` (never merely contain a marker: substring
containment accepted `production_cidx_test` and `cidx_test_prod`, which is the
weakness Codex's review found). A DSN pointing anywhere else FAILS loudly
rather than skipping, so a misconfiguration is visible instead of silently
tolerated.

Guard 2 -- and the real protection: every test creates its OWN uniquely-named
schema, creates its tables INSIDE it, reaches them through a ``search_path``
option on the DSN, and drops ONLY that schema. Nothing in ``public`` is
created, modified, or dropped, so even a DSN aimed at a shared database cannot
lose data. ``test_private_schema_isolation_leaves_public_schema_intact`` is the
standing regression guard for that property.
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
_SENTINEL_PREFIX = "cidx_bug1533_sentinel_"
_SENTINEL_VALUE = "must-survive"


class _WorkerInput:
    """Only the attributes the lineage resolver reads."""

    def __init__(self, repo_path: str) -> None:
        self.username = USERNAME
        self.repository_alias = ACTIVATED_ALIAS
        self.repo_path = repo_path


def _database_name(dsn: str) -> str:
    """The database name from a URI-form or key=value-form libpq DSN."""
    if "://" in dsn:
        return urlparse(dsn).path.lstrip("/")
    for token in dsn.split():
        key, _, value = token.partition("=")
        if key.strip() == "dbname":
            return value.strip().strip("'\"")
    return ""


def _refuse_unless_disposable(dsn: str) -> None:
    """FAIL (never silently skip) unless the target database's name FULLY
    matches the disposable format."""
    import re

    db_name = _database_name(dsn).lower()
    if not db_name:
        pytest.fail(
            "TEST_POSTGRES_DSN does not name a database -- refusing to run "
            "live-PostgreSQL tests against an unidentified target."
        )
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
        assert "Refusing" in str(exc_info.value) or "does not name" in str(
            exc_info.value
        )

    @pytest.mark.parametrize("db_name", ACCEPTED_NAMES)
    def test_accepts_disposable_database_name(self, db_name: str) -> None:
        _refuse_unless_disposable(f"postgresql://u@h:5432/{db_name}")

    def test_key_value_dsn_form_is_also_guarded(self) -> None:
        """libpq accepts key=value DSNs too; the guard must not be bypassable
        by using that form."""
        with pytest.raises(pytest.fail.Exception):
            _refuse_unless_disposable("host=h port=5432 dbname=cidx_server")


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
    _refuse_unless_disposable(dsn)
    try:
        import psycopg

        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
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


def _public_activated_repos_oid(dsn: str) -> Optional[str]:
    """``public.activated_repos``'s OID, or None when it does not exist.

    The OID (not merely existence) is compared so that a drop-and-recreate --
    which would silently destroy rows while leaving a same-named table -- is
    also caught.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT to_regclass('public.activated_repos')::oid::text"
        ).fetchone()
    return None if row is None else row[0]


def test_private_schema_isolation_leaves_public_schema_intact(
    pg_dsn_for_lineage: str, request: pytest.FixtureRequest
) -> None:
    """Destruction-safety regression guard for THIS module's fixtures.

    Snapshots the state of ``public`` BEFORE the fixtures run (the whole point
    -- a bare after-the-fact absence check cannot tell a pre-existing table
    from one created and dropped, nor a dropped-and-recreated one from an
    untouched one), plants a sentinel table with a row in ``public``, then
    triggers the full fixture setup via ``getfixturevalue`` and asserts:

    * ``public.activated_repos``'s OID is unchanged (absent stays absent;
      present stays the SAME table, not a recreated empty one);
    * the sentinel row is still there.

    The sentinel table is uniquely named and created by this test, so dropping
    it in ``finally`` destroys nothing but our own scratch object.
    """
    import psycopg
    from psycopg import sql

    sentinel_ident = sql.Identifier(f"{_SENTINEL_PREFIX}{uuid.uuid4().hex[:12]}")
    oid_before = _public_activated_repos_oid(pg_dsn_for_lineage)

    with psycopg.connect(pg_dsn_for_lineage, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE TABLE public.{} (marker TEXT)").format(sentinel_ident)
        )
        conn.execute(
            sql.SQL("INSERT INTO public.{} (marker) VALUES (%s)").format(
                sentinel_ident
            ),
            (_SENTINEL_VALUE,),
        )
    try:
        # Runs the real fixture chain (schema creation, DDL, row insert,
        # manager construction) against the same database.
        request.getfixturevalue("cluster_node_managers")

        assert _public_activated_repos_oid(pg_dsn_for_lineage) == oid_before, (
            "public.activated_repos changed identity while these fixtures ran "
            "-- they must operate ONLY inside their private schema"
        )
        with psycopg.connect(pg_dsn_for_lineage, autocommit=True) as conn:
            markers = conn.execute(
                sql.SQL("SELECT marker FROM public.{}").format(sentinel_ident)
            ).fetchall()
        assert markers == [(_SENTINEL_VALUE,)], (
            "a sentinel row in the public schema did not survive the live-PG "
            "fixtures -- something is writing outside the private schema"
        )
    finally:
        with psycopg.connect(pg_dsn_for_lineage, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP TABLE IF EXISTS public.{}").format(sentinel_ident)
            )
