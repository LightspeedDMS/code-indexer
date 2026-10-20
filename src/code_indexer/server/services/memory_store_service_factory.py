"""
Factory for assembling MemoryStoreService from runtime dependencies.

Story #877 — Shared Technical Memory Store (singleton wiring).

Key placement rules (see CLAUDE.md "GOLDEN REPO VERSIONED PATH ARCHITECTURE"):
- memories_dir: {golden_repos_dir}/cidx-meta/memories
  BASE clone path — NEVER .versioned snapshot.
- locks_root: {server_data_dir}/locks
  OUTSIDE the base clone so lock files are never copied into versioned
  snapshots or surfaced as search noise (Story #877 algorithm lines 73-76).
"""

from pathlib import Path
from typing import Any, NamedTuple

from code_indexer.server.services.memory_file_lock_manager import MemoryFileLockManager
from code_indexer.server.services.memory_rate_limiter import (
    MemoryRateLimiter,
    RateLimitConfig,
)
from code_indexer.server.services.memory_store_service import (
    MemoryStoreConfig,
    MemoryStoreService,
)


class MemoryStoreBundle(NamedTuple):
    """Result of build_memory_store_service.

    Typical lifespan wiring:

        bundle = build_memory_store_service(...)
        app.state.memory_store_service = bundle.service
        # bundle.memories_dir is the authoritative path; access decisions
        # read memory frontmatter from it fresh.
        access_filtering_service = AccessFilteringService(
            group_manager,
            memories_dir=bundle.memories_dir,
        )
    """

    service: MemoryStoreService
    memories_dir: Path


def build_memory_store_service(
    *,
    golden_repos_dir: Path,
    server_data_dir: Path,
    refresh_scheduler: Any,
    refresh_debouncer: Any,
    max_summary_chars: int = 1000,
    rate_limit_capacity: int = 30,
    rate_limit_refill_per_second: float = 0.5,
    per_memory_lock_ttl_seconds: int = 30,
    coarse_lock_ttl_seconds: int = 60,
) -> MemoryStoreBundle:
    """Assemble MemoryStoreService from runtime dependencies.

    Args:
        golden_repos_dir: Base clone directory (NOT a .versioned snapshot).
        server_data_dir: Server data root; lock files are stored under
            {server_data_dir}/locks to keep them outside the base clone.
        refresh_scheduler: Concrete RefreshScheduler instance.
        refresh_debouncer: Concrete CidxMetaRefreshDebouncer instance.
        max_summary_chars: Maximum characters allowed in a memory summary field.
        rate_limit_capacity: Token-bucket capacity per user.
        rate_limit_refill_per_second: Token refill rate per user.
        per_memory_lock_ttl_seconds: TTL for per-memory write locks.
        coarse_lock_ttl_seconds: TTL for the coarse cidx-meta write lock.

    Returns:
        MemoryStoreBundle(service, memories_dir).
    """
    memories_dir = Path(golden_repos_dir) / "cidx-meta" / "memories"
    memories_dir.mkdir(parents=True, exist_ok=True)

    locks_root = Path(server_data_dir) / "locks"
    locks_root.mkdir(parents=True, exist_ok=True)

    lock_manager = MemoryFileLockManager(locks_root=locks_root)

    rate_limiter = MemoryRateLimiter(
        RateLimitConfig(
            capacity=rate_limit_capacity,
            refill_per_second=rate_limit_refill_per_second,
        )
    )

    config = MemoryStoreConfig(
        memories_dir=memories_dir,
        max_summary_chars=max_summary_chars,
        per_memory_lock_ttl_seconds=per_memory_lock_ttl_seconds,
        coarse_lock_ttl_seconds=coarse_lock_ttl_seconds,
    )

    service = MemoryStoreService(
        config=config,
        lock_manager=lock_manager,
        refresh_scheduler=refresh_scheduler,
        refresh_debouncer=refresh_debouncer,
        rate_limiter=rate_limiter,
    )

    return MemoryStoreBundle(service=service, memories_dir=memories_dir)
