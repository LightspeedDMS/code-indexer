"""
Unit tests for memory_store_service_factory.build_memory_store_service.

Story #877 — Shared Technical Memory Store (singleton wiring).

These tests verify:
- memories_dir is {golden_repos_dir}/cidx-meta/memories (base clone, NOT .versioned)
- lock_manager locks_root is {server_data_dir}/locks (OUTSIDE the base clone)
- rate limiter capacity and refill_per_second are passed through to RateLimitConfig
- max_summary_chars is passed through to MemoryStoreConfig
- the factory creates memories_dir if it does not exist
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.memory_store_service_factory import (
    MemoryStoreBundle,
    build_memory_store_service,
)
from code_indexer.server.services.memory_store_service import MemoryStoreService
from code_indexer.server.services.memory_file_lock_manager import MemoryFileLockManager
from code_indexer.server.services.memory_rate_limiter import MemoryRateLimiter

# ---------------------------------------------------------------------------
# Named constants for non-default test values
# ---------------------------------------------------------------------------

TEST_RATE_LIMIT_CAPACITY = 99
TEST_RATE_LIMIT_REFILL_PER_SECOND = 2.5
TEST_MAX_SUMMARY_CHARS = 500


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_scheduler() -> MagicMock:
    """Create a minimal mock satisfying RefreshSchedulerProtocol."""
    scheduler = MagicMock()
    scheduler.acquire_write_lock = MagicMock(return_value=True)
    scheduler.release_write_lock = MagicMock(return_value=True)
    scheduler.is_write_lock_held = MagicMock(return_value=False)
    scheduler.trigger_refresh_for_repo = MagicMock()
    return scheduler


def _make_debouncer() -> MagicMock:
    """Create a minimal mock satisfying RefreshDebouncerProtocol."""
    debouncer = MagicMock()
    debouncer.signal_dirty = MagicMock()
    return debouncer


@pytest.fixture()
def dirs(tmp_path: Path):
    """Return (golden_repos_dir, server_data_dir) both pre-created under tmp_path."""
    golden_repos_dir = tmp_path / "golden-repos"
    server_data_dir = tmp_path / "server"
    golden_repos_dir.mkdir(parents=True)
    server_data_dir.mkdir(parents=True)
    return golden_repos_dir, server_data_dir


def _build(golden_repos_dir: Path, server_data_dir: Path, **kwargs: Any) -> MemoryStoreService:
    """Thin wrapper around build_memory_store_service with default mocks.

    Unpacks the returned MemoryStoreBundle and returns only the service,
    so existing tests that inspect service internals continue to work unchanged.
    """
    bundle: MemoryStoreBundle = build_memory_store_service(
        golden_repos_dir=golden_repos_dir,
        server_data_dir=server_data_dir,
        refresh_scheduler=_make_scheduler(),
        refresh_debouncer=_make_debouncer(),
        **kwargs,
    )
    return bundle.service


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_builds_service_with_correct_memories_dir(dirs) -> None:
    """memories_dir must be {golden_repos_dir}/cidx-meta/memories (base clone path)."""
    golden_repos_dir, server_data_dir = dirs
    service = _build(golden_repos_dir, server_data_dir)

    expected = golden_repos_dir / "cidx-meta" / "memories"
    assert isinstance(service, MemoryStoreService)
    assert service._config.memories_dir == expected


def test_builds_service_with_lock_manager_rooted_outside_base_clone(dirs) -> None:
    """MemoryFileLockManager locks_root must be {server_data_dir}/locks — NOT inside golden_repos_dir."""
    golden_repos_dir, server_data_dir = dirs
    service = _build(golden_repos_dir, server_data_dir)

    expected_locks_dir = server_data_dir / "locks" / "cidx-meta" / "memories"
    assert isinstance(service._lock_manager, MemoryFileLockManager)
    assert service._lock_manager._locks_dir == expected_locks_dir
    assert not str(service._lock_manager._locks_dir).startswith(str(golden_repos_dir))


def test_rate_limiter_capacity_honored(dirs) -> None:
    """rate_limit_capacity is passed through to RateLimitConfig.capacity."""
    golden_repos_dir, server_data_dir = dirs
    service = _build(golden_repos_dir, server_data_dir, rate_limit_capacity=TEST_RATE_LIMIT_CAPACITY)

    assert isinstance(service._rate_limiter, MemoryRateLimiter)
    assert service._rate_limiter._config.capacity == TEST_RATE_LIMIT_CAPACITY


def test_rate_limiter_refill_rate_honored(dirs) -> None:
    """rate_limit_refill_per_second is passed through to RateLimitConfig.refill_per_second."""
    golden_repos_dir, server_data_dir = dirs
    service = _build(
        golden_repos_dir,
        server_data_dir,
        rate_limit_refill_per_second=TEST_RATE_LIMIT_REFILL_PER_SECOND,
    )

    assert service._rate_limiter._config.refill_per_second == TEST_RATE_LIMIT_REFILL_PER_SECOND


def test_max_summary_chars_passed_to_config(dirs) -> None:
    """max_summary_chars is passed through to MemoryStoreConfig."""
    golden_repos_dir, server_data_dir = dirs
    service = _build(golden_repos_dir, server_data_dir, max_summary_chars=TEST_MAX_SUMMARY_CHARS)

    assert service._config.max_summary_chars == TEST_MAX_SUMMARY_CHARS


def test_creates_memories_dir_if_missing(tmp_path: Path) -> None:
    """Factory must create memories_dir (mkdir parents=True, exist_ok=True) when it does not exist."""
    golden_repos_dir = tmp_path / "golden-repos"
    server_data_dir = tmp_path / "server"
    # Neither dir pre-created — factory must handle nested mkdir

    build_memory_store_service(
        golden_repos_dir=golden_repos_dir,
        server_data_dir=server_data_dir,
        refresh_scheduler=_make_scheduler(),
        refresh_debouncer=_make_debouncer(),
    )

    expected_memories_dir = golden_repos_dir / "cidx-meta" / "memories"
    assert expected_memories_dir.exists()
    assert expected_memories_dir.is_dir()
