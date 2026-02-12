"""
Langfuse Trace Sync Service (Story #165).

Background service that pulls traces from Langfuse REST API and writes them
to the filesystem using overlap window + content hash strategy to handle
trace mutations.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .langfuse_api_client import LangfuseApiClient
from ..utils.config_manager import LangfuseConfig, LangfusePullProject

logger = logging.getLogger(__name__)

OVERLAP_WINDOW_HOURS = 2


class SyncMetrics:
    """Tracks sync operation metrics."""

    def __init__(self):
        self.traces_checked = 0
        self.traces_written_new = 0
        self.traces_written_updated = 0
        self.traces_unchanged = 0
        self.errors_count = 0
        self.last_sync_time: Optional[str] = None
        self.last_sync_duration_ms: int = 0


class LangfuseTraceSyncService:
    """Background service that syncs traces from Langfuse to filesystem."""

    def __init__(
        self,
        config_getter: Callable[[], Any],
        data_dir: str,
        on_sync_complete: Optional[Callable[[], None]] = None,
    ):
        """
        Args:
            config_getter: Callable returning ServerConfig (for dynamic config)
            data_dir: Base directory for data (typically ~/.cidx-server/data)
            on_sync_complete: Optional callback invoked after each sync cycle completes
        """
        self._config_getter = config_getter
        self._data_dir = data_dir
        self._on_sync_complete = on_sync_complete
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._metrics: Dict[str, SyncMetrics] = {}  # Per-project metrics
        self._lock = threading.Lock()  # Metrics lock
        self._sync_lock = threading.Lock()  # Concurrent sync guard (C1/H4)

    def start(self) -> None:
        """Start background sync thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Langfuse trace sync service already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._sync_loop,
            name="LangfuseTraceSyncService",
            daemon=True,
        )
        self._thread.start()
        logger.info("Langfuse trace sync service started")

    def stop(self) -> None:
        """Stop background sync gracefully."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
        logger.info("Langfuse trace sync service stopped")

    def get_metrics(self) -> Dict[str, dict]:
        """Return per-project sync metrics for dashboard."""
        with self._lock:
            return {
                project: {
                    "traces_checked": m.traces_checked,
                    "traces_written_new": m.traces_written_new,
                    "traces_written_updated": m.traces_written_updated,
                    "traces_unchanged": m.traces_unchanged,
                    "errors_count": m.errors_count,
                    "last_sync_time": m.last_sync_time,
                    "last_sync_duration_ms": m.last_sync_duration_ms,
                }
                for project, m in self._metrics.items()
            }

    def trigger_sync(self) -> bool:
        """
        Trigger an immediate sync in background thread (C1 fix).

        Returns:
            True if sync triggered successfully, False if sync already in progress.
        """
        if not self._sync_lock.acquire(blocking=False):
            logger.warning("Sync already in progress, skipping manual trigger")
            return False

        def _do_sync():
            try:
                self.sync_all_projects()
            finally:
                self._sync_lock.release()

        threading.Thread(target=_do_sync, name="LangfuseManualSync", daemon=True).start()
        logger.info("Manual Langfuse sync triggered")
        return True

    def sync_all_projects(self) -> None:
        """Sync all configured projects. Called by background loop."""
        config = self._config_getter()
        langfuse = config.langfuse_config
        if not langfuse or not langfuse.pull_enabled:
            return

        logger.info(f"Syncing {len(langfuse.pull_projects)} Langfuse project(s)")

        host = langfuse.pull_host
        for project_creds in langfuse.pull_projects:
            try:
                self.sync_project(host, project_creds, langfuse.pull_trace_age_days)
            except Exception as e:
                logger.error(f"Error syncing project: {e}", exc_info=True)

        # After syncing all projects, notify callback for auto-registration
        if self._on_sync_complete:
            try:
                self._on_sync_complete()
            except Exception as e:
                logger.warning(f"Post-sync callback failed: {e}")

    def sync_project(
        self, host: str, creds: LangfusePullProject, trace_age_days: int
    ) -> None:
        """Sync a single project."""
        # 1. Create API client
        api_client = LangfuseApiClient(host, creds)

        # 2. Discover project name via GET /api/public/projects
        project_info = api_client.discover_project()
        project_name = project_info.get("name", "unknown")

        # 3. Load sync state
        state = self._load_sync_state(project_name)

        # 4. Migrate old hash format (Finding 2 backward compat)
        trace_hashes = state.get("trace_hashes", {})
        for tid, val in list(trace_hashes.items()):
            if isinstance(val, str):
                # Old format: {trace_id: hash_string}
                # New format: {trace_id: {"updated_at": str, "content_hash": str}}
                trace_hashes[tid] = {"updated_at": "", "content_hash": val}

        # 5. Calculate time window
        now = datetime.now(timezone.utc)
        max_age = now - timedelta(days=trace_age_days)

        if state.get("last_sync_timestamp"):
            # Overlap window: re-fetch from last_sync - 2 hours
            last_sync = datetime.fromisoformat(state["last_sync_timestamp"])
            from_time = last_sync - timedelta(hours=OVERLAP_WINDOW_HOURS)
            from_time = max(from_time, max_age)  # Don't go beyond trace age
        else:
            from_time = max_age  # First sync: fetch all within age limit

        # 6. Fetch and process traces (streaming - low memory)
        metrics = SyncMetrics()
        start_time = time.monotonic()
        seen_trace_ids = set()  # Finding 1: track seen traces for pruning
        pending_renames = []  # Lightweight: (timestamp, trace_id, folder_path, trace_type, short_id)

        page = 1
        while True:
            traces = api_client.fetch_traces_page(page, from_time)
            if not traces:
                break

            for trace in traces:
                metrics.traces_checked += 1
                trace_id = trace.get("id")
                if trace_id:
                    seen_trace_ids.add(trace_id)  # Finding 1: track seen
                try:
                    rename_info = self._process_trace(
                        api_client, trace, project_name, trace_hashes, metrics
                    )
                    if rename_info:
                        pending_renames.append(rename_info)
                except Exception as e:
                    metrics.errors_count += 1
                    logger.error(f"Error processing trace {trace.get('id')}: {e}")

            page += 1
            if self._stop_event.is_set():
                break

        # Phase 2: Finalize trace files (move from staging to destination with sequential names)
        if pending_renames:
            self._finalize_trace_files(pending_renames, trace_hashes)

        # Cleanup staging directories
        self._cleanup_staging(project_name)

        # 7. Prune trace_hashes: keep seen traces + traces still within age window
        pruned_hashes = {}
        for tid, h in trace_hashes.items():
            if tid in seen_trace_ids:
                # Seen in current sync - keep
                pruned_hashes[tid] = h
            elif isinstance(h, dict) and h.get("updated_at"):
                # Not seen, but check if still within age window
                try:
                    trace_time = datetime.fromisoformat(h["updated_at"])
                    if trace_time >= max_age:
                        # Still within age window - keep
                        pruned_hashes[tid] = h
                except (ValueError, TypeError):
                    # Malformed timestamp - discard
                    pass

        # INVARIANT: State must only be saved AFTER _finalize_trace_files completes.
        # If process crashes before here, staged files are orphaned but trace_hashes
        # (with filename=None) are not persisted. Next sync re-processes these traces
        # from scratch (self-healing). Staged files are cleaned up on next sync.
        # 8. Save state
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        metrics.last_sync_time = now.isoformat()
        metrics.last_sync_duration_ms = elapsed_ms

        state["last_sync_timestamp"] = now.isoformat()
        state["trace_hashes"] = pruned_hashes  # Finding 1: save pruned hashes
        self._save_sync_state(project_name, state)

        with self._lock:
            self._metrics[project_name] = metrics

        logger.info(
            f"Synced project '{project_name}': {metrics.traces_checked} checked, "
            f"{metrics.traces_written_new} new, {metrics.traces_written_updated} updated, "
            f"{metrics.traces_unchanged} unchanged, {metrics.errors_count} errors ({elapsed_ms}ms)"
        )


    def _process_trace(
        self,
        api_client: LangfuseApiClient,
        trace: dict,
        project_name: str,
        trace_hashes: dict,
        metrics: SyncMetrics,
    ) -> Optional[Tuple[str, str, str, str, str, str]]:
        """
        Process a single trace with two-phase hash check.

        Finding 2: Use updatedAt for quick check before fetching observations.
        Only fetch observations if trace changed.

        Returns:
            Tuple (timestamp, trace_id, staging_folder, dest_folder, trace_type, short_id)
            if trace is new and needs sequential naming in Phase 2, None otherwise.
        """
        trace_id = trace["id"]

        # Phase 1: Quick check using trace's updatedAt (Finding 2)
        updated_at = trace.get("updatedAt", "")
        stored = trace_hashes.get(trace_id)

        # Compute destination folder (golden-repos)
        dest_folder = self._get_trace_folder(project_name, trace)

        # Resolve file path: use stored filename, or fall back to old naming
        stored_filename = stored.get("filename") if stored else None
        if stored_filename:
            file_path = dest_folder / stored_filename
        else:
            # Old state without "filename" key, or new trace
            file_path = dest_folder / f"{trace_id}.json"

        # If stored hash exists and updatedAt matches, skip (no change)
        if stored and stored.get("updated_at") == updated_at:
            if file_path.exists():
                metrics.traces_unchanged += 1
                return None
            # File missing from disk - fall through to fetch and re-write

        # Phase 2: Trace changed or new - fetch observations and compute full hash
        observations = api_client.fetch_observations(trace_id)
        canonical = self._build_canonical_json(trace, observations)
        content_hash = self._compute_hash(canonical)

        # Check full content hash (updatedAt might change without content change)
        if stored and stored.get("content_hash") == content_hash:
            if file_path.exists():
                # Update the updatedAt but don't rewrite file
                # Preserve existing filename in state
                updated_entry = {
                    "updated_at": updated_at,
                    "content_hash": content_hash,
                }
                if stored_filename:
                    updated_entry["filename"] = stored_filename
                trace_hashes[trace_id] = updated_entry
                metrics.traces_unchanged += 1
                return None
            # File missing from disk - fall through to re-write

        is_new = trace_id not in trace_hashes

        # Determine filename for writing
        if stored_filename:
            # Existing trace with sequential name - write directly to destination
            filename = stored_filename
            self._write_trace(dest_folder, filename, trace, observations)

            # Update hash with updatedAt, content hash, and filename
            trace_hashes[trace_id] = {
                "updated_at": updated_at,
                "content_hash": content_hash,
                "filename": filename,
            }

            if is_new:
                metrics.traces_written_new += 1
            else:
                metrics.traces_written_updated += 1

            return None
        else:
            # New trace or migration from old naming - write to STAGING directory
            # Return metadata for Phase 2 finalize (move to destination with sequential name)
            staging_folder = self._get_staging_dir(project_name, trace)
            temp_filename = f"{trace_id}.json"
            self._write_trace(staging_folder, temp_filename, trace, observations)

            # Update hash with temporary filename marker (will be updated in Phase 2)
            trace_hashes[trace_id] = {
                "updated_at": updated_at,
                "content_hash": content_hash,
                "filename": None,  # Will be set in Phase 2
            }

            if is_new:
                metrics.traces_written_new += 1
            else:
                metrics.traces_written_updated += 1

            # Return metadata for Phase 2 finalize (6-tuple with staging AND dest)
            timestamp = trace.get("timestamp")
            trace_type = self._extract_trace_type(trace)
            short_id = self._extract_short_id(trace_id)
            return (timestamp, trace_id, str(staging_folder), str(dest_folder), trace_type, short_id)

    @staticmethod
    def _finalize_trace_files(
        pending: List[Tuple[str, str, str, str, str, str]],
        trace_hashes: dict,
    ) -> None:
        """Move staged trace files to destination with sequential names in chronological order.

        Groups pending items by destination folder, sorts by timestamp within each folder,
        determines next sequence number, moves files from staging to destination, and updates state.

        Args:
            pending: List of (timestamp, trace_id, staging_folder, dest_folder, trace_type, short_id)
            trace_hashes: State dict to update with new filenames
        """
        # Group by destination folder
        by_dest = defaultdict(list)
        for timestamp, trace_id, staging, dest, trace_type, short_id in pending:
            by_dest[dest].append((timestamp, trace_id, staging, trace_type, short_id))

        for dest_folder_path, entries in by_dest.items():
            dest_folder = Path(dest_folder_path)
            dest_folder.mkdir(parents=True, exist_ok=True)

            # Sort by timestamp (oldest first), handle None
            entries.sort(key=lambda e: e[0] or "")

            # Get next seq from DESTINATION folder
            seq = LangfuseTraceSyncService._get_next_seq_number(dest_folder)

            for timestamp, trace_id, staging_folder, trace_type, short_id in entries:
                staging_file = Path(staging_folder) / f"{trace_id}.json"
                new_filename = LangfuseTraceSyncService._build_trace_filename(seq, trace_type, short_id)
                dest_file = dest_folder / new_filename

                if staging_file.exists():
                    if dest_file.exists():
                        logger.warning(f"Destination already exists, overwriting: {dest_file}")
                        dest_file.unlink()  # Remove existing file before move
                    shutil.move(str(staging_file), str(dest_file))
                    if trace_id in trace_hashes:
                        trace_hashes[trace_id]["filename"] = new_filename
                else:
                    logger.warning(f"Staged file missing for trace {trace_id}: {staging_file}")
                    # Don't update trace_hashes - next sync will re-process

                seq += 1

    @staticmethod
    def _build_canonical_json(trace: dict, observations: list) -> str:
        """Build deterministic JSON for content hashing."""
        combined = {
            "trace": trace,
            "observations": sorted(observations, key=lambda o: o.get("id", "")),
        }
        return json.dumps(combined, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _compute_hash(canonical_json: str) -> str:
        """Compute SHA256 hash of canonical JSON."""
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    @staticmethod
    def _extract_trace_type(trace: dict) -> str:
        """Extract trace type from trace data for sequential filename.

        If trace name starts with 'subagent:' -> 'subagent-{sanitized_name}'
        Otherwise -> 'turn'
        """
        name = trace.get("name", "") or ""
        if name.startswith("subagent:"):
            subagent_name = name[len("subagent:"):]
            safe_name = LangfuseTraceSyncService._sanitize_folder_name(subagent_name)
            return f"subagent-{safe_name}"
        return "turn"

    @staticmethod
    def _extract_short_id(trace_id: str) -> str:
        """Extract last 8 characters of trace ID as short identifier."""
        return trace_id[-8:] if len(trace_id) >= 8 else trace_id

    @staticmethod
    def _get_next_seq_number(folder: Path) -> int:
        """Determine next sequence number by scanning existing files in folder.

        Parses {seq:03d}_* pattern from .json filenames to find max.
        Returns max + 1, or 1 if no sequential files found.
        """
        if not folder.exists():
            return 1

        max_seq = 0
        for f in folder.glob("*.json"):
            parts = f.stem.split("_", 1)
            if parts and parts[0].isdigit():
                seq = int(parts[0])
                if seq > max_seq:
                    max_seq = seq

        return max_seq + 1

    @staticmethod
    def _build_trace_filename(seq: int, trace_type: str, short_id: str) -> str:
        """Build sequential trace filename: {seq:03d}_{type}_{short_id}.json"""
        return f"{seq:03d}_{trace_type}_{short_id}.json"

    @staticmethod
    def _sanitize_folder_name(name: str) -> str:
        """Replace invalid filesystem characters with underscores."""
        return re.sub(r'[/\\:*?"<>|@#$%^&()\[\]{};\'\\,! ]', "_", name)

    def _get_trace_folder(self, project_name: str, trace: dict) -> Path:
        """Build folder path: langfuse_<project>_<userId>/<sessionId>/"""
        user_id = trace.get("userId") or "no_user"
        session_id = trace.get("sessionId") or "no_session"

        safe_project = self._sanitize_folder_name(project_name)
        safe_user = self._sanitize_folder_name(user_id)
        safe_session = self._sanitize_folder_name(session_id)

        folder_name = f"langfuse_{safe_project}_{safe_user}"
        return Path(self._data_dir) / "golden-repos" / folder_name / safe_session

    def _get_staging_dir(self, project_name: str, trace: dict) -> Path:
        """Get staging directory for a trace (outside golden-repos tree).

        Staging directories mirror the golden-repos structure but are temporary.
        Files are written here first, then moved to golden-repos with final names.
        """
        user_id = trace.get("userId") or "no_user"
        session_id = trace.get("sessionId") or "no_session"

        safe_project = self._sanitize_folder_name(project_name)
        safe_user = self._sanitize_folder_name(user_id)
        safe_session = self._sanitize_folder_name(session_id)

        return Path(self._data_dir) / ".langfuse_staging" / safe_project / safe_user / safe_session

    def _cleanup_staging(self, project_name: str) -> None:
        """Remove empty staging directories after sync.

        Walks staging directory tree bottom-up and removes empty directories.
        This handles cleanup after normal sync and recovery from crashes.
        """
        safe_project = self._sanitize_folder_name(project_name)
        staging_dir = Path(self._data_dir) / ".langfuse_staging" / safe_project
        if staging_dir.exists():
            # Walk bottom-up removing empty dirs
            for dirpath, dirnames, filenames in os.walk(str(staging_dir), topdown=False):
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)

    def _write_trace(
        self, folder: Path, filename: str, trace: dict, observations: list
    ) -> None:
        """Write trace JSON to filesystem.

        Args:
            folder: Directory to write the trace file into.
            filename: The filename to use (e.g. '001_turn_0b5c9e0c.json').
            trace: Trace data dict.
            observations: List of observation dicts.
        """
        folder.mkdir(parents=True, exist_ok=True)

        combined = {
            "trace": trace,
            "observations": sorted(observations, key=lambda o: o.get("startTime", "")),
        }

        file_path = folder / filename
        # Pretty-printed for readability (no sort_keys - trace first, observations chronological)
        content = json.dumps(combined, indent=2)

        # Atomic write: write to temp, then rename
        tmp_path = file_path.with_suffix(".json.tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.rename(file_path)

    def _load_sync_state(self, project_name: str) -> dict:
        """Load sync state from file."""
        state_file = self._get_state_file_path(project_name)
        if state_file.exists():
            try:
                return json.loads(state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load sync state for {project_name}: {e}")
        return {}

    def _save_sync_state(self, project_name: str, state: dict) -> None:
        """Save sync state atomically."""
        state_file = self._get_state_file_path(project_name)
        state_file.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = state_file.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp_path.rename(state_file)

    def _get_state_file_path(self, project_name: str) -> Path:
        """Get state file path for a project."""
        safe_name = self._sanitize_folder_name(project_name)
        return Path(self._data_dir) / f"langfuse_sync_state_{safe_name}.json"

    def _sync_loop(self) -> None:
        """
        Background thread main loop.

        Finding 6: Move config fetch inside try/except with fallback default.
        H4 fix: Guard sync with lock to prevent concurrent background + manual syncs.
        """
        logger.info("Langfuse trace sync loop started")
        while not self._stop_event.is_set():
            interval = 300  # Default interval
            try:
                config = self._config_getter()
                if config.langfuse_config and config.langfuse_config.pull_enabled:
                    interval = config.langfuse_config.pull_sync_interval_seconds
                    logger.info(f"Langfuse sync iteration starting (pull_enabled=True, interval={interval}s)")
                    # H4: Acquire lock before sync (skip if manual sync in progress)
                    if self._sync_lock.acquire(blocking=False):
                        try:
                            self.sync_all_projects()
                        finally:
                            self._sync_lock.release()
                    else:
                        logger.debug("Skipping background sync - manual sync in progress")
            except Exception as e:
                logger.error(f"Error in sync loop: {e}", exc_info=True)

            self._stop_event.wait(timeout=interval)
