"""
Tests for MemoryStoreService invalidation wiring — Story #877 Phase 3-A Item 3.

Verifies that the optional metadata_cache_invalidator callable is:
  - Called with the correct memory_id on successful create.
  - Called with the correct memory_id on successful edit.
  - Called with the correct memory_id on successful delete.
  - NOT called on rate-limit-exceeded (RateLimitError).
  - NOT called on schema validation error (MemorySchemaValidationError).
  - NOT called on stale-content hash mismatch (StaleContentError).
  - NOT called on lock conflict (ConflictError).

Real filesystem (tmp_path), real MemoryFileLockManager, real MemoryRateLimiter,
real memory_io, real memory_schema. MagicMock for RefreshScheduler,
RefreshDebouncer, and the invalidator callable only (external boundaries).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.memory_file_lock_manager import MemoryFileLockManager
from code_indexer.server.services.memory_rate_limiter import (
    MemoryRateLimiter,
    RateLimitConfig,
)
from code_indexer.server.services.memory_schema import MemorySchemaValidationError
from code_indexer.server.services.memory_store_service import (
    ConflictError,
    MemoryStoreConfig,
    MemoryStoreService,
    RateLimitError,
    StaleContentError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXED_UUID = "aaaabbbbccccddddeeeeffff00001111"


# ---------------------------------------------------------------------------
# Payload factory
# ---------------------------------------------------------------------------


def _valid_create_payload() -> Dict[str, Any]:
    return {
        "type": "architectural-fact",
        "scope": "global",
        "summary": "short",
        "evidence": [{"commit": "abc123"}],
    }


# ---------------------------------------------------------------------------
# Infrastructure fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def memories_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def lock_manager(tmp_path: Path) -> MemoryFileLockManager:
    return MemoryFileLockManager(tmp_path / "locks")


@pytest.fixture
def rate_limiter() -> MemoryRateLimiter:
    return MemoryRateLimiter(RateLimitConfig(capacity=100, refill_per_second=100.0))


# ---------------------------------------------------------------------------
# Builder helper
# ---------------------------------------------------------------------------


def _build_service(
    memories_dir: Path,
    lock_manager: MemoryFileLockManager,
    rate_limiter: MemoryRateLimiter,
    invalidator: Any,
    *,
    id_factory=None,
) -> MemoryStoreService:
    config = MemoryStoreConfig(
        memories_dir=memories_dir,
        max_summary_chars=1000,
        per_memory_lock_ttl_seconds=30,
        coarse_lock_ttl_seconds=60,
    )
    scheduler = MagicMock()
    scheduler.is_write_lock_held.return_value = False
    scheduler.acquire_write_lock.return_value = True
    scheduler.release_write_lock.return_value = True
    debouncer = MagicMock()
    return MemoryStoreService(
        config=config,
        lock_manager=lock_manager,
        refresh_scheduler=scheduler,
        refresh_debouncer=debouncer,
        rate_limiter=rate_limiter,
        hostname="testhost",
        id_factory=id_factory or (lambda: FIXED_UUID),
        metadata_cache_invalidator=invalidator,
    )


def _create_one(
    memories_dir: Path,
    lock_manager: MemoryFileLockManager,
    rate_limiter: MemoryRateLimiter,
    invalidator: Any,
    counter: list,
) -> Tuple[MemoryStoreService, str, str]:
    """Create one memory and return (svc, memory_id, content_hash)."""
    counter[0] += 1
    uid = f"id{counter[0]:08x}" + "0" * (32 - 10)  # pad to 32 chars
    svc = _build_service(
        memories_dir, lock_manager, rate_limiter, invalidator, id_factory=lambda: uid
    )
    result = svc.create_memory(_valid_create_payload(), username="alice")
    return svc, result["id"], result["content_hash"]


# ---------------------------------------------------------------------------
# Test: invalidator called on successful create
# ---------------------------------------------------------------------------


def test_invalidator_called_on_successful_create(
    memories_dir: Path,
    lock_manager: MemoryFileLockManager,
    rate_limiter: MemoryRateLimiter,
) -> None:
    invalidator = MagicMock()
    svc = _build_service(memories_dir, lock_manager, rate_limiter, invalidator)

    svc.create_memory(_valid_create_payload(), username="alice")

    invalidator.assert_called_once_with(FIXED_UUID)


# ---------------------------------------------------------------------------
# Test: invalidator called on successful edit
# ---------------------------------------------------------------------------


def test_invalidator_called_on_successful_edit(
    memories_dir: Path,
    lock_manager: MemoryFileLockManager,
    rate_limiter: MemoryRateLimiter,
) -> None:
    counter = [0]
    invalidator = MagicMock()
    svc, memory_id, content_hash = _create_one(
        memories_dir, lock_manager, rate_limiter, invalidator, counter
    )
    invalidator.reset_mock()

    edit_svc = _build_service(
        memories_dir,
        lock_manager,
        rate_limiter,
        invalidator,
        id_factory=lambda: memory_id,
    )
    edit_svc.edit_memory(
        memory_id, _valid_create_payload(), content_hash, username="alice"
    )

    invalidator.assert_called_once_with(memory_id)


# ---------------------------------------------------------------------------
# Test: invalidator called on successful delete
# ---------------------------------------------------------------------------


def test_invalidator_called_on_successful_delete(
    memories_dir: Path,
    lock_manager: MemoryFileLockManager,
    rate_limiter: MemoryRateLimiter,
) -> None:
    counter = [0]
    invalidator = MagicMock()
    svc, memory_id, content_hash = _create_one(
        memories_dir, lock_manager, rate_limiter, invalidator, counter
    )
    invalidator.reset_mock()

    del_svc = _build_service(
        memories_dir,
        lock_manager,
        rate_limiter,
        invalidator,
        id_factory=lambda: memory_id,
    )
    del_svc.delete_memory(memory_id, content_hash, username="alice")

    invalidator.assert_called_once_with(memory_id)


# ---------------------------------------------------------------------------
# Test: invalidator NOT called on rate-limit exceeded during create
# ---------------------------------------------------------------------------


def test_invalidator_not_called_on_rate_limit_exceeded(
    memories_dir: Path, lock_manager: MemoryFileLockManager
) -> None:
    frozen = [0.0]
    exhausted_rl = MemoryRateLimiter(
        RateLimitConfig(capacity=1, refill_per_second=0.001),
        clock=lambda: frozen[0],
    )
    counter = [0]
    invalidator = MagicMock()

    # First create consumes the only token
    _create_one(memories_dir, lock_manager, exhausted_rl, invalidator, counter)
    invalidator.reset_mock()

    svc = _build_service(memories_dir, lock_manager, exhausted_rl, invalidator)
    with pytest.raises(RateLimitError):
        svc.create_memory(_valid_create_payload(), username="alice")

    invalidator.assert_not_called()


# ---------------------------------------------------------------------------
# Test: invalidator NOT called on schema validation error during create
# ---------------------------------------------------------------------------


def test_invalidator_not_called_on_schema_validation_error(
    memories_dir: Path,
    lock_manager: MemoryFileLockManager,
    rate_limiter: MemoryRateLimiter,
) -> None:
    invalidator = MagicMock()
    svc = _build_service(memories_dir, lock_manager, rate_limiter, invalidator)

    bad_payload = _valid_create_payload()
    bad_payload["evidence"] = []  # violates: at least one entry required

    with pytest.raises(MemorySchemaValidationError):
        svc.create_memory(bad_payload, username="alice")

    invalidator.assert_not_called()


# ---------------------------------------------------------------------------
# Test: invalidator NOT called on StaleContentError during edit
# ---------------------------------------------------------------------------


def test_invalidator_not_called_on_stale_content_error(
    memories_dir: Path,
    lock_manager: MemoryFileLockManager,
    rate_limiter: MemoryRateLimiter,
) -> None:
    counter = [0]
    invalidator = MagicMock()
    svc, memory_id, _real_hash = _create_one(
        memories_dir, lock_manager, rate_limiter, invalidator, counter
    )
    invalidator.reset_mock()

    edit_svc = _build_service(
        memories_dir,
        lock_manager,
        rate_limiter,
        invalidator,
        id_factory=lambda: memory_id,
    )
    with pytest.raises(StaleContentError):
        edit_svc.edit_memory(
            memory_id, _valid_create_payload(), "wrong_hash", username="alice"
        )

    invalidator.assert_not_called()


# ---------------------------------------------------------------------------
# Test: invalidator NOT called on ConflictError (per-memory lock held)
# ---------------------------------------------------------------------------


def test_invalidator_not_called_on_conflict_error(
    memories_dir: Path,
    lock_manager: MemoryFileLockManager,
    rate_limiter: MemoryRateLimiter,
) -> None:
    invalidator = MagicMock()
    svc = _build_service(memories_dir, lock_manager, rate_limiter, invalidator)

    external_owner = "external@testhost/other"
    lock_manager.acquire(FIXED_UUID, external_owner, ttl_seconds=60)
    try:
        with pytest.raises(ConflictError):
            svc.create_memory(_valid_create_payload(), username="alice")
    finally:
        lock_manager.release(FIXED_UUID, external_owner)

    invalidator.assert_not_called()
