"""Enter catalog write mode for real in a test.

A write-mode write needs the marker's token to own the write-mode lock, so a
bare marker file authorizes nothing. This takes a real SQLite-backed lock
under a fresh token, writes the real marker carrying it, and installs the
real renewer on the FileCRUDService under test (restored afterwards).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

from code_indexer.global_repos.alias_lock_coordinator import AliasLockCoordinator
from code_indexer.server.mcp.handlers._write_mode_marker import (
    WRITE_MODE_OWNER,
    renew_write_mode_lease,
    write_marker,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)


@contextmanager
def real_write_mode(
    service: Any,
    golden_repos_dir: Path,
    alias: str,
    source_path: Path,
    lock_dir: Path,
) -> Iterator[AliasLockCoordinator]:
    store = SqliteAliasLockStore(lock_dir)
    locks = AliasLockCoordinator(
        golden_repos_dir=golden_repos_dir,
        db_backed_enabled_getter=lambda: True,
        store_resolver=lambda: store,
    )
    token = uuid.uuid4().hex
    if not locks.acquire(alias, owner_name=WRITE_MODE_OWNER, owner_token=token):
        raise RuntimeError(f"write-mode lock for {alias!r} is already held")
    write_marker(golden_repos_dir, alias, str(source_path), token)
    worker = SimpleNamespace(write_lock_manager=locks)
    saved = service._write_mode_lease_renewer
    service.set_write_mode_lease_renewer(
        lambda golden, name: renew_write_mode_lease(worker, golden, name)
    )
    try:
        yield locks
    finally:
        service._write_mode_lease_renewer = saved
        locks.release(alias, owner_name=WRITE_MODE_OWNER, owner_token=token)
