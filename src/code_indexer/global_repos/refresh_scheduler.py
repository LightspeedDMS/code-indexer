"""
Refresh Scheduler for timer-triggered global repo updates.

Orchestrates the complete refresh cycle: timer triggers git pull,
change detection, index creation, alias swap, and cleanup scheduling.
"""

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union, TYPE_CHECKING, cast

from code_indexer.config import ConfigManager
from .alias_manager import AliasManager
from .git_pull_updater import GitPullUpdater
from .query_tracker import QueryTracker
from .cleanup_manager import CleanupManager
from .shared_operations import GlobalRepoOperations

if TYPE_CHECKING:
    from code_indexer.server.utils.config_manager import ServerResourceConfig
    from code_indexer.server.repositories.background_jobs import BackgroundJobManager

logger = logging.getLogger(__name__)

# Git URL prefixes — any repo_url NOT starting with one of these is treated as
# a local repo and is completely excluded from the scheduled auto-refresh cycle.
# Local repos are only refreshed via explicit trigger_refresh_for_repo() calls.
_GIT_URL_PREFIXES = ("https://", "http://", "git@", "ssh://", "git://")


def _is_git_repo_url(repo_url: str) -> bool:
    """
    Return True if repo_url represents a remote git repository.

    Remote git repos start with https://, http://, git@, ssh://, or git://.
    Everything else (local:// aliases, bare filesystem paths, empty strings)
    is considered a local repo and must be excluded from scheduled refresh.
    """
    if not repo_url:
        return False
    return any(repo_url.startswith(prefix) for prefix in _GIT_URL_PREFIXES)


# TTL for .write_mode/{alias}.json marker files (Bug #240).
# Markers older than this are treated as orphaned (client disconnected without
# calling exit_write_mode) and are cleaned up automatically.
WRITE_MODE_MARKER_TTL_SECONDS = 1800  # 30 minutes


class RefreshScheduler:
    """
    Timer-based scheduler for refreshing global repositories.

    Manages periodic refresh cycles for all registered global repos,
    coordinating git pulls, indexing, alias swaps, and cleanup.
    """

    def __init__(
        self,
        golden_repos_dir: str,
        config_source: Union[ConfigManager, GlobalRepoOperations],
        query_tracker: QueryTracker,
        cleanup_manager: CleanupManager,
        resource_config: Optional["ServerResourceConfig"] = None,
        background_job_manager: Optional["BackgroundJobManager"] = None,
        registry: Optional["GlobalRegistry"] = None,
    ):
        """
        Initialize the refresh scheduler.

        Args:
            golden_repos_dir: Path to golden repos directory
            config_source: Configuration source (ConfigManager for CLI, GlobalRepoOperations for server)
            query_tracker: Query tracker for reference counting
            cleanup_manager: Cleanup manager for old index removal
            resource_config: Optional resource configuration for timeouts (server mode)
            background_job_manager: Optional job manager for dashboard visibility (server mode)
            registry: Optional registry instance (for testing); if None, creates SQLite backend (production)
        """
        self.golden_repos_dir = Path(golden_repos_dir)
        self.config_source = config_source
        self.query_tracker = query_tracker
        self.cleanup_manager = cleanup_manager
        self.resource_config = resource_config
        self.background_job_manager = background_job_manager

        # Initialize managers
        self.alias_manager = AliasManager(str(self.golden_repos_dir / "aliases"))

        # Use injected registry if provided (testing), otherwise create SQLite backend (production)
        if registry is not None:
            self.registry = registry
        else:
            # Lazy import to avoid circular dependency (Story #713)
            from code_indexer.server.utils.registry_factory import (
                get_server_global_registry,
            )
            self.registry = get_server_global_registry(str(self.golden_repos_dir))

        # Thread management
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()  # Event-based signaling for efficient stop

        # Per-repo locking for concurrent refresh serialization
        self._repo_locks: dict[str, threading.Lock] = {}
        self._repo_locks_lock = threading.Lock()  # Protects _repo_locks dict

        # File-based write-lock manager for external writers (Story #230).
        # Replaces the in-memory threading.Lock registry from Story #227.
        # Keyed by repo alias without -global suffix (e.g., "cidx-meta").
        from .write_lock_manager import WriteLockManager
        self.write_lock_manager = WriteLockManager(golden_repos_dir=self.golden_repos_dir)

    def _get_repo_lock(self, alias_name: str) -> threading.Lock:
        """
        Get or create a lock for a specific repository.

        Thread-safe method to retrieve existing lock or create new one.

        Args:
            alias_name: Repository alias name

        Returns:
            Lock instance for the specified repository
        """
        with self._repo_locks_lock:
            if alias_name not in self._repo_locks:
                self._repo_locks[alias_name] = threading.Lock()
            return self._repo_locks[alias_name]

    # ------------------------------------------------------------------
    # Write-lock registry (Story #227, delegating to WriteLockManager Story #230)
    # ------------------------------------------------------------------

    def acquire_write_lock(self, alias: str, owner_name: str = "refresh_scheduler") -> bool:
        """
        Non-blocking acquire of the write lock for a repo alias.

        Delegates to WriteLockManager which uses file-based locks with owner
        identity, PID-based staleness detection, and TTL expiry (Story #230).

        Args:
            alias: Repo alias without -global suffix (e.g., "cidx-meta")
            owner_name: Human-readable caller identity recorded in the lock file.
                        Defaults to "refresh_scheduler" for internal scheduler use.
                        Pass the actual service name when calling from other services
                        (e.g., "dependency_map_service", "langfuse_trace_sync").

        Returns:
            True if lock was acquired, False if already held
        """
        return self.write_lock_manager.acquire(alias, owner_name=owner_name)

    def release_write_lock(self, alias: str, owner_name: str = "refresh_scheduler") -> None:
        """
        Release the write lock for a repo alias.

        Delegates to WriteLockManager (Story #230).

        Args:
            alias: Repo alias without -global suffix (e.g., "cidx-meta")
            owner_name: Must match the owner name used when acquiring the lock.
                        Defaults to "refresh_scheduler".
        """
        result = self.write_lock_manager.release(alias, owner_name=owner_name)
        if not result:
            logger.warning(
                f"Attempted to release write lock for '{alias}' but owner mismatch"
            )

    def is_write_locked(self, alias: str) -> bool:
        """
        Check whether the write lock for a repo alias is currently held.

        Delegates to WriteLockManager which checks file existence and staleness
        (Story #230).

        Args:
            alias: Repo alias without -global suffix (e.g., "cidx-meta")

        Returns:
            True if write lock is held, False otherwise
        """
        return self.write_lock_manager.is_locked(alias)

    def write_lock(self, alias: str, owner_name: str = "refresh_scheduler"):
        """
        Context manager that acquires the write lock on entry and releases on exit.

        Usage::

            with scheduler.write_lock("cidx-meta"):
                # write files here
                ...

            # With explicit caller identity (for non-scheduler callers):
            with scheduler.write_lock("cidx-meta", owner_name="dependency_map_service"):
                ...

        Args:
            alias: Repo alias without -global suffix (e.g., "cidx-meta")
            owner_name: Human-readable caller identity recorded in the lock file.
                        Defaults to "refresh_scheduler".
        """
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            acquired = self.acquire_write_lock(alias, owner_name=owner_name)
            if not acquired:
                raise RuntimeError(f"Write lock for '{alias}' is already held")
            try:
                yield
            finally:
                self.release_write_lock(alias, owner_name=owner_name)

        return _ctx()

    def cleanup_stale_write_mode_markers(self, force: bool = False) -> None:
        """
        Clean up orphaned .write_mode/{alias}.json marker files (Bug #240).

        When an MCP client calls enter_write_mode but disconnects without calling
        exit_write_mode, the marker file persists indefinitely because there is no
        session-lifecycle hook to trigger cleanup.

        This method is called:
        - On start() with force=True: removes ALL markers because no MCP sessions
          survive a server restart (every marker left over is definitively orphaned).
        - On each _scheduler_loop iteration: removes markers older than
          WRITE_MODE_MARKER_TTL_SECONDS (30 minutes), preserving fresh markers for
          active sessions.

        For each removed marker the corresponding write lock (owner='mcp_write_mode')
        is released so the refresh scheduler is not permanently blocked.

        Args:
            force: If True, remove ALL markers regardless of age (startup cleanup).
                   If False (default), only remove markers older than
                   WRITE_MODE_MARKER_TTL_SECONDS.
        """
        write_mode_dir = self.golden_repos_dir / ".write_mode"
        if not write_mode_dir.exists():
            return

        for marker_path in write_mode_dir.glob("*.json"):
            alias = marker_path.stem  # filename without .json
            try:
                self._cleanup_single_write_mode_marker(marker_path, alias, force)
            except Exception as exc:
                # Never let a single marker failure crash the scheduler
                logger.warning(
                    f"Unexpected error cleaning write mode marker {marker_path.name}: "
                    f"{type(exc).__name__}: {exc}"
                )

    def _cleanup_single_write_mode_marker(
        self, marker_path: Path, alias: str, force: bool
    ) -> None:
        """
        Evaluate and optionally remove a single write mode marker file.

        If force=True, the marker is always removed (startup cleanup).
        If force=False, the marker is removed only when:
        - it contains invalid/corrupt JSON, OR
        - the 'entered_at' field is missing, OR
        - entered_at + WRITE_MODE_MARKER_TTL_SECONDS < now.

        When a marker is removed the corresponding write lock is released.
        """
        import json as _json
        from datetime import datetime, timezone

        should_remove = force  # Always remove on startup
        entered_at_str = ""  # H1: initialized here so TOCTOU guard can reference it

        if not should_remove:
            # Determine staleness from marker content
            try:
                content = _json.loads(marker_path.read_text())
            except (OSError, _json.JSONDecodeError) as exc:
                logger.warning(
                    f"Corrupt write mode marker {marker_path.name} ({exc}), treating as stale"
                )
                should_remove = True
            else:
                entered_at_str = content.get("entered_at", "")
                if not entered_at_str:
                    logger.warning(
                        f"Write mode marker {marker_path.name} has no 'entered_at', treating as stale"
                    )
                    should_remove = True
                else:
                    try:
                        entered_at = datetime.fromisoformat(entered_at_str)
                        if entered_at.tzinfo is None:
                            entered_at = entered_at.replace(tzinfo=timezone.utc)
                        elapsed = (datetime.now(timezone.utc) - entered_at).total_seconds()
                        if elapsed > WRITE_MODE_MARKER_TTL_SECONDS:
                            should_remove = True
                    except (ValueError, TypeError):
                        logger.warning(
                            f"Write mode marker {marker_path.name} has unparseable "
                            f"'entered_at' ({entered_at_str!r}), treating as stale"
                        )
                        should_remove = True

        if not should_remove:
            return

        # H1 fix: Re-read marker to check a new session hasn't taken over (TOCTOU guard).
        # Only applies for non-force cleanups: startup force=True removes everything unconditionally.
        if not force:
            try:
                current_content = _json.loads(marker_path.read_text())
                current_entered_at = current_content.get("entered_at", "")
                if current_entered_at != entered_at_str:
                    logger.debug(
                        f"Write mode marker {marker_path.name} was refreshed by new session, skipping cleanup"
                    )
                    return
            except (OSError, _json.JSONDecodeError):
                pass  # File gone or corrupt — proceed with cleanup

        # Remove the marker file
        try:
            marker_path.unlink(missing_ok=True)
            logger.info(
                f"Cleaned up {'orphaned' if not force else 'startup'} write mode marker: "
                f"{marker_path.name} (alias={alias!r})"
            )
        except OSError as exc:
            logger.warning(f"Could not delete write mode marker {marker_path.name}: {exc}")
            return

        # Release the corresponding write lock so the scheduler is not blocked
        self.release_write_lock(alias, owner_name="mcp_write_mode")
        if self.is_write_locked(alias):
            logger.warning(
                f"Stale marker removed for {alias!r} but write lock release was refused "
                f"(owner mismatch). Lock may be held by a different service."
            )

    def _resolve_global_alias(self, alias_name: str) -> str:
        """Resolve bare alias to global alias format.

        Accepts either bare alias ("my-repo") or global alias ("my-repo-global").
        Tries the alias as-is first (fast path for callers already using global format).
        If not found, appends the "-global" suffix and retries.

        This keeps the -global suffix convention encapsulated in the scheduler layer,
        so callers (MCP handlers, REST endpoints, Web UI) can pass bare aliases.

        Args:
            alias_name: Either bare alias ("my-repo") or global alias ("my-repo-global")

        Returns:
            Global alias name (e.g., "my-repo-global")

        Raises:
            ValueError: If alias is not found in either bare or global format
        """
        # Try as-is first (already global format or exact match)
        if self.registry.get_global_repo(alias_name) is not None:
            return alias_name
        # Only append -global if not already present (avoid double-suffix edge case)
        if not alias_name.endswith("-global"):
            global_name = f"{alias_name}-global"
            if self.registry.get_global_repo(global_name) is not None:
                return global_name
        raise ValueError(f"Repository '{alias_name}' not found in global registry")

    def trigger_refresh_for_repo(self, alias_name: str, submitter_username: str = "system") -> Optional[str]:
        """
        Request a refresh for a specific repo after external writes complete.

        Accepts either bare alias ("my-repo") or global alias ("my-repo-global").
        Resolves to global format internally via _resolve_global_alias().

        Routes through BackgroundJobManager if available (server mode with dashboard
        visibility), otherwise falls back to direct _execute_refresh() (CLI mode).

        Args:
            alias_name: Bare alias (e.g., "my-repo") or global alias (e.g., "cidx-meta-global")
            submitter_username: Username to attribute the job to (default: "system" for
                background/scheduled refreshes; pass actual username for user-initiated refreshes)

        Returns:
            Job ID string if submitted to BackgroundJobManager, None if executed directly
            (CLI mode) or if no BackgroundJobManager is configured.

        Raises:
            ValueError: If alias is not found in the global registry
        """
        global_alias = self._resolve_global_alias(alias_name)
        if self.background_job_manager:
            return self._submit_refresh_job(global_alias, submitter_username=submitter_username)
        else:
            self._execute_refresh(global_alias)
            return None

    def get_refresh_interval(self) -> int:
        """
        Get the configured refresh interval.

        Returns:
            Refresh interval in seconds
        """
        # Support both ConfigManager (CLI) and GlobalRepoOperations (server)
        if isinstance(self.config_source, GlobalRepoOperations):
            config = self.config_source.get_config()
            return cast(int, config["refresh_interval"])
        else:
            # ConfigManager (CLI)
            return cast(int, self.config_source.get_global_refresh_interval())

    def is_running(self) -> bool:
        """
        Check if scheduler is running.

        Returns:
            True if background thread is active
        """
        return self._running

    def start(self) -> None:
        """
        Start the refresh scheduler background thread.

        Idempotent: Safe to call multiple times
        """
        if self._running:
            logger.debug("Refresh scheduler already running")
            return

        self._running = True
        self._stop_event.clear()  # Reset event for new start

        # Bug #240: On startup, ALL .write_mode/ markers are orphaned because no
        # MCP sessions survive a server restart. Force-clean them before the
        # scheduler loop starts so refresh cycles are not blocked.
        self.cleanup_stale_write_mode_markers(force=True)

        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._thread.start()
        logger.info("Refresh scheduler started")

    def stop(self) -> None:
        """
        Stop the refresh scheduler background thread.

        Waits for thread to exit gracefully.

        Idempotent: Safe to call multiple times
        """
        if not self._running:
            logger.debug("Refresh scheduler already stopped")
            return

        self._running = False
        self._stop_event.set()  # Signal scheduler loop to exit immediately

        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

        logger.info("Refresh scheduler stopped")

    def _scheduler_loop(self) -> None:
        """
        Background thread loop for scheduled refreshes.

        Checks all registered global repos at configured interval
        and triggers refreshes.
        """
        logger.debug("Refresh scheduler loop started")

        while self._running:
            try:
                # Bug #240: Periodically evict orphaned write mode markers from
                # clients that disconnected without calling exit_write_mode.
                self.cleanup_stale_write_mode_markers()

                # Get all registered global repos
                repos = self.registry.list_global_repos()

                for repo in repos:
                    if not self._running:
                        break

                    alias_name = repo.get("alias_name")
                    repo_url = repo.get("repo_url", "")

                    if not alias_name:
                        continue

                    # Skip local repos completely from the scheduled refresh cycle.
                    # Local repos (local://, bare filesystem paths, empty URLs) are
                    # only refreshed via explicit trigger_refresh_for_repo() calls
                    # from their writer services (DependencyMapService,
                    # LangfuseTraceSyncService, MetaDescriptionHook).
                    # Submitting them here causes failures for uninitialized repos
                    # and wastes background jobs for initialized ones.
                    if not _is_git_repo_url(repo_url):
                        logger.info(
                            f"Skipping local repo {alias_name} from scheduled refresh "
                            f"(repo_url={repo_url!r}). Local repos are only refreshed "
                            f"via explicit trigger."
                        )
                        continue

                    try:
                        self._submit_refresh_job(alias_name)
                    except Exception as e:
                        logger.error(
                            f"Refresh failed for {alias_name}: {type(e).__name__}: {e}", exc_info=True
                        )

            except Exception as e:
                logger.error(f"Error in scheduler loop: {type(e).__name__}: {e}", exc_info=True)

            # Wait using Event.wait() for interruptible sleep
            # Event.wait() returns True if event is set, False on timeout
            interval = self.get_refresh_interval()
            self._stop_event.wait(timeout=interval)

        logger.debug("Refresh scheduler loop exited")

    def _submit_refresh_job(self, alias_name: str, submitter_username: str = "system") -> Optional[str]:
        """
        Submit a refresh job to BackgroundJobManager.

        If no BackgroundJobManager is configured (CLI mode), falls back to
        direct execution via _execute_refresh().

        Args:
            alias_name: Global alias name (e.g., "my-repo-global")
            submitter_username: Username to attribute the job to (default: "system")

        Returns:
            Job ID if submitted to BackgroundJobManager, None if executed directly
        """
        if not self.background_job_manager:
            # Fallback to direct execution if no job manager (CLI mode)
            self._execute_refresh(alias_name)
            return None

        job_id: str = self.background_job_manager.submit_job(
            operation_type="global_repo_refresh",
            func=lambda: self._execute_refresh(alias_name),
            submitter_username=submitter_username,
            is_admin=True,
            repo_alias=alias_name,
        )
        logger.info(f"Submitted refresh job {job_id} for {alias_name}")
        return job_id

    def refresh_repo(self, alias_name: str) -> None:
        """
        Public API for manual refresh (backwards compatibility).

        Delegates to _execute_refresh() for the actual work.

        Args:
            alias_name: Global alias name (e.g., "my-repo-global")
        """
        self._execute_refresh(alias_name)

    def _execute_refresh(self, alias_name: str) -> Dict[str, Any]:
        """
        Execute refresh for a repository (called by BackgroundJobManager).

        Orchestrates the complete refresh cycle:
        1. Git pull (via updater)
        2. Change detection
        3. New index creation (if changes)
        4. Alias swap
        5. Cleanup scheduling

        Per-repo locking ensures concurrent refresh attempts on the same repo
        are serialized, while different repos can refresh in parallel.

        Args:
            alias_name: Global alias name (e.g., "my-repo-global")

        Returns:
            Dict with success status and details for BackgroundJobManager tracking
        """
        # Acquire per-repo lock to serialize concurrent refresh attempts
        repo_lock = self._get_repo_lock(alias_name)

        with repo_lock:
            try:
                logger.info(f"Starting refresh for {alias_name}")

                # Get current alias target
                current_target = self.alias_manager.read_alias(alias_name)
                if not current_target:
                    logger.warning(f"Alias {alias_name} not found, skipping refresh")
                    return {
                        "success": True,
                        "alias": alias_name,
                        "message": "Alias not found, skipped",
                    }

                # Get repo info from registry
                repo_info = self.registry.get_global_repo(alias_name)
                if not repo_info:
                    logger.warning(
                        f"Repo {alias_name} not in registry, skipping refresh"
                    )
                    return {
                        "success": True,
                        "alias": alias_name,
                        "message": "Repo not in registry, skipped",
                    }

                # Determine if this is a local (non-git) repo
                repo_url = repo_info.get("repo_url", "")
                is_local_repo = repo_url.startswith("local://") if repo_url else False

                # Story #236 Fix 2: Always derive master path from golden_repos_dir / repo_name.
                # current_target from alias JSON may point to a .versioned/ snapshot after first
                # refresh — using it for git pull or as snapshot source would be wrong.
                repo_name = alias_name.replace("-global", "")
                master_path = str(self.golden_repos_dir / repo_name)

                # AC6: Reconcile registry with filesystem at START of refresh
                # This ensures registry flags reflect actual index state before refresh begins
                detected_indexes = self._detect_existing_indexes(Path(current_target))
                self._reconcile_registry_with_filesystem(alias_name, detected_indexes)
                logger.info(
                    f"Reconciled registry with filesystem at START for {alias_name}: {detected_indexes}"
                )

                if is_local_repo:
                    # C3: For local repos, source_path is the LIVE directory (where writers put files),
                    # NOT the current alias target which may point to a versioned snapshot.
                    source_path = master_path

                    # Bug #268: Skip uninitialized local repos gracefully.
                    # Per-user Langfuse repos may not have .code-indexer/ yet when the
                    # scheduler fires before LangfuseTraceSyncService has written any traces.
                    # Attempting cidx index on an uninitialized dir fails with:
                    #   "Command 'index' is not available in no configuration found"
                    # These repos are only refreshed via explicit trigger from their writers.
                    code_indexer_dir = Path(source_path) / ".code-indexer"
                    if not code_indexer_dir.exists():
                        logger.info(
                            f"Skipping scheduled refresh for local repo {alias_name}: "
                            f"not yet initialized (no .code-indexer/ in {source_path}). "
                            f"Will be refreshed via explicit trigger when writers populate it."
                        )
                        return {
                            "success": True,
                            "alias": alias_name,
                            "message": f"Not yet initialized, skipped",
                        }

                    # Story #227: Skip CoW clone if an external writer holds the write lock.
                    # Writers (DependencyMapService, LangfuseTraceSyncService) acquire the lock
                    # before writing and trigger an explicit refresh when done.
                    # Non-blocking check — never wait, just skip this cycle.
                    if self.is_write_locked(repo_name):
                        logger.info(
                            f"Skipping refresh for {alias_name}, write lock held by external writer"
                        )
                        return {
                            "success": True,
                            "alias": alias_name,
                            "message": "Skipped, write lock held",
                        }

                    # C2: Use mtime-based change detection for local repos
                    has_changes = self._has_local_changes(source_path, alias_name)

                    if not has_changes:
                        logger.info(
                            f"No changes detected for local repo {alias_name}, skipping refresh"
                        )
                        return {
                            "success": True,
                            "alias": alias_name,
                            "message": "No changes detected",
                        }

                    logger.info(f"Changes detected in local repo {alias_name}, creating new index")
                else:
                    # Bug #239: Check write lock for git repos too. Protects against
                    # reconciliation restoring a master while refresh tries to snapshot it.
                    if self.is_write_locked(repo_name):
                        logger.info(
                            f"Skipping refresh for {alias_name}, write lock held by external writer"
                        )
                        return {
                            "success": True,
                            "alias": alias_name,
                            "message": "Skipped, write lock held",
                        }

                    # Story #236 Fix 2: Always git pull into the master golden repo, never into
                    # a versioned snapshot. current_target may be a .versioned/ path after first
                    # refresh, but git pull must always operate on the canonical master.
                    updater = GitPullUpdater(master_path)

                    has_changes = updater.has_changes()

                    if not has_changes:
                        logger.info(
                            f"No changes detected for {alias_name}, skipping refresh"
                        )
                        return {
                            "success": True,
                            "alias": alias_name,
                            "message": "No changes detected",
                        }

                    # Pull latest changes into master
                    logger.info(f"Pulling latest changes for {alias_name} into master: {master_path}")
                    updater.update()
                    # Story #236 Fix 3: Always create snapshot from master, not from current_target.
                    source_path = master_path

                # Story #223 AC7: Sync file extensions from server config before indexing
                try:
                    from code_indexer.server.services.config_service import get_config_service
                    config_service = get_config_service()
                    config_service.sync_repo_extensions_if_drifted(source_path)
                except Exception as e:
                    logger.warning("Could not sync extensions before index for %s: %s", alias_name, e)

                # Index source first, then create versioned snapshot (Story #229)
                self._index_source(alias_name=alias_name, source_path=source_path)
                new_index_path = self._create_snapshot(
                    alias_name=alias_name, source_path=source_path
                )

                # Swap alias to new index
                logger.info(f"Swapping alias {alias_name} to new index")
                self.alias_manager.swap_alias(
                    alias_name=alias_name,
                    new_target=new_index_path,
                    old_target=current_target,
                )

                # Story #236 Fix 1: Only schedule cleanup for versioned snapshots.
                # Never schedule cleanup for the master golden repo (golden-repos/{alias}/).
                # On first refresh, current_target IS the master golden repo — scheduling it
                # for cleanup would permanently destroy the master.
                if ".versioned" in current_target:
                    logger.info(f"Scheduling cleanup of old versioned snapshot: {current_target}")
                    self.cleanup_manager.schedule_cleanup(current_target)
                else:
                    logger.info(
                        f"Preserving master golden repo (not scheduling cleanup): {current_target}"
                    )

                # Update registry timestamp
                self.registry.update_refresh_timestamp(alias_name)

                # AC6: Reconcile registry with filesystem at END of refresh
                # This captures any new indexes created during refresh (semantic, FTS, temporal, SCIP)
                detected_indexes = self._detect_existing_indexes(Path(new_index_path))
                self._reconcile_registry_with_filesystem(alias_name, detected_indexes)
                logger.info(
                    f"Reconciled registry with filesystem at END for {alias_name}: {detected_indexes}"
                )

                logger.info(f"Refresh complete for {alias_name}")
                return {
                    "success": True,
                    "alias": alias_name,
                    "message": "Refresh complete",
                }

            except Exception as e:
                logger.error(f"Refresh failed for {alias_name}: {type(e).__name__}: {e}", exc_info=True)
                # Bug #84 fix: Raise exception instead of returning error dict
                # BackgroundJobManager marks jobs as FAILED only when exceptions are raised
                raise RuntimeError(f"Refresh failed for {alias_name}: {type(e).__name__}: {e}")

    def _index_source(self, alias_name: str, source_path: str) -> None:
        """
        Index the golden repo source in place (Story #229: index-source-first).

        Runs all indexing (semantic+FTS, temporal, SCIP) directly on the source
        repository, before any CoW clone is performed.  The versioned snapshot
        created later by _create_snapshot() then inherits the indexes via reflink,
        eliminating the disk cost of re-indexing an identical copy.

        Ordering contract:
        - Must be called BEFORE _create_snapshot().
        - cidx fix-config is NOT called here (only called on the clone in _create_snapshot).

        Args:
            alias_name: Global alias name (e.g., "my-repo-global")
            source_path: Path to the golden repo source directory

        Raises:
            RuntimeError: If any indexing step fails or times out
        """
        cidx_index_timeout = 3600  # Default: 1 hour
        if self.resource_config:
            cidx_index_timeout = self.resource_config.cidx_index_timeout

        # Step 1: Run cidx index for semantic + FTS (always required)
        index_command = ["cidx", "index", "--fts"]
        logger.info(
            f"Running cidx index (semantic+FTS) on source for {alias_name}: {' '.join(index_command)}"
        )
        try:
            subprocess.run(
                index_command,
                cwd=str(source_path),
                capture_output=True,
                text=True,
                timeout=cidx_index_timeout,
                check=True,
            )
            logger.info("cidx index (semantic+FTS) on source completed successfully")
        except subprocess.CalledProcessError as e:
            logger.error(
                f"Indexing (semantic+FTS) on source failed for {alias_name}: {type(e).__name__}: {e.stderr}",
                exc_info=True,
            )
            raise RuntimeError(
                f"Indexing (semantic+FTS) on source failed for {alias_name}: {type(e).__name__}: {e.stderr}"
            )
        except subprocess.TimeoutExpired as e:
            logger.error(
                f"Indexing (semantic+FTS) on source timed out for {alias_name} "
                f"after {cidx_index_timeout} seconds: {type(e).__name__}",
                exc_info=True,
            )
            raise RuntimeError(
                f"Indexing (semantic+FTS) on source timed out for {alias_name} "
                f"after {cidx_index_timeout} seconds: {type(e).__name__}"
            )

        # Step 2: Temporal indexing on source (if enabled and not local://)
        repo_info = self.registry.get_global_repo(alias_name)
        enable_temporal = repo_info.get("enable_temporal", False) if repo_info else False
        temporal_options = repo_info.get("temporal_options") if repo_info else None

        repo_url = repo_info.get("repo_url", "") if repo_info else ""
        is_local_repo = repo_url.startswith("local://") if repo_url else False

        if enable_temporal and is_local_repo:
            logger.warning(
                f"Skipping temporal indexing for local repo {alias_name} "
                f"(local:// repos have no git history, ignoring enable_temporal flag)"
            )
            enable_temporal = False

        if enable_temporal:
            temporal_command = ["cidx", "index", "--index-commits"]
            logger.info(f"Temporal indexing enabled for {alias_name}")

            if temporal_options:
                if temporal_options.get("max_commits"):
                    temporal_command.extend(
                        ["--max-commits", str(temporal_options["max_commits"])]
                    )
                if temporal_options.get("since_date"):
                    temporal_command.extend(
                        ["--since-date", temporal_options["since_date"]]
                    )
                if temporal_options.get("diff_context"):
                    temporal_command.extend(
                        ["--diff-context", str(temporal_options["diff_context"])]
                    )

            logger.info(
                f"Running cidx index (temporal) on source for {alias_name}: {' '.join(temporal_command)}"
            )
            try:
                subprocess.run(
                    temporal_command,
                    cwd=str(source_path),
                    capture_output=True,
                    text=True,
                    timeout=cidx_index_timeout,
                    check=True,
                )
                logger.info("cidx index (temporal) on source completed successfully")
            except subprocess.CalledProcessError as e:
                logger.error(
                    f"Temporal indexing on source failed for {alias_name}: {type(e).__name__}: {e.stderr}",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"Temporal indexing on source failed for {alias_name}: {type(e).__name__}: {e.stderr}"
                )
            except subprocess.TimeoutExpired as e:
                logger.error(
                    f"Temporal indexing on source timed out for {alias_name} "
                    f"after {cidx_index_timeout} seconds: {type(e).__name__}",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"Temporal indexing on source timed out for {alias_name} "
                    f"after {cidx_index_timeout} seconds: {type(e).__name__}"
                )

        # Step 3: SCIP indexing on source (if enabled)
        enable_scip = repo_info.get("enable_scip", False) if repo_info else False

        if enable_scip:
            scip_timeout = 1800  # Default: 30 minutes
            if self.resource_config:
                scip_timeout = getattr(self.resource_config, "cidx_scip_generate_timeout", 1800)

            scip_command = ["cidx", "scip", "generate"]
            logger.info(f"SCIP indexing enabled for {alias_name}")
            logger.info(
                f"Running cidx scip generate on source for {alias_name}: {' '.join(scip_command)}"
            )
            try:
                subprocess.run(
                    scip_command,
                    cwd=str(source_path),
                    capture_output=True,
                    text=True,
                    timeout=scip_timeout,
                    check=True,
                )
                logger.info("cidx scip generate on source completed successfully")
            except subprocess.CalledProcessError as e:
                logger.error(
                    f"SCIP indexing on source failed for {alias_name}: {type(e).__name__}: {e.stderr}",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"SCIP indexing on source failed for {alias_name}: {type(e).__name__}: {e.stderr}"
                )
            except subprocess.TimeoutExpired as e:
                logger.error(
                    f"SCIP indexing on source timed out for {alias_name} "
                    f"after {scip_timeout} seconds: {type(e).__name__}",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"SCIP indexing on source timed out for {alias_name} "
                    f"after {scip_timeout} seconds: {type(e).__name__}"
                )

    def _create_snapshot(self, alias_name: str, source_path: str) -> str:
        """
        Create a versioned CoW snapshot of the already-indexed source (Story #229).

        Must be called AFTER _index_source() has built the indexes on source_path.
        The snapshot inherits all indexes from source via reflink — no re-indexing
        needed here.

        Workflow:
        1. Generate version timestamp and compute versioned_path
        2. CoW clone: cp --reflink=auto -a source_path versioned_path
           NOTE: tantivy_index is NOT deleted — it was built on source and is correct
        3. Git timestamp fix on clone (non-fatal)
        4. cidx fix-config --force on CLONE only (never on source)
        5. Validate .code-indexer/index exists
        6. Return str(versioned_path)
        7. Cleanup versioned_path on any failure, then re-raise

        Args:
            alias_name: Global alias name (e.g., "my-repo-global")
            source_path: Path to the golden repo source (already indexed by _index_source)

        Returns:
            Path to new versioned snapshot directory

        Raises:
            RuntimeError: If any step fails (with cleanup of partial artifacts)
        """
        # Get timeouts from resource config or use defaults
        cow_timeout = 600  # Default: 10 minutes
        git_update_timeout = 300  # Default: 5 minutes
        git_restore_timeout = 300  # Default: 5 minutes
        cidx_fix_timeout = 60  # Default: 1 minute

        if self.resource_config:
            cow_timeout = self.resource_config.cow_clone_timeout
            git_update_timeout = self.resource_config.git_update_index_timeout
            git_restore_timeout = self.resource_config.git_restore_timeout
            cidx_fix_timeout = self.resource_config.cidx_fix_config_timeout

        # Generate version timestamp (use time.time() for correct UTC epoch;
        # datetime.utcnow().timestamp() is wrong on non-UTC servers due to
        # naive datetime timezone interpretation)
        timestamp = int(time.time())
        version = f"v_{timestamp}"

        repo_name = alias_name.replace("-global", "")
        versioned_base = self.golden_repos_dir / ".versioned" / repo_name
        versioned_path = versioned_base / version

        logger.info(f"Creating versioned snapshot at: {versioned_path}")

        try:
            # Step 1: Create versioned directory structure
            versioned_base.mkdir(parents=True, exist_ok=True)

            # Step 2: Perform CoW clone
            # NOTE: tantivy_index is NOT deleted — indexes were built on source
            # and are correct. The CoW clone inherits them directly.
            logger.info(f"CoW cloning from {source_path} to {versioned_path}")
            try:
                subprocess.run(
                    [
                        "cp",
                        "--reflink=auto",
                        "-a",
                        str(source_path),
                        str(versioned_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=cow_timeout,
                    check=True,
                )
                logger.info("CoW clone completed successfully")
            except subprocess.CalledProcessError as e:
                logger.error(
                    f"CoW clone failed for {alias_name}: {type(e).__name__}: {e.stderr}",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"CoW clone failed for {alias_name}: {type(e).__name__}: {e.stderr}"
                )
            except subprocess.TimeoutExpired as e:
                logger.error(
                    f"CoW clone timed out for {alias_name} after {cow_timeout} seconds: {type(e).__name__}",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"CoW clone timed out for {alias_name} after {cow_timeout} seconds: {type(e).__name__}"
                )

            # Step 3: Fix git status on clone (only if .git exists) — non-fatal
            git_dir = versioned_path / ".git"
            if git_dir.exists():
                logger.info("Running git update-index --refresh to fix CoW timestamps")
                try:
                    result = subprocess.run(
                        ["git", "update-index", "--refresh"],
                        cwd=str(versioned_path),
                        capture_output=True,
                        text=True,
                        timeout=git_update_timeout,
                        check=False,  # Non-fatal
                    )
                    if result.returncode != 0:
                        logger.debug(f"git update-index output: {result.stderr}")
                except subprocess.TimeoutExpired:
                    logger.warning(
                        f"git update-index timed out after {git_update_timeout} seconds"
                    )

                logger.info("Running git restore . to clean up timestamp changes")
                try:
                    result = subprocess.run(
                        ["git", "restore", "."],
                        cwd=str(versioned_path),
                        capture_output=True,
                        text=True,
                        timeout=git_restore_timeout,
                        check=False,  # Non-fatal
                    )
                    if result.returncode != 0:
                        logger.debug(f"git restore output: {result.stderr}")
                except subprocess.TimeoutExpired:
                    logger.warning(
                        f"git restore timed out after {git_restore_timeout} seconds"
                    )

            # Step 4: Run cidx fix-config --force on CLONE only (never on source)
            logger.info("Running cidx fix-config --force on clone to update paths")
            try:
                subprocess.run(
                    ["cidx", "fix-config", "--force"],
                    cwd=str(versioned_path),
                    capture_output=True,
                    text=True,
                    timeout=cidx_fix_timeout,
                    check=True,
                )
                logger.info("cidx fix-config on clone completed successfully")
            except subprocess.CalledProcessError as e:
                logger.error(
                    f"cidx fix-config failed for {alias_name}: {type(e).__name__}: {e.stderr}",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"cidx fix-config failed for {alias_name}: {type(e).__name__}: {e.stderr}"
                )
            except subprocess.TimeoutExpired as e:
                logger.error(
                    f"cidx fix-config timed out for {alias_name} after {cidx_fix_timeout} seconds: {type(e).__name__}",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"cidx fix-config timed out for {alias_name} after {cidx_fix_timeout} seconds: {type(e).__name__}"
                )

            # Step 5: Validate index exists
            index_dir = versioned_path / ".code-indexer" / "index"
            if not index_dir.exists():
                logger.error(f"Index validation failed: {index_dir} does not exist")
                raise RuntimeError("Index validation failed: index directory not created")

            logger.info(f"Versioned snapshot created successfully at: {versioned_path}")
            return str(versioned_path)

        except Exception as e:
            # Cleanup partial artifacts on failure
            logger.error(
                f"Failed to create snapshot for {alias_name}, cleaning up: {type(e).__name__}: {e}",
                exc_info=True,
            )
            if versioned_path.exists():
                try:
                    shutil.rmtree(versioned_path)
                    logger.info(f"Cleaned up partial snapshot at: {versioned_path}")
                except Exception as cleanup_error:
                    logger.error(
                        f"Failed to cleanup partial snapshot for {alias_name}: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}",
                        exc_info=True,
                    )
            raise RuntimeError(
                f"Failed to create snapshot for {alias_name}: {type(e).__name__}: {e}"
            )

    def _create_new_index(self, alias_name: str, source_path: str) -> str:
        """
        Create a new versioned index directory with CoW clone and indexing.

        Story #229: This method is now a thin delegator that calls
        _index_source() followed by _create_snapshot() in sequence.
        Kept for backward compatibility with existing tests and callers.

        Complete workflow (delegated):
        1. _index_source(): Run all indexing on golden repo source
        2. _create_snapshot(): CoW clone, git fix, cidx fix-config, validate

        Args:
            alias_name: Global alias name (e.g., "my-repo-global")
            source_path: Path to source repository (golden repo)

        Returns:
            Path to new index directory (only if validation passes)

        Raises:
            RuntimeError: If any step fails (with cleanup of partial artifacts)
        """
        self._index_source(alias_name=alias_name, source_path=source_path)
        return self._create_snapshot(alias_name=alias_name, source_path=source_path)

    def _has_local_changes(self, source_path: str, alias_name: str) -> bool:
        """
        Detect changes in a local (non-git) repository using file mtime comparison.

        Compares the maximum mtime of all non-hidden files in source_path against
        the timestamp embedded in the latest versioned directory name.

        Algorithm:
        1. Derive repo_name from alias_name (strip "-global")
        2. Look in .versioned/{repo_name}/ for v_* directories
        3. If none exist -> return True (first version needed)
        4. Parse latest version timestamp from directory name
        5. Walk source_path, skip hidden dirs/files (starting with '.')
        6. Get max mtime of all non-hidden files
        7. If no visible files -> return False
        8. Return max_mtime > latest_version_timestamp

        Args:
            source_path: Path to the live local repository directory
            alias_name: Global alias name (e.g., "cidx-meta-global")

        Returns:
            True if changes detected or first version needed, False otherwise
        """
        repo_name = alias_name.replace("-global", "")
        versioned_base = self.golden_repos_dir / ".versioned" / repo_name

        # Find all v_* versioned directories
        if not versioned_base.exists():
            logger.debug(
                f"No .versioned/{repo_name}/ dir found — treating as first version"
            )
            return True

        version_dirs = [
            d for d in versioned_base.iterdir()
            if d.is_dir() and d.name.startswith("v_")
        ]

        if not version_dirs:
            logger.debug(
                f"No v_* dirs in .versioned/{repo_name}/ — treating as first version"
            )
            return True

        # Extract timestamp from directory name (v_TIMESTAMP)
        def parse_timestamp(d: Path) -> int:
            try:
                return int(d.name[2:])  # strip "v_" prefix
            except (ValueError, IndexError):
                return 0

        latest_version_dir = max(version_dirs, key=parse_timestamp)
        latest_timestamp = parse_timestamp(latest_version_dir)

        logger.debug(
            f"Latest versioned dir: {latest_version_dir.name} (timestamp={latest_timestamp})"
        )

        # Walk source_path, skip hidden dirs and files
        max_mtime: float = 0.0
        found_files = False

        for root, dirs, files in os.walk(source_path):
            # Skip hidden directories in-place (prevents descent into them)
            dirs[:] = [d for d in dirs if not d.startswith(".")]

            for filename in files:
                if filename.startswith("."):
                    continue
                file_path = os.path.join(root, filename)
                try:
                    mtime = os.stat(file_path).st_mtime
                    found_files = True
                    if mtime > max_mtime:
                        max_mtime = mtime
                except OSError as e:
                    logger.debug(f"Cannot stat {file_path}: {e}")

        if not found_files:
            logger.debug(f"No visible files in {source_path} — no changes")
            return False

        has_changes = max_mtime > latest_timestamp
        logger.debug(
            f"mtime check for {alias_name}: max_mtime={max_mtime:.0f} "
            f"vs latest_version={latest_timestamp} -> changes={has_changes}"
        )
        return has_changes

    def _detect_existing_indexes(self, repo_path: Path) -> Dict[str, bool]:
        """
        Detect which index types exist in the repository's .code-indexer directory.

        Args:
            repo_path: Path to the repository root

        Returns:
            Dictionary with index types as keys and existence as boolean values:
            - semantic: True if semantic vector index exists
            - fts: True if FTS (Tantivy) index exists
            - temporal: True if temporal index exists
            - scip: True if SCIP code intelligence indexes exist
        """
        code_indexer_dir = repo_path / ".code-indexer"

        # Check semantic index: .code-indexer/index/ directory with collections
        semantic_index_dir = code_indexer_dir / "index"
        if semantic_index_dir.exists() and semantic_index_dir.is_dir():
            # Check for collection subdirectories with vector data (exclude temporal collection)
            collections = [
                d
                for d in semantic_index_dir.iterdir()
                if d.is_dir() and d.name != "code-indexer-temporal"
            ]
            semantic_exists = len(collections) > 0
        else:
            semantic_exists = False

        # Check FTS index: .code-indexer/tantivy_index/ directory (production path)
        fts_index_dir = code_indexer_dir / "tantivy_index"
        fts_exists = fts_index_dir.exists() and fts_index_dir.is_dir()

        # Check temporal index: .code-indexer/index/code-indexer-temporal/ directory (production path)
        temporal_index_dir = semantic_index_dir / "code-indexer-temporal"
        temporal_exists = temporal_index_dir.exists() and temporal_index_dir.is_dir()

        # Check SCIP indexes: delegate to _has_scip_indexes()
        scip_exists = self._has_scip_indexes(repo_path)

        return {
            "semantic": semantic_exists,
            "fts": fts_exists,
            "temporal": temporal_exists,
            "scip": scip_exists,
        }

    def _has_scip_indexes(self, repo_path: Path) -> bool:
        """
        Check if SCIP code intelligence indexes exist in the repository.

        SCIP indexes are stored in .code-indexer/scip/ with .scip.db files
        (one per language project).

        Args:
            repo_path: Path to the repository root

        Returns:
            True if at least one .scip.db file exists, False otherwise
        """
        scip_dir = repo_path / ".code-indexer" / "scip"

        if not scip_dir.exists() or not scip_dir.is_dir():
            return False

        # Check for .scip.db files (converted SQLite indexes)
        scip_db_files = list(scip_dir.glob("*.scip.db"))
        return len(scip_db_files) > 0

    def _restore_master_from_versioned(self, alias_name: str, master_path: Path) -> bool:
        """
        Restore a missing master golden repo via reverse CoW clone from latest versioned snapshot.

        Finds the highest-timestamp v_* directory under .versioned/{repo_name}/,
        clones it to master_path via ``cp --reflink=auto -a``, then runs
        ``cidx fix-config --force`` on the restored master to repair paths.

        Args:
            alias_name: Global alias name (e.g., "my-repo-global")
            master_path: Destination path for the restored master

        Returns:
            True if restoration succeeded, False on any error
        """
        repo_name = alias_name.replace("-global", "")
        versioned_base = self.golden_repos_dir / ".versioned" / repo_name

        if not versioned_base.exists():
            logger.warning(f"Reconciliation: {alias_name} has no master and no versioned dir")
            return False

        version_dirs = [
            d for d in versioned_base.iterdir()
            if d.is_dir() and d.name.startswith("v_")
        ]

        if not version_dirs:
            logger.warning(f"Reconciliation: {alias_name} has no v_* snapshots to restore from")
            return False

        def _parse_ts(d: Path) -> int:
            try:
                return int(d.name[2:])
            except (ValueError, IndexError):
                return 0

        latest_version = max(version_dirs, key=_parse_ts)
        logger.info(
            f"Reconciliation: restoring {alias_name} master from {latest_version} via reverse CoW"
        )

        try:
            subprocess.run(
                ["cp", "--reflink=auto", "-a", str(latest_version), str(master_path)],
                capture_output=True, text=True, timeout=600, check=True,
            )
        except Exception as e:
            logger.error(
                f"Reconciliation: reverse CoW clone failed for {alias_name}: "
                f"{type(e).__name__}: {e}", exc_info=True,
            )
            return False

        # Fix .code-indexer/ paths — non-fatal if cidx is not available
        try:
            subprocess.run(
                ["cidx", "fix-config", "--force"],
                cwd=str(master_path), capture_output=True, text=True, timeout=60, check=False,
            )
            logger.info(f"Reconciliation: cidx fix-config --force done for {alias_name}")
        except Exception as fix_err:
            logger.warning(
                f"Reconciliation: cidx fix-config failed for {alias_name} "
                f"(non-fatal): {type(fix_err).__name__}: {fix_err}"
            )

        return True

    def _queue_missing_description(
        self, alias_name: str, master_path: Path, claude_cli_manager: Any
    ) -> bool:
        """
        Queue description generation when cidx-meta description file is missing.

        Checks for golden-repos/cidx-meta/{alias_name}.md and submits work to
        ClaudeCliManager if the file does not exist.

        Args:
            alias_name: Global alias name (e.g., "my-repo-global")
            master_path: Master golden repo path (used as repo_path for generation)
            claude_cli_manager: ClaudeCliManager instance to submit work to

        Returns:
            True if description was queued, False if already exists or error
        """
        cidx_meta_dir = self.golden_repos_dir / "cidx-meta"
        repo_name = alias_name.replace("-global", "")
        description_file = cidx_meta_dir / f"{repo_name}.md"

        if description_file.exists():
            return False

        logger.info(
            f"Reconciliation: queueing description for {alias_name} (missing {description_file})"
        )
        try:
            claude_cli_manager.submit_work(
                master_path,
                lambda success, result, _a=alias_name: logger.info(
                    f"Description generation for {_a}: "
                    f"{'success' if success else 'failed'} — {result}"
                ),
            )
            return True
        except Exception as e:
            logger.warning(
                f"Reconciliation: failed to queue description for {alias_name}: "
                f"{type(e).__name__}: {e}"
            )
            return False

    def reconcile_golden_repos(self, claude_cli_manager: Optional[Any] = None) -> None:
        """
        Startup reconciliation: restore missing master golden repos via reverse CoW clone.

        Runs ONCE on server startup, guarded by a marker file. Iterates all registered
        git-backed repos, restores any with missing masters from versioned snapshots, and
        optionally queues description generation for repos missing cidx-meta files.

        Non-blocking: per-repo failures are logged and skipped (AC7).
        Idempotent: marker file golden-repos/.reconciliation_complete_v1 (AC6).

        Args:
            claude_cli_manager: Optional ClaudeCliManager for description queuing (AC5).

        Story #236 AC4-AC7.
        """
        from datetime import datetime

        marker_file = self.golden_repos_dir / ".reconciliation_complete_v1"

        if marker_file.exists():
            logger.info("Startup reconciliation already completed (marker exists), skipping")
            return

        logger.info("Starting startup reconciliation of golden repos (Story #236)")
        restored_count = 0
        description_queued_count = 0

        try:
            all_repos = self.registry.list_global_repos()
        except Exception as e:
            logger.error(f"Failed to list repos for reconciliation: {type(e).__name__}: {e}")
            marker_file.write_text(f"Completed (with errors) at {datetime.now().isoformat()}")
            return

        for repo in all_repos:
            alias_name = repo.get("alias_name", "")
            repo_url = repo.get("repo_url", "")
            if not alias_name:
                continue
            # Skip local repos — no versioned copies to restore from.
            # This covers both local:// aliases and bare filesystem paths.
            if not _is_git_repo_url(repo_url):
                continue

            repo_name = alias_name.replace("-global", "")
            master_path = self.golden_repos_dir / repo_name

            # AC4: Restore missing master via reverse CoW clone
            if not master_path.exists():
                # Bug #239: Acquire write lock before restoring to prevent
                # RefreshScheduler from creating a CoW snapshot of a
                # partially-restored master directory.
                _write_lock_acquired = self.acquire_write_lock(
                    repo_name, owner_name="reconciliation"
                )
                if not _write_lock_acquired:
                    logger.warning(
                        f"Reconciliation: could not acquire write lock for {repo_name}, "
                        f"skipping restoration to avoid race condition"
                    )
                    continue
                try:
                    if self._restore_master_from_versioned(alias_name, master_path):
                        restored_count += 1
                except Exception as e:
                    logger.error(
                        f"Reconciliation: unexpected error for {alias_name}: "
                        f"{type(e).__name__}: {e}", exc_info=True,
                    )
                finally:
                    self.release_write_lock(
                        repo_name, owner_name="reconciliation"
                    )

            # AC5: Queue description generation if cidx-meta file is missing
            if claude_cli_manager is not None:
                try:
                    if self._queue_missing_description(alias_name, master_path, claude_cli_manager):
                        description_queued_count += 1
                except Exception as e:
                    logger.warning(
                        f"Reconciliation: description queue error for {alias_name}: "
                        f"{type(e).__name__}: {e}"
                    )

        logger.info(
            f"Startup reconciliation complete: {restored_count} masters restored, "
            f"{description_queued_count} descriptions queued"
        )
        marker_file.write_text(f"Completed at {datetime.now().isoformat()}")

    def _reconcile_registry_with_filesystem(
        self, alias_name: str, detected: Dict[str, bool]
    ) -> None:
        """
        Reconcile registry flags with detected filesystem state.

        Updates enable_temporal and enable_scip flags in the registry to match
        what actually exists on disk. This ensures registry state stays in sync
        with filesystem reality.

        Args:
            alias_name: Repository alias name (without -global suffix)
            detected: Dictionary from _detect_existing_indexes() with existence flags
        """
        # Get current registry state
        repo_info = self.registry.get_global_repo(alias_name)
        if not repo_info:
            logger.warning(
                f"Cannot reconcile registry for {alias_name}: repo not found in registry"
            )
            return

        # Reconcile temporal flag
        registry_temporal = repo_info.get("enable_temporal", False)
        filesystem_temporal = detected.get("temporal", False)

        if registry_temporal != filesystem_temporal:
            logger.info(
                f"Reconciling temporal flag for {alias_name}: "
                f"registry={registry_temporal} -> filesystem={filesystem_temporal}"
            )
            self.registry.update_enable_temporal(alias_name, filesystem_temporal)

        # Reconcile SCIP flag
        registry_scip = repo_info.get("enable_scip", False)
        filesystem_scip = detected.get("scip", False)

        if registry_scip != filesystem_scip:
            logger.info(
                f"Reconciling SCIP flag for {alias_name}: "
                f"registry={registry_scip} -> filesystem={filesystem_scip}"
            )
            self.registry.update_enable_scip(alias_name, filesystem_scip)
