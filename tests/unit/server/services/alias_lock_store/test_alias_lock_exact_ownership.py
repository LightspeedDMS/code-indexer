"""Only the acquirer of an alias lock may release or renew it.

Several services use the SAME owner name on every node ("refresh_scheduler",
"dependency_map_service", ...). Release and renew therefore bind to the exact
owner token of the acquisition -- never to the owner name alone -- so a node
that never took the lock, or whose lease lapsed and was taken over, cannot
free or extend another node's lock.

Two independent coordinators over the same storage stand in for two nodes.
Real SQLite and real PostgreSQL (``TEST_POSTGRES_DSN``); no mocks.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from code_indexer.global_repos.alias_lock_coordinator import AliasLockCoordinator
from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)

_SHARED_OWNER = "refresh_scheduler"
_SHORT_TTL_SECONDS = 1
_EXPIRY_WAIT_SECONDS = 1.6

MakeNode = Callable[[], AliasLockCoordinator]


@pytest.fixture(params=["sqlite", "postgres"])
def make_node(request, tmp_path: Path) -> Iterator[MakeNode]:
    if request.param == "sqlite":
        lock_dir = tmp_path / "alias_locks"

        def new_store() -> Any:
            return SqliteAliasLockStore(lock_dir)
    else:
        dsn = os.environ.get("TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("TEST_POSTGRES_DSN not set")
        from code_indexer.server.services.alias_lock_store.postgres_store import (
            PostgresAliasLockStore,
        )

        def new_store() -> Any:
            return PostgresAliasLockStore(dsn)

    def make() -> AliasLockCoordinator:
        store = new_store()
        return AliasLockCoordinator(
            golden_repos_dir=tmp_path,
            db_backed_enabled_getter=lambda: True,
            store_resolver=lambda: store,
        )

    yield make


@pytest.fixture
def alias() -> str:
    return f"example-repo-{uuid.uuid4().hex[:12]}"


class TestOnlyTheAcquirerReleasesOrRenews:
    def test_non_acquiring_node_cannot_release_a_lock_under_the_same_owner(
        self, make_node: MakeNode, alias: str
    ) -> None:
        node_a, node_b = make_node(), make_node()
        assert node_a.acquire(alias, owner_name=_SHARED_OWNER)

        node_b.release(alias, owner_name=_SHARED_OWNER)

        assert node_b.is_locked(alias) is True
        assert node_b.acquire(alias, owner_name=_SHARED_OWNER) is False
        assert node_a.release(alias, owner_name=_SHARED_OWNER) is True
        assert node_b.is_locked(alias) is False

    def test_non_acquiring_node_cannot_renew_a_lock_under_the_same_owner(
        self, make_node: MakeNode, alias: str
    ) -> None:
        node_a, node_b = make_node(), make_node()
        assert node_a.acquire(alias, owner_name=_SHARED_OWNER)
        try:
            try:
                renewed = node_b.renew(alias, owner_name=_SHARED_OWNER)
            except AliasLockOwnershipLostError:
                renewed = False
            assert renewed is False
        finally:
            assert node_a.release(alias, owner_name=_SHARED_OWNER)

    def test_lapsed_holder_detects_takeover_by_same_owner_name(
        self, make_node: MakeNode, alias: str
    ) -> None:
        node_a, node_b = make_node(), make_node()
        assert node_a.acquire(
            alias, owner_name=_SHARED_OWNER, ttl_seconds=_SHORT_TTL_SECONDS
        )
        time.sleep(_EXPIRY_WAIT_SECONDS)
        assert node_b.acquire(alias, owner_name=_SHARED_OWNER)

        with pytest.raises(AliasLockOwnershipLostError):
            node_a.renew(alias, owner_name=_SHARED_OWNER)
        node_a.release(alias, owner_name=_SHARED_OWNER)

        assert node_b.is_locked(alias) is True
        assert node_b.release(alias, owner_name=_SHARED_OWNER) is True


class _ReleaseFailsOnce(SqliteAliasLockStore):
    """Real SQLite store whose first owner-addressed release hits a real
    sqlite3 error (a transient store failure)."""

    failed = False

    def release_owned(self, lock_key, operation, *, owner_token):
        if not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("disk I/O error")
        return super().release_owned(lock_key, operation, owner_token=owner_token)


class TestRememberedToken:
    def _node(self, tmp_path: Path, store: Any) -> AliasLockCoordinator:
        return AliasLockCoordinator(
            golden_repos_dir=tmp_path,
            db_backed_enabled_getter=lambda: True,
            store_resolver=lambda: store,
        )

    def test_token_survives_a_failed_release_for_the_retry(
        self, tmp_path: Path, alias: str
    ) -> None:
        store = _ReleaseFailsOnce(tmp_path / "alias_locks")
        node = self._node(tmp_path, store)
        assert node.acquire(alias, owner_name=_SHARED_OWNER)
        with pytest.raises(sqlite3.OperationalError):
            node.release(alias, owner_name=_SHARED_OWNER)
        assert node.release(alias, owner_name=_SHARED_OWNER) is True
        assert node.is_locked(alias) is False

    def test_empty_token_is_rejected_and_never_falls_back(
        self, tmp_path: Path, alias: str
    ) -> None:
        node = self._node(tmp_path, SqliteAliasLockStore(tmp_path / "alias_locks"))
        assert node.acquire(alias, owner_name=_SHARED_OWNER)
        with pytest.raises(ValueError):
            node.release(alias, owner_name=_SHARED_OWNER, owner_token="")
        with pytest.raises(ValueError):
            node.renew(alias, owner_name=_SHARED_OWNER, owner_token="")
        assert node.is_locked(alias) is True
        assert node.release(alias, owner_name=_SHARED_OWNER) is True
