"""Temporal Progressive Metadata - Track indexing progress per provider collection."""

import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Set

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

    def mark_commit_indexed(self, commit_hash: str) -> None:
        """Mark a single commit as indexed (canonical per-commit update method).

        Atomic read-modify-write under file lock.
        """
        self._atomic_update(lambda data: data["completed_commits"].append(commit_hash))

    def save_completed(self, commit_hash: str) -> None:
        """Mark a commit as completed. Legacy API — delegates to mark_commit_indexed."""
        self.mark_commit_indexed(commit_hash)

    def mark_completed(self, commit_hashes: list) -> None:
        """Mark multiple commits as completed."""
        self._atomic_update(
            lambda data: data["completed_commits"].extend(commit_hashes)
        )

    def load_completed(self) -> Set[str]:
        """Load set of completed commit hashes."""
        data = self._load()
        return set(data.get("completed_commits", []))

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
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX)

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
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()

    def _write_atomic(self, data: dict) -> None:
        """Write data atomically via tmp file + os.replace."""
        self.temporal_dir.mkdir(parents=True, exist_ok=True)
        with open(self._tmp_path, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(self._tmp_path), str(self.progress_path))
