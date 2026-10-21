"""A memory write requests its catalog refresh only after it released the
catalog write lock, and a refresh already queued or running is an expected
outcome (the debouncer owes the refresh), never an unexpected exception.

A refresh that starts while a writer holds the catalog write lock skips the
cycle ("write lock held by external writer") and owes nothing: the lock
holder is the one that triggers the refresh once it is done. A writer that
triggered while still holding the lock made its own refresh skip, leaving
the write unindexed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.services.memory_file_lock_manager import MemoryFileLockManager
from code_indexer.server.services.memory_rate_limiter import (
    MemoryRateLimiter,
    RateLimitConfig,
)
from code_indexer.server.services.memory_store_service import (
    MemoryStoreConfig,
    MemoryStoreService,
)

COARSE_ALIAS = "cidx-meta"
REFRESH_ALIAS = "cidx-meta-global"


class _LockingScheduler:
    """Scheduler double with real write-lock state; records whether the
    lock was held at each refresh request."""

    def __init__(self, duplicate: bool = False) -> None:
        self.holder: Optional[str] = None
        self.lock_held_at_trigger: List[bool] = []
        self._duplicate = duplicate

    def is_write_locked(self, alias: str) -> bool:
        assert alias == COARSE_ALIAS
        return self.holder is not None

    def acquire_write_lock(
        self, alias: str, owner_name: str, ttl_seconds: Optional[int] = None
    ) -> bool:
        if self.holder is not None:
            return False
        self.holder = owner_name
        return True

    def release_write_lock(
        self, alias: str, owner_name: str, owner_token: Optional[str] = None
    ) -> bool:
        assert self.holder == owner_name
        self.holder = None
        return True

    def trigger_refresh_for_repo(
        self,
        alias_name: str,
        submitter_username: str = "system",
        force_reset: bool = False,
    ) -> Optional[str]:
        assert alias_name == REFRESH_ALIAS
        self.lock_held_at_trigger.append(self.holder is not None)
        if self._duplicate:
            raise DuplicateJobError("global_repo_refresh", alias_name, "job-running")
        return "job-1"


class _CountingDebouncer:
    def __init__(self) -> None:
        self.signals = 0

    def signal_dirty(self) -> None:
        self.signals += 1


def _payload(summary: str) -> Dict[str, Any]:
    return {
        "type": "architectural-fact",
        "scope": "global",
        "summary": summary,
        "evidence": [{"commit": "abc123"}],
    }


def _service(
    tmp_path: Path, scheduler: _LockingScheduler, debouncer: _CountingDebouncer
) -> MemoryStoreService:
    return MemoryStoreService(
        config=MemoryStoreConfig(
            memories_dir=tmp_path / "memories",
            max_summary_chars=1000,
            per_memory_lock_ttl_seconds=30,
            coarse_lock_ttl_seconds=60,
        ),
        lock_manager=MemoryFileLockManager(tmp_path / "locks"),
        refresh_scheduler=scheduler,
        refresh_debouncer=debouncer,
        rate_limiter=MemoryRateLimiter(
            RateLimitConfig(capacity=100, refill_per_second=100.0)
        ),
        hostname="testhost",
    )


def test_create_edit_delete_request_refresh_after_releasing_the_lock(
    tmp_path: Path,
) -> None:
    scheduler = _LockingScheduler()
    debouncer = _CountingDebouncer()
    svc = _service(tmp_path, scheduler, debouncer)

    created = svc.create_memory(_payload("first"), username="alice")
    edited = svc.edit_memory(
        created["id"], _payload("second"), created["content_hash"], username="alice"
    )
    svc.delete_memory(created["id"], edited["content_hash"], username="alice")

    assert scheduler.lock_held_at_trigger == [False, False, False]
    assert scheduler.holder is None
    assert debouncer.signals == 0


def test_refresh_already_running_is_owed_to_debouncer_without_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    scheduler = _LockingScheduler(duplicate=True)
    debouncer = _CountingDebouncer()
    svc = _service(tmp_path, scheduler, debouncer)

    with caplog.at_level(logging.DEBUG):
        svc.create_memory(_payload("first"), username="alice")

    assert debouncer.signals == 1
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [], [r.getMessage() for r in warnings]
