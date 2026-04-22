"""
MemoryStoreService — orchestration layer for shared technical memory writes.

Story #877 Phase 1b.

Algorithm (issue #877 body lines 67-143):
1. Non-blocking acquire per-memory lock (ConflictError on failure).
2. Edit/delete: read current memory, compute hash, raise StaleContentError
   if expected hash mismatches.
3. Schema validate; enforce summary length cap; rate-limit check.
4. Piggyback-or-acquire coarse `cidx-meta` lock.
5. Atomic write (or delete) to base clone path.
6. Refresh trigger: direct-acquire path calls trigger_refresh_for_repo with
   DuplicateJobError / exception fallback to debouncer. Piggyback path ALWAYS
   signals debouncer.
7. Release per-memory lock in finally.
"""

from __future__ import annotations

import logging
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

from code_indexer.server.services.job_tracker import DuplicateJobError
from code_indexer.server.services.memory_file_lock_manager import (
    MemoryFileLockManager,
    _validate_memory_id,
)
from code_indexer.server.services.memory_io import (
    MemoryFileNotFoundError,
    atomic_delete_memory_file,
    atomic_write_memory_file,
    read_memory_file,
)
from code_indexer.server.services.memory_rate_limiter import MemoryRateLimiter
from code_indexer.server.services.memory_schema import (
    validate_create_payload,
    validate_edit_payload,
)

logger = logging.getLogger(__name__)

_COARSE_ALIAS = "cidx-meta"
_COARSE_REFRESH_ALIAS = "cidx-meta-global"


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class ConflictError(Exception):
    """Another writer holds the per-memory lock (retry later)."""


class StaleContentError(Exception):
    """Content hash mismatch on edit/delete (caller must re-read)."""

    def __init__(self, current_hash: str, message: str) -> None:
        self.current_hash = current_hash
        super().__init__(message)


class NotFoundError(Exception):
    """Memory with given id does not exist."""


class RateLimitError(Exception):
    """User exceeded rate limit."""


# ---------------------------------------------------------------------------
# Protocols (minimal subsets used by this service)
# ---------------------------------------------------------------------------


@runtime_checkable
class RefreshSchedulerProtocol(Protocol):
    """Minimal subset of refresh_scheduler used by MemoryStoreService."""

    def acquire_write_lock(
        self, alias: str, owner_name: str, ttl_seconds: int = 60
    ) -> bool: ...

    def release_write_lock(self, alias: str, owner_name: str) -> bool: ...

    def is_write_lock_held(self, alias: str) -> bool: ...

    def trigger_refresh_for_repo(self, repo_alias: str) -> Any: ...


@runtime_checkable
class RefreshDebouncerProtocol(Protocol):
    """Minimal subset of CidxMetaRefreshDebouncer used by MemoryStoreService."""

    def signal_dirty(self) -> None: ...


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class MemoryStoreConfig:
    memories_dir: Path
    max_summary_chars: int = 1000
    per_memory_lock_ttl_seconds: int = 30
    coarse_lock_ttl_seconds: int = 60


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MemoryStoreService:
    def __init__(
        self,
        config: MemoryStoreConfig,
        lock_manager: MemoryFileLockManager,
        refresh_scheduler: RefreshSchedulerProtocol,
        refresh_debouncer: RefreshDebouncerProtocol,
        rate_limiter: MemoryRateLimiter,
        hostname: Optional[str] = None,
        clock: Optional[Callable[[], datetime]] = None,
        id_factory: Optional[Callable[[], str]] = None,
        metadata_cache_invalidator: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._config = config
        self._lock_manager = lock_manager
        self._scheduler = refresh_scheduler
        self._debouncer = refresh_debouncer
        self._rate_limiter = rate_limiter
        self._hostname = hostname or socket.gethostname()
        self._clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(timezone.utc)
        )
        self._id_factory: Callable[[], str] = id_factory or (lambda: uuid.uuid4().hex)
        # Story #877 Phase 3-A: optional callback to invalidate metadata cache
        # after successful write. Called with memory_id; never on exception paths.
        self._cache_invalidator: Optional[Callable[[str], None]] = (
            metadata_cache_invalidator
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_memory(self, payload: Dict[str, Any], username: str) -> Dict[str, Any]:
        """Create a new memory. Returns {"id", "content_hash", "path"}."""
        memory_id = self._id_factory()
        owner = self._owner_name(username)

        def _body() -> Dict[str, Any]:
            if not self._rate_limiter.consume(username):
                raise RateLimitError(f"Rate limit exceeded for user {username!r}")

            now_str = self._clock().isoformat()
            full_payload: Dict[str, Any] = {
                **payload,
                "id": memory_id,
                "created_by": username,
                "created_at": now_str,
                "edited_by": None,
                "edited_at": None,
            }
            validate_create_payload(full_payload, self._config.max_summary_chars)

            path = self._memory_path(memory_id)
            content_hash = self._run_with_coarse_lock(
                owner, lambda: atomic_write_memory_file(path, full_payload)
            )
            self._invalidate_cache(memory_id)
            return {"id": memory_id, "content_hash": content_hash, "path": str(path)}

        return self._run_with_per_memory_lock(memory_id, owner, _body)

    def edit_memory(
        self,
        memory_id: str,
        payload: Dict[str, Any],
        expected_content_hash: str,
        username: str,
    ) -> Dict[str, Any]:
        """Full-replacement edit (PUT semantics). Returns {"id", "content_hash", "path"}."""
        owner = self._owner_name(username)

        def _body() -> Dict[str, Any]:
            path = self._memory_path(memory_id)
            current_fm, _body_text, current_hash = self._read_or_raise(path, memory_id)

            if current_hash != expected_content_hash:
                raise StaleContentError(
                    current_hash,
                    f"Content hash mismatch for memory {memory_id!r}: "
                    f"expected {expected_content_hash!r}, got {current_hash!r}",
                )

            if not self._rate_limiter.consume(username):
                raise RateLimitError(f"Rate limit exceeded for user {username!r}")

            now_str = self._clock().isoformat()
            edit_dict = {**payload, "edited_by": username, "edited_at": now_str}
            validate_edit_payload(edit_dict, current_fm, self._config.max_summary_chars)

            merged: Dict[str, Any] = {**current_fm, **edit_dict}
            content_hash = self._run_with_coarse_lock(
                owner, lambda: atomic_write_memory_file(path, merged)
            )
            self._invalidate_cache(memory_id)
            return {"id": memory_id, "content_hash": content_hash, "path": str(path)}

        return self._run_with_per_memory_lock(memory_id, owner, _body)

    def delete_memory(
        self,
        memory_id: str,
        expected_content_hash: str,
        username: str,
    ) -> None:
        """Delete a memory."""
        owner = self._owner_name(username)

        def _body() -> None:
            path = self._memory_path(memory_id)
            _fm, _body_text, current_hash = self._read_or_raise(path, memory_id)

            if current_hash != expected_content_hash:
                raise StaleContentError(
                    current_hash,
                    f"Content hash mismatch for memory {memory_id!r}: "
                    f"expected {expected_content_hash!r}, got {current_hash!r}",
                )

            if not self._rate_limiter.consume(username):
                raise RateLimitError(f"Rate limit exceeded for user {username!r}")

            self._run_with_coarse_lock(owner, lambda: atomic_delete_memory_file(path))
            self._invalidate_cache(memory_id)

        self._run_with_per_memory_lock(memory_id, owner, _body)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _invalidate_cache(self, memory_id: str) -> None:
        """Call the metadata cache invalidator if one was injected.

        Invoked after a successful write (create/edit/delete). Never called
        on exception paths so callers can rely on: invalidator called ↔ write succeeded.

        Exceptions from the invalidator are logged as warnings and suppressed
        so they never mask the successful write result.
        """
        if self._cache_invalidator is None:
            return
        try:
            self._cache_invalidator(memory_id)
        except Exception:
            logger.warning(
                "metadata_cache_invalidator raised an exception for memory_id=%r — "
                "cache entry may be stale until TTL; write was successful.",
                memory_id,
                exc_info=True,
            )

    def _owner_name(self, username: str) -> str:
        return f"memory_store@{self._hostname}/{username}"

    def _memory_path(self, memory_id: str) -> Path:
        """Build and validate the path for a memory file.

        Uses _validate_memory_id for traversal-unsafe chars, then
        Path.relative_to() to confirm the resolved path stays within
        memories_dir (handles symlinks and edge cases correctly).
        """
        _validate_memory_id(memory_id)
        path = self._config.memories_dir / f"{memory_id}.md"
        try:
            path.resolve().relative_to(self._config.memories_dir.resolve())
        except ValueError:
            raise ValueError(f"memory_id {memory_id!r} resolves outside memories_dir")
        return path

    def _read_or_raise(self, path: Path, memory_id: str):
        """Read memory file or raise NotFoundError."""
        try:
            return read_memory_file(path)
        except MemoryFileNotFoundError:
            raise NotFoundError(f"Memory {memory_id!r} does not exist")

    def _run_with_per_memory_lock(
        self, memory_id: str, owner: str, operation: Callable[[], Any]
    ) -> Any:
        """Non-blocking acquire of per-memory lock; release guaranteed in finally.

        Raises ConflictError immediately if lock is not available.
        """
        if not self._lock_manager.acquire(
            memory_id, owner, ttl_seconds=self._config.per_memory_lock_ttl_seconds
        ):
            raise ConflictError(f"Memory {memory_id!r} is locked by another writer")
        try:
            return operation()
        finally:
            released = self._lock_manager.release(memory_id, owner)
            if not released:
                logger.warning(
                    "Per-memory lock release returned False for memory_id=%r owner=%r "
                    "(owner mismatch or file gone); lock may be stale until TTL.",
                    memory_id,
                    owner,
                )

    def _coarse_piggyback_or_acquire(self, owner: str) -> bool:
        """Return True if piggybacking (skip acquire/release).

        Piggyback when is_write_lock_held returns True, or when acquire
        returns False (race — another process acquired between check and here).
        """
        if self._scheduler.is_write_lock_held(_COARSE_ALIAS):
            return True
        acquired = self._scheduler.acquire_write_lock(
            _COARSE_ALIAS, owner, ttl_seconds=self._config.coarse_lock_ttl_seconds
        )
        # acquired=False means a race into piggyback; we never held the lock.
        return not acquired

    def _signal_refresh(self, piggyback: bool) -> None:
        """Signal refresh. Piggyback always uses debouncer."""
        if piggyback:
            self._debouncer.signal_dirty()
            return
        try:
            self._scheduler.trigger_refresh_for_repo(_COARSE_REFRESH_ALIAS)
        except DuplicateJobError:
            logger.debug(
                "trigger_refresh_for_repo raised DuplicateJobError — "
                "falling back to debouncer"
            )
            self._debouncer.signal_dirty()
        except Exception:
            logger.warning(
                "trigger_refresh_for_repo raised unexpected exception — "
                "falling back to debouncer",
                exc_info=True,
            )
            self._debouncer.signal_dirty()

    def _release_coarse_lock(self, owner: str) -> None:
        """Release coarse lock; log warning if release fails."""
        released = self._scheduler.release_write_lock(_COARSE_ALIAS, owner)
        if not released:
            logger.warning(
                "Coarse lock release returned False for owner=%r; "
                "lock may persist until TTL.",
                owner,
            )

    def _run_with_coarse_lock(self, owner: str, operation: Callable[[], Any]) -> Any:
        """Acquire coarse lock (or piggyback), run operation, trigger refresh.

        Guarantees coarse lock release via try/finally when we hold it.
        Operation exceptions propagate; coarse lock is still released.
        """
        piggyback = self._coarse_piggyback_or_acquire(owner)
        coarse_held = not piggyback
        try:
            result = operation()
        except Exception:
            if coarse_held:
                self._release_coarse_lock(owner)
            raise
        try:
            self._signal_refresh(piggyback)
        finally:
            if coarse_held:
                self._release_coarse_lock(owner)
        return result
