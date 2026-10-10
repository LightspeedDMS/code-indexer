"""
Progressive metadata manager for resumable indexing operations.
"""

import json
import logging
import time
import fcntl
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

from code_indexer.utils.file_locking import nfs_safe_flock
from code_indexer.services.resume_state_seal import (
    RESUME_SEAL_FIELD,
    compute_resume_seal,
    content_digest,
    seal_matches,
)

logger = logging.getLogger(__name__)

#: Story #2121: the resume-from-list fields older releases stored (the work
#: list, the completed list and the cursor into the work list).
_RETIRED_RESUME_LIST_FIELDS = (
    "files_to_index",
    "completed_files",
    "current_file_index",
)


class ProgressiveMetadata:
    """Manages progressive metadata for resumable indexing."""

    def __init__(self, metadata_path: Path):
        self.metadata_path = metadata_path
        # Seal of the on-disk state as loaded, and the digest of that state's
        # content -- kept so enable_resume_seal() can verify it later, once
        # the server-held key is known, without holding a second full copy.
        self._loaded_seal: Optional[object] = None
        self._loaded_digest: Optional[bytes] = None
        self._resume_seal_key: Optional[bytes] = None
        self._resume_seal_binding = ""
        self._loaded_state_sealed = False
        self.metadata = self._load_metadata()

    def _load_metadata(self) -> Dict[str, Any]:
        """Load existing metadata or create empty structure."""
        # Define default metadata structure
        default_metadata = {
            "status": "not_started",
            "last_index_timestamp": 0.0,
            "indexed_at": None,
            "git_available": False,
            "project_id": None,
            "current_branch": None,
            "current_commit": None,
            "embedding_provider": None,
            "embedding_model": None,
            "files_processed": 0,
            "chunks_indexed": 0,
            "failed_files": 0,
            # Story #2121: the size of the current run's plan (no file list
            # is stored; an interrupted run is recovered by a reconcile).
            "total_files_to_index": 0,
            "failed_file_paths": [],  # Files the last run could not index
            # Git commit watermark tracking for incremental indexing
            "branch_commit_watermarks": {},  # Per-branch last indexed commit: {branch: commit_hash}
            "last_commit_check_timestamp": 0.0,  # When we last checked for git changes
            # Issue #1975: a completed reconcile verified the stored points
            # against disk; cleared whenever a new run starts from zero.
            "store_verified_by_reconcile": False,
            # Outcome of the last finished run (record_finished_run).
            "run_sequence": 0,
            "last_run_changed_index": None,
        }

        if self.metadata_path.exists():
            try:
                with open(self.metadata_path, "r") as f:
                    loaded_data = json.load(f)
                    if isinstance(loaded_data, dict):
                        # The seal is never part of the working metadata: it
                        # is verified once (enable_resume_seal) and rewritten
                        # on every sealed save.
                        self._loaded_seal = loaded_data.pop(RESUME_SEAL_FIELD, None)
                        self._loaded_digest = content_digest(loaded_data)
                        # Merge existing data with default structure to ensure new fields are present
                        merged_metadata = default_metadata.copy()
                        merged_metadata.update(loaded_data)
                        # Story #2121: a stored work list written by an older
                        # release is never kept, replayed or saved again.
                        for retired in _RETIRED_RESUME_LIST_FIELDS:
                            merged_metadata.pop(retired, None)
                        return merged_metadata
            except (json.JSONDecodeError, IOError):
                # Corrupt metadata, start fresh
                pass

        return default_metadata

    def _save_metadata(self):
        """Save metadata to disk atomically — temp file + rename prevents corruption on crash."""
        import os
        import tempfile

        # Ensure parent directory exists
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)

        payload: Dict[str, Any] = {
            k: v for k, v in self.metadata.items() if k != RESUME_SEAL_FIELD
        }
        if self._resume_seal_key is not None:
            payload[RESUME_SEAL_FIELD] = compute_resume_seal(
                self._resume_seal_key,
                self._resume_seal_binding,
                content_digest(payload),
            )

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self.metadata_path.parent), suffix=".tmp"
        )
        fd_owned = False
        try:
            try:
                tmp_f = os.fdopen(tmp_fd, "w")
                fd_owned = True
                with tmp_f:
                    json.dump(payload, tmp_f, indent=2)
                os.replace(tmp_path, str(self.metadata_path))
            finally:
                if not fd_owned:
                    try:
                        os.close(tmp_fd)
                    except OSError:
                        pass  # Already closed or invalid — discard
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                # Best-effort cleanup — temp file may already be gone.
                # Discard silently; the original exception propagates unmodified.
                pass
            raise

    def enable_resume_seal(self, key: bytes) -> None:
        """Seal every subsequent save with the server-held ``key``, and
        verify whether the state as LOADED from disk carried a valid seal.

        The seal is bound to the resolved metadata path, so resume state
        copied in from another repository never validates. Verification
        uses the load-time snapshot; when it fails, the caller discards the
        stored file lists (``discard_file_tracking``), so no file list from
        state without a valid seal reaches a later sealed save.
        """
        try:
            binding = str(self.metadata_path.resolve())
        except (OSError, RuntimeError) as exc:
            logger.warning(
                "Cannot resolve the resume metadata path (%s); stored resume "
                "state will not be trusted and saves will not be sealed.",
                type(exc).__name__,
            )
            return
        self._resume_seal_key = key
        self._resume_seal_binding = binding
        self._loaded_state_sealed = self._loaded_digest is not None and seal_matches(
            key, binding, self._loaded_digest, self._loaded_seal
        )

    def loaded_state_is_sealed(self) -> bool:
        """True only when the on-disk state loaded by this instance carried a
        valid server seal (see ``enable_resume_seal``)."""
        return self._loaded_state_sealed

    def start_indexing(
        self, provider_name: str, model_name: str, git_status: Dict[str, Any]
    ):
        """Mark the start of an indexing operation."""
        # INVARIANT: error_message describes only the run whose metadata it
        # currently sits in. Any transition that starts, resumes, records
        # new work for, or completes a run must therefore drop the previous
        # run's error before that transition's own work begins. State this
        # as the rule, not a list -- an enumeration silently falls one short
        # the moment a new transition is added and this comment isn't. The
        # two call sites that currently enforce it: start_indexing() (here)
        # and complete_indexing(). Story #2121 retired resume-from-list, so
        # every run that does work starts with start_indexing().
        self.metadata.pop("error_message", None)
        self.metadata.update(
            {
                "status": "in_progress",
                "indexed_at": datetime.now(timezone.utc).isoformat(),
                "embedding_provider": provider_name,
                "embedding_model": model_name,
                "git_available": git_status.get("git_available", False),
                "project_id": git_status.get("project_id"),
                "current_branch": git_status.get("current_branch"),
                "current_commit": git_status.get("current_commit"),
                "files_processed": 0,
                "chunks_indexed": 0,
                "failed_files": 0,
                "store_verified_by_reconcile": False,
            }
        )
        self._save_metadata()

    def get_fts_restore_pending(self) -> List[str]:
        """Un-hidden files whose full-text document could not be restored;
        a later reconcile retries them (Issue #1999)."""
        return [str(p) for p in self.metadata.get("fts_restore_pending") or []]

    def set_fts_restore_pending(self, paths: List[str]) -> None:
        """Replace the list of files whose FTS restore is still owed."""
        self.metadata["fts_restore_pending"] = list(
            dict.fromkeys(str(p) for p in paths)
        )
        self._save_metadata()

    def mark_store_verified(self) -> None:
        """Record that a completed reconcile verified the stored points
        against disk, so a zero processed-file count is no longer ambiguous
        (Issue #1975)."""
        self.metadata["store_verified_by_reconcile"] = True
        self._save_metadata()

    def start_fresh_indexing(
        self, provider_name: str, model_name: str, git_status: Dict[str, Any]
    ):
        """Start a new run after discarding the prior run's file tracking
        (its plan size and recorded failures), so a completed status can
        never be persisted alongside another run's tracking."""
        self._reset_file_tracking()
        self.start_indexing(provider_name, model_name, git_status)

    def update_progress(
        self, files_processed: int = 0, chunks_added: int = 0, failed_files: int = 0
    ):
        """Update progress counters and timestamp after each file."""
        current_timestamp = time.time()

        self.metadata["last_index_timestamp"] = current_timestamp
        self.metadata["files_processed"] += files_processed
        self.metadata["chunks_indexed"] += chunks_added
        self.metadata["failed_files"] += failed_files

        # Save after every update for resumability
        self._save_metadata()

    def complete_indexing(self, listing_started: float) -> None:
        """Mark indexing as completed.

        ``listing_started`` is the epoch time captured BEFORE the run listed
        the repository (Story #2121). It becomes the incremental watermark,
        not the completion time: a file written while the run was working is
        newer than it, so the next incremental run picks that file up."""
        if not listing_started > 0:
            raise ValueError(
                f"listing_started must be a positive epoch time: {listing_started!r}"
            )
        self.metadata["status"] = "completed"
        self.metadata["indexed_at"] = datetime.now(timezone.utc).isoformat()
        self.metadata["last_index_timestamp"] = float(listing_started)
        self.metadata.pop("error_message", None)
        self._save_metadata()

    def record_finished_run(self, commit: Optional[str], changed_index: bool) -> None:
        """Record the outcome of a run that returned (was not cancelled).

        ``run_sequence`` advances on every finished run, so the refresh
        scheduler sees that this file was rewritten by a run, and
        ``last_run_changed_index`` says whether that run changed the index
        (a forced reconcile that changed nothing publishes no snapshot).

        A run left ``completed`` records the HEAD it indexed as
        ``current_commit``: ``start_indexing`` writes it only when a run
        starts a session, so a run that found nothing to (re)index would
        leave the previous commit behind and the scheduler's drift check
        (current_commit != HEAD) would force a reconcile forever. An
        interrupted or failed run keeps its stale signal. A commit git could
        not detect ("unknown") never replaces the recorded one."""
        self.metadata["run_sequence"] = int(self.metadata.get("run_sequence") or 0) + 1
        self.metadata["last_run_changed_index"] = bool(changed_index)
        commit_known = commit is not None and commit.strip().lower() not in (
            "",
            "unknown",
        )
        if commit_known and self.metadata.get("status") == "completed":
            self.metadata["current_commit"] = commit
        self._save_metadata()

    def fail_indexing(self, error_message: Optional[str] = None):
        """Mark indexing as failed."""
        self.metadata["status"] = "failed"
        if error_message:
            self.metadata["error_message"] = error_message
        self._save_metadata()

    def get_resume_timestamp(self, safety_buffer_seconds: int = 60) -> float:
        """Get timestamp for resuming indexing with safety buffer.

        Args:
            safety_buffer_seconds: Number of seconds to go back for safety (default: 60)

        Returns:
            Timestamp to resume from, or 0.0 if full index needed
        """
        # Bug #467: Accept "failed" status for resume — interrupted indexing
        # may have been marked failed, but vectors on disk are still valid
        if self.metadata["status"] not in ["in_progress", "completed", "failed"]:
            return 0.0

        last_timestamp = self.metadata.get("last_index_timestamp", 0.0)
        if not isinstance(last_timestamp, (int, float)) or last_timestamp == 0.0:
            return 0.0

        # Apply safety buffer - go back N seconds to catch any files we might have missed
        return max(0.0, float(last_timestamp) - safety_buffer_seconds)

    def should_force_full_index(
        self,
        current_provider: str,
        current_model: str,
        current_git_status: Dict[str, Any],
    ) -> bool:
        """Check if we need to force a full index due to configuration changes."""

        # Check if embedding provider or model changed
        if (
            self.metadata.get("embedding_provider") != current_provider
            or self.metadata.get("embedding_model") != current_model
        ):
            return True

        # Check if git availability changed
        if self.metadata.get("git_available") != current_git_status.get(
            "git_available", False
        ):
            return True

        # Check if project changed (different directory or git repo)
        if self.metadata.get("project_id") != current_git_status.get("project_id"):
            return True

        return False

    def get_stats(self) -> Dict[str, Any]:
        """Get current indexing statistics."""
        can_resume_incremental = (
            self.metadata.get("status") in ["in_progress", "completed"]
            and self.metadata.get("last_index_timestamp", 0) > 0
        )

        return {
            "status": self.metadata.get("status", "not_started"),
            "last_indexed": self.metadata.get("indexed_at"),
            "files_processed": self.metadata.get("files_processed", 0),
            "chunks_indexed": self.metadata.get("chunks_indexed", 0),
            "failed_files": self.metadata.get("failed_files", 0),
            "embedding_provider": self.metadata.get("embedding_provider"),
            "embedding_model": self.metadata.get("embedding_model"),
            "project_id": self.metadata.get("project_id"),
            "current_branch": self.metadata.get("current_branch"),
            "can_resume": can_resume_incremental,
            "total_files_to_index": self.metadata.get("total_files_to_index", 0),
        }

    def clear(self):
        """Clear all metadata (for fresh start)."""
        self.metadata = {
            "status": "not_started",
            "last_index_timestamp": 0.0,
            "indexed_at": None,
            "git_available": False,
            "project_id": None,
            "current_branch": None,
            "current_commit": None,
            "embedding_provider": None,
            "embedding_model": None,
            "files_processed": 0,
            "chunks_indexed": 0,
            "failed_files": 0,
            # Plan size of the current run and the last run's failures
            "total_files_to_index": 0,
            "failed_file_paths": [],
            # Git commit watermark tracking for incremental indexing
            "branch_commit_watermarks": {},
            "last_commit_check_timestamp": 0.0,
            "store_verified_by_reconcile": False,
        }
        self._save_metadata()

    def _reset_file_tracking(self) -> None:
        """Discard file tracking belonging to a prior indexing run. The
        retired resume-list fields never reach ``self.metadata``
        (``_load_metadata`` drops them), so only these two remain."""
        self.metadata.update({"total_files_to_index": 0, "failed_file_paths": []})

    def record_plan(self, planned_files: int) -> None:
        """Record the size of this run's plan (Story #2121).

        Only the count is stored, never the files: an interrupted run is
        recovered by a reconcile that derives its plan from the store, so no
        stored list is ever replayed. The count keeps a killed run's plan
        observable (``total_files_to_index``)."""
        if planned_files < 0:
            raise ValueError(f"planned_files must not be negative: {planned_files}")
        self.metadata["total_files_to_index"] = planned_files
        self._save_metadata()

    def set_failed_file_paths(
        self, file_paths: List[str], failed_count: Optional[int] = None
    ) -> None:
        """Replace the recorded list of files the last run could not index.

        The next run retries every recorded file (see SmartIndexer), so the
        list always describes the most recent run only. ``failed_count``,
        when given, also sets ``failed_files`` for a run that records no
        other progress.
        """
        # Bug #1998: order-preserving O(F) dedup -- every file can fail in one
        # run, so a list-membership dedup here was O(files^2).
        unique: List[str] = list(dict.fromkeys(str(path) for path in file_paths))
        self.metadata["failed_file_paths"] = unique
        if failed_count is not None:
            self.metadata["failed_files"] = failed_count
        self._save_metadata()

    def discard_file_tracking(self) -> None:
        """Drop the stored file lists (work list, completed and failed
        files) from this in-memory state without saving; used when the
        stored state is not trusted, so none of its file lists are acted on
        or carried into a later save."""
        self._reset_file_tracking()

    def get_current_branch(self) -> str:
        """Get the current branch from metadata."""
        branch = self.metadata.get("current_branch", "unknown")
        return str(branch) if branch is not None else "unknown"

    def update_current_branch(self, branch_name: str) -> None:
        """Update the current branch safely with file locking."""
        # Use file locking for safe concurrent updates
        try:
            # Ensure parent directory exists
            self.metadata_path.parent.mkdir(parents=True, exist_ok=True)

            with open(self.metadata_path, "r+") as f:
                # Acquire exclusive lock
                nfs_safe_flock(f.fileno(), fcntl.LOCK_EX)

                # Read current metadata
                f.seek(0)
                try:
                    current_data = json.load(f)
                except (json.JSONDecodeError, EOFError):
                    # If file is corrupted, use current in-memory state
                    current_data = self.metadata

                # Update branch
                current_data["current_branch"] = branch_name

                # Write back
                f.seek(0)
                f.truncate()
                json.dump(current_data, f, indent=2)

                # Update in-memory state
                self.metadata["current_branch"] = branch_name

        except FileNotFoundError:
            # File doesn't exist yet, just update in-memory state
            self.metadata["current_branch"] = branch_name
            self._save_metadata()

    def get_current_branch_with_retry(
        self, fallback: str = "unknown", max_retries: int = 1
    ) -> str:
        """Get current branch with retry logic for file locking scenarios."""
        for attempt in range(max_retries + 1):
            try:
                if not self.metadata_path.exists():
                    return fallback

                with open(self.metadata_path, "r") as f:
                    # Try to acquire shared lock (non-blocking)
                    nfs_safe_flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                    data = json.load(f)
                    branch = data.get("current_branch", fallback)
                    return str(branch) if branch is not None else fallback

            except (OSError, IOError, json.JSONDecodeError):
                if attempt < max_retries:
                    # Wait a bit and retry
                    time.sleep(0.1)
                    continue
                else:
                    # Max retries exceeded, return fallback
                    return fallback

        return fallback

    def get_last_indexed_commit(self, branch: str) -> Optional[str]:
        """Get the last indexed commit hash for a specific branch.

        Args:
            branch: The branch name to get the commit for

        Returns:
            The commit hash, or None if no commit has been indexed for this branch
        """
        watermarks = self.metadata.get("branch_commit_watermarks", {})
        result = watermarks.get(branch)
        return str(result) if result is not None else None

    def update_commit_watermark(self, branch: str, commit_hash: str) -> None:
        """Update the last indexed commit hash for a branch.

        Args:
            branch: The branch name
            commit_hash: The commit hash that was just indexed
        """
        if "branch_commit_watermarks" not in self.metadata:
            self.metadata["branch_commit_watermarks"] = {}

        self.metadata["branch_commit_watermarks"][branch] = commit_hash
        self.metadata["last_commit_check_timestamp"] = time.time()
        self._save_metadata()

    def clear_commit_watermarks(self) -> None:
        """Clear all commit watermarks (for testing or forced reindex)."""
        self.metadata["branch_commit_watermarks"] = {}
        self.metadata["last_commit_check_timestamp"] = 0.0
        self._save_metadata()

    def get_all_commit_watermarks(self) -> Dict[str, str]:
        """Get all branch commit watermarks.

        Returns:
            Dictionary mapping branch names to commit hashes
        """
        watermarks = self.metadata.get("branch_commit_watermarks", {})
        return {str(k): str(v) for k, v in watermarks.items()}
