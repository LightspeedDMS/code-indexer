"""Row-owned, leased alias locks: release and expiry across processes.

The lock is owned by its committed row (lock_key + operation + owner_token +
holder identity + lease), never by the database session that inserted it:

- a release served by ANOTHER coordinator or OS process (a second worker
  behind a load balancer) frees the lock when it presents the acquirer's
  exact owner token -- the token write mode records at enter; without the
  token it frees nothing;
- a lock whose lease lapsed without renewal stops counting as held and can be
  taken over, so an abandoned write mode never blocks refresh until restart;
- a release by a different owner name is refused, exactly like the file lock.

Real SQLite and real PostgreSQL (``TEST_POSTGRES_DSN``) only; no mocks.
"""

from __future__ import annotations

import multiprocessing
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from code_indexer.global_repos.alias_lock_coordinator import AliasLockCoordinator
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)

_WRITE_MODE_OWNER = "mcp_write_mode"
_SHORT_TTL_SECONDS = 1
_EXPIRY_WAIT_SECONDS = 1.6
_RENEW_TTL_SECONDS = 2
_RENEW_ROUNDS = 3
_RENEW_INTERVAL_SECONDS = 1.0
_CHILD_TIMEOUT_SECONDS = 30

StoreFactory = Callable[[], Any]


def _postgres_store_factory() -> StoreFactory:
    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )

    dsn = os.environ["TEST_POSTGRES_DSN"]
    return lambda: PostgresAliasLockStore(dsn)


@pytest.fixture(params=["sqlite", "postgres"])
def store_factory(request, tmp_path: Path) -> Iterator[StoreFactory]:
    """Each call returns a NEW store instance over the same shared backing
    storage -- the in-process stand-in for a second worker process."""
    if request.param == "sqlite":
        lock_dir = tmp_path / "alias_locks"
        yield lambda: SqliteAliasLockStore(lock_dir)
        return
    if not os.environ.get("TEST_POSTGRES_DSN"):
        pytest.skip("TEST_POSTGRES_DSN not set")
    yield _postgres_store_factory()


@pytest.fixture
def alias() -> str:
    return f"example-repo-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def token() -> str:
    return f"write-mode-{uuid.uuid4().hex}"


def _coordinator(golden_repos_dir: Path, store: Any) -> AliasLockCoordinator:
    return AliasLockCoordinator(
        golden_repos_dir=golden_repos_dir,
        db_backed_enabled_getter=lambda: True,
        store_resolver=lambda: store,
    )


class TestReleaseServedByAnotherWorker:
    def test_release_with_acquirers_token_from_second_coordinator(
        self, tmp_path: Path, store_factory: StoreFactory, alias: str, token: str
    ) -> None:
        enter_worker = _coordinator(tmp_path, store_factory())
        exit_worker = _coordinator(tmp_path, store_factory())
        assert enter_worker.acquire(
            alias, owner_name=_WRITE_MODE_OWNER, owner_token=token
        )

        exit_worker.release(alias, owner_name=_WRITE_MODE_OWNER)
        assert exit_worker.is_locked(alias) is True

        assert (
            exit_worker.release(alias, owner_name=_WRITE_MODE_OWNER, owner_token=token)
            is True
        )
        assert exit_worker.is_locked(alias) is False
        assert enter_worker.is_locked(alias) is False

    def test_release_by_a_different_owner_is_refused(
        self, tmp_path: Path, store_factory: StoreFactory, alias: str, token: str
    ) -> None:
        enter_worker = _coordinator(tmp_path, store_factory())
        other_worker = _coordinator(tmp_path, store_factory())
        assert enter_worker.acquire(
            alias, owner_name=_WRITE_MODE_OWNER, owner_token=token
        )
        assert (
            other_worker.release(
                alias, owner_name="refresh_scheduler", owner_token=token
            )
            is False
        )
        assert other_worker.is_locked(alias) is True
        assert other_worker.release(
            alias, owner_name=_WRITE_MODE_OWNER, owner_token=token
        )

    def test_lock_info_from_second_coordinator_names_the_real_owner(
        self, tmp_path: Path, store_factory: StoreFactory, alias: str, token: str
    ) -> None:
        enter_worker = _coordinator(tmp_path, store_factory())
        other_worker = _coordinator(tmp_path, store_factory())
        assert enter_worker.acquire(
            alias, owner_name=_WRITE_MODE_OWNER, owner_token=token
        )
        info = other_worker.get_lock_info(alias)
        assert info is not None
        assert info["owner"] == _WRITE_MODE_OWNER
        assert info["pid"] == os.getpid()
        assert other_worker.release(
            alias, owner_name=_WRITE_MODE_OWNER, owner_token=token
        )
        assert other_worker.get_lock_info(alias) is None


class TestAbandonedLockExpires:
    def test_lapsed_lease_is_not_held_and_can_be_taken_over(
        self, tmp_path: Path, store_factory: StoreFactory, alias: str, token: str
    ) -> None:
        abandoned = _coordinator(tmp_path, store_factory())
        # ^ why write mode acquires without a heartbeat so an abandoned session lapses
        assert abandoned.acquire(
            alias,
            owner_name=_WRITE_MODE_OWNER,
            ttl_seconds=_SHORT_TTL_SECONDS,
            owner_token=token,
            heartbeat=False,
        )
        time.sleep(_EXPIRY_WAIT_SECONDS)

        refresher = _coordinator(tmp_path, store_factory())
        assert refresher.is_locked(alias) is False
        assert refresher.get_lock_info(alias) is None
        assert refresher.acquire(alias, owner_name="refresh_scheduler")
        # The abandoned holder's late exit must not free the new holder's lock.
        assert (
            abandoned.release(alias, owner_name=_WRITE_MODE_OWNER, owner_token=token)
            is False
        )
        assert refresher.is_locked(alias) is True
        assert refresher.release(alias, owner_name="refresh_scheduler")

    def test_renew_keeps_a_short_lease_alive(
        self, tmp_path: Path, store_factory: StoreFactory, alias: str
    ) -> None:
        holder = _coordinator(tmp_path, store_factory())
        observer = _coordinator(tmp_path, store_factory())
        assert holder.acquire(
            alias, owner_name="migration", ttl_seconds=_RENEW_TTL_SECONDS
        )
        for _ in range(_RENEW_ROUNDS):
            time.sleep(_RENEW_INTERVAL_SECONDS)
            assert holder.renew(
                alias, owner_name="migration", ttl_seconds=_RENEW_TTL_SECONDS
            )
        assert observer.is_locked(alias) is True
        assert holder.release(alias, owner_name="migration")


def _hold_in_child(
    golden_repos_dir: str, alias: str, token: str, ready: Any, done: Any, result: Any
) -> None:
    """Child process: acquire through its own coordinator and store, report,
    then stay alive until told to stop."""
    worker = _coordinator(Path(golden_repos_dir), _postgres_store_factory()())
    acquired = worker.acquire(alias, owner_name=_WRITE_MODE_OWNER, owner_token=token)
    result.put(("acquired", acquired))
    ready.set()
    if not done.wait(_CHILD_TIMEOUT_SECONDS):
        result.put(("timeout", None))
        return
    result.put(("locked_after_exit", worker.is_locked(alias)))


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not set"
)
def test_exit_in_another_os_process_releases_live_holders_lock(
    tmp_path: Path, alias: str, token: str
) -> None:
    ctx = multiprocessing.get_context("fork")
    ready, done, result = ctx.Event(), ctx.Event(), ctx.Queue()
    child = ctx.Process(
        target=_hold_in_child, args=(str(tmp_path), alias, token, ready, done, result)
    )
    child.start()
    try:
        assert ready.wait(_CHILD_TIMEOUT_SECONDS)
        assert result.get(timeout=_CHILD_TIMEOUT_SECONDS) == ("acquired", True)

        exit_worker = _coordinator(tmp_path, _postgres_store_factory()())
        assert exit_worker.is_locked(alias) is True
        assert exit_worker.release(
            alias, owner_name=_WRITE_MODE_OWNER, owner_token=token
        )
        assert exit_worker.is_locked(alias) is False

        done.set()
        assert result.get(timeout=_CHILD_TIMEOUT_SECONDS) == (
            "locked_after_exit",
            False,
        )
    finally:
        done.set()
        child.join(_CHILD_TIMEOUT_SECONDS)
        if child.is_alive():
            child.terminate()
            child.join(_CHILD_TIMEOUT_SECONDS)
    assert child.exitcode == 0
