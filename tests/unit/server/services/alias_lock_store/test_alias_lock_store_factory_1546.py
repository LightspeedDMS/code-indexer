"""Tests for AliasLockStoreFactory (Issue #1546 Phase 2).

Backend selection is a CORRECTNESS property, not a convenience setting
(per CLAUDE.md's Critical Architecture Invariants): cluster/PostgreSQL
mode MUST use the PostgreSQL store; solo mode MUST use a node-local
SQLite store. The golden-repos NFS mount is `vers=3,nolock,hard` --
`nolock` makes byte-range locks client-side-only, so a SQLite lock file
placed there would give each node its own private lock and silently
reproduce the exact split-brain bug this story exists to eliminate. The
factory dispatches via `is_postgres_storage_mode()`
(server/utils/registry_factory.py) -- THE single authority for this
probe -- never re-implements the check.
"""

from __future__ import annotations

import contextlib

import pytest

from code_indexer.server.services.alias_lock_store.factory import (
    AliasLockStoreFactory,
    default_sqlite_lock_dir,
)
from code_indexer.server.services.alias_lock_store.postgres_store import (
    PostgresAliasLockStore,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)

_MODULE = "code_indexer.server.services.alias_lock_store.factory"


class TestSoloModeResolvesSqlite:
    def test_resolves_sqlite_store_when_not_postgres_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{_MODULE}.is_postgres_storage_mode", lambda: False)
        factory = AliasLockStoreFactory(server_data_dir=str(tmp_path))

        store = factory.resolve()

        assert isinstance(store, SqliteAliasLockStore)

    def test_sqlite_lock_dir_is_node_local_not_golden_repos_dir(self, tmp_path):
        """The lock dir must be derived from server_data_dir/CIDX_DATA_DIR
        (node-local), NEVER from golden_repos_dir (the NFS-shared,
        nolock-mounted directory) -- see module docstring."""
        lock_dir = default_sqlite_lock_dir(server_data_dir=str(tmp_path))
        assert str(lock_dir).startswith(str(tmp_path))
        assert "golden-repos" not in str(lock_dir)
        assert "alias_locks" in str(lock_dir)

    def test_resolution_is_cached_same_instance_returned(self, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{_MODULE}.is_postgres_storage_mode", lambda: False)
        factory = AliasLockStoreFactory(server_data_dir=str(tmp_path))

        first = factory.resolve()
        second = factory.resolve()

        assert first is second


class TestClusterModeResolvesPostgres:
    def test_resolves_postgres_store_when_postgres_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{_MODULE}.is_postgres_storage_mode", lambda: True)
        factory = AliasLockStoreFactory(
            postgres_dsn="postgresql://user:pass@host/db",
            server_data_dir=str(tmp_path),
        )

        store = factory.resolve()

        assert isinstance(store, PostgresAliasLockStore)

    def test_resolution_is_cached_same_instance_returned(self, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{_MODULE}.is_postgres_storage_mode", lambda: True)
        factory = AliasLockStoreFactory(
            postgres_dsn="postgresql://user:pass@host/db",
            server_data_dir=str(tmp_path),
        )

        first = factory.resolve()
        second = factory.resolve()

        assert first is second

    def test_postgres_mode_without_dsn_fails_loud(self, monkeypatch, tmp_path):
        """AC: fail LOUD if cluster ever resolves to SQLite -- a missing
        DSN in postgres mode is a wiring bug, not a reason to silently
        degrade to a node-local store that other cluster nodes cannot
        see."""
        monkeypatch.setattr(f"{_MODULE}.is_postgres_storage_mode", lambda: True)
        factory = AliasLockStoreFactory(
            postgres_dsn=None, server_data_dir=str(tmp_path)
        )

        with pytest.raises(RuntimeError, match="postgres_dsn"):
            factory.resolve()

    def test_postgres_mode_never_falls_back_to_sqlite_on_missing_dsn(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(f"{_MODULE}.is_postgres_storage_mode", lambda: True)
        factory = AliasLockStoreFactory(
            postgres_dsn=None, server_data_dir=str(tmp_path)
        )

        with pytest.raises(RuntimeError):
            factory.resolve()

        # Even after the failure, a later successful call (DSN fixed)
        # must still resolve PostgreSQL, never a cached SQLite fallback.
        factory._postgres_dsn = "postgresql://user:pass@host/db"
        store = factory.resolve()
        assert isinstance(store, PostgresAliasLockStore)


# ---------------------------------------------------------------------------
# Codex Fix 1 (most serious): a cluster node must never be able to silently
# resolve SQLite while storage mode is still genuinely undetermined -- see
# registry_factory.is_storage_mode_undetermined()'s "pending sentinel"
# contract. Exercises the REAL app.state (not monkeypatched
# is_postgres_storage_mode) so this proves the actual end-to-end contract.
# ---------------------------------------------------------------------------

_FAKE_POSTGRES_DSN = (
    "postgresql://not-a-real-user:not-a-real-password@example.invalid/db"
)


@contextlib.contextmanager
def _app_state_storage_mode(value):
    from code_indexer.server import app as app_module

    _unset = object()
    saved = getattr(app_module.app.state, "storage_mode", _unset)
    try:
        app_module.app.state.storage_mode = value
        yield
    finally:
        if saved is _unset:
            if hasattr(app_module.app.state, "storage_mode"):
                delattr(app_module.app.state, "storage_mode")
        else:
            app_module.app.state.storage_mode = saved


class TestUndeterminedStorageModeFailsLoud:
    def test_resolve_fails_loud_during_pending_window_never_caches_sqlite(
        self, tmp_path
    ):
        from code_indexer.server.utils.registry_factory import (
            STORAGE_MODE_PENDING_SENTINEL,
        )

        factory = AliasLockStoreFactory(
            postgres_dsn=_FAKE_POSTGRES_DSN,
            server_data_dir=str(tmp_path),
        )

        with _app_state_storage_mode(STORAGE_MODE_PENDING_SENTINEL):
            with pytest.raises(RuntimeError, match="not yet determined"):
                factory.resolve()

        # Nothing must have been cached from the failed pending-window
        # attempt -- the factory's own internal SQLite slot stays empty.
        assert factory._sqlite_store is None

    def test_resolve_succeeds_postgres_once_mode_becomes_known(self, tmp_path):
        from code_indexer.server.utils.registry_factory import (
            STORAGE_MODE_PENDING_SENTINEL,
        )

        factory = AliasLockStoreFactory(
            postgres_dsn=_FAKE_POSTGRES_DSN,
            server_data_dir=str(tmp_path),
        )

        with _app_state_storage_mode(STORAGE_MODE_PENDING_SENTINEL):
            with pytest.raises(RuntimeError):
                factory.resolve()

        with _app_state_storage_mode("postgres"):
            store = factory.resolve()

        assert isinstance(store, PostgresAliasLockStore)


# ---------------------------------------------------------------------------
# Codex Fix 1 (containment): default_sqlite_lock_dir() must never resolve
# under golden_repos_dir -- that NFS mount is vers=3,nolock,hard, so a
# SQLite lock file there is a private, per-node lock masquerading as a
# shared one.
# ---------------------------------------------------------------------------


class TestSqliteLockDirContainmentValidation:
    def test_refuses_lock_dir_equal_to_golden_repos_dir(self, tmp_path):
        golden_repos_dir = tmp_path / "golden-repos"
        golden_repos_dir.mkdir()

        with pytest.raises(RuntimeError, match="golden-repos"):
            default_sqlite_lock_dir(
                server_data_dir=str(golden_repos_dir),
                golden_repos_dir=str(golden_repos_dir),
            )

    def test_refuses_lock_dir_nested_under_golden_repos_dir(self, tmp_path):
        golden_repos_dir = tmp_path / "mnt" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        with pytest.raises(RuntimeError, match="golden-repos"):
            default_sqlite_lock_dir(
                server_data_dir=str(golden_repos_dir / "data"),
                golden_repos_dir=str(golden_repos_dir),
            )

    def test_allows_lock_dir_outside_golden_repos_dir(self, tmp_path):
        golden_repos_dir = tmp_path / "golden-repos"
        node_local_dir = tmp_path / "node-local"

        lock_dir = default_sqlite_lock_dir(
            server_data_dir=str(node_local_dir),
            golden_repos_dir=str(golden_repos_dir),
        )

        assert str(lock_dir).startswith(str(node_local_dir))


class TestFactoryContainmentValidation:
    def test_resolve_sqlite_fails_loud_when_lock_dir_under_golden_repos(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(f"{_MODULE}.is_postgres_storage_mode", lambda: False)
        golden_repos_dir = tmp_path / "golden-repos"
        golden_repos_dir.mkdir()

        factory = AliasLockStoreFactory(
            server_data_dir=str(golden_repos_dir),
            golden_repos_dir=str(golden_repos_dir),
        )

        with pytest.raises(RuntimeError, match="golden-repos"):
            factory.resolve()
