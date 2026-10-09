"""PayloadCache{Sqlite,Postgres}Backend expiring entries.

Invariants, each proven on SQLite and on PostgreSQL:
  - store_expiring() stamps an entry with the store's own clock and its own
    TTL; consume() measures age on that same clock;
  - consume() returns True exactly once for a live entry, also under
    concurrent consumers on independent backend instances;
  - a missing entry, or one past its own TTL, is never consumed;
  - storing a handle again restarts its lifetime;
  - store_expiring() propagates a store failure.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, List, Type

import pytest

from code_indexer.server.storage.protocols import PayloadCacheBackend
from tests.unit.server.services._git_confirm_helpers import (
    SharedStore,
    shared_store_fixture,  # noqa: F401 -- registers the `shared_store` fixture
)

_HANDLE = "expiring:example"
_TTL = 300
_INSIDE = 290
_PAST = 310
_CONSUMERS = 8
_JOIN_TIMEOUT_SECONDS = 60
_JUST_INSIDE = 299.0
_JUST_PAST = 300.3


def _store_error(store: SharedStore) -> Type[BaseException]:
    if store.kind == "sqlite":
        return sqlite3.Error
    import psycopg

    return psycopg.Error  # type: ignore[no-any-return]


def test_protocol_declares_expiring_entry_methods() -> None:
    assert "store_expiring" in dir(PayloadCacheBackend)
    assert "consume" in dir(PayloadCacheBackend)


def test_live_entry_is_consumed_exactly_once(shared_store: SharedStore) -> None:
    backend = shared_store.new_backend()
    backend.store_expiring(_HANDLE, "content", _TTL)

    assert backend.consume(_HANDLE) is True
    assert backend.consume(_HANDLE) is False
    assert shared_store.rows() == []


def test_missing_entry_is_not_consumed(shared_store: SharedStore) -> None:
    assert shared_store.new_backend().consume(_HANDLE) is False


def test_entry_past_its_own_ttl_is_not_consumed(shared_store: SharedStore) -> None:
    backend = shared_store.new_backend()
    backend.store_expiring(_HANDLE, "content", _TTL)
    shared_store.age_rows(_PAST)

    assert backend.consume(_HANDLE) is False


def test_entry_inside_its_ttl_is_consumed(shared_store: SharedStore) -> None:
    backend = shared_store.new_backend()
    backend.store_expiring(_HANDLE, "content", _TTL)
    shared_store.age_rows(_INSIDE)

    assert backend.consume(_HANDLE) is True


def test_storing_again_restarts_the_lifetime(shared_store: SharedStore) -> None:
    backend = shared_store.new_backend()
    backend.store_expiring(_HANDLE, "content", _TTL)
    shared_store.age_rows(_PAST)

    backend.store_expiring(_HANDLE, "content", _TTL)

    assert backend.consume(_HANDLE) is True


def test_concurrent_consumers_succeed_exactly_once(shared_store: SharedStore) -> None:
    shared_store.new_backend().store_expiring(_HANDLE, "content", _TTL)
    backends = [shared_store.new_backend() for _ in range(_CONSUMERS)]
    barrier = threading.Barrier(_CONSUMERS)
    outcomes: List[Any] = []
    lock = threading.Lock()

    def _consume(backend: Any) -> None:
        barrier.wait()
        try:
            outcome: Any = backend.consume(_HANDLE)
        except Exception as exc:  # recorded, asserted below
            outcome = repr(exc)
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=_consume, args=(b,)) for b in backends]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_JOIN_TIMEOUT_SECONDS)

    assert sorted(outcomes, key=str) == [False] * (_CONSUMERS - 1) + [True]


def test_entry_inside_its_ttl_survives_cleanup_and_is_consumed(
    shared_store: SharedStore,
) -> None:
    # Exact sub-second boundaries are pinned by the fixed-time table below.
    backend = shared_store.new_backend()
    backend.store_expiring(_HANDLE, "content", _TTL)
    shared_store.age_rows(_JUST_INSIDE)
    backend.cleanup_expired()

    assert backend.consume(_HANDLE) is True


def test_entry_just_past_its_ttl_is_not_consumed(shared_store: SharedStore) -> None:
    backend = shared_store.new_backend()
    backend.store_expiring(_HANDLE, "content", _TTL)
    shared_store.age_rows(_JUST_PAST)

    assert backend.consume(_HANDLE) is False


def _live_at(store: SharedStore, created_at: str, now: str) -> bool:
    """Evaluate the backend's own liveness predicate at a fixed `now`."""
    from code_indexer.server.storage.postgres import (
        payload_cache_backend as postgres_backend,
    )
    from code_indexer.server.storage.sqlite_backends import (
        payload_cache_backend as sqlite_backend,
    )

    backend = store.new_backend()
    backend.store_expiring(_HANDLE, "content", _TTL)
    if store.kind == "sqlite":
        store._execute("UPDATE payload_cache SET created_at = ?", (created_at,))
        predicate = sqlite_backend._live_predicate("?")
    else:
        store._execute("UPDATE payload_cache SET created_at = %s", (created_at,))
        predicate = postgres_backend._live_predicate("%s::timestamptz")
    rows = store._execute(
        f"SELECT COUNT(*) FROM payload_cache WHERE cache_handle = '{_HANDLE}' "
        f"AND {predicate}",
        (now,),
    )
    return int(rows[0][0]) == 1


@pytest.mark.parametrize(
    "created_at, now, live",
    [
        ("2026-10-07T00:00:00.000+00:00", "2026-10-07T00:04:59.999+00:00", True),
        ("2026-10-07T00:00:00.000+00:00", "2026-10-07T00:05:00.000+00:00", False),
        ("2026-10-07T00:00:00.000+00:00", "2026-10-07T00:05:00.001+00:00", False),
        ("2026-10-07T00:00:00.000+00:00", "2026-10-07T00:05:01.000+00:00", False),
        ("2026-10-07T00:00:00.900+00:00", "2026-10-07T00:05:00.899+00:00", True),
        ("2026-10-07T00:00:00.900+00:00", "2026-10-07T00:05:00.900+00:00", False),
    ],
)
def test_entry_is_live_exactly_while_younger_than_its_ttl(
    shared_store: SharedStore, created_at: str, now: str, live: bool
) -> None:
    assert _live_at(shared_store, created_at, now) is live


def test_store_expiring_propagates_a_store_failure(shared_store: SharedStore) -> None:
    backend = shared_store.new_backend()
    shared_store._execute("DROP TABLE payload_cache")

    with pytest.raises(_store_error(shared_store)):
        backend.store_expiring(_HANDLE, "content", _TTL)
