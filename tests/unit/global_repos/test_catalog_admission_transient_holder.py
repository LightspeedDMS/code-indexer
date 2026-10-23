"""Catalog admission refuses a write only on an OBSERVED non-cooperative
holder. A holder that reads as None -- another writer caught mid-acquire on
the file lock (its intra-process lock held, its lock file created but its
owner not yet written) -- is re-checked; one that stays None through every
attempt is a retryable "busy" refusal, never "another exclusive operation"
and never a silent write.

Real components: the real RefreshScheduler and its file lock. The other
writer's half-finished acquire is reproduced with the lock's own
intra-process lock and an empty lock file; it completes through the real
acquire when a holder read reaches the configured point.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, List
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.catalog_lock_holder import (
    CatalogLockedExclusively,
    admit_catalog_write,
)
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.memory_file_lock_manager import MemoryFileLockManager
from code_indexer.server.services.memory_rate_limiter import (
    MemoryRateLimiter,
    RateLimitConfig,
)
from code_indexer.server.services.memory_store_service import (
    ConflictError,
    MemoryStoreConfig,
    MemoryStoreService,
)
from tests.utils.catalog_lock_schedulers import CATALOG, catalog_lock_scheduler

PAYLOAD = {
    "type": "architectural-fact",
    "scope": "global",
    "summary": "example fact",
    "evidence": [{"commit": "abc123"}],
}


class _FrozenAcquire:
    """Another writer stopped between O_EXCL create and its owner write."""

    def __init__(self, scheduler: RefreshScheduler) -> None:
        self._files = scheduler.write_lock_manager._file_manager
        self._intra = self._files._get_intra_lock(CATALOG)
        self._lock_file = self._files._lock_file(CATALOG)
        self.completed = False

    def freeze(self) -> None:
        assert self._intra.acquire(blocking=False)
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file.touch()

    def complete(self, owner: str) -> None:
        """The other writer finishes its acquire for real."""
        self._lock_file.unlink()
        self._intra.release()
        assert self._files.acquire(CATALOG, owner_name=owner)
        self.completed = True

    def thaw(self) -> None:
        if not self.completed:
            self._lock_file.unlink(missing_ok=True)
            self._intra.release()


@pytest.fixture
def scheduler(tmp_path: Path) -> RefreshScheduler:
    return catalog_lock_scheduler("file", tmp_path)


@pytest.fixture
def frozen(scheduler: RefreshScheduler) -> Iterator[_FrozenAcquire]:
    other = _FrozenAcquire(scheduler)
    other.freeze()
    try:
        yield other
    finally:
        other.thaw()


def _complete_on_read(
    scheduler: RefreshScheduler,
    other: _FrozenAcquire,
    monkeypatch: pytest.MonkeyPatch,
    read_number: int,
) -> None:
    """Let the other writer finish as the lifecycle writer just before the
    writer's *read_number*-th holder read."""
    locks = scheduler.write_lock_manager
    real_info = locks.get_lock_info
    reads: List[int] = []

    def _read(alias: str) -> Any:
        reads.append(1)
        if alias == CATALOG and len(reads) == read_number:
            other.complete("lifecycle_writer")
        return real_info(alias)

    monkeypatch.setattr(locks, "get_lock_info", _read)


def _memory(tmp_path: Path, scheduler: RefreshScheduler) -> MemoryStoreService:
    return MemoryStoreService(
        config=MemoryStoreConfig(memories_dir=tmp_path / "memories"),
        lock_manager=MemoryFileLockManager(tmp_path / "memory-locks"),
        refresh_scheduler=scheduler,
        refresh_debouncer=MagicMock(),
        rate_limiter=MemoryRateLimiter(
            RateLimitConfig(capacity=100, refill_per_second=100.0)
        ),
        hostname="testhost",
    )


def test_a_holder_that_never_appears_is_a_retryable_busy_refusal(
    tmp_path: Path, scheduler: RefreshScheduler, frozen: _FrozenAcquire
) -> None:
    with pytest.raises(ConflictError) as refused:
        _memory(tmp_path, scheduler).create_memory(PAYLOAD, username="carol")

    assert "busy" in str(refused.value), str(refused.value)
    assert "exclusive" not in str(refused.value)
    memories = tmp_path / "memories"
    assert not memories.exists() or not any(memories.iterdir())


def test_a_memory_write_rechecks_a_holder_caught_mid_acquire(
    tmp_path: Path,
    scheduler: RefreshScheduler,
    frozen: _FrozenAcquire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _complete_on_read(scheduler, frozen, monkeypatch, read_number=2)

    created = _memory(tmp_path, scheduler).create_memory(PAYLOAD, username="carol")

    assert (tmp_path / "memories" / f"{created['id']}.md").exists()
    assert scheduler.write_lock_manager._file_manager.release(
        CATALOG, owner_name="lifecycle_writer"
    )


def test_admission_piggybacks_once_the_holder_appears_cooperative(
    scheduler: RefreshScheduler,
    frozen: _FrozenAcquire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _complete_on_read(scheduler, frozen, monkeypatch, read_number=2)

    try:
        assert admit_catalog_write(scheduler, "lifecycle_writer") is False
    except CatalogLockedExclusively as refused:
        pytest.fail(f"refused on a transient holder: {refused}")
    finally:
        scheduler.write_lock_manager._file_manager.release(
            CATALOG, owner_name="lifecycle_writer"
        )
