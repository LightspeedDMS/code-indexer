"""A lock's owner token never appears in exception text or a handle's repr.

The owner token is the capability that releases a lock; messages and logs
name the lock key and the owner instead. Real SQLite and PostgreSQL stores.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)
from tests.shared.alias_lock_pg import postgres_lock_store_dsn

_KEY = "example-repo"
_OWNER = "example_owner"
_TOKEN = "capability-0123456789abcdef"


@pytest.fixture(params=["sqlite", "postgres"])
def make_store(request, tmp_path: Path) -> Iterator[Callable[[], Any]]:
    if request.param == "sqlite":
        yield lambda: SqliteAliasLockStore(tmp_path / "locks")
        return
    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )

    with postgres_lock_store_dsn(_KEY) as dsn:
        yield lambda: PostgresAliasLockStore(dsn)


def _assert_no_token(exc: BaseException) -> None:
    text = str(exc)
    assert _TOKEN not in text
    assert _KEY in text and _OWNER in text


class TestMessagesOmitTheToken:
    def test_double_release(self, make_store) -> None:
        store = make_store()
        handle = store.try_acquire(_KEY, _OWNER, owner_token=_TOKEN)
        store.release(handle)
        with pytest.raises(AliasLockOwnershipLostError) as raised:
            store.release(handle)
        _assert_no_token(raised.value)

    def test_renew_after_release(self, make_store) -> None:
        store = make_store()
        handle = store.try_acquire(_KEY, _OWNER, owner_token=_TOKEN)
        store.release(handle)
        with pytest.raises(AliasLockOwnershipLostError) as raised:
            store.renew(handle)
        _assert_no_token(raised.value)

    def test_release_and_renew_after_takeover(self, make_store) -> None:
        store = make_store()
        handle = store.try_acquire(_KEY, _OWNER, owner_token=_TOKEN)
        assert store.release_owned(_KEY, _OWNER, owner_token=_TOKEN).value
        assert store.try_acquire(_KEY, "other_owner") is not None
        with pytest.raises(AliasLockOwnershipLostError) as renewed:
            store.renew(handle)
        _assert_no_token(renewed.value)
        with pytest.raises(AliasLockOwnershipLostError) as released:
            store.release(handle)
        _assert_no_token(released.value)

    def test_handle_repr(self, make_store) -> None:
        handle = make_store().try_acquire(_KEY, _OWNER, owner_token=_TOKEN)
        assert _TOKEN not in repr(handle)
