"""Temporal Progressive Metadata - Track indexing progress per provider collection."""

import datetime
import fcntl
import json
import logging
import os
import threading
from pathlib import Path
from typing import Set

from code_indexer.utils.file_locking import (
    nfs_safe_flock,
    nfs_safe_funlock,
    nfs_safe_fsync,
)

logger = logging.getLogger(__name__)

FORMAT_VERSION = 2
VALID_STATES = {"idle", "building", "failed"}


class TemporalProgressiveMetadata:
    """Track progressive state for temporal indexing with atomic writes and locking."""

    def __init__(self, temporal_dir: Path):
        """Initialize progressive metadata tracker.

        Args:
            temporal_dir: Path to the provider-specific collection directory
        """
        self.temporal_dir = temporal_dir
        self.progress_path = temporal_dir / "temporal_progress.json"
        self._lock_path = temporal_dir / "temporal_progress.json.lock"
        self._tmp_path = temporal_dir / "temporal_progress.json.tmp"
        # Bug #1206 Fix 2: in-memory staging set.  mark_commit_indexed() adds to
        # this set without touching the disk.  flush_pending() drains it in one
        # atomic write, bounding per-commit disk cost to O(1) amortized.
        self._pending: Set[str] = set()
        # Bug #1206 Fix 2 (race fix): guard _pending mutations so that commits
        # added between flush_pending()'s snapshot and its .clear() are not lost.
        # The lock is held ONLY for the in-memory snapshot+clear; the slow
        # _atomic_update (fsync) runs OUTSIDE the lock so add() is never blocked
        # during disk I/O.
        self._pending_lock = threading.Lock()

    def mark_commit_indexed(self, commit_hash: str) -> None:
        """Stage a commit as indexed in memory (O(1), no disk write).

        Bug #1206 Fix 2: previously this called _atomic_update which re-sorted and
        rewrote the entire progress file on every call.  Now it stages the hash in
        an in-memory set.  Call flush_pending() to persist the staged hashes.

        Durability contract: staged but unflushed commits are absent from
        load_completed() on a fresh instance (correct for crash-resume: the
        indexer will re-index them).
        """
        with self._pending_lock:
            self._pending.add(commit_hash)

    def flush_pending(self) -> None:
        """Flush all staged commits to disk in ONE atomic write.

        Bug #1206 Fix 2: snapshots and clears _pending under _pending_lock so
        that commits added between the snapshot and the clear are not lost.
        The slow _atomic_update (fsync) runs OUTSIDE the lock so that concurrent
        mark_commit_indexed() calls are never blocked during disk I/O.

        Must be called AFTER the corresponding vectors have been persisted so
        that a crash before this call does not mark a commit complete whose
        vectors are absent.
        """
        with self._pending_lock:
            if not self._pending:
                return
            to_flush = set(self._pending)
            self._pending.clear()
        # _atomic_update runs outside the lock — other threads can add() freely
        # during the fsync without their commits being lost.
        self._atomic_update(lambda data: data["completed_commits"].extend(to_flush))

    def save_completed(self, commit_hash: str) -> None:
        """Mark a commit as completed and immediately persist to disk.

        Legacy API: stages the commit then flushes immediately, preserving the
        original single-call durability semantics for callers that do not use
        the new batch-then-flush pattern.
        """
        self._pending.add(commit_hash)
        self.flush_pending()

    def mark_completed(self, commit_hashes: list) -> None:
        """Mark multiple commits as completed."""
        self._atomic_update(
            lambda data: data["completed_commits"].extend(commit_hashes)
        )

    def load_completed(self) -> Set[str]:
        """Load set of completed commit hashes.

        Returns the union of on-disk flushed commits and any in-memory staged
        commits not yet flushed.  A fresh instance (after crash/restart) returns
        only flushed commits — staged-but-unflushed commits are intentionally
        absent (correct crash-resume semantics: the indexer will re-index them).
        """
        data = self._load()
        on_disk = set(data.get("completed_commits", []))
        with self._pending_lock:
            pending_snapshot = set(self._pending)
        return on_disk | pending_snapshot

    def set_state(self, state: str) -> None:
        """Set the indexing state (idle, building, failed)."""
        if state not in VALID_STATES:
            raise ValueError(
                f"Invalid state '{state}'. Must be one of: {sorted(VALID_STATES)}"
            )
        self._atomic_update(lambda data: data.__setitem__("state", state))

    def get_state(self) -> str:
        """Get current indexing state."""
        data = self._load()
        return str(data.get("state", "idle"))

    def clear(self) -> None:
        """Clear progress tracking."""
        if self.progress_path.exists():
            self.progress_path.unlink()

    def load_progress(self) -> dict:
        """Load and return the full progress data dict."""
        return self._load()

    def _load(self) -> dict:
        """Load progress data, migrating legacy format if needed."""
        if not self.progress_path.exists():
            return self._default_data()
        try:
            with open(self.progress_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(
                "Failed to load %s, returning default data: %s",
                self.progress_path,
                e,
            )
            return self._default_data()

        # Migrate legacy format if needed
        if "format_version" not in data:
            data = self._migrate_legacy(data)

        return dict(data)

    def _migrate_legacy(self, data: dict) -> dict:
        """Migrate legacy format to version 2."""
        # Deduplicate completed_commits preserving first occurrence order
        commits = list(dict.fromkeys(data.get("completed_commits", [])))

        # Map old status to new state
        old_status = data.get("status", "")
        if old_status == "failed":
            new_state = "failed"
        else:
            # in_progress or complete: old run is dead, treat as idle
            new_state = "idle"

        migrated = {
            "format_version": FORMAT_VERSION,
            "completed_commits": sorted(commits),
            "last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "state": new_state,
        }

        # Write migrated data back atomically
        self._write_atomic(migrated)
        logger.info(
            "Migrated temporal_progress.json to format version %d", FORMAT_VERSION
        )

        return migrated

    def _default_data(self) -> dict:
        """Return default empty progress data."""
        return {
            "format_version": FORMAT_VERSION,
            "completed_commits": [],
            "last_updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "state": "idle",
        }

    def _atomic_update(self, modifier) -> None:  # type: ignore[type-arg]
        """Atomic read-modify-write under file lock.

        Acquires lock, reads current data, applies modifier, deduplicates
        commits, writes atomically.
        """
        self.temporal_dir.mkdir(parents=True, exist_ok=True)
        lock_file = open(self._lock_path, "w")
        _used_lockf = False
        _lock_acquired = False
        try:
            _used_lockf = nfs_safe_flock(lock_file.fileno(), fcntl.LOCK_EX)
            _lock_acquired = True

            data = self._load()
            modifier(data)

            # Deduplicate and sort commits
            data["completed_commits"] = sorted(set(data["completed_commits"]))
            data["last_updated"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            data["format_version"] = FORMAT_VERSION

            self._write_atomic(data)
        finally:
            if _lock_acquired:
                nfs_safe_funlock(lock_file.fileno(), _used_lockf)
            lock_file.close()

    def _write_atomic(self, data: dict) -> None:
        """Write data atomically via tmp file + os.replace, then fsync the
        directory (Bug #1407 Foundation) so the rename itself survives a
        crash/power-loss (precedent: id_index_manager.py's save_index()).
        """
        self.temporal_dir.mkdir(parents=True, exist_ok=True)
        with open(self._tmp_path, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            nfs_safe_fsync(f.fileno())
        os.replace(str(self._tmp_path), str(self.progress_path))

        dir_fd = os.open(str(self.temporal_dir), os.O_RDONLY)
        try:
            nfs_safe_fsync(dir_fd)
        finally:
            os.close(dir_fd)
