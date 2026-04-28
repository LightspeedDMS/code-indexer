"""Story #932 regression: memory CRUD lock-helper must work against a real RefreshScheduler.

Pre-fix: MagicMock complicity hid the is_write_lock_held vs is_write_locked typo.
Every call to create_memory / edit_memory / delete_memory hit AttributeError at
runtime while 174 unit tests passed silently because MagicMock auto-synthesises
any attribute name.

This test wires a REAL RefreshScheduler (with a real WriteLockManager) and a
REAL CidxMetaRefreshDebouncer so any future Protocol/concrete-class name mismatch
raises AttributeError at test time rather than in production.

No mocks: every collaborator on the lock path is a real implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.global_repos.meta_description_hook import CidxMetaRefreshDebouncer
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.write_lock_manager import WriteLockManager
from code_indexer.server.services.memory_file_lock_manager import MemoryFileLockManager
from code_indexer.server.services.memory_rate_limiter import (
    MemoryRateLimiter,
    RateLimitConfig,
)
from code_indexer.server.services.memory_store_service import (
    MemoryStoreConfig,
    MemoryStoreService,
)

# ---------------------------------------------------------------------------
# Test configuration constants (no magic numbers)
# ---------------------------------------------------------------------------

# Minimal debounce so the timer thread fires quickly and does not block cleanup.
_TEST_DEBOUNCE_SECONDS: float = 0.01

# Memory store config — generous limits so tests never hit boundaries.
_TEST_MAX_SUMMARY_CHARS: int = 1000
_TEST_PER_MEMORY_LOCK_TTL_SECONDS: int = 30
_TEST_COARSE_LOCK_TTL_SECONDS: int = 60

# Rate limiter config — large enough to never throttle test requests.
_TEST_RATE_CAPACITY: int = 100
_TEST_RATE_REFILL_PER_SECOND: float = 100.0

# Alias used for coarse-lock tests (matches _COARSE_ALIAS in production code).
_COARSE_ALIAS: str = "cidx-meta"


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


def _make_real_scheduler(golden_repos_dir: Path) -> RefreshScheduler:
    """Build a minimal real RefreshScheduler with only write_lock_manager set.

    All three Protocol methods used by MemoryStoreService (acquire_write_lock,
    release_write_lock, is_write_locked) delegate to self.write_lock_manager.
    No other attributes are needed for _coarse_piggyback_or_acquire.

    Uses object.__new__ to bypass the full __init__ (which requires DB connections,
    background threads, etc.) — the same pattern used in
    tests/unit/golden_repos/test_refresh_scheduler_temporal.py.
    """
    scheduler = object.__new__(RefreshScheduler)
    scheduler.write_lock_manager = WriteLockManager(golden_repos_dir=golden_repos_dir)
    return scheduler


@pytest.fixture()
def real_scheduler(tmp_path: Path) -> RefreshScheduler:
    """Real RefreshScheduler with a real WriteLockManager rooted in tmp_path."""
    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True)
    return _make_real_scheduler(golden_repos_dir)


@pytest.fixture()
def memory_service(
    tmp_path: Path, real_scheduler: RefreshScheduler
) -> MemoryStoreService:
    """MemoryStoreService wired with all-real collaborators on the lock path."""
    debouncer = CidxMetaRefreshDebouncer(
        refresh_scheduler=real_scheduler,
        debounce_seconds=_TEST_DEBOUNCE_SECONDS,
    )
    config = MemoryStoreConfig(
        memories_dir=tmp_path / "memories",
        max_summary_chars=_TEST_MAX_SUMMARY_CHARS,
        per_memory_lock_ttl_seconds=_TEST_PER_MEMORY_LOCK_TTL_SECONDS,
        coarse_lock_ttl_seconds=_TEST_COARSE_LOCK_TTL_SECONDS,
    )
    return MemoryStoreService(
        config=config,
        lock_manager=MemoryFileLockManager(tmp_path / "locks"),
        refresh_scheduler=real_scheduler,
        refresh_debouncer=debouncer,
        rate_limiter=MemoryRateLimiter(
            RateLimitConfig(
                capacity=_TEST_RATE_CAPACITY,
                refill_per_second=_TEST_RATE_REFILL_PER_SECOND,
            )
        ),
        hostname="integration-test-host",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMemoryStoreServiceWithRealRefreshScheduler:
    """Layer 2 regression guard: real scheduler must not cause AttributeError."""

    def test_coarse_piggyback_or_acquire_does_not_attribute_error_on_real_scheduler(
        self, memory_service: MemoryStoreService
    ) -> None:
        """_coarse_piggyback_or_acquire must work against a real RefreshScheduler.

        Pre-#932: calling this method raised AttributeError because the Protocol
        declared is_write_lock_held but RefreshScheduler implements is_write_locked.
        MagicMock auto-synthesised the misspelled name so unit tests never caught it.

        This test uses a REAL RefreshScheduler so any future name mismatch raises
        AttributeError immediately at test time rather than in production.
        """
        owner = "integration-test"
        piggybacking = None
        try:
            piggybacking = memory_service._coarse_piggyback_or_acquire(owner=owner)
            assert isinstance(piggybacking, bool), (
                f"Expected bool from _coarse_piggyback_or_acquire, got {type(piggybacking)}"
            )
        except AttributeError as exc:
            pytest.fail(
                f"Story #932 regression: AttributeError on real RefreshScheduler: {exc}\n"
                "This means is_write_locked method name mismatch has been reintroduced."
            )
        finally:
            # Release the coarse lock if this call acquired it (piggybacking=False).
            # piggybacking=True means we did NOT acquire — nothing to release.
            # piggybacking=None means the call itself raised — also nothing to release.
            if piggybacking is False:
                memory_service._scheduler.release_write_lock(
                    _COARSE_ALIAS, owner_name=owner
                )

    def test_is_write_locked_on_real_scheduler_returns_false_when_no_lock_held(
        self, real_scheduler: RefreshScheduler
    ) -> None:
        """is_write_locked on a real scheduler returns False when no lock is held.

        Direct smoke test for the method that was misspelled pre-#932.
        """
        result = real_scheduler.is_write_locked(_COARSE_ALIAS)
        assert result is False

    def test_acquire_and_release_write_lock_round_trip(
        self, real_scheduler: RefreshScheduler
    ) -> None:
        """acquire_write_lock followed by release_write_lock completes without error.

        Exercises the full lock round-trip used by _coarse_piggyback_or_acquire
        when it owns the coarse lock (not piggybacking). Lock release is guaranteed
        via try/finally so the tmp_path fixture can clean up the lock file.
        """
        owner = "integration-test-owner"
        acquired = False
        try:
            acquired = real_scheduler.acquire_write_lock(
                _COARSE_ALIAS, owner_name=owner
            )
            assert isinstance(acquired, bool)
            assert acquired is True
            assert real_scheduler.is_write_locked(_COARSE_ALIAS) is True
        finally:
            if acquired:
                real_scheduler.release_write_lock(_COARSE_ALIAS, owner_name=owner)
        assert real_scheduler.is_write_locked(_COARSE_ALIAS) is False
