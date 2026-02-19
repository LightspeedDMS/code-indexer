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

        # Write-lock registry for external writers (Story #227).
        # Separate from _repo_locks: these coordinate between external writers
        # (DependencyMapService, LangfuseTraceSyncService) and the snapshotter.
        # Keyed by repo alias without -global suffix (e.g., "cidx-meta").
        self._write_locks: dict[str, threading.Lock] = {}
        self._write_locks_guard = threading.Lock()  # Protects _write_locks dict creation

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
    # Write-lock registry (Story #227)
    # ------------------------------------------------------------------

    def _get_or_create_write_lock(self, alias: str) -> threading.Lock:
        """Get or create a write lock for a repo alias (thread-safe)."""
        with self._write_locks_guard:
            if alias not in self._write_locks:
                self._write_locks[alias] = threading.Lock()
            return self._write_locks[alias]

    def acquire_write_lock(self, alias: str) -> bool:
        """
        Non-blocking acquire of the write lock for a repo alias.

        Args:
            alias: Repo alias without -global suffix (e.g., "cidx-meta")

        Returns:
            True if lock was acquired, False if already held
        """
        lock = self._get_or_create_write_lock(alias)
        return lock.acquire(blocking=False)

    def release_write_lock(self, alias: str) -> None:
        """
        Release the write lock for a repo alias.

        Args:
            alias: Repo alias without -global suffix (e.g., "cidx-meta")
        """
        lock = self._write_locks.get(alias)
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                logger.warning(
                    f"Attempted to release unheld write lock for '{alias}'"
                )

    def is_write_locked(self, alias: str) -> bool:
        """
        Check whether the write lock for a repo alias is currently held.

        Uses a non-blocking acquire probe: if we can acquire it, it was free
        (release immediately and return False); if we cannot, it is held (True).

        Args:
            alias: Repo alias without -global suffix (e.g., "cidx-meta")

        Returns:
            True if write lock is held, False otherwise
        """
        lock = self._write_locks.get(alias)
        if lock is None:
            return False
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
            return False
        return True

    def write_lock(self, alias: str):
        """
        Context manager that acquires the write lock on entry and releases on exit.

        Usage::

            with scheduler.write_lock("cidx-meta"):
                # write files here
                ...

        Args:
            alias: Repo alias without -global suffix (e.g., "cidx-meta")
        """
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            acquired = self.acquire_write_lock(alias)
            if not acquired:
                raise RuntimeError(f"Write lock for '{alias}' is already held")
            try:
                yield
            finally:
                self.release_write_lock(alias)

        return _ctx()

    def trigger_refresh_for_repo(self, alias_name: str) -> None:
        """
        Request a refresh for a specific repo after external writes complete.

        Routes through BackgroundJobManager if available (server mode with dashboard
        visibility), otherwise falls back to direct _execute_refresh() (CLI mode).

        Args:
            alias_name: Global alias name (e.g., "cidx-meta-global")
        """
        if self.background_job_manager:
            self._submit_refresh_job(alias_name)
        else:
            self._execute_refresh(alias_name)

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
                # Get all registered global repos
                repos = self.registry.list_global_repos()

                for repo in repos:
                    if not self._running:
                        break

                    alias_name = repo.get("alias_name")
                    if alias_name:
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

    def _submit_refresh_job(self, alias_name: str) -> Optional[str]:
        """
        Submit a refresh job to BackgroundJobManager.

        If no BackgroundJobManager is configured (CLI mode), falls back to
        direct execution via _execute_refresh().

        Args:
            alias_name: Global alias name (e.g., "my-repo-global")

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
            submitter_username="system",
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

                # Get golden repo path from alias (registry path becomes stale after refresh)
                golden_repo_path = current_target

                # AC6: Reconcile registry with filesystem at START of refresh
                # This ensures registry flags reflect actual index state before refresh begins
                detected_indexes = self._detect_existing_indexes(Path(golden_repo_path))
                self._reconcile_registry_with_filesystem(alias_name, detected_indexes)
                logger.info(
                    f"Reconciled registry with filesystem at START for {alias_name}: {detected_indexes}"
                )

                if is_local_repo:
                    # C3: For local repos, source_path is the LIVE directory (where writers put files),
                    # NOT the current alias target which may point to a versioned snapshot.
                    repo_name = alias_name.replace("-global", "")
                    source_path = str(self.golden_repos_dir / repo_name)

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
                    # Git repo: use GitPullUpdater for change detection and pull
                    updater = GitPullUpdater(golden_repo_path)

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

                    # Pull latest changes
                    logger.info(f"Pulling latest changes for {alias_name}")
                    updater.update()
                    source_path = updater.get_source_path()

                # Create new versioned index
                new_index_path = self._create_new_index(
                    alias_name=alias_name, source_path=source_path
                )

                # Swap alias to new index
                logger.info(f"Swapping alias {alias_name} to new index")
                self.alias_manager.swap_alias(
                    alias_name=alias_name,
                    new_target=new_index_path,
                    old_target=current_target,
                )

                # Schedule cleanup of old index
                logger.info(f"Scheduling cleanup of old index: {current_target}")
                self.cleanup_manager.schedule_cleanup(current_target)

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

    def _create_new_index(self, alias_name: str, source_path: str) -> str:
        """
        Create a new versioned index directory with CoW clone and indexing.

        Complete workflow:
        1. Create .versioned/{repo_name}/v_{timestamp}/ directory structure
        2. Perform CoW clone using cp --reflink=auto -a
        3. Fix git status (git update-index --refresh, git restore .)
        4. Run cidx fix-config --force
        5. Run cidx index to create indexes
        6. Validate index exists before returning
        7. Return path only if validation passes

        Args:
            alias_name: Global alias name (e.g., "my-repo-global")
            source_path: Path to source repository (golden repo)

        Returns:
            Path to new index directory (only if validation passes)

        Raises:
            RuntimeError: If any step fails (with cleanup of partial artifacts)
        """
        # Get timeouts from resource config or use defaults
        cow_timeout = 600  # Default: 10 minutes
        git_update_timeout = 300  # Default: 5 minutes
        git_restore_timeout = 300  # Default: 5 minutes
        cidx_fix_timeout = 60  # Default: 1 minute
        cidx_index_timeout = 3600  # Default: 1 hour

        if self.resource_config:
            cow_timeout = self.resource_config.cow_clone_timeout
            git_update_timeout = self.resource_config.git_update_index_timeout
            git_restore_timeout = self.resource_config.git_restore_timeout
            cidx_fix_timeout = self.resource_config.cidx_fix_config_timeout
            cidx_index_timeout = self.resource_config.cidx_index_timeout

        # Generate version timestamp (use time.time() for correct UTC epoch;
        # datetime.utcnow().timestamp() is wrong on non-UTC servers due to
        # naive datetime timezone interpretation)
        timestamp = int(time.time())
        version = f"v_{timestamp}"

        # Create versioned directory path
        repo_name = alias_name.replace("-global", "")
        versioned_base = self.golden_repos_dir / ".versioned" / repo_name
        versioned_path = versioned_base / version

        logger.info(f"Creating new versioned index at: {versioned_path}")

        try:
            # Step 1: Create versioned directory structure
            versioned_base.mkdir(parents=True, exist_ok=True)

            # Step 2: Perform CoW clone
            logger.info(f"CoW cloning from {source_path} to {versioned_path}")
            try:
                result = subprocess.run(
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
                    exc_info=True
                )
                raise RuntimeError(f"CoW clone failed for {alias_name}: {type(e).__name__}: {e.stderr}")
            except subprocess.TimeoutExpired as e:
                logger.error(
                    f"CoW clone timed out for {alias_name} after {cow_timeout} seconds: {type(e).__name__}",
                    exc_info=True
                )
                raise RuntimeError(f"CoW clone timed out for {alias_name} after {cow_timeout} seconds: {type(e).__name__}")

            # Step 2b: Delete inherited tantivy FTS index to prevent ghost vectors.
            # The CoW clone copies .code-indexer/tantivy_index/ from the source,
            # which may contain entries for files no longer present in this snapshot.
            # Deleting it here forces cidx index --fts to rebuild from scratch.
            tantivy_dir = versioned_path / ".code-indexer" / "tantivy_index"
            if tantivy_dir.exists():
                shutil.rmtree(tantivy_dir, ignore_errors=True)
                if not tantivy_dir.exists():
                    logger.info(
                        f"Deleted inherited tantivy index for clean FTS rebuild: {tantivy_dir}"
                    )
                else:
                    logger.warning(
                        f"Failed to fully delete inherited tantivy index: {tantivy_dir}"
                    )

            # Step 3: Fix git status (only if .git exists)
            git_dir = versioned_path / ".git"
            if git_dir.exists():
                # Step 3a: git update-index --refresh
                logger.info("Running git update-index --refresh to fix CoW timestamps")
                try:
                    result = subprocess.run(
                        ["git", "update-index", "--refresh"],
                        cwd=str(versioned_path),
                        capture_output=True,
                        text=True,
                        timeout=git_update_timeout,
                        check=False,  # Non-fatal - may show modified files
                    )
                    if result.returncode != 0:
                        logger.debug(f"git update-index output: {result.stderr}")
                except subprocess.TimeoutExpired:
                    logger.warning(
                        f"git update-index timed out after {git_update_timeout} seconds"
                    )

                # Step 3b: git restore .
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

            # Step 4: Run cidx fix-config --force
            logger.info("Running cidx fix-config --force to update paths")
            try:
                result = subprocess.run(
                    ["cidx", "fix-config", "--force"],
                    cwd=str(versioned_path),
                    capture_output=True,
                    text=True,
                    timeout=cidx_fix_timeout,
                    check=True,
                )
                logger.info("cidx fix-config completed successfully")
            except subprocess.CalledProcessError as e:
                logger.error(
                    f"cidx fix-config failed for {alias_name}: {type(e).__name__}: {e.stderr}",
                    exc_info=True
                )
                raise RuntimeError(f"cidx fix-config failed for {alias_name}: {type(e).__name__}: {e.stderr}")
            except subprocess.TimeoutExpired as e:
                logger.error(
                    f"cidx fix-config timed out for {alias_name} after {cidx_fix_timeout} seconds: {type(e).__name__}",
                    exc_info=True
                )
                raise RuntimeError(
                    f"cidx fix-config timed out for {alias_name} after {cidx_fix_timeout} seconds: {type(e).__name__}"
                )

            # Step 5: Run cidx index for semantic + FTS (always required)
            # Note: --index-commits ONLY does temporal indexing, not semantic+FTS
            # So we need two separate cidx index calls: one for semantic+FTS, one for temporal
            index_command = ["cidx", "index", "--fts"]

            logger.info(
                f"Running cidx index for semantic+FTS: {' '.join(index_command)}"
            )
            try:
                result = subprocess.run(
                    index_command,
                    cwd=str(versioned_path),
                    capture_output=True,
                    text=True,
                    timeout=cidx_index_timeout,
                    check=True,
                )
                logger.info("cidx index (semantic+FTS) completed successfully")
            except subprocess.CalledProcessError as e:
                logger.error(
                    f"Indexing (semantic+FTS) failed for {alias_name}: {type(e).__name__}: {e.stderr}",
                    exc_info=True
                )
                raise RuntimeError(f"Indexing (semantic+FTS) failed for {alias_name}: {type(e).__name__}: {e.stderr}")
            except subprocess.TimeoutExpired as e:
                logger.error(
                    f"Indexing (semantic+FTS) timed out for {alias_name} after {cidx_index_timeout} seconds: {type(e).__name__}",
                    exc_info=True
                )
                raise RuntimeError(
                    f"Indexing (semantic+FTS) timed out for {alias_name} after {cidx_index_timeout} seconds: {type(e).__name__}"
                )

            # Step 5b: Run cidx index --index-commits for temporal indexing (if enabled)
            # Read temporal settings from registry
            repo_info = self.registry.get_global_repo(alias_name)
            enable_temporal = (
                repo_info.get("enable_temporal", False) if repo_info else False
            )
            temporal_options = repo_info.get("temporal_options") if repo_info else None

            # Skip temporal indexing for local:// repos - they have no git history
            repo_url_for_temporal = repo_info.get("repo_url", "") if repo_info else ""
            is_local_repo_for_temporal = repo_url_for_temporal.startswith("local://") if repo_url_for_temporal else False
            if enable_temporal and is_local_repo_for_temporal:
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
                    f"Running cidx index for temporal: {' '.join(temporal_command)}"
                )
                try:
                    result = subprocess.run(
                        temporal_command,
                        cwd=str(versioned_path),
                        capture_output=True,
                        text=True,
                        timeout=cidx_index_timeout,
                        check=True,
                    )
                    logger.info("cidx index (temporal) completed successfully")
                except subprocess.CalledProcessError as e:
                    logger.error(
                        f"Temporal indexing failed for {alias_name}: {type(e).__name__}: {e.stderr}",
                        exc_info=True
                    )
                    raise RuntimeError(f"Temporal indexing failed for {alias_name}: {type(e).__name__}: {e.stderr}")
                except subprocess.TimeoutExpired as e:
                    logger.error(
                        f"Temporal indexing timed out for {alias_name} after {cidx_index_timeout} seconds: {type(e).__name__}",
                        exc_info=True
                    )
                    raise RuntimeError(
                        f"Temporal indexing timed out for {alias_name} after {cidx_index_timeout} seconds: {type(e).__name__}"
                    )

            # Step 5c: Run cidx scip generate for code intelligence indexing (if enabled)
            # Read SCIP settings from registry
            enable_scip = (
                repo_info.get("enable_scip", False) if repo_info else False
            )

            if enable_scip:
                # Get SCIP timeout from resource config or use default (AC4)
                scip_timeout = 1800  # Default: 30 minutes
                if self.resource_config:
                    scip_timeout = getattr(
                        self.resource_config, "cidx_scip_generate_timeout", 1800
                    )

                scip_command = ["cidx", "scip", "generate"]
                logger.info(f"SCIP indexing enabled for {alias_name}")

                logger.info(
                    f"Running cidx scip generate: {' '.join(scip_command)}"
                )
                try:
                    result = subprocess.run(
                        scip_command,
                        cwd=str(versioned_path),
                        capture_output=True,
                        text=True,
                        timeout=scip_timeout,
                        check=True,
                    )
                    logger.info("cidx scip generate completed successfully")
                except subprocess.CalledProcessError as e:
                    # AC5: SCIP failures should raise RuntimeError
                    logger.error(
                        f"SCIP indexing failed for {alias_name}: {type(e).__name__}: {e.stderr}",
                        exc_info=True
                    )
                    raise RuntimeError(f"SCIP indexing failed for {alias_name}: {type(e).__name__}: {e.stderr}")
                except subprocess.TimeoutExpired as e:
                    # AC5: SCIP timeout should raise RuntimeError
                    logger.error(
                        f"SCIP indexing timed out for {alias_name} after {scip_timeout} seconds: {type(e).__name__}",
                        exc_info=True
                    )
                    raise RuntimeError(
                        f"SCIP indexing timed out for {alias_name} after {scip_timeout} seconds: {type(e).__name__}"
                    )

            # Step 6: Validate index exists
            index_dir = versioned_path / ".code-indexer" / "index"
            if not index_dir.exists():
                logger.error(f"Index validation failed: {index_dir} does not exist")
                raise RuntimeError(
                    "Index validation failed: index directory not created"
                )

            logger.info(
                f"New versioned index created successfully at: {versioned_path}"
            )
            return str(versioned_path)

        except Exception as e:
            # Step 7: Cleanup partial artifacts on failure
            logger.error(
                f"Failed to create new index for {alias_name}, cleaning up: {type(e).__name__}: {e}",
                exc_info=True
            )
            if versioned_path.exists():
                try:
                    shutil.rmtree(versioned_path)
                    logger.info(f"Cleaned up partial index at: {versioned_path}")
                except Exception as cleanup_error:
                    logger.error(
                        f"Failed to cleanup partial index for {alias_name}: {type(cleanup_error).__name__}: {cleanup_error}",
                        exc_info=True
                    )

            # Re-raise with context
            raise RuntimeError(f"Failed to create new index for {alias_name}: {type(e).__name__}: {e}")

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

        has_changes = int(max_mtime) > latest_timestamp
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
