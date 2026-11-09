"""Every read-modify-write of an alias document takes the per-alias lock.

``update_refresh_timestamp`` and ``record_republished_index`` read the
document, change it and put it back. Without the lock ``swap_alias`` could
land between the read and the put, and the stale document put back would
revert the pointer to the old target.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.utils.index_store import INDEX_VERSION_FIELD

ALIAS = "example-repo-global"


@pytest.fixture
def manager(tmp_path: Path) -> AliasManager:
    m = AliasManager(str(tmp_path / "aliases"))
    m.create_alias(ALIAS, "/snap/v1", "example-repo", extra={INDEX_VERSION_FIELD: "1"})
    return m


def _blocks_while_locked(manager: AliasManager, call: Callable[[], None]) -> None:
    """*call* waits for the alias lock, then completes once it is released."""
    done = threading.Event()

    def run() -> None:
        call()
        done.set()

    lock = manager._get_alias_write_lock(ALIAS)
    with lock:
        worker = threading.Thread(target=run)
        worker.start()
        assert not done.wait(0.3), "wrote the alias without taking its lock"
    worker.join(5)
    assert done.is_set()


def test_update_refresh_timestamp_takes_the_alias_lock(manager: AliasManager) -> None:
    _blocks_while_locked(manager, lambda: manager.update_refresh_timestamp(ALIAS))
    record = manager.read_record(ALIAS)
    assert record is not None and record.get("last_refresh")


def test_record_republished_index_takes_the_alias_lock(
    manager: AliasManager,
) -> None:
    _blocks_while_locked(
        manager,
        lambda: manager.record_republished_index(
            ALIAS, "/snap/v1", "1", {INDEX_VERSION_FIELD: "2"}
        ),
    )
    record = manager.read_record(ALIAS)
    assert record is not None and record[INDEX_VERSION_FIELD] == "2"


def test_timestamp_update_after_a_swap_keeps_the_new_target(
    manager: AliasManager,
) -> None:
    manager.swap_alias(ALIAS, "/snap/v2", "/snap/v1")
    manager.update_refresh_timestamp(ALIAS)
    assert manager.read_alias(ALIAS) == "/snap/v2"
