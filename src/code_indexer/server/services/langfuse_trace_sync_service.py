"""
Langfuse Trace Sync Service (Story #165).

Background service that pulls traces from Langfuse REST API and writes them
to the filesystem using overlap window + content hash strategy to handle
trace mutations.
"""

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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

        # 6. Fetch and process traces
        metrics = SyncMetrics()
        start_time = time.monotonic()
        seen_trace_ids = set()  # Finding 1: track seen traces for pruning

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
                    self._process_trace(
                        api_client, trace, project_name, trace_hashes, metrics
                    )
                except Exception as e:
                    metrics.errors_count += 1
                    logger.error(f"Error processing trace {trace.get('id')}: {e}")

            page += 1
            if self._stop_event.is_set():
                break

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
    ) -> None:
        """
        Process a single trace with two-phase hash check.

        Finding 2: Use updatedAt for quick check before fetching observations.
        Only fetch observations if trace changed.
        """
        trace_id = trace["id"]

        # Phase 1: Quick check using trace's updatedAt (Finding 2)
        updated_at = trace.get("updatedAt", "")
        stored = trace_hashes.get(trace_id)

        # Compute file path for existence checks (detect deleted files)
        folder = self._get_trace_folder(project_name, trace)
        file_path = folder / f"{trace_id}.json"

        # If stored hash exists and updatedAt matches, skip (no change)
        if stored and stored.get("updated_at") == updated_at:
            if file_path.exists():
                metrics.traces_unchanged += 1
                return
            # File missing from disk - fall through to fetch and re-write

        # Phase 2: Trace changed or new - fetch observations and compute full hash
        observations = api_client.fetch_observations(trace_id)
        canonical = self._build_canonical_json(trace, observations)
        content_hash = self._compute_hash(canonical)

        # Check full content hash (updatedAt might change without content change)
        if stored and stored.get("content_hash") == content_hash:
            if file_path.exists():
                # Update the updatedAt but don't rewrite file
                trace_hashes[trace_id] = {
                    "updated_at": updated_at,
                    "content_hash": content_hash,
                }
                metrics.traces_unchanged += 1
                return
            # File missing from disk - fall through to re-write

        is_new = trace_id not in trace_hashes

        # Write to filesystem (folder already computed above)
        self._write_trace(folder, trace_id, trace, observations)

        # Update hash with both updatedAt and content hash
        trace_hashes[trace_id] = {
            "updated_at": updated_at,
            "content_hash": content_hash,
        }

        if is_new:
            metrics.traces_written_new += 1
        else:
            metrics.traces_written_updated += 1

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

    def _write_trace(
        self, folder: Path, trace_id: str, trace: dict, observations: list
    ) -> None:
        """Write trace JSON to filesystem."""
        folder.mkdir(parents=True, exist_ok=True)

        combined = {
            "trace": trace,
            "observations": sorted(observations, key=lambda o: o.get("startTime", "")),
        }

        file_path = folder / f"{trace_id}.json"
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
