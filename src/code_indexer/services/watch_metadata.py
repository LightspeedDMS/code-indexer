"""
Watch metadata management for persistent state across watch sessions.

Provides resumable watch functionality with timestamp tracking, git state monitoring,
and crash recovery capabilities.
"""

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import ClassVar, Dict, Any, Optional, List, Callable
from datetime import datetime, timezone
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class WatchMetadata:
    """Persistent metadata for resumable watch operations."""

    _save_lock: ClassVar[threading.Lock] = threading.Lock()

    # Timestamp tracking
    last_sync_timestamp: float = 0.0
    watch_started_at: Optional[str] = None

    # Git state tracking
    current_branch: Optional[str] = None
    current_commit: Optional[str] = None
    git_available: bool = False

    # Indexing state
    embedding_provider: Optional[str] = None
    embedding_model: Optional[str] = None
    collection_name: Optional[str] = None

    # Recovery state
    files_being_processed: Optional[List[str]] = None
    processing_interrupted: bool = False
    last_error: Optional[str] = None
    pending_branch_change: Optional[Dict[str, Any]] = None

    # Statistics
    total_files_processed: int = 0
    total_indexing_cycles: int = 0
    total_branch_changes_detected: int = 0

    def __post_init__(self):
        """Initialize default values."""
        if self.files_being_processed is None:
            self.files_being_processed = []

    @classmethod
    def load_from_disk(cls, metadata_path: Path) -> "WatchMetadata":
        """Load watch metadata from disk or create new instance. An
        unreadable file is moved aside and reported at ERROR: the work it
        recorded as owed is unknown, so the session starts interrupted."""
        if metadata_path.exists():
            try:
                with open(metadata_path, "r") as f:
                    data = json.load(f)
                    return cls(**data)
            except (ValueError, OSError, TypeError) as e:
                return cls._unreadable(metadata_path, e)

        return cls()

    @classmethod
    def _unreadable(cls, metadata_path: Path, error: Exception) -> "WatchMetadata":
        # ^ invariant one name per occurrence => an earlier set-aside record is never replaced
        kept = metadata_path.with_name(
            f"{metadata_path.name}.unreadable-{time.time_ns()}"
        )
        try:
            os.replace(metadata_path, kept)
        except OSError as move_error:
            kept = metadata_path
            logger.error(f"Could not set aside {metadata_path}: {move_error}")
        logger.error(
            f"Watch metadata {metadata_path} is unreadable ({error}); the changes "
            f"it recorded as owed are unknown (kept at {kept}); run `cidx index` "
            f"to reconcile the index with the working tree"
        )
        metadata = cls()
        metadata.mark_processing_interrupted(
            "watch metadata unreadable: owed work unknown; `cidx index` reconciles it"
        )
        return metadata

    def save_to_disk(self, metadata_path: Path, create_parents: bool = True):
        """Save watch metadata atomically (temp file + fsync + replace),
        one writer at a time: it carries the watch's owed work.
        create_parents=False never creates the metadata directory's parent
        (raises FileNotFoundError instead)."""
        metadata_path.parent.mkdir(parents=create_parents, exist_ok=True)
        # ^ invariant one save at a time, never in place => a reader or crash sees the old or the new record, never a torn one (#2093)
        with WatchMetadata._save_lock:
            payload = json.dumps(asdict(self), indent=2)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{metadata_path.name}.",
                suffix=".tmp",
                dir=metadata_path.parent,
            )
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_name, metadata_path)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp_name)
                raise

    def start_watch_session(
        self,
        provider_name: str,
        model_name: str,
        git_status: Dict[str, Any],
        collection_name: str,
    ):
        """Mark the start of a watch session."""
        self.watch_started_at = datetime.now(timezone.utc).isoformat()
        self.embedding_provider = provider_name
        self.embedding_model = model_name
        self.collection_name = collection_name

        # Update git state
        self.git_available = git_status.get("git_available", False)
        self.current_branch = git_status.get("current_branch")
        self.current_commit = git_status.get("current_commit")

        # Reset processing state
        self.files_being_processed = []
        self.processing_interrupted = False
        self.last_error = None

    def update_after_sync_cycle(self, files_processed: int = 0):
        """Update metadata after a successful sync cycle."""
        self.last_sync_timestamp = time.time()
        self.total_files_processed += files_processed
        self.total_indexing_cycles += 1

        # Clear processing state
        self.files_being_processed = []
        self.processing_interrupted = False
        self.last_error = None

    def update_git_state(self, new_branch: str, new_commit: str):
        """Update git state when branch/commit changes."""
        if new_branch != self.current_branch:
            self.total_branch_changes_detected += 1

        self.current_branch = new_branch
        self.current_commit = new_commit

    def mark_processing_start(self, files_to_process: List[str]):
        """Mark the start of file processing for crash recovery."""
        self.files_being_processed = files_to_process.copy()
        self.processing_interrupted = False

    def mark_processing_interrupted(self, error: Optional[str] = None):
        """Mark processing as interrupted for recovery."""
        self.processing_interrupted = True
        self.last_error = error

    def should_reprocess_file(self, file_path: Path) -> bool:
        """Check if file should be reprocessed based on timestamps."""
        if not file_path.exists():
            return False

        file_mtime = file_path.stat().st_mtime
        return file_mtime > self.last_sync_timestamp

    def get_recovery_files(self) -> List[str]:
        """Get list of files that need recovery processing."""
        if not self.processing_interrupted:
            return []

        return self.files_being_processed.copy() if self.files_being_processed else []

    def is_provider_changed(self, provider_name: str, model_name: str) -> bool:
        """Check if embedding provider/model has changed."""
        return (
            self.embedding_provider != provider_name
            or self.embedding_model != model_name
        )

    def get_statistics(self) -> Dict[str, Any]:
        """Get watch session statistics."""
        return {
            "watch_started_at": self.watch_started_at,
            "total_files_processed": self.total_files_processed,
            "total_indexing_cycles": self.total_indexing_cycles,
            "total_branch_changes": self.total_branch_changes_detected,
            "current_branch": self.current_branch,
            "last_sync_timestamp": self.last_sync_timestamp,
            "processing_interrupted": self.processing_interrupted,
            "files_in_recovery": (
                len(self.files_being_processed) if self.files_being_processed else 0
            ),
        }


class GitStateMonitor:
    """Monitors git state changes during watch operations."""

    def __init__(self, git_topology_service, check_interval: float = 1.0):
        """Initialize git state monitor.

        Args:
            git_topology_service: GitTopologyService instance
            check_interval: How often to check git state (seconds)
        """
        self.git_topology_service = git_topology_service
        self.check_interval = check_interval
        self.current_branch: Optional[str] = None
        self.current_commit: Optional[str] = None
        self.branch_change_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._monitoring = False

    def start_monitoring(self):
        """Start monitoring git state changes."""
        if not self.git_topology_service.is_git_available():
            return False

        # Initialize current state
        self.current_branch = self.git_topology_service.get_current_branch()
        self.current_commit = self._get_current_commit()
        self._monitoring = True
        return True

    def stop_monitoring(self):
        """Stop monitoring git state changes."""
        self._monitoring = False

    def check_for_changes(self) -> Optional[Dict[str, Any]]:
        """Check for git state changes. Returns change event if detected."""
        if not self._monitoring or not self.git_topology_service.is_git_available():
            return None

        new_branch = self.git_topology_service.get_current_branch()
        new_commit = self._get_current_commit()

        # Check for changes
        if new_branch != self.current_branch or new_commit != self.current_commit:
            change_event = {
                "type": "git_state_change",
                "old_branch": self.current_branch,
                "new_branch": new_branch,
                "old_commit": self.current_commit,
                "new_commit": new_commit,
                "timestamp": time.time(),
            }

            # Update current state
            self.current_branch = new_branch
            self.current_commit = new_commit

            # Notify callbacks
            for callback in self.branch_change_callbacks:
                try:
                    callback(change_event)
                except Exception as e:
                    print(f"Warning: Branch change callback failed: {e}")

            return change_event

        return None

    def register_branch_change_callback(self, callback):
        """Register callback for branch change events."""
        self.branch_change_callbacks.append(callback)

    def _get_current_commit(self) -> Optional[str]:
        """Get current commit hash."""
        try:
            import subprocess

            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.git_topology_service.codebase_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    def get_current_state(self) -> Dict[str, Any]:
        """Get current git state."""
        return {
            "git_available": self.git_topology_service.is_git_available(),
            "current_branch": self.current_branch,
            "current_commit": self.current_commit,
        }
