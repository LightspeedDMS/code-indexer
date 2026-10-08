"""Shared fixtures for the git confirmation-token tests (imported, never
collected: no ``test_`` prefix).

`shared_store` is parametrized over both PayloadCache backends:
  - ``sqlite``: PayloadCacheSqliteBackend on one on-disk file (solo server);
  - ``postgres``: PayloadCachePostgresBackend on TEST_POSTGRES_DSN, inside a
    per-test schema that is dropped afterwards (skips when the DSN is unset).

Every `new_cache()` call returns an INDEPENDENT PayloadCache (its own
backend object, and for PostgreSQL its own connection pool) over the SAME
underlying store -- the shape of two workers or two cluster nodes.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.services.git_operations_service import GitOperationsService
from code_indexer.server.utils.config_manager import GitTimeoutsConfig

logger = logging.getLogger(__name__)

ALICE = "alice"
BOB = "bob"
REPO_A = "repo-a"
REPO_B = "repo-b"
UNTRACKED = "junk.txt"
TRACKED = "f.txt"
COMMITTED_TEXT = "one\n"
BRANCH = "feature"


def git(args: List[str], cwd: Path) -> str:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def make_repo(root: Path, name: str) -> Path:
    """Real repo: one commit, a merged branch `feature`, an untracked file,
    and an uncommitted edit to the tracked file -- so clean, hard reset and
    branch delete each have an observable effect."""
    repo = root / name
    repo.mkdir(parents=True)
    git(["init", "-q", "-b", "main"], repo)
    git(["config", "user.email", "test@example.com"], repo)
    git(["config", "user.name", "Test User"], repo)
    (repo / TRACKED).write_text(COMMITTED_TEXT)
    git(["add", TRACKED], repo)
    git(["commit", "-q", "-m", "first"], repo)
    git(["branch", BRANCH], repo)
    (repo / UNTRACKED).write_text("untracked\n")
    (repo / TRACKED).write_text("dirty\n")
    return repo


class AliasPaths:
    """Activated-repo lookup double: resolves an alias to a fixed path."""

    def __init__(self, paths: Dict[str, Path]) -> None:
        self._paths = paths

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        if user_alias not in self._paths:
            raise FileNotFoundError(user_alias)
        return str(self._paths[user_alias])


def make_service(cache: PayloadCache, paths: Dict[str, Path]) -> Any:
    """A fresh GitOperationsService over `cache` (Any: the alias double
    stands in for ActivatedRepoManager)."""
    service: Any = GitOperationsService()
    service._git_timeouts = GitTimeoutsConfig()
    service.activated_repo_manager = AliasPaths(paths)
    service.payload_cache = cache
    return service


class SharedStore:
    """Hands out independent PayloadCache instances over one store."""

    def __init__(self, kind: str, location: str, schema: str) -> None:
        self.kind = kind
        self.location = location
        self.schema = schema
        self._pools: List[Any] = []

    def new_backend(self) -> Any:
        """A fresh backend over the shared store (Any: the two backend
        classes share only a structural Protocol)."""
        if self.kind == "sqlite":
            from code_indexer.server.storage.sqlite_backends.payload_cache_backend import (
                PayloadCacheSqliteBackend,
            )

            return PayloadCacheSqliteBackend(self.location)
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )
        from code_indexer.server.storage.postgres.payload_cache_backend import (
            PayloadCachePostgresBackend,
        )

        pool = ConnectionPool(self._pg_dsn(), min_size=1, max_size=2)
        self._pools.append(pool)
        return PayloadCachePostgresBackend(pool)

    def new_cache(self, cache_ttl_seconds: int = 900) -> PayloadCache:
        backend = self.new_backend()
        cache = PayloadCache(
            db_path=Path(self.location).parent / "unused_payload_cache.db"
            if self.kind == "sqlite"
            else Path("unused_payload_cache.db"),
            config=PayloadCacheConfig(cache_ttl_seconds=cache_ttl_seconds),
            storage_backend=backend,
        )
        cache.initialize()
        return cache

    def _pg_dsn(self) -> str:
        from psycopg.conninfo import make_conninfo

        return make_conninfo(self.location, options=f"-csearch_path={self.schema}")

    def _execute(self, sql: str, params: Tuple[Any, ...] = ()) -> List[Any]:
        """Run one committed statement on a dedicated observer connection,
        independent of the caches under test."""
        if self.kind == "sqlite":
            conn = sqlite3.connect(self.location)
            try:
                with conn:
                    cur = conn.execute(sql, params)
                    return list(cur.fetchall()) if cur.description else []
            finally:
                conn.close()
        import psycopg

        pg = psycopg.connect(self._pg_dsn(), autocommit=True)
        try:
            pg_cur = pg.execute(sql, params)
            return list(pg_cur.fetchall()) if pg_cur.description else []
        finally:
            pg.close()

    def store_subsecond_fraction(self) -> float:
        """Fractional part of the store clock's current second."""
        sql = (
            "SELECT CAST(strftime('%f', 'now') AS REAL)"
            if self.kind == "sqlite"
            else "SELECT EXTRACT(MICROSECONDS FROM clock_timestamp())::float8 / 1e6"
        )
        value = float(self._execute(sql)[0][0])
        return value - int(value)

    def age_rows(self, seconds: float) -> None:
        """Set every row's created_at to the store's own clock minus
        `seconds` (fractional seconds allowed), as if issued that long ago."""
        if self.kind == "sqlite":
            self._execute(
                "UPDATE payload_cache SET created_at = "
                "strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', ?)",
                (f"-{seconds} seconds",),
            )
            return
        self._execute(
            "UPDATE payload_cache SET created_at = to_char("
            "(now() - make_interval(secs => %s)) AT TIME ZONE 'UTC', "
            '\'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"\')',
            (seconds,),
        )

    def rows(self) -> List[Tuple[str, str, str]]:
        """(cache_handle, content, created_at) of every stored row."""
        return [
            (str(r[0]), str(r[1]), str(r[2]))
            for r in self._execute(
                "SELECT cache_handle, content, created_at FROM payload_cache"
            )
        ]

    def store_now_epoch(self) -> float:
        """The store's own clock, in epoch seconds."""
        sql = (
            "SELECT CAST(strftime('%s', 'now') AS REAL)"
            if self.kind == "sqlite"
            else "SELECT EXTRACT(EPOCH FROM now())::float8"
        )
        return float(self._execute(sql)[0][0])

    def child_args(self) -> List[str]:
        """Store arguments for _git_confirm_child.py."""
        if self.kind == "sqlite":
            return ["sqlite", self.location]
        return ["postgres", self.location]

    def child_tail(self) -> List[str]:
        return [] if self.kind == "sqlite" else [self.schema]

    def close(self) -> None:
        for pool in self._pools:
            try:
                pool.close()
            except Exception:  # noqa: BLE001 -- teardown must not mask results
                logger.warning("pool close failed", exc_info=True)


@pytest.fixture(name="shared_store", params=["sqlite", "postgres"])
def shared_store_fixture(request: Any, tmp_path: Path) -> Iterator[SharedStore]:
    if request.param == "sqlite":
        store = SharedStore("sqlite", str(tmp_path / "payload_cache.db"), "")
        try:
            yield store
        finally:
            store.close()
        return

    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    try:
        import psycopg
    except ImportError:
        pytest.skip("psycopg not available")
    schema = f"git_confirm_{uuid.uuid4().hex[:12]}"
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')
    except Exception as exc:
        pytest.skip(f"Cannot use PostgreSQL: {exc}")
    store = SharedStore("postgres", dsn, schema)
    try:
        yield store
    finally:
        store.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture(name="singleton_confirmation_store")
def singleton_confirmation_store_fixture(
    tmp_path: Path, monkeypatch: Any
) -> Iterator[PayloadCache]:
    """Wire a real SQLite-backed shared store into the module-level
    git_operations_service for one test."""
    from code_indexer.server.services.git_operations_service import (
        git_operations_service,
    )

    store = SharedStore("sqlite", str(tmp_path / "singleton_payload_cache.db"), "")
    try:
        cache = store.new_cache()
        monkeypatch.setattr(git_operations_service, "payload_cache", cache)
        yield cache
    finally:
        store.close()
