"""
Refresh Scheduler for timer-triggered global repo updates.

Orchestrates the complete refresh cycle: timer triggers git pull,
change detection, index creation, alias swap, and cleanup scheduling.
"""

import json
import logging
import os
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    NoReturn,
    Optional,
    Union,
    TYPE_CHECKING,
    cast,
)

from code_indexer.config import ConfigManager
from .alias_manager import AliasManager
from .git_error_classifier import GitFetchError
from code_indexer.global_repos.orphaned_repo_error import OrphanedRepoError
from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env
from .git_pull_updater import GitPullUpdater
from .meta_directory_updater import MetaDirectoryUpdater
from .update_strategy import UpdateStrategy
from .query_tracker import QueryTracker
from .cleanup_manager import CleanupManager
from .shared_operations import DEFAULT_REFRESH_INTERVAL, GlobalRepoOperations
from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.repositories.golden_repo_manager import (
    _make_hnsw_orphan_event_logger,
)
from code_indexer.server.services.cidx_meta_backup import (
    CidxMetaBackupBootstrap,
    ClaudeConflictResolver,
    CidxMetaBackupSync,
    detect_default_branch,
)
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.db_outage_throttle import DbOutageThrottle
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend
from code_indexer.server.storage.shared.nfs_visibility import (
    _configured_visibility_timeout,
    wait_for_nfs_visibility,
)
from code_indexer.server.utils.config_manager import ServerResourceConfig
from code_indexer.utils.subprocess_env import build_cidx_subprocess_env

if TYPE_CHECKING:
    from code_indexer.server.repositories.background_jobs import BackgroundJobManager
    from code_indexer.server.services.job_tracker import JobTracker
    from code_indexer.server.storage.shared.snapshot_manager import (
        VersionedSnapshotManager,
    )

logger = logging.getLogger(__name__)

# Git URL prefixes — any repo_url NOT starting with one of these is treated as
# a local repo and is completely excluded from the scheduled auto-refresh cycle.
# Local repos are only refreshed via explicit trigger_refresh_for_repo() calls.
_GIT_URL_PREFIXES = ("https://", "http://", "git@", "ssh://", "git://")


def has_files_with_extensions(
    repo_path: str, extensions: set, exclude_dirs: set
) -> bool:
    """Return True as soon as a file with a matching extension is found (short-circuit).

    Walks the directory tree rooted at repo_path. Directories listed in exclude_dirs
    or whose names start with '.' are pruned and never descended into.

    Args:
        repo_path: Root directory to scan.
        extensions: Set of bare extension names to match (without leading dots, e.g. 'py').
        exclude_dirs: Set of directory names to skip (e.g. {'.git', 'node_modules'}).

    Returns:
        True if any file with a matching extension is found, False otherwise.
    """
    if not extensions:
        return False
    for dirpath, dirnames, filenames in os.walk(repo_path, topdown=True):
        dirnames[:] = [
            d for d in dirnames if d not in exclude_dirs and not d.startswith(".")
        ]
        for filename in filenames:
            ext = os.path.splitext(filename)[1].lstrip(".")
            if ext in extensions:
                return True
    return False


def _read_max_commits_from_temporal_meta(source_path: Path) -> Optional[int]:
    """Read max_commits from temporal_meta.json when temporal_options is NULL.

    Bug #642 Step 2 safety net: after path migration the DB temporal_options may
    be NULL even though the index was originally created with a commit limit.
    Scan both the legacy path and any provider-aware path for temporal_meta.json
    and return the best available value.

    Priority:
    1. 'max_commits' field (written by Bug #642 Step 3 fix)
    2. 'total_commits' field as conservative fallback

    Returns:
        max_commits integer if found, None otherwise.
    """
    import json as _json

    index_dir = source_path / ".code-indexer" / "index"
    if not index_dir.is_dir():
        return None

    # Candidate directory names: legacy first, then any provider-aware dirs
    candidate_dirs = []
    legacy = index_dir / "code-indexer-temporal"
    if legacy.is_dir():
        candidate_dirs.append(legacy)

    for entry in index_dir.iterdir():
        if entry.is_dir() and entry.name.startswith("code-indexer-temporal-"):
            candidate_dirs.append(entry)

    for candidate in candidate_dirs:
        meta_file = candidate / "temporal_meta.json"
        if not meta_file.exists():
            continue
        try:
            meta = _json.loads(meta_file.read_text())
            # Prefer the explicit max_commits field (Step 3 fix)
            if meta.get("max_commits") is not None:
                return int(meta["max_commits"])
            # Conservative fallback: total_commits as upper bound
            if meta.get("total_commits") is not None:
                return int(meta["total_commits"])
        except (OSError, UnicodeDecodeError, _json.JSONDecodeError, ValueError) as exc:
            logger.debug(
                "Bug #642: failed reading temporal_meta.json from %s: %s",
                meta_file,
                exc,
            )
            continue

    return None


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
        registry: Optional["GlobalRegistry"] = None,  # type: ignore[name-defined]  # noqa: F821
        job_tracker: Optional["JobTracker"] = None,
        snapshot_manager: Optional["VersionedSnapshotManager"] = None,
        golden_repo_metadata_backend: Optional[Any] = None,
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
            job_tracker: Optional JobTracker for drain-status visibility (Bug #935). When provided,
                each in-flight _execute_refresh call registers itself so drain-status sees it.
                When None (CLI mode), no registration occurs.
            snapshot_manager: Optional VersionedSnapshotManager for CoW snapshot coordination
                (Commit 1 injection point — stored, used in later commits).
            golden_repo_metadata_backend: Optional GoldenRepoMetadataBackend instance
                (for testing/production wiring); if None, resolution is deferred to the
                `golden_repo_metadata` property below (Bug #1390), mirroring `registry`'s
                Bug #1308 deferred-resolution pattern. Needed so
                _reconcile_registry_with_filesystem() can update the bare-alias-keyed
                golden_repos_metadata table alongside the -global-alias-keyed
                global_repos table (self.registry) -- the two are structurally separate
                stores for the same logical repo and can otherwise drift independently.
        """
        self.golden_repos_dir = Path(golden_repos_dir)
        self.config_source = config_source
        self.query_tracker = query_tracker
        self.cleanup_manager = cleanup_manager
        self.resource_config = resource_config
        self.background_job_manager = background_job_manager
        self._job_tracker = job_tracker
        self._snapshot_manager = snapshot_manager

        # Initialize managers
        self.alias_manager = AliasManager(str(self.golden_repos_dir / "aliases"))

        # Registry resolution (Bug #1308): an explicitly injected registry
        # (testing) is cached immediately. Otherwise resolution is DEFERRED
        # to the `registry` property below instead of eagerly binding a
        # per-node SQLite GlobalRegistry here. Eager construction-time
        # binding split-brained cluster refresh against the shared
        # PostgreSQL registry that the read/list path already used, because
        # app.state.backend_registry is not guaranteed to be populated yet
        # at construction time during server startup.
        self._registry = registry
        self._registry_lock = threading.Lock()

        # golden_repo_metadata resolution (Bug #1390): same deferred-resolution
        # shape as `registry` above, for the structurally separate
        # golden_repos_metadata table (bare-alias-keyed).
        self._golden_repo_metadata_backend = golden_repo_metadata_backend
        self._golden_repo_metadata_lock = threading.Lock()

        # Thread management
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()  # Event-based signaling for efficient stop

        # Bug #1249: collapse a PG-outage error storm into a single ERROR +
        # DEBUG follow-ups instead of logging a fresh traceback on every
        # per-repo _submit_refresh_job() failure during a PG outage. This is
        # SEPARATE from the Bug #735 outer circuit breaker (whole-iteration
        # failures) below — that one is untouched.
        self._db_throttle = DbOutageThrottle(service_name="RefreshScheduler")

        # Per-repo locking for concurrent refresh serialization
        self._repo_locks: dict[str, threading.Lock] = {}
        self._repo_locks_lock = threading.Lock()  # Protects _repo_locks dict

        # File-based write-lock manager for external writers (Story #230).
        # Replaces the in-memory threading.Lock registry from Story #227.
        # Keyed by repo alias without -global suffix (e.g., "cidx-meta").
        from .write_lock_manager import WriteLockManager

        self.write_lock_manager = WriteLockManager(
            golden_repos_dir=self.golden_repos_dir
        )

        # Story #295: Per-alias consecutive fetch failure counters and re-clone
        # cooldown timestamps.  In-memory only — reset on server restart is fine
        # since transient counters are ephemeral by nature.
        self._fetch_failure_counts: Dict[str, int] = {}
        self._reclone_cooldowns: Dict[str, float] = {}

    def _get_registry_lock(self) -> threading.Lock:
        """
        Return the registry resolution lock, creating it lazily if absent.

        Some tests construct RefreshScheduler via `object.__new__(RefreshScheduler)`
        / `RefreshScheduler.__new__(...)` to build a lightweight instance without
        running __init__ (deliberate pattern used across
        tests/unit/golden_repos/), so `_registry_lock` may not exist as an
        instance attribute yet. Falling back to getattr()+lazy-create here
        (instead of assuming __init__ always ran) preserves that pattern while
        keeping Bug #1308's deferred resolution.
        """
        lock = getattr(self, "_registry_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._registry_lock = lock
        return lock

    @property
    def registry(self) -> Any:
        """
        Lazily resolve the GlobalRegistry / PostgresGlobalRegistryAdapter.

        Bug #1308: mirrors GlobalRepoOperations.registry (shared_operations.py)
        -- an explicitly injected registry (test double) is returned as-is;
        otherwise resolution is deferred to first access (not __init__) so
        app.state.backend_registry is guaranteed to be populated in
        postgres/cluster mode. Falls back to the per-node SQLite
        GlobalRegistry in solo/CLI mode (no app.state), preserving existing
        behavior byte-for-byte. Result is cached after first successful
        resolution; in postgres mode with backend not yet available, the
        result is NOT cached so the next access re-checks.

        Uses getattr(self, "_registry", None) rather than assuming __init__
        ran, so instances built via object.__new__(RefreshScheduler) (a
        deliberate lightweight-construction pattern used by several existing
        tests) don't raise AttributeError before _registry is ever set.
        Likewise, `golden_repos_dir` is read via getattr() rather than direct
        attribute access: in postgres/cluster mode the resolved backend makes
        golden_repos_dir irrelevant, so a bare uninitialized instance must
        still resolve cleanly in that mode. Only when no backend is available
        AND golden_repos_dir was never set do we raise -- with a clear,
        explicit RuntimeError instead of the incidental AttributeError that
        used to leak out of self.golden_repos_dir.
        """
        existing = getattr(self, "_registry", None)
        if existing is not None:
            return existing

        with self._get_registry_lock():
            existing = getattr(self, "_registry", None)
            if existing is not None:
                return existing

            # Lazy import to avoid circular dependency (Story #713)
            from code_indexer.server.utils.registry_factory import (
                get_server_global_registry,
                resolve_backend_registry_state,
            )

            backend, postgres_mode_without_backend = resolve_backend_registry_state(
                caller_name="RefreshScheduler"
            )
            golden_repos_dir = getattr(self, "golden_repos_dir", None)
            if backend is None and golden_repos_dir is None:
                raise RuntimeError(
                    "RefreshScheduler.registry accessed before initialization: "
                    "no golden_repos_dir and no cluster backend available"
                )
            resolved = get_server_global_registry(
                str(golden_repos_dir) if golden_repos_dir is not None else "",
                backend=backend,
            )

            if not postgres_mode_without_backend:
                self._registry = resolved

            return resolved

    @registry.setter
    def registry(self, value: Any) -> None:
        """Allow explicit (re-)injection, e.g. by tests that set `.registry` post-construction."""
        with self._get_registry_lock():
            self._registry = value

    def _get_golden_repo_metadata_lock(self) -> threading.Lock:
        """
        Return the golden_repo_metadata resolution lock, creating it lazily if absent.

        Mirrors `_get_registry_lock()`: some tests construct RefreshScheduler via
        `object.__new__(RefreshScheduler)` without running __init__, so
        `_golden_repo_metadata_lock` may not exist as an instance attribute yet.
        """
        lock = getattr(self, "_golden_repo_metadata_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._golden_repo_metadata_lock = lock
        return lock

    @property
    def golden_repo_metadata(self) -> Any:
        """
        Lazily resolve the golden_repos_metadata backend (bare-alias-keyed table).

        Bug #1390: mirrors the `registry` property's Bug #1308-hardened deferred
        resolution, for the structurally SEPARATE golden_repos_metadata table.
        `_reconcile_registry_with_filesystem` must update BOTH this table
        (bare alias) AND `self.registry`'s global_repos table (-global-suffixed
        alias) for the same logical repo -- Bug #1373's `_set_enable_temporal_flag`
        (server/mcp/handlers/repos.py) established this same dual-write
        requirement for the explicit-enable path; this property gives the
        filesystem-reconciliation path the same access.

        An explicitly injected backend (test double, or production wiring via
        `golden_repo_metadata_backend=` at construction) is returned as-is.
        Otherwise resolution is deferred to first access (not __init__) for the
        same reason as `registry`: app.state.backend_registry is not guaranteed
        populated yet at server-startup construction time in postgres/cluster
        mode. Falls back to a per-node SQLite GoldenRepoMetadataSqliteBackend in
        solo/CLI mode (no app.state) or when postgres backend_registry is not yet
        available -- in the latter case the result is NOT cached so the next
        access re-checks (identical caching contract to `registry`).

        Uses getattr(self, "_golden_repo_metadata_backend", None) rather than
        assuming __init__ ran, matching `registry`'s tolerance of bare
        object.__new__(RefreshScheduler) instances.
        """
        existing = getattr(self, "_golden_repo_metadata_backend", None)
        if existing is not None:
            return existing

        with self._get_golden_repo_metadata_lock():
            existing = getattr(self, "_golden_repo_metadata_backend", None)
            if existing is not None:
                return existing

            # Lazy import to avoid circular dependency (mirrors `registry` above)
            from code_indexer.server.utils.registry_factory import (
                get_server_golden_repo_metadata_backend,
                resolve_backend_registry_attr,
            )

            backend, postgres_mode_without_backend = resolve_backend_registry_attr(
                "golden_repo_metadata", caller_name="RefreshScheduler"
            )
            golden_repos_dir = getattr(self, "golden_repos_dir", None)
            if backend is None and golden_repos_dir is None:
                raise RuntimeError(
                    "RefreshScheduler.golden_repo_metadata accessed before "
                    "initialization: no golden_repos_dir and no cluster backend "
                    "available"
                )
            # Server data dir is the parent of golden_repos_dir (mirrors
            # get_server_global_registry's own golden_repos_dir.parent
            # derivation for the SQLite db path).
            server_data_dir = (
                str(Path(golden_repos_dir).parent)
                if golden_repos_dir is not None
                else ""
            )
            resolved = get_server_golden_repo_metadata_backend(
                server_data_dir, backend=backend
            )

            if not postgres_mode_without_backend:
                self._golden_repo_metadata_backend = resolved

            return resolved

    @golden_repo_metadata.setter
    def golden_repo_metadata(self, value: Any) -> None:
        """Allow explicit (re-)injection, e.g. by tests that set `.golden_repo_metadata` post-construction."""
        with self._get_golden_repo_metadata_lock():
            self._golden_repo_metadata_backend = value

    def _is_versioned_snapshot(self, path: str) -> bool:
        """Return True when *path* is a versioned snapshot (Bug #1084 Phase A4).

        Delegates to the wired :class:`VersionedSnapshotManager` facade (which
        knows the backend mount point and recognizes both canonical and legacy
        cow-daemon shapes). When no snapshot_manager is wired (e.g. unit tests in
        pure-local mode), falls back to the module-level canonical predicate,
        which still recognizes the local ``.versioned`` layout.
        """
        if self._snapshot_manager is not None:
            return bool(self._snapshot_manager.is_versioned_snapshot(path))
        from code_indexer.server.storage.shared.snapshot_paths import (
            is_versioned_snapshot,
        )

        return bool(is_versioned_snapshot(path))

    #: Fallback keep-last-N when the configured value is missing or invalid.
    _DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST = 3

    def _retention_keep_last(self) -> int:
        """Return the configured keep-last-N, falling back to the safe default.

        A value < 1 would schedule EVERY snapshot for deletion (including the
        live one once it ages out), so non-positive / unreadable values fall back
        to :attr:`_DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST`.
        """
        try:
            keep = int(get_config_service().get_config().snapshot_retention_keep_last)
        except Exception:
            return self._DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST
        if keep < 1:
            return self._DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST
        return keep

    def _latest_versioned_timestamp(self, alias_name: str) -> Optional[int]:
        """Return the newest snapshot timestamp for *alias_name*, or None (Bug #1084 A7).

        Uses the discovery API (:meth:`VersionedSnapshotManager.list_snapshots`)
        when a snapshot_manager is wired — this is backend-correct on cow-daemon
        where snapshots live under the NFS mount, NOT under
        ``golden_repos_dir/.versioned``. When no snapshot_manager is wired (unit
        tests / pure-local), falls back to globbing the local ``.versioned``
        directory so existing local behavior is preserved.
        """
        if self._snapshot_manager is not None:
            try:
                snaps = self._snapshot_manager.list_snapshots(alias_name)
            except Exception as exc:
                logger.warning(
                    f"_latest_versioned_timestamp: discovery failed for "
                    f"{alias_name} (non-fatal): {type(exc).__name__}: {exc}"
                )
                return None
            if not snaps:
                return None
            # snaps sorted ascending by ts; last is newest.
            return int(snaps[-1][1])

        # Local fallback: glob golden_repos_dir/.versioned/{repo}/v_*.
        repo_name = alias_name.removesuffix("-global")
        versioned_base = self.golden_repos_dir / ".versioned" / repo_name
        if not versioned_base.exists():
            return None
        timestamps = []
        for d in versioned_base.iterdir():
            if d.is_dir() and d.name.startswith("v_"):
                try:
                    timestamps.append(int(d.name[2:]))
                except (ValueError, IndexError):
                    continue
        return max(timestamps) if timestamps else None

    def _enforce_retention(self, alias_name: str, current_target: str) -> None:
        """Schedule deletion of superseded snapshots beyond keep-last-N (Bug #1084 A6).

        After a successful swap, lists snapshots via the discovery API and
        schedules (through the refcount-gated CleanupManager) every snapshot
        EXCEPT: the N newest, the current alias ``target_path``, and the alias
        ``previous_path``. Enabled on local + cow-daemon; on ONTAP the discovery
        API returns ``[]`` so this is naturally inert. Non-fatal: any failure is
        logged and swallowed so a refresh never fails on retention.
        """
        if self._snapshot_manager is None:
            return
        try:
            keep_last = self._retention_keep_last()
            snapshots = self._snapshot_manager.list_snapshots(alias_name)
            if len(snapshots) <= keep_last:
                return

            # Force-keep set: current target + previous_path (rollback) + N newest.
            protected: set = set()
            if current_target:
                protected.add(current_target)
            previous_path = self.alias_manager.get_previous_path(alias_name)
            if previous_path:
                protected.add(previous_path)
            # snapshots are sorted ascending by ts; the last keep_last are newest.
            for path, _ts in snapshots[-keep_last:]:
                protected.add(path)

            for path, _ts in snapshots:
                if path not in protected:
                    logger.info(
                        f"[retention] Scheduling cleanup of superseded snapshot "
                        f"{path} (keep_last={keep_last}) for {alias_name}"
                    )
                    self.cleanup_manager.schedule_cleanup(path)
        except Exception as exc:
            logger.warning(
                f"[retention] keep-last-N enforcement failed for {alias_name} "
                f"(non-fatal): {type(exc).__name__}: {exc}"
            )

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
    # Story #295: Auto-recovery for corrupted git object databases
    # ------------------------------------------------------------------

    # After this many consecutive transient failures the scheduler escalates
    # to a full re-clone.  Corruption always triggers immediate re-clone.
    MAX_TRANSIENT_FAILURES: int = 3

    # After a re-clone attempt (success or failure) wait at least this long
    # before trying again.
    RECLONE_COOLDOWN_SECONDS: int = 3600  # 1 hour

    # Timeout for the git clone subprocess during auto re-clone.
    CLONE_TIMEOUT_SECONDS: int = 300  # 5 minutes

    # ------------------------------------------------------------------
    # Bug #1341: Exponential backoff for sustained fetch failures.
    #
    # A repo is NEVER removed from scheduling and NEVER reaches a
    # terminal/quarantine state -- every failure just pushes the next
    # attempt further into the future (via registry.update_next_refresh),
    # capped so it is always retried eventually, just less often while
    # broken. This directly fixes the #1341 complaint of retrying (and
    # re-escalating to re-clone) every single cycle forever.
    #
    # TRANSIENT/CORRUPTION errors keep today's immediate-retry cadence
    # (and re-clone escalation) for the first MAX_TRANSIENT_FAILURES
    # failures -- these are expected to recover on their own. Only once
    # sustained past that threshold does backoff engage, capped at the
    # same interval as RECLONE_COOLDOWN_SECONDS so fetch retries and
    # re-clone attempts settle at the same cadence.
    TRANSIENT_BACKOFF_BASE_SECONDS: int = 60  # 1 minute
    TRANSIENT_BACKOFF_CAP_SECONDS: int = 3600  # 1 hour

    # PERMANENT errors (access revoked / repo deleted -- GitLab/GitHub
    # "not found or no permission") are NOT expected to recover quickly, so
    # backoff engages from the very first failure and caps much longer.
    # Re-clone is never attempted for a permanent error (see
    # _handle_fetch_error): re-cloning an inaccessible/nonexistent repo
    # cannot possibly succeed and would only waste a subprocess + network
    # round trip.
    PERMANENT_BACKOFF_BASE_SECONDS: int = 300  # 5 minutes
    PERMANENT_BACKOFF_CAP_SECONDS: int = 21600  # 6 hours

    # ------------------------------------------------------------------
    # Story #284: Back-propagating jitter for staggered refresh scheduling
    # ------------------------------------------------------------------

    # Jitter applied to each back-propagated next_refresh: +/- 10% of interval
    JITTER_PERCENTAGE: float = 0.10

    # Floor and ceiling for the short poll interval (interval/DIVISOR, clamped)
    MIN_POLL_SECONDS: int = 10
    MAX_POLL_SECONDS: int = 30

    # Divisor used to derive base poll interval from the refresh interval.
    # interval/20 gives a responsive check frequency (e.g., 3600s -> 180s -> clamped to 30s).
    POLL_INTERVAL_DIVISOR: int = 20

    def _calculate_jitter(self, refresh_interval_seconds: int) -> float:
        """
        Calculate random jitter within +/- JITTER_PERCENTAGE of interval.

        Story #284 AC3: jitter stays within +/- (N * 0.10) of refresh_interval.

        Args:
            refresh_interval_seconds: The configured refresh interval in seconds.

        Returns:
            Float jitter value in range [-max_jitter, +max_jitter].
        """
        max_jitter = refresh_interval_seconds * self.JITTER_PERCENTAGE
        return random.uniform(-max_jitter, max_jitter)

    def _count_active_refresh_jobs(self) -> int:
        """Return the number of active (PENDING/RUNNING) refresh jobs.

        Bug #1063 Part 1: delegates to BackgroundJobManager.count_active_refresh_jobs()
        when a manager is configured; returns 0 in CLI mode (no manager).
        """
        if self.background_job_manager is None:
            return 0
        return int(self.background_job_manager.count_active_refresh_jobs())

    def _get_refresh_budget(self) -> int:
        """Compute how many refresh jobs can be submitted this poll cycle.

        Bug #1063 Part 1: refresh budget = max_concurrent_refresh_jobs - active.
        When no BackgroundJobManager is configured (CLI mode), returns a large
        sentinel (all due repos can be submitted — CLI runs them synchronously).

        Returns:
            Non-negative integer: 0 means budget exhausted, >0 means slots available.
        """
        if self.background_job_manager is None:
            # CLI mode: no concurrency gate — return a large cap so list_due_repos
            # returns all due repos (same behaviour as before the fix).
            return 10_000

        # Retrieve max_concurrent_refresh_jobs from the manager's config
        refresh_limit = getattr(
            self.background_job_manager._background_jobs_config,
            "max_concurrent_refresh_jobs",
            max(1, self.background_job_manager.max_concurrent_jobs // 2),
        )
        active = self._count_active_refresh_jobs()
        return int(max(0, refresh_limit - active))

    def _calculate_poll_interval(self, refresh_interval_seconds: int) -> float:
        """
        Calculate the background loop poll interval.

        Uses interval/POLL_INTERVAL_DIVISOR clamped to [MIN_POLL_SECONDS, MAX_POLL_SECONDS].
        This gives a short, responsive poll interval that avoids burning CPU
        with too-frequent checks while still detecting due repos promptly.

        Args:
            refresh_interval_seconds: The configured refresh interval in seconds.

        Returns:
            Poll interval in seconds.
        """
        poll = refresh_interval_seconds / self.POLL_INTERVAL_DIVISOR
        return max(self.MIN_POLL_SECONDS, min(self.MAX_POLL_SECONDS, poll))

    def _assign_initial_spread(
        self, repos: List[Dict[str, Any]], refresh_interval_seconds: int
    ) -> None:
        """
        Evenly stagger repos across the interval from now.

        Story #284 AC2: repos with NULL next_refresh get evenly staggered offsets,
        NOT refreshed on first iteration. Each repo slot = spacing * (i+1) from now,
        where spacing = interval / count.

        Args:
            repos: List of repo dicts (must have 'alias_name' key).
            refresh_interval_seconds: The configured refresh interval in seconds.
        """
        count = len(repos)
        if count == 0:
            return
        spacing = refresh_interval_seconds / count
        now = time.time()
        for i, repo in enumerate(repos):
            offset = spacing * (i + 1)
            next_refresh = now + offset
            alias_name = repo.get("alias_name")
            if alias_name:
                self.registry.update_next_refresh(alias_name, next_refresh)

    def _reset_fetch_failures(self, alias_name: str) -> None:
        """Reset the consecutive fetch failure counter for an alias."""
        self._fetch_failure_counts[alias_name] = 0

    @staticmethod
    def _is_backoff_log_milestone(count: int) -> bool:
        """
        Return True when count is a power-of-two milestone (1, 2, 4, 8, 16, ...).

        Bug #1341 log-throttle: sustained fetch failures are ERROR-logged
        only at these milestones instead of on every single cycle, so a
        persistently broken upstream cannot flood the log (bounded to
        O(log N) ERROR lines over N consecutive failures) while the
        failure still surfaces periodically.
        """
        return count > 0 and (count & (count - 1)) == 0

    def _compute_backoff_seconds(
        self, category: str, consecutive_failures: int
    ) -> Optional[int]:
        """
        Compute the backoff delay (seconds) before the next scheduled attempt
        for a sustained fetch failure (Bug #1341), or None when the normal
        refresh-interval cadence applies unchanged (immediate retry).

        PERMANENT errors back off from the first failure (base
        PERMANENT_BACKOFF_BASE_SECONDS, doubling per failure, capped at
        PERMANENT_BACKOFF_CAP_SECONDS). TRANSIENT errors keep immediate
        retry for the first MAX_TRANSIENT_FAILURES-1 failures, then back
        off too (base TRANSIENT_BACKOFF_BASE_SECONDS, doubling, capped at
        TRANSIENT_BACKOFF_CAP_SECONDS). CORRUPTION/unknown are unaffected
        (returns None) -- out of scope for #1341.
        """
        if category == "permanent":
            exponent = max(0, consecutive_failures - 1)
            # int ** int is typed Any in typeshed (negative exponents yield
            # float) -- exponent is always >= 0 here, so int(...) is safe
            # and satisfies the declared Optional[int] return type.
            return int(
                min(
                    self.PERMANENT_BACKOFF_BASE_SECONDS * (2**exponent),
                    self.PERMANENT_BACKOFF_CAP_SECONDS,
                )
            )

        if (
            category == "transient"
            and consecutive_failures >= self.MAX_TRANSIENT_FAILURES
        ):
            exponent = consecutive_failures - self.MAX_TRANSIENT_FAILURES
            return int(
                min(
                    self.TRANSIENT_BACKOFF_BASE_SECONDS * (2**exponent),
                    self.TRANSIENT_BACKOFF_CAP_SECONDS,
                )
            )

        return None

    def _log_permanent_fetch_failure(
        self, alias_name: str, count: int, error: "GitFetchError"
    ) -> None:
        """Log a PERMANENT-classified fetch failure, milestone-throttled (Bug #1341)."""
        if self._is_backoff_log_milestone(count):
            logger.error(
                "Repo %s fetch failing with a PERMANENT error (consecutive=%d): "
                "%s -- will keep retrying at a growing backoff (capped at "
                "%ds) but will not recover without operator action: verify "
                "the upstream repository still exists and that credentials/"
                "access rights are valid.",
                alias_name,
                count,
                error.stderr.strip(),
                self.PERMANENT_BACKOFF_CAP_SECONDS,
            )
        else:
            logger.debug(
                "Repo %s still failing with a PERMANENT fetch error "
                "(consecutive=%d, ERROR log throttled until next milestone)",
                alias_name,
                count,
            )

    def _decide_non_permanent_reclone(
        self, alias_name: str, error: "GitFetchError", count: int, in_cooldown: bool
    ) -> bool:
        """
        Decide whether a TRANSIENT/CORRUPTION fetch error should trigger a
        re-clone attempt, logging appropriately (milestone-throttled ERROR
        once escalated). Pre-existing decision logic, unchanged by #1341.
        """
        if in_cooldown:
            logger.warning(
                f"Fetch failed for {alias_name} (category={error.category}, "
                f"consecutive={count}), but re-clone cooldown is active — skipping"
            )
            return False

        if error.category == "corruption":
            logger.error(
                f"Repo {alias_name} has corrupted git objects, initiating auto re-clone"
            )
            return True

        if count >= self.MAX_TRANSIENT_FAILURES:
            relative = count - self.MAX_TRANSIENT_FAILURES + 1
            if self._is_backoff_log_milestone(relative):
                logger.error(
                    f"Repo {alias_name} has {count} consecutive transient fetch failures, "
                    "escalating to auto re-clone"
                )
            else:
                logger.debug(
                    f"Repo {alias_name} still has {count} consecutive transient "
                    "fetch failures (ERROR log throttled until next milestone)"
                )
            return True

        logger.warning(
            f"Transient fetch failure #{count} for {alias_name} "
            f"(threshold={self.MAX_TRANSIENT_FAILURES}): {error.stderr}"
        )
        return False

    def _handle_non_permanent_fetch_error(
        self,
        alias_name: str,
        repo_url: str,
        master_path: str,
        error: "GitFetchError",
        count: int,
    ) -> None:
        """
        Handle TRANSIENT/CORRUPTION fetch errors -- pre-existing behavior,
        unchanged by Bug #1341 (expected to recover via retry/re-clone).
        """
        now = time.monotonic()
        cooldown_until = self._reclone_cooldowns.get(alias_name, 0.0)
        in_cooldown = now < cooldown_until

        should_reclone = self._decide_non_permanent_reclone(
            alias_name, error, count, in_cooldown
        )
        if should_reclone:
            # Set cooldown before attempting — prevents retry storms even if
            # the attempt raises an exception.
            self._reclone_cooldowns[alias_name] = now + self.RECLONE_COOLDOWN_SECONDS
            self._attempt_reclone(alias_name, repo_url, master_path)

    def _apply_fetch_backoff(self, alias_name: str, category: str, count: int) -> None:
        """Push next_refresh out by the computed backoff, if any (Bug #1341)."""
        backoff_seconds = self._compute_backoff_seconds(category, count)
        if backoff_seconds is None:
            return
        try:
            self.registry.update_next_refresh(alias_name, time.time() + backoff_seconds)
        except Exception as exc:
            logger.warning(
                "Bug #1341: failed to persist backoff next_refresh for %s: %s",
                alias_name,
                exc,
            )

    def _handle_fetch_error(
        self,
        alias_name: str,
        repo_url: str,
        master_path: str,
        error: "GitFetchError",
    ) -> NoReturn:
        """
        Handle a GitFetchError from has_changes().

        Bug #1341: a repo is NEVER removed from scheduling and NEVER reaches
        a terminal/quarantine state, no matter how the error classifies.
        PERMANENT-classified errors never trigger re-clone (see
        _log_permanent_fetch_failure) but the fetch itself IS still retried,
        just at a growing backoff (_compute_backoff_seconds /
        _apply_fetch_backoff) pushed onto the alias's next_refresh, so it is
        always retried eventually. TRANSIENT/CORRUPTION errors keep the
        pre-existing immediate-retry + re-clone-escalation behavior
        completely unchanged (_handle_non_permanent_fetch_error); backoff
        also engages for transient only once sustained past the escalation
        threshold. ERROR-level logging is milestone-throttled, fixing the
        original #1341 log-flood complaint.

        Args:
            alias_name: Global alias name (e.g., "my-repo-global")
            repo_url: Remote git URL for re-clone
            master_path: Filesystem path of the master clone to delete + re-clone
            error: The GitFetchError raised by GitPullUpdater.has_changes()

        Raises:
            RuntimeError: Always raised with failure details after handling
        """
        count = self._fetch_failure_counts.get(alias_name, 0) + 1
        self._fetch_failure_counts[alias_name] = count

        if error.category == "permanent":
            self._log_permanent_fetch_failure(alias_name, count, error)
        else:
            self._handle_non_permanent_fetch_error(
                alias_name, repo_url, master_path, error, count
            )

        self._apply_fetch_backoff(alias_name, error.category, count)

        raise RuntimeError(
            f"Fetch failed for {alias_name} (category={error.category}): {error.stderr}"
        )

    def _attempt_reclone(
        self, alias_name: str, repo_url: str, master_path: str
    ) -> bool:
        """
        Re-clone from the remote URL using a safe clone-to-temp-then-swap strategy.

        Clones into a sibling temp directory (.reclone-{name}-tmp) first.
        Only deletes the old master clone and renames temp to master on success.
        On any failure the original master clone is preserved and the temp
        directory is cleaned up.

        Only touches golden-repos/{alias}/ (the master clone) and the sibling
        temp dir.  Never touches .versioned/{alias}/ snapshot directories.

        Args:
            alias_name: Global alias name (for logging)
            repo_url: Remote git URL to clone from
            master_path: Absolute path of the master clone directory

        Returns:
            True on success, False on failure (also logs CRITICAL on failure).
            Never raises — all exceptions are caught and logged.
        """
        master = Path(master_path)
        temp_clone = master.parent / f".reclone-{master.name}-tmp"

        # Clean up any leftover temp dir from a previous failed attempt.
        if temp_clone.exists():
            shutil.rmtree(str(temp_clone))

        try:
            clone_result = subprocess.run(
                ["git", "clone", repo_url, str(temp_clone)],
                capture_output=True,
                text=True,
                timeout=self.CLONE_TIMEOUT_SECONDS,
                env=build_non_interactive_git_env(),
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.critical(
                f"Auto re-clone FAILED for {alias_name}: {type(e).__name__}: {e}"
            )
            if temp_clone.exists():
                shutil.rmtree(str(temp_clone))
            return False

        if clone_result.returncode != 0:
            logger.critical(
                f"Auto re-clone FAILED for {alias_name}: {clone_result.stderr}"
            )
            if temp_clone.exists():
                shutil.rmtree(str(temp_clone))
            return False

        # Clone succeeded — swap temp into place.
        try:
            if master.exists():
                shutil.rmtree(str(master))
            temp_clone.rename(master)
        except OSError as e:
            logger.critical(
                f"Auto re-clone swap FAILED for {alias_name}: {type(e).__name__}: {e}"
            )
            if temp_clone.exists():
                shutil.rmtree(str(temp_clone))
            return False

        logger.info(f"Auto re-clone succeeded for {alias_name} into {master}")
        return True

    # ------------------------------------------------------------------
    # Write-lock registry (Story #227, delegating to WriteLockManager Story #230)
    # ------------------------------------------------------------------

    def acquire_write_lock(
        self, alias: str, owner_name: str = "refresh_scheduler"
    ) -> bool:
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

    def release_write_lock(
        self, alias: str, owner_name: str = "refresh_scheduler"
    ) -> None:
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

    def check_refresh_not_in_progress(self, alias: str) -> None:
        """
        Raise DuplicateJobError if a global_repo_refresh job is currently
        active (running or pending) for the given bare golden repo alias.

        Bug #1393: WriteLockManager alone cannot signal "a refresh is
        ALREADY executing" -- _execute_refresh() only CHECKS
        is_write_locked(), it never HOLDS the write lock itself while
        running (the lock is reserved for the enumerated external-writer
        set: DependencyMapService, LangfuseTraceSyncService, branch_change,
        etc -- Story #227). JobTracker is the cluster-visible signal for
        "currently executing" instead, since _execute_refresh() registers
        itself into it (Bug #935) under the alias_name form (bare alias +
        "-global").

        Callers should catch job_tracker.DuplicateJobError and translate it
        into their own domain error with an actionable message. This
        mirrors the register_job_if_no_conflict/DuplicateJobError fail-fast
        convention already used elsewhere in this codebase for conflicting
        operations, rather than blocking on an unbounded/long wait -- a
        refresh can legitimately run for well over an hour on a very large
        golden repo, and this method is called from within a caller's own
        background-job thread, which should not be tied up indefinitely.

        Args:
            alias: Repo alias without -global suffix (e.g., "evolution").

        Raises:
            job_tracker.DuplicateJobError: If a global_repo_refresh job is
                active for "{alias}-global".
        """
        if self._job_tracker is None:
            return
        self._job_tracker.check_operation_conflict(
            "global_repo_refresh", repo_alias=f"{alias}-global"
        )

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
                        elapsed = (
                            datetime.now(timezone.utc) - entered_at
                        ).total_seconds()
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
            logger.warning(
                f"Could not delete write mode marker {marker_path.name}: {exc}"
            )
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

    def trigger_refresh_for_repo(
        self,
        alias_name: str,
        submitter_username: str = "system",
        force_reset: bool = False,
    ) -> Optional[str]:
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
            force_reset: When True, skip change detection and force-reset the repo to the
                remote branch before indexing. Used by the manual "Force Re-sync" UI action
                (Story #272 AC3/AC4).

        Returns:
            Job ID string if submitted to BackgroundJobManager, None if executed directly
            (CLI mode) or if no BackgroundJobManager is configured.

        Raises:
            ValueError: If alias is not found in the global registry
        """
        global_alias = self._resolve_global_alias(alias_name)
        if self.background_job_manager:
            return self._submit_refresh_job(
                global_alias,
                submitter_username=submitter_username,
                force_reset=force_reset,
            )
        else:
            self._execute_refresh(global_alias, force_reset=force_reset)
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
        # Bug #734: wrap in try/except — if cleanup raises, log the error and
        # proceed to thread launch regardless (same defensive pattern as in-loop fix).
        try:
            self.cleanup_stale_write_mode_markers(force=True)
        except Exception as e:
            logger.error(
                "Bug #734: startup cleanup_stale_write_mode_markers failed: %s; "
                "continuing to launch scheduler thread anyway",
                e,
                exc_info=True,
            )

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

        Uses per-repo next_refresh timestamps with back-propagating jitter
        to spread refresh cycles across the interval (Story #284).

        Each iteration:
        1. Assigns initial spread to repos with NULL next_refresh
        2. Checks each git repo individually against its next_refresh time
        3. Submits due repos and back-propagates next_refresh with jitter
        4. Sleeps for a short poll interval (_calculate_poll_interval) instead
           of the full refresh interval
        """
        logger.debug("Refresh scheduler loop started")

        # Initialize before the loop so _calculate_poll_interval() at line 893
        # always has a bound value even when the try block raises before
        # get_refresh_interval() is called (Bug #722 — UnboundLocalError fix).
        refresh_interval = DEFAULT_REFRESH_INTERVAL

        # Bug #735: exponential-backoff circuit breaker — track consecutive
        # iteration failures so a permanently-broken upstream does not flood
        # logs at a fixed cadence.  Counter resets to 0 on each successful
        # iteration.  Backoff: 30s, 60s, 120s, ..., capped at 1 hour.
        consecutive_failures = 0
        MAX_BACKOFF_SECONDS = 3600  # 1 hour ceiling — Bug #735

        while self._running:
            try:
                # Bug #240: Periodically evict orphaned write mode markers from
                # clients that disconnected without calling exit_write_mode.
                self.cleanup_stale_write_mode_markers()

                repos = self.registry.list_global_repos()
                refresh_interval = self.get_refresh_interval()

                # Filter to git repos only — local repos are excluded from
                # the scheduled refresh cycle and are only refreshed via
                # explicit trigger_refresh_for_repo() calls.
                git_repos = [
                    r
                    for r in repos
                    if r.get("alias_name") and _is_git_repo_url(r.get("repo_url", ""))
                ]

                # Story #284 AC2: assign initial spread to repos with no next_refresh
                unscheduled = [r for r in git_repos if r.get("next_refresh") is None]
                if unscheduled:
                    self._assign_initial_spread(unscheduled, refresh_interval)

                # Bug #1063 Part 1: capped oldest-first due-query.
                # Compute how many refresh slots are free this cycle.
                # max_concurrent_refresh_jobs defaults to max(1, max_bg_jobs // 2).
                refresh_budget = self._get_refresh_budget()
                if refresh_budget <= 0:
                    logger.debug(
                        "Refresh budget exhausted this cycle "
                        f"(active={self._count_active_refresh_jobs()}); "
                        "skipping submission pass."
                    )
                    # Still fall through to the per-repo loop below (will be empty)

                now = time.time()
                # list_due_repos returns oldest-first, capped at budget.
                # Repos still unscheduled (NULL next_refresh) are excluded here —
                # they were just given future timestamps by _assign_initial_spread.
                due_repos = self.registry.list_due_repos(limit=refresh_budget, now=now)

                for repo in due_repos:
                    if not self._running:
                        break

                    alias_name = repo.get("alias_name")

                    # Skip non-git repos (list_due_repos returns all repo types)
                    if not _is_git_repo_url(repo.get("repo_url", "")):
                        continue

                    # Repo is due for refresh — submit job.
                    # Bug #1066: track whether a generic (non-DuplicateJobError)
                    # exception occurred so we can skip next_refresh advancement.
                    # Advancing on a transient failure would silently skip one full
                    # refresh cycle; leaving next_refresh unchanged lets the scheduler
                    # retry on the very next poll.
                    _submit_failed = False
                    try:
                        self._submit_refresh_job(alias_name)
                        self._db_throttle.on_db_success(logger)
                    except DuplicateJobError:
                        # Job already running for this repo — expected when prior refresh
                        # is still in flight (possibly extended by a verification pass).
                        # Advance next_refresh so we don't re-submit immediately after
                        # the in-flight job completes.
                        logger.info(
                            f"Refresh skipped for {alias_name}: prior refresh still running "
                            f"(possibly extended by verification pass)"
                        )
                    except Exception as e:
                        # Transient failure — do NOT advance next_refresh.
                        # The repo remains overdue and will be retried on the next poll.
                        _submit_failed = True
                        # Bug #1249: a connectivity error (e.g. PoolTimeout from
                        # job_tracker._atomic_insert_or_raise during a PG outage)
                        # is throttled (single ERROR transition, DEBUG
                        # follow-ups); anything else still logs normally.
                        if not self._db_throttle.on_db_error(e, logger):
                            logger.error(
                                f"Refresh failed for {alias_name}: {type(e).__name__}: {e}",
                                exc_info=True,
                            )

                    # Story #284 AC1: back-propagate next_refresh with jitter.
                    # Bug #1066: skip advancement when submit raised a generic exception.
                    if not _submit_failed:
                        jitter = self._calculate_jitter(refresh_interval)
                        new_next_refresh = now + refresh_interval + jitter
                        try:
                            self.registry.update_next_refresh(
                                alias_name, new_next_refresh
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed to persist next_refresh for {alias_name}: {e}"
                            )

                # Bug #735: successful iteration — reset consecutive failure counter.
                consecutive_failures = 0

            except Exception as e:
                # Bug #735: increment counter and apply exponential backoff so a
                # permanently-broken upstream does not flood logs at a fixed cadence.
                consecutive_failures += 1
                backoff = min(
                    30 * (2 ** (consecutive_failures - 1)), MAX_BACKOFF_SECONDS
                )
                logger.error(
                    "Bug #735: scheduler iteration failed (%d consecutive); backing off %ds: %s",
                    consecutive_failures,
                    backoff,
                    e,
                    exc_info=True,
                )
                if self._stop_event.wait(timeout=backoff):
                    return
                continue

            # Short poll interval instead of full refresh_interval wait.
            # Bug #1249: during a per-repo DB outage, back off the overall
            # poll cadence too (capped) instead of hammering every tick.
            poll_interval = self._db_throttle.next_wait_seconds(
                self._calculate_poll_interval(refresh_interval)
            )
            self._stop_event.wait(timeout=poll_interval)

        logger.debug("Refresh scheduler loop exited")

    def _submit_refresh_job(
        self,
        alias_name: str,
        submitter_username: str = "system",
        force_reset: bool = False,
    ) -> Optional[str]:
        """
        Submit a refresh job to BackgroundJobManager.

        If no BackgroundJobManager is configured (CLI mode), falls back to
        direct execution via _execute_refresh().

        Args:
            alias_name: Global alias name (e.g., "my-repo-global")
            submitter_username: Username to attribute the job to (default: "system")
            force_reset: When True, skip change detection and force-reset the repo to
                the remote branch before indexing. Captured in the lambda closure so
                BackgroundJobManager executes with the correct flag (Story #272 AC3).

        Returns:
            Job ID if submitted to BackgroundJobManager, None if executed directly
        """
        if not self.background_job_manager:
            # Fallback to direct execution if no job manager (CLI mode)
            self._execute_refresh(alias_name, force_reset=force_reset)
            return None

        # Story #482 PATH C: Use a named function (not a lambda) so
        # BackgroundJobManager can detect and inject progress_callback.
        #
        # EVO-64385: tracked_by_caller=True — submit_job() below claims
        # (global_repo_refresh, alias_name) in the job tracker (Bug #1065), and
        # that row holds the idx_active_job_per_repo slot for the whole run. The
        # worker must NOT register a second job for the same pair or it collides
        # with its own parent row and the refresh is marked failed before it
        # starts.
        def _refresh_worker(progress_callback=None):
            return self._execute_refresh(
                alias_name,
                force_reset=force_reset,
                progress_callback=progress_callback,
                tracked_by_caller=True,
            )

        job_id: str = self.background_job_manager.submit_job(
            operation_type="global_repo_refresh",
            func=_refresh_worker,
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

    def _execute_refresh(
        self,
        alias_name: str,
        force_reset: bool = False,
        progress_callback=None,
        tracked_by_caller: bool = False,
    ) -> Dict[str, Any]:
        """
        Execute refresh for a repository (called by BackgroundJobManager).

        Orchestrates the complete refresh cycle:
        1. Git pull (via updater) — or force reset when force_reset=True
        2. Change detection — skipped when force_reset=True (AC4)
        3. New index creation (if changes, or always when force_reset=True)
        4. Alias swap
        5. Cleanup scheduling

        Per-repo locking ensures concurrent refresh attempts on the same repo
        are serialized, while different repos can refresh in parallel.

        Args:
            alias_name: Global alias name (e.g., "my-repo-global")
            force_reset: When True, skip change detection and call
                updater.update(force_reset=True) to force-reset the repo to the
                remote branch before indexing (Story #272 AC3/AC4).
            tracked_by_caller: True when this refresh already runs under a
                BackgroundJobManager job, whose submit_job() has ALREADY claimed
                (global_repo_refresh, alias_name) in the job tracker. Registering
                again here would insert a SECOND active row for the same pair --
                see the EVO-64385 note below.

        Returns:
            Dict with success status and details for BackgroundJobManager tracking
        """
        # Acquire per-repo lock to serialize concurrent refresh attempts
        repo_lock = self._get_repo_lock(alias_name)

        # Bug #935: Register in-flight refresh with JobTracker so drain-status
        # sees it and the auto-updater waits before restarting.
        # Guard with is not None — CLI mode has no tracker.
        #
        # EVO-64385: ...but ONLY when nobody has registered us already. When this
        # refresh runs under a BackgroundJobManager job, submit_job() (Bug #1065)
        # has already called register_job_if_no_conflict() for the SAME
        # (operation_type="global_repo_refresh", repo_alias=alias_name) pair, and
        # that row occupies the idx_active_job_per_repo partial unique index. A
        # second registration here collides with our own parent row: postgres
        # raises 23505, the raw error escapes this function, and
        # BackgroundJobManager marks the whole refresh FAILED -- so the refresh
        # never runs at all (observed in cluster mode: every scheduled refresh
        # failed and last_refresh froze for days). The outer job IS the
        # cluster-visible active job Bug #935 wanted drain-status to see, so let
        # it own the registration AND the complete/fail lifecycle below.
        _tracker_job_id = f"refresh-{alias_name}"
        # The tracker WE own: None in CLI mode (no tracker at all) and None when
        # the caller already registered this refresh (tracked_by_caller).
        _tracker = None if tracked_by_caller else self._job_tracker
        if _tracker is not None:
            _tracker.register_job(
                _tracker_job_id,
                operation_type="global_repo_refresh",
                username="system",
                repo_alias=alias_name,
            )
            _tracker.update_status(_tracker_job_id, status="running")

        # Bug #935: track whether the refresh raised so the finally block can
        # call fail_job (raised) vs complete_job (all normal exits incl. early returns).
        _tracker_raised = False
        try:
            with repo_lock:
                try:
                    logger.info(f"Starting refresh for {alias_name}")

                    # Get current alias target
                    current_target = self.alias_manager.read_alias(alias_name)
                    if not current_target:
                        logger.warning(
                            f"Alias {alias_name} not found, skipping refresh"
                        )
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

                    # Determine repo type from repo_url.
                    # repo_url=None  → meta-directory (cidx-meta), uses MetaDirectoryUpdater
                    # repo_url starts with "local://" → local filesystem repo
                    # anything else → remote git repo, uses GitPullUpdater
                    repo_url = repo_info.get("repo_url", "")
                    is_meta_repo = repo_url is None
                    is_local_repo = (
                        repo_url.startswith("local://") if repo_url else False
                    )

                    # Story #236 Fix 2: Always derive master path from golden_repos_dir / repo_name.
                    # current_target from alias JSON may point to a .versioned/ snapshot after first
                    # refresh — using it for git pull or as snapshot source would be wrong.
                    repo_name = alias_name.removesuffix("-global")
                    master_path = str(self.golden_repos_dir / repo_name)

                    # AC6: Reconcile registry with filesystem at START of refresh
                    # This ensures registry flags reflect actual index state before refresh begins
                    detected_indexes = self._detect_existing_indexes(
                        Path(current_target)
                    )
                    self._reconcile_registry_with_filesystem(
                        alias_name, detected_indexes
                    )
                    logger.info(
                        f"Reconciled registry with filesystem at START for {alias_name}: {detected_indexes}"
                    )
                    sync_failure: Optional[str] = None
                    # Initialized here so _check_extension_drift can set it before
                    # any early-return exit in the local/git branching below.
                    force_reconcile = False

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
                                "message": "Not yet initialized, skipped",
                            }

                        # Bug #1253: Self-heal a .code-indexer/ directory that exists but
                        # has no valid config.json. golden_repo_manager.register_local_repo()
                        # runs `cidx init` exactly ONCE per alias at first registration; if
                        # that init fails partway through (e.g. the directory gets created by
                        # ConfigManager.save_with_documentation()'s mkdir() but config.json
                        # never gets written, or config.json is later truncated/corrupted by
                        # a concurrent writer), the CalledProcessError is only logged --
                        # registration "continues" and the broken golden repo is registered
                        # anyway. Because registration never retries, the repo is then stuck
                        # forever: code_indexer_dir.exists() is True so the guard above never
                        # fires, yet `cidx index` fails the SAME way on every scheduled cycle
                        # with "Command 'index' is not available in no configuration found".
                        # Observed as 231 recurring failures for langfuse_Claude_Code_*-global
                        # repos on staging. Repair by re-running `cidx init` before indexing
                        # instead of failing identically forever.
                        config_json_path = code_indexer_dir / "config.json"
                        if not self._is_local_config_valid(config_json_path):
                            logger.warning(
                                f"Local repo {alias_name} has .code-indexer/ but no valid "
                                f"config.json at {config_json_path} (likely a partial "
                                f"'cidx init' during registration -- Bug #1253). "
                                f"Attempting self-heal via 'cidx init' before indexing."
                            )
                            if not self._repair_uninitialized_local_repo(
                                source_path, alias_name
                            ):
                                return {
                                    "success": False,
                                    "alias": alias_name,
                                    "message": (
                                        "Local repo config invalid and repair via "
                                        "cidx init failed"
                                    ),
                                }
                            logger.info(
                                f"Self-healed .code-indexer/ config for local repo "
                                f"{alias_name}; proceeding with refresh"
                            )

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

                        # Story #926: After migrate_legacy_cidx_meta() runs at server startup,
                        # cidx-meta-global gets repo_url="local://cidx-meta", making is_local_repo=True
                        # and is_meta_repo=False.  The backup gate must fire here (before mtime-based
                        # change detection) so that remote drift and idempotent bootstrap are always
                        # handled regardless of whether local files have changed.
                        # _handled_by_backup=True means the backup path ran; the mtime block below
                        # is skipped so that the existing single mtime path is not duplicated.
                        _handled_by_backup = False
                        if alias_name == "cidx-meta-global":
                            _backup_cfg_local = None
                            try:
                                _backup_cfg_local = (
                                    get_config_service()
                                    .get_config()
                                    .cidx_meta_backup_config
                                )
                            except Exception as _cfg_err:
                                logger.warning(
                                    "Could not load cidx_meta_backup_config for %s, "
                                    "falling back to mtime-based detection: %s",
                                    alias_name,
                                    _cfg_err,
                                )

                            if (
                                _backup_cfg_local is not None
                                and _backup_cfg_local.enabled
                            ):
                                _handled_by_backup = True

                                # MED-3: MetaDirectoryUpdater first so description files are
                                # in place before CidxMetaBackupSync runs `git add -A`.
                                try:
                                    MetaDirectoryUpdater(
                                        master_path,
                                        self.registry,
                                        refresh_scheduler=self,
                                    ).update()
                                except Exception as _meta_err:
                                    logger.warning(
                                        "MetaDirectoryUpdater failed for %s before backup sync: %s",
                                        alias_name,
                                        _meta_err,
                                    )

                                # MED-2: Idempotent bootstrap — cheap when remote URL unchanged.
                                if _backup_cfg_local.remote_url:
                                    try:
                                        CidxMetaBackupBootstrap().bootstrap(
                                            master_path, _backup_cfg_local.remote_url
                                        )
                                    except Exception as _bootstrap_err:
                                        logger.warning(
                                            "cidx-meta backup bootstrap failed for %s: %s",
                                            alias_name,
                                            _bootstrap_err,
                                        )

                                _branch = detect_default_branch(master_path) or "master"
                                _sync_result = CidxMetaBackupSync(
                                    master_path,
                                    _branch,
                                    ClaudeConflictResolver(),
                                ).sync()

                                if _sync_result.skipped and not force_reset:
                                    logger.info(
                                        "No cidx-meta backup changes detected for %s, "
                                        "skipping refresh",
                                        alias_name,
                                    )
                                    return {
                                        "success": True,
                                        "alias": alias_name,
                                        "message": "No changes detected",
                                    }

                                sync_failure = _sync_result.sync_failure
                                logger.info(
                                    f"Changes detected in local repo {alias_name}, creating new index"
                                )
                                # Fall through to the shared indexing pipeline below.

                        # C2: Use mtime-based change detection for local repos.
                        # Skipped when the backup-aware path already handled cidx-meta-global.
                        if not _handled_by_backup:
                            has_changes = self._has_local_changes(
                                source_path, alias_name
                            )

                            if not has_changes:
                                force_reconcile = self._check_extension_drift(
                                    source_path, alias_name
                                )
                                if not force_reconcile:
                                    logger.info(
                                        f"No changes detected for local repo {alias_name}, skipping refresh"
                                    )
                                    return {
                                        "success": True,
                                        "alias": alias_name,
                                        "message": "No changes detected",
                                    }

                            logger.info(
                                f"Changes detected in local repo {alias_name}, creating new index"
                            )
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

                        backup_cfg = None
                        if is_meta_repo and alias_name == "cidx-meta-global":
                            try:
                                backup_cfg = (
                                    get_config_service()
                                    .get_config()
                                    .cidx_meta_backup_config
                                )
                            except Exception:
                                backup_cfg = None

                        if (
                            is_meta_repo
                            and alias_name == "cidx-meta-global"
                            and backup_cfg is not None
                            and backup_cfg.enabled
                        ):
                            # MED-3: Run MetaDirectoryUpdater before sync so description
                            # files are created/removed on disk before CidxMetaBackupSync
                            # runs `git add -A`.  git add -A is a superset of
                            # MetaDirectoryUpdater's filesystem writes once it has run.
                            # Wrapped in try/except so a MetaDirectoryUpdater failure
                            # does not block backup sync (matching the defensive pattern
                            # used for backup_cfg fetch and bootstrap errors in this block).
                            try:
                                MetaDirectoryUpdater(
                                    master_path, self.registry, refresh_scheduler=self
                                ).update()
                            except Exception as meta_err:
                                logger.warning(
                                    "MetaDirectoryUpdater failed for %s before backup sync: %s",
                                    alias_name,
                                    meta_err,
                                )

                            # MED-2: Idempotent bootstrap call ensures URL changes applied
                            # via DB-only paths (not through the Save route) are applied
                            # on the next refresh cycle.  CidxMetaBackupBootstrap.bootstrap()
                            # is cheap when the remote URL has not changed (reads
                            # `git remote get-url origin` and returns immediately on match).
                            if backup_cfg.remote_url:
                                try:
                                    CidxMetaBackupBootstrap().bootstrap(
                                        master_path, backup_cfg.remote_url
                                    )
                                except Exception as bootstrap_err:
                                    logger.warning(
                                        "cidx-meta backup bootstrap failed for %s: %s",
                                        alias_name,
                                        bootstrap_err,
                                    )
                            branch = detect_default_branch(master_path) or "master"
                            sync_result = CidxMetaBackupSync(
                                master_path,
                                branch,
                                ClaudeConflictResolver(),
                            ).sync()
                            if sync_result.skipped and not force_reset:
                                logger.info(
                                    "No cidx-meta backup changes detected for %s, skipping refresh",
                                    alias_name,
                                )
                                return {
                                    "success": True,
                                    "alias": alias_name,
                                    "message": "No changes detected",
                                }
                            sync_failure = sync_result.sync_failure
                            source_path = master_path
                        else:
                            # Meta-directories (repo_url=None) use MetaDirectoryUpdater instead of
                            # GitPullUpdater — they sync description files, not git history.
                            updater: UpdateStrategy
                            if is_meta_repo:
                                updater = MetaDirectoryUpdater(
                                    master_path, self.registry, refresh_scheduler=self
                                )
                            else:
                                # Story #236 Fix 2: Always git pull into the master golden repo, never into
                                # a versioned snapshot. current_target may be a .versioned/ path after first
                                # refresh, but git pull must always operate on the canonical master.
                                #
                                # Bug #1336 (hardened by #1338): an orphaned
                                # golden alias (registry row present, on-disk
                                # clone directory absent at master_path) makes
                                # GitPullUpdater's constructor raise the typed
                                # OrphanedRepoError. Before #1336, that
                                # exception propagated out of _execute_refresh()
                                # as a RuntimeError (Bug #84 re-raise), failing
                                # the whole global_repo_refresh job. Skip
                                # gracefully instead -- orphan CLEANUP (removing
                                # the stale registry row) is delegated to the
                                # #1317 reconciler; this refresh path only
                                # no-ops here. #1338: caught by TYPE, never by
                                # message-substring matching.
                                try:
                                    updater = GitPullUpdater(master_path)
                                except OrphanedRepoError as orphan_exc:
                                    logger.warning(
                                        "Golden repo %s is orphaned (registry row "
                                        "present, clone missing at %s): %s; "
                                        "skipping refresh",
                                        alias_name,
                                        master_path,
                                        orphan_exc,
                                    )
                                    return {
                                        "success": True,
                                        "alias": alias_name,
                                        "message": (
                                            "Orphaned golden repo (clone missing), skipped"
                                        ),
                                    }

                            # Bug #469 Fix 1: Verify base clone is on expected default_branch before
                            # pulling.  If the clone was switched to a wrong branch by any previous
                            # operation, reset it now so we don't perpetuate the contamination.
                            try:
                                branch_result = subprocess.run(
                                    ["git", "branch", "--show-current"],
                                    cwd=master_path,
                                    capture_output=True,
                                    text=True,
                                    timeout=10,
                                )
                                current_branch = branch_result.stdout.strip()
                                default_branch = repo_info.get("default_branch")
                                try:
                                    db_path = str(
                                        self.golden_repos_dir.parent / "cidx_server.db"
                                    )
                                    _meta_backend = GoldenRepoMetadataSqliteBackend(
                                        db_path
                                    )
                                    base_alias = alias_name.removesuffix("-global")
                                    meta = _meta_backend.get_repo(base_alias)
                                    if meta and meta.get("default_branch"):
                                        default_branch = meta["default_branch"]
                                except Exception as e:
                                    logger.debug(
                                        "Could not read default_branch from golden_repos_metadata"
                                        " for %s: %s",
                                        alias_name,
                                        e,
                                    )

                                if not default_branch:
                                    try:
                                        symref_result = subprocess.run(
                                            [
                                                "git",
                                                "symbolic-ref",
                                                "--short",
                                                "refs/remotes/origin/HEAD",
                                            ],
                                            cwd=master_path,
                                            capture_output=True,
                                            text=True,
                                            timeout=10,
                                        )
                                        if symref_result.returncode == 0:
                                            ref = symref_result.stdout.strip()
                                            if ref.startswith("origin/"):
                                                default_branch = ref[len("origin/") :]
                                    except Exception as e:
                                        logger.debug(
                                            "git symbolic-ref fallback failed for %s: %s",
                                            alias_name,
                                            e,
                                        )

                                if (
                                    default_branch
                                    and current_branch
                                    and current_branch != default_branch
                                ):
                                    logger.warning(
                                        f"Base clone for {alias_name} on '{current_branch}' instead of "
                                        f"'{default_branch}', resetting to default branch"
                                    )
                                    checkout_result = subprocess.run(
                                        ["git", "checkout", default_branch],
                                        cwd=master_path,
                                        capture_output=True,
                                        text=True,
                                        timeout=30,
                                    )
                                    if checkout_result.returncode != 0:
                                        logger.error(
                                            f"Failed to reset {alias_name} to {default_branch}: "
                                            f"{checkout_result.stderr}"
                                        )
                            except Exception as e:
                                logger.warning(
                                    f"Branch verification failed for {alias_name}: {e}"
                                )

                            if force_reset:
                                logger.info(
                                    f"Force reset requested for {alias_name}, "
                                    "skipping change detection and resetting to remote branch"
                                )
                                updater.update(force_reset=True)
                            else:
                                try:
                                    has_changes = updater.has_changes()
                                    self._reset_fetch_failures(alias_name)

                                    if not has_changes:
                                        # Check drift before early return: config may have
                                        # changed even when the repo has no new commits.
                                        # source_path is set here (same as line 1491) so
                                        # _check_extension_drift can scan the right directory.
                                        source_path = master_path
                                        force_reconcile = self._check_extension_drift(
                                            source_path, alias_name
                                        )
                                        if not force_reconcile:
                                            logger.info(
                                                f"No changes detected for {alias_name}, skipping refresh"
                                            )
                                            return {
                                                "success": True,
                                                "alias": alias_name,
                                                "message": "No changes detected",
                                            }
                                        # Drift detected — skip git pull but proceed to indexing.
                                    else:
                                        logger.info(
                                            f"Pulling latest changes for {alias_name} into master: {master_path}"
                                        )
                                        updater.update()
                                except GitFetchError as e:
                                    self._handle_fetch_error(
                                        alias_name, repo_url, master_path, e
                                    )
                                    raise

                            source_path = master_path

                    # Story #223 AC7 / Story #1001: Sync file extensions from server config
                    # before indexing.  _check_extension_drift() may have already been called
                    # in the no-changes early-return path above (to detect drift before skipping).
                    # Guard here avoids a second call (sync_repo_extensions_if_drifted is one-shot:
                    # it writes config on the first call and returns None on subsequent calls).
                    if not force_reconcile:
                        force_reconcile = self._check_extension_drift(
                            source_path, alias_name
                        )

                    # Index source first, then create versioned snapshot (Story #229)
                    # Story #482 PATH C: Forward progress_callback into _index_source
                    # Bug #1388: pass an alias-bound orphan_event_callback
                    # (see _make_hnsw_orphan_event_logger docstring in
                    # golden_repo_manager.py) so a marker line scraped from
                    # the child's stderr is re-logged tagged with this
                    # repo's alias -- a channel entirely separate from
                    # progress_callback, which is forwarded unwrapped.
                    # Reuses the SAME factory the golden-repo
                    # add/registration path already applies -- never a
                    # second, duplicated copy.
                    self._index_source(
                        alias_name=alias_name,
                        source_path=source_path,
                        progress_callback=progress_callback,
                        orphan_event_callback=_make_hnsw_orphan_event_logger(
                            alias_name
                        ),
                        force_reconcile=force_reconcile,
                    )
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

                    # Bug #881 Phase 2: Evict stale HNSW cache entries for the old snapshot
                    # immediately after swap, rather than waiting for 10-minute TTL.
                    # format_error_log imported before try so it is always bound in except.
                    # get_global_cache and get_correlation_id imported inside try because they
                    # are only available in server context; the except guard covers import failures.
                    from code_indexer.server.logging_utils import (
                        format_error_log as _fmt_err,
                    )

                    try:
                        from code_indexer.server.cache import get_global_cache
                        from code_indexer.server.middleware.correlation import (
                            get_correlation_id as _get_corr_id,
                        )

                        _evicted = get_global_cache().invalidate_prefix(current_target)
                        logger.info(
                            f"[REFRESH-{alias_name}] Evicted {_evicted} HNSW cache entries "
                            f"for old snapshot {current_target}",
                            extra={"correlation_id": _get_corr_id()},
                        )
                    except Exception as _cache_evict_err:
                        logger.warning(
                            _fmt_err(
                                "REPO-GENERAL-055",
                                f"Failed to evict HNSW cache for old snapshot "
                                f"{current_target}: {_cache_evict_err}",
                            )
                        )

                    # Story #236 Fix 1 + Bug #1084 Phase A4: Only schedule cleanup
                    # for versioned snapshots, never for the master golden repo
                    # (golden-repos/{repo}/). On first refresh current_target IS the
                    # master — scheduling it would permanently destroy it. The
                    # canonical predicate recognizes local AND cow-daemon (canonical
                    # + legacy) snapshot shapes; the explicit master_path comparison
                    # makes the Story #236 master-guard backend-independent.
                    if (
                        current_target
                        and self._is_versioned_snapshot(current_target)
                        and current_target != master_path
                    ):
                        logger.info(
                            f"Scheduling cleanup of old versioned snapshot: {current_target}"
                        )
                        self.cleanup_manager.schedule_cleanup(current_target)
                    else:
                        logger.info(
                            f"Preserving master golden repo (not scheduling cleanup): {current_target}"
                        )

                    # Bug #1084 Phase A6: keep-last-N retention (defense in depth).
                    # Schedule deletion (through the same refcount-gated
                    # CleanupManager) of all but the N newest snapshots, never the
                    # current target or previous_path. Inert on ONTAP (discovery []).
                    self._enforce_retention(alias_name, new_index_path)

                    # Update registry timestamp
                    self.registry.update_refresh_timestamp(alias_name)

                    # AC6: Reconcile registry with filesystem at END of refresh
                    # This captures any new indexes created during refresh (semantic, FTS, temporal, SCIP)
                    detected_indexes = self._detect_existing_indexes(
                        Path(new_index_path)
                    )
                    self._reconcile_registry_with_filesystem(
                        alias_name, detected_indexes
                    )
                    logger.info(
                        f"Reconciled registry with filesystem at END for {alias_name}: {detected_indexes}"
                    )

                    if sync_failure:
                        raise RuntimeError(
                            "refresh complete, indexing succeeded, but backup "
                            + sync_failure
                        )

                    logger.info(f"Refresh complete for {alias_name}")
                    return {
                        "success": True,
                        "alias": alias_name,
                        "message": "Refresh complete",
                    }

                except Exception as e:
                    logger.error(
                        f"Refresh failed for {alias_name}: {type(e).__name__}: {e}",
                        exc_info=True,
                    )
                    # Bug #84 fix: Raise exception instead of returning error dict
                    # BackgroundJobManager marks jobs as FAILED only when exceptions are raised
                    _tracker_raised = True
                    raise RuntimeError(
                        f"Refresh failed for {alias_name}: {type(e).__name__}: {e}"
                    )

        finally:
            # Bug #935: always unregister from JobTracker (finally runs on return AND raise).
            # EVO-64385: only for the job WE registered. When tracked_by_caller,
            # `refresh-{alias}` was never inserted by us and the outer
            # BackgroundJobManager job owns its own completion -- completing or
            # failing it here would touch a job that is not ours.
            if _tracker is not None:
                if _tracker_raised:
                    _tracker.fail_job(
                        _tracker_job_id,
                        error=f"refresh failed for {alias_name}",
                    )
                else:
                    _tracker.complete_job(_tracker_job_id)

    def _index_source(
        self,
        alias_name: str,
        source_path: str,
        progress_callback=None,
        force_reconcile: bool = False,
        orphan_event_callback: Optional[Any] = None,
    ) -> None:
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
            force_reconcile: When True, forces --reconcile regardless of metadata status.
                Story #1001: set by _execute_refresh() when extension drift is detected
                and matching files exist in the repo.
            orphan_event_callback: Bug #1388 optional callable(line: str) for
                HNSW orphan-repair marker lines scraped from the child
                subprocess's stderr -- forwarded verbatim to
                run_with_popen_progress. A channel entirely separate from
                progress_callback (see _make_hnsw_orphan_event_logger in
                golden_repo_manager.py).

        Raises:
            RuntimeError: If any indexing step fails or times out
        """
        # Step 1: Run cidx index for semantic + FTS (always required)
        # Bug #467: No timeout — let indexing run to completion.
        # Check if previous indexing was interrupted — use --reconcile to recover.
        # --reconcile compares content IDs against existing vectors, skips unchanged
        # files. Only used when needed (interrupted state or extension drift), otherwise
        # normal incremental.
        needs_reconcile = False
        metadata_path = Path(source_path) / ".code-indexer" / "metadata.json"
        if metadata_path.exists():
            try:
                import json as _json

                with open(metadata_path) as _f:
                    _meta = _json.load(_f)
                meta_status = _meta.get("status", "")
                if meta_status in ("in_progress", "failed"):
                    needs_reconcile = True
                    logger.info(
                        f"Previous indexing interrupted (status={meta_status}), "
                        f"using --reconcile for crash recovery on {alias_name}"
                    )
            except Exception as _meta_err:
                logger.warning(
                    "Could not read metadata.json for %s, proceeding without --reconcile: %s",
                    alias_name,
                    _meta_err,
                )

        # Story #1001: OR with force_reconcile from extension-drift detection.
        needs_reconcile = needs_reconcile or force_reconcile

        if needs_reconcile:
            index_command = ["cidx", "index", "--fts", "--reconcile", "--progress-json"]
        else:
            index_command = ["cidx", "index", "--fts", "--progress-json"]

        # Step 2: Temporal indexing on source (if enabled and not local://)
        repo_info = self.registry.get_global_repo(alias_name)
        enable_temporal = (
            repo_info.get("enable_temporal", False) if repo_info else False
        )

        # Bug #1414: temporal_options must be read from golden_repos_metadata
        # (bare-alias-keyed, Web-UI-authoritative -- GoldenRepoManager.
        # save_temporal_options is the Web UI's ONLY write path and writes
        # exclusively to this table), NOT from self.registry's global_repos
        # table, which is frozen at registration time. A stale registry read
        # here silently ignores every operator edit to max_commits/
        # since_date/diff_context/all_branches, forever -- most dangerously
        # for all_branches under Story #1412's gate (the #1406-class
        # scenario: operator disables all_branches via the Web UI, but the
        # stale registry copy still says True, so the scheduler keeps doing
        # multi-branch indexing against explicit operator intent). Reuses
        # the #1373 bare/-global alias normalization pattern already
        # established in _reconcile_registry_with_filesystem. enable_temporal
        # (above) and enable_scip (below) are UNCHANGED -- their
        # registry-based consistency is handled separately and correctly
        # (Bug #1390/#1406), and is explicitly out of scope here.
        _GLOBAL_SUFFIX = "-global"
        bare_alias = alias_name.removesuffix(_GLOBAL_SUFFIX)
        try:
            golden_meta_info = self.golden_repo_metadata.get_repo(bare_alias)
        except Exception as exc:
            logger.warning(
                "Bug #1414: could not read golden_repos_metadata for %s, "
                "temporal_options unavailable this cycle (Bug #642 NULL "
                "fallback will apply): %s: %s",
                bare_alias,
                type(exc).__name__,
                exc,
            )
            golden_meta_info = None
        temporal_options = (
            golden_meta_info.get("temporal_options") if golden_meta_info else None
        )

        repo_url = repo_info.get("repo_url", "") if repo_info else ""
        is_local_repo = repo_url.startswith("local://") if repo_url else False

        if enable_temporal and is_local_repo:
            logger.warning(
                f"Skipping temporal indexing for local repo {alias_name} "
                f"(local:// repos have no git history, ignoring enable_temporal flag)"
            )
            enable_temporal = False

        temporal_command = None
        if enable_temporal:
            temporal_command = ["cidx", "index", "--index-commits", "--progress-json"]
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
                diff_context = temporal_options.get("diff_context")
                if diff_context is not None:
                    temporal_command.extend(["--diff-context", str(diff_context)])
                if temporal_options.get("all_branches"):
                    # Story #1412: golden/server temporal all-branches
                    # indexing is gated behind a server-wide runtime flag,
                    # shipped OFF by default. Defense-in-depth: skip the
                    # flag (never trust a stored legacy value or a gate
                    # flipped off after the option was set) and log loudly
                    # so the downgrade to single-branch is observable.
                    _gate_config = get_config_service().get_config()
                    _gate_enabled = bool(
                        getattr(
                            _gate_config.indexing_config,
                            "temporal_all_branches_enabled",
                            False,
                        )
                    )
                    if _gate_enabled:
                        temporal_command.append("--all-branches")
                    else:
                        logger.warning(
                            "all_branches requested for golden '%s' but "
                            "temporal_all_branches_enabled=false; indexing "
                            "single-branch",
                            alias_name,
                        )
            else:
                # Bug #642 Step 2: temporal_options is NULL (e.g. after path migration
                # where options were never written back to DB). Fall back to reading
                # max_commits from temporal_meta.json in the repo's index directory.
                _fallback_max = _read_max_commits_from_temporal_meta(Path(source_path))
                if _fallback_max is not None:
                    logger.info(
                        "Bug #642 fallback: temporal_options NULL for %s, "
                        "using max_commits=%s from temporal_meta.json",
                        alias_name,
                        _fallback_max,
                    )
                    temporal_command.extend(["--max-commits", str(_fallback_max)])

        # Step 3: SCIP indexing on source (if enabled)
        enable_scip = repo_info.get("enable_scip", False) if repo_info else False

        # Story #482 PATH C: Build ProgressPhaseAllocator for phase-aware progress.
        # Phases: "semantic" (covers cidx index --fts), "temporal" (if enabled),
        # "scip" (if enabled, coarse markers only).
        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )
        from code_indexer.services.progress_subprocess_runner import (
            gather_repo_metrics,
            run_with_popen_progress,
            IndexingSubprocessError,
        )

        _phase_types = ["semantic"]
        if enable_temporal:
            _phase_types.append("temporal")
        if enable_scip:
            _phase_types.append("scip")

        file_count, commit_count = gather_repo_metrics(source_path)
        _opts = temporal_options or {}
        max_commits_opt = _opts.get("max_commits") if temporal_options else None

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=_phase_types,
            file_count=file_count,
            commit_count=commit_count,
            max_commits=max_commits_opt,
        )

        _popen_stdout: list = []
        _popen_stderr: list = []

        def _run_popen_c(
            command: list,
            phase_name: str,
            error_label: str,
            env: Optional[dict] = None,
        ) -> None:
            """Run command with Popen progress, re-raising as RuntimeError on failure.

            Bug #1313 round-3: env is forwarded to run_with_popen_progress so
            the temporal (--index-commits) child subprocess can be handed
            CIDX_TEMPORAL_PG_BOOTSTRAP_DIR in cluster/postgres mode -- see the
            temporal call site below. Bug #1325: the semantic/FTS call site
            now always passes an explicit sanitized env (never relies on this
            default), so this None default only matters for a hypothetical
            future caller.
            """
            _popen_stdout.clear()
            _popen_stderr.clear()
            # Bug #1313 round-3 regression guard: only pass the env= kwarg
            # when it is not None, so callers that intentionally pass None
            # (e.g. temporal in sqlite mode, to stay byte-unchanged) do not
            # have that None overwritten here -- several pre-existing tests
            # mock run_with_popen_progress with a strict (non-**kwargs)
            # signature that does not accept an env kwarg at all.
            _popen_kwargs: dict = dict(
                command=command,
                phase_name=phase_name,
                allocator=allocator,
                progress_callback=progress_callback,
                all_stdout=_popen_stdout,
                all_stderr=_popen_stderr,
                cwd=str(source_path),
                error_label=error_label,
            )
            if env is not None:
                _popen_kwargs["env"] = env
            # Bug #1388: only pass orphan_event_callback when not None, for
            # the same reason as env above -- several pre-existing tests
            # mock run_with_popen_progress with a strict signature lacking
            # **kwargs.
            if orphan_event_callback is not None:
                _popen_kwargs["orphan_event_callback"] = orphan_event_callback
            try:
                run_with_popen_progress(**_popen_kwargs)
            except IndexingSubprocessError as e:
                error_msg = str(e)
                # SIGTERM check — returncode -15 is in the error message from popen
                if "Exit code -15" in error_msg or "returncode=-15" in error_msg:
                    logger.warning(
                        f"{phase_name} indexing on source interrupted by server shutdown for {alias_name}"
                    )
                    raise RuntimeError(
                        f"Indexing interrupted by server shutdown for {alias_name}"
                    )
                logger.error(
                    f"{phase_name} indexing on source failed for {alias_name}: {error_msg}",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"{phase_name} indexing on source failed for {alias_name}: {error_msg}"
                )

        # Bug #678: Wrapper that seeds config before and drains health events after
        # each cidx index subprocess. Fire-and-forget: telemetry failures are logged
        # at DEBUG and never interrupt indexing.
        def _run_popen_c_with_telemetry(
            command: list,
            phase_name: str,
            error_label: str,
            env: Optional[dict] = None,
        ) -> None:
            try:
                from code_indexer.server.services.config_seeding import (
                    seed_provider_config,
                )

                seed_provider_config(str(source_path))
            except Exception as _seed_exc:  # noqa: BLE001
                logger.debug(
                    "Bug #678: seed_provider_config failed (non-fatal): %s", _seed_exc
                )
            try:
                _run_popen_c(
                    command, phase_name=phase_name, error_label=error_label, env=env
                )
            finally:
                try:
                    from code_indexer.services.provider_health_bridge import (
                        drain_and_feed_monitor,
                    )

                    drain_and_feed_monitor(str(source_path))
                except Exception as _drain_exc:  # noqa: BLE001
                    logger.debug(
                        "Bug #678: drain_and_feed_monitor failed (non-fatal): %s",
                        _drain_exc,
                    )

        # Execute Step 1: cidx index --fts (semantic + FTS, Popen for real progress)
        # Bug #1325 (code-review follow-up): pass a sanitized env with an
        # absolutized PYTHONPATH -- otherwise a relative PYTHONPATH inherited
        # from the server process re-anchors into source_path once the
        # child's cwd changes, letting a src/-layout package in source_path
        # shadow an installed cidx dependency.
        logger.info(
            f"Running cidx index on source for {alias_name}: {' '.join(index_command)}"
        )
        _run_popen_c_with_telemetry(
            index_command,
            phase_name="semantic",
            error_label=f"indexing on source for {alias_name}",
            env=build_cidx_subprocess_env(),
        )
        logger.info(f"cidx index on source completed successfully for {alias_name}")

        # Step 1b: build the trigram index for index-assisted regex search, so
        # /api/regex/search can pre-filter candidate files instead of scanning the
        # whole (NFS-backed) working tree. Built here at index time from the same
        # gitignore-aware file set. Non-fatal: on any failure regex search simply
        # falls back to a full scan.
        try:
            from code_indexer.global_repos.trigram_index_manager import (
                TrigramIndexManager,
            )

            _tri_source_path = Path(source_path)
            _tri_dir = _tri_source_path / ".code-indexer" / "trigram_index"
            _tri_files = TrigramIndexManager(_tri_dir).build(_tri_source_path)
            logger.info(f"Trigram index built for {alias_name} ({_tri_files} files)")
        except Exception as _tri_exc:  # never fail indexing over the pre-filter
            logger.warning(
                f"Trigram index build failed for {alias_name} "
                f"(regex search will full-scan): {_tri_exc}"
            )

        # Execute Step 2: temporal indexing (if enabled)
        if temporal_command is not None:
            # Bug #1313 round-3: in postgres/cluster mode, hand the child
            # subprocess CIDX_TEMPORAL_PG_BOOTSTRAP_DIR so it installs the
            # PostgreSQL temporal-metadata backend instead of silently
            # falling back to SQLite-on-NFS. sqlite/solo mode (or a failed
            # bootstrap read) yields env=None -- byte-unchanged existing
            # behavior. ONLY this temporal call site is postgres-aware; the
            # semantic call above is untouched.
            #
            # Bug #1325: when postgres mode DOES produce a temporal env dict,
            # run it through build_cidx_subprocess_env() too so its PYTHONPATH
            # (inherited from the server process) is absolutized, while
            # PRESERVING CIDX_TEMPORAL_PG_BOOTSTRAP_DIR (Bug #1313). sqlite
            # mode keeps env=None, byte-unchanged.
            from code_indexer.server.storage.postgres.temporal_child_wiring import (
                build_temporal_child_env,
            )

            _temporal_env = build_temporal_child_env(get_config_service().get_config())
            _temporal_env = (
                build_cidx_subprocess_env(_temporal_env)
                if _temporal_env is not None
                else None
            )

            logger.info(
                f"Running cidx index (temporal) on source for {alias_name}: {' '.join(temporal_command)}"
            )
            _run_popen_c_with_telemetry(
                temporal_command,
                phase_name="temporal",
                error_label=f"temporal indexing on source for {alias_name}",
                env=_temporal_env,
            )
            logger.info("cidx index (temporal) on source completed successfully")

        # Execute Step 3: SCIP indexing (coarse markers, subprocess.run stays — it has no --progress-json)
        if enable_scip:
            scip_command = ["cidx", "scip", "generate"]
            logger.info(f"SCIP indexing enabled for {alias_name}")
            logger.info(
                f"Running cidx scip generate on source for {alias_name}: {' '.join(scip_command)}"
            )
            if progress_callback is not None:
                progress_callback(
                    int(allocator.phase_start("scip")),
                    phase="scip",
                    detail="SCIP: generating code intelligence index...",
                )
            try:
                subprocess.run(
                    scip_command,
                    cwd=str(source_path),
                    capture_output=True,
                    text=True,
                    check=True,
                    env=build_cidx_subprocess_env(),
                )
                logger.info("cidx scip generate on source completed successfully")
                if progress_callback is not None:
                    progress_callback(
                        int(allocator.phase_end("scip")),
                        phase="scip",
                        detail="SCIP: complete",
                    )
            except subprocess.CalledProcessError as e:
                if e.returncode == -15:  # SIGTERM — server restart interrupted indexing
                    logger.warning(
                        f"SCIP indexing on source interrupted by server shutdown for {alias_name}"
                    )
                    raise RuntimeError(
                        f"Indexing interrupted by server shutdown for {alias_name}"
                    )
                logger.error(
                    f"SCIP indexing on source failed for {alias_name}: {type(e).__name__}: {e.stderr}",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"SCIP indexing on source failed for {alias_name}: {type(e).__name__}: {e.stderr}"
                )

    def _run_subprocess(self, *args: Any, **kwargs: Any) -> Any:
        """Run a subprocess, optionally through a per-instance injection seam.

        Bug #1381 (mirrors bug #1375's DependencyMapAnalyzer.cli_dispatcher
        pattern): defaults to the real `subprocess.run`, resolved dynamically
        at call time via the module-level `subprocess` name so existing
        tests that globally patch `subprocess.run` (e.g. via
        `unittest.mock.patch("subprocess.run", ...)`) continue to work
        unchanged. Tests that need a seam scoped to THIS scheduler instance
        only — immune to cross-thread interference from unrelated
        concurrently-running code under full-suite load — can set
        `self._subprocess_runner` directly (an instance attribute, not a
        constructor parameter, exactly like `analyzer._cli_dispatcher = ...`
        in test_delta_merge_frontmatter.py).
        """
        runner: Optional[Callable[..., Any]] = getattr(self, "_subprocess_runner", None)
        if runner is None:
            runner = subprocess.run
        return runner(*args, **kwargs)

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
        # Use resource_config defaults when none supplied — ServerResourceConfig
        # is the single source of truth for both default and operator-tuned values.
        cfg = self.resource_config or ServerResourceConfig()
        git_update_timeout = cfg.git_update_index_timeout
        git_restore_timeout = cfg.git_restore_timeout
        cidx_fix_timeout = cfg.cidx_fix_config_timeout

        repo_name = alias_name.removesuffix("-global")

        # versioned_path is set inside the try block by snapshot_manager.create_snapshot().
        # Initialize to None so the cleanup except block can guard against None safely.
        versioned_path: Optional[Path] = None

        logger.info(f"Creating versioned snapshot for: {alias_name}")

        try:
            # Story #1034 Commit 3: Route CoW clone through VersionedSnapshotManager
            # (and through CoW Daemon HTTP API in cluster mode). Eliminates NFS-cp
            # fallback that caused 600s timeouts on langfuse repos.
            if self._snapshot_manager is None:
                raise RuntimeError(
                    "RefreshScheduler invoked without snapshot_manager — wiring bug in lifespan.py. "
                    "Story #1034 Commit 3 requires snapshot_manager injection. Fail-loud per Codex B4."
                )

            logger.info(f"CoW cloning via snapshot_manager: source={source_path}")
            try:
                versioned_path = Path(
                    self._snapshot_manager.create_snapshot(repo_name, str(source_path))
                )
                logger.info(f"CoW clone completed successfully: {versioned_path}")
            except Exception as e:
                logger.error(
                    f"CoW clone failed for {alias_name} via snapshot_manager: {type(e).__name__}: {e}",
                    exc_info=True,
                )
                raise RuntimeError(
                    f"CoW clone failed for {alias_name} via snapshot_manager: {type(e).__name__}: {e}"
                )

            # Bug #1084 defense-in-depth: confirm the freshly created snapshot is
            # visible on THIS node before any subprocess.run(cwd=versioned_path).
            # The clone backend already waits at its create boundary, but a future
            # non-cow backend or a very slow NFS could still expose the
            # read-after-create dcache race that ENOENTs the git restore /
            # cidx fix-config steps below. Idempotent and fast when already
            # visible; raises (anti-fallback) if it never appears.
            wait_for_nfs_visibility(
                str(versioned_path), timeout=_configured_visibility_timeout()
            )

            # Step 3: Fix git status on clone (only if .git exists) — non-fatal
            git_dir = versioned_path / ".git"
            if git_dir.exists():
                logger.info("Running git update-index --refresh to fix CoW timestamps")
                try:
                    result = self._run_subprocess(
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
                    result = self._run_subprocess(
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
                self._run_subprocess(
                    ["cidx", "fix-config", "--force"],
                    cwd=str(versioned_path),
                    capture_output=True,
                    text=True,
                    env=build_cidx_subprocess_env(),
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
                raise RuntimeError(
                    "Index validation failed: index directory not created"
                )

            logger.info(f"Versioned snapshot created successfully at: {versioned_path}")
            return str(versioned_path)

        except Exception as e:
            # Cleanup partial artifacts on failure
            logger.error(
                f"Failed to create snapshot for {alias_name}, cleaning up: {type(e).__name__}: {e}",
                exc_info=True,
            )
            if versioned_path is not None and versioned_path.exists():
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

    def _is_local_config_valid(self, config_json_path: Path) -> bool:
        """
        Check whether a local repo's .code-indexer/config.json is present and
        parseable (Bug #1253).

        Mirrors CommandModeDetector._validate_local_config()'s leniency: any
        valid JSON file is accepted (mode detection does not require specific
        fields). A missing file or invalid JSON means `cidx index` would fail
        mode validation with "no configuration found".

        Args:
            config_json_path: Path to the candidate .code-indexer/config.json

        Returns:
            True if the file exists and contains valid JSON, False otherwise.
        """
        if not config_json_path.exists():
            return False
        try:
            with open(config_json_path) as f:
                json.load(f)
            return True
        except (json.JSONDecodeError, OSError) as e:
            logger.debug(f"Invalid local config at {config_json_path}: {e}")
            return False

    def _repair_uninitialized_local_repo(
        self, source_path: str, alias_name: str
    ) -> bool:
        """
        Self-heal a local repo whose .code-indexer/ directory exists but has
        no valid config.json (Bug #1253), by re-running the same `cidx init`
        invocation used at registration time (golden_repo_manager.py
        register_local_repo()).

        `--force` is required here (unlike first-time registration) because
        the .code-indexer/ directory already exists; init refuses to touch an
        existing config.json without it. This is safe: FilesystemBackend's
        initialize() only does `vectors_dir.mkdir(parents=True, exist_ok=True)`
        and never deletes existing index data, so any already-indexed content
        under .code-indexer/index/ survives the repair.

        Args:
            source_path: Path to the local repo directory (cwd for `cidx init`)
            alias_name: Global alias name, for logging only

        Returns:
            True if the repair subprocess succeeded, False otherwise.
        """
        try:
            subprocess.run(
                ["cidx", "init", "--no-override-file", "--force"],
                cwd=source_path,
                check=True,
                capture_output=True,
                text=True,
                env=build_cidx_subprocess_env(),
                timeout=60,
            )
            return True
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as e:
            stderr = getattr(e, "stderr", None) or str(e)
            logger.error(
                f"Failed to repair uninitialized local repo {alias_name} via "
                f"'cidx init' at {source_path}: {stderr}"
            )
            return False

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
        # Bug #1084 Phase A7 (Defect C): use the discovery API instead of globbing
        # golden_repos_dir/.versioned. On cow-daemon the snapshots live under the
        # NFS mount, so the old glob always missed -> always "first version" ->
        # spurious re-index + snapshot every cycle.
        latest_timestamp = self._latest_versioned_timestamp(alias_name)

        if latest_timestamp is None:
            logger.debug(
                f"No versioned snapshot found for {alias_name} — treating as first version"
            )
            return True

        logger.debug(
            f"Latest versioned snapshot timestamp for {alias_name}: {latest_timestamp}"
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

    def _check_extension_drift(self, source_path: str, alias_name: str) -> bool:
        """Check for extension drift and return True if --reconcile is needed.

        Calls sync_repo_extensions_if_drifted() on the config service.  If drift
        is detected AND matching files exist in source_path, returns True so the
        caller can trigger a reconcile index run.

        This helper is intentionally one-shot safe: sync_repo_extensions_if_drifted()
        writes the updated config on the first call and returns None on subsequent
        calls within the same refresh cycle, so calling this helper both before
        an early-return and again at the shared drift-check site is harmless.

        Args:
            source_path: Absolute path to the live repo directory.
            alias_name: Global alias name used only for logging.

        Returns:
            True if drift detected and matching files found, False otherwise.
        """
        try:
            config_service = get_config_service()
            drift = config_service.sync_repo_extensions_if_drifted(source_path)
            if drift is not None and (drift.added or drift.removed):
                drifted_exts = drift.added | drift.removed
                _exclude = {
                    ".git",
                    "node_modules",
                    "__pycache__",
                    ".code-indexer",
                    ".versioned",
                }
                if has_files_with_extensions(source_path, drifted_exts, _exclude):
                    logger.info(
                        "Extension drift detected (%d added, %d removed) "
                        "and matching files found for %s -- triggering reconcile",
                        len(drift.added),
                        len(drift.removed),
                        alias_name,
                    )
                    return True
                logger.info(
                    "Extension drift detected for %s but no matching files "
                    "found -- using normal incremental index",
                    alias_name,
                )
        except Exception as e:
            logger.warning(
                "Could not sync extensions before index for %s: %s",
                alias_name,
                e,
            )
        return False

    def _detect_existing_indexes(self, repo_path: Path) -> Dict[str, bool]:
        """
        Detect which index types exist in the repository's .code-indexer directory.

        Args:
            repo_path: Path to the repository root

        Returns:
            Dictionary with index types as keys and existence as boolean values:
            - semantic: True if semantic vector index exists
            - fts: True if FTS (Tantivy) index exists
            - temporal: True if a temporal collection exists WITH real shard data
              (Bug #1390: name-match alone is not enough -- see below)
            - scip: True if SCIP code intelligence indexes exist
        """
        from code_indexer.services.temporal.temporal_collection_naming import (
            is_temporal_collection as _is_temporal,
        )
        from code_indexer.server.services.hnsw_orphan_sweep.discovery import (
            iter_index_files_for_repo,
        )

        code_indexer_dir = repo_path / ".code-indexer"

        # Check semantic index: .code-indexer/index/ directory with collections
        semantic_index_dir = code_indexer_dir / "index"
        if semantic_index_dir.exists() and semantic_index_dir.is_dir():
            # Check for collection subdirectories with vector data (exclude temporal collections)
            collections = [
                d
                for d in semantic_index_dir.iterdir()
                if d.is_dir() and not _is_temporal(d.name)
            ]
            semantic_exists = len(collections) > 0
        else:
            semantic_exists = False

        # Check FTS index: .code-indexer/tantivy_index/ directory (production path)
        fts_index_dir = code_indexer_dir / "tantivy_index"
        fts_exists = fts_index_dir.exists() and fts_index_dir.is_dir()

        # Check temporal index: Bug #1390 -- a directory NAME match alone is
        # not sufficient. A temporal-named directory can contain only the
        # temporal metadata database (no real quarter-shard hnsw_index.bin /
        # collection_meta.json), e.g. when shard data was relocated/sidelined
        # for a maintenance operation while the metadata directory was left
        # behind. Reuses iter_index_files_for_repo (Story #1360's HNSW-fleet-
        # sweep discovery primitive) -- the same "hnsw_index.bin +
        # collection_meta.json pair is a real HNSW collection" structural
        # definition already used elsewhere in this codebase -- to confirm at
        # least one temporal-named collection actually has real data, rather
        # than trusting the name pattern alone.
        temporal_exists = False
        if semantic_index_dir.exists() and semantic_index_dir.is_dir():
            for relpath in iter_index_files_for_repo(repo_path):
                if _is_temporal(relpath.parent.name):
                    temporal_exists = True
                    break

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

    def _restore_master_from_versioned(
        self, alias_name: str, master_path: Path
    ) -> bool:
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
        # Bug #1084 Phase A7 (Defect E): locate the newest snapshot via the
        # discovery API instead of globbing golden_repos_dir/.versioned, so a lost
        # master can be restored on cow-daemon (snapshots live under the NFS mount)
        # and local alike. cp --reflink=auto reads over the NFS mount fine.
        if self._snapshot_manager is None:
            logger.warning(
                f"Reconciliation: {alias_name} restore needs snapshot_manager (unwired)"
            )
            return False

        latest_version = self._snapshot_manager.latest_snapshot(alias_name)
        if not latest_version:
            logger.warning(
                f"Reconciliation: {alias_name} has no snapshot to restore from"
            )
            return False

        logger.info(
            f"Reconciliation: restoring {alias_name} master from {latest_version} via reverse CoW"
        )

        cow_timeout = 600
        try:
            self._snapshot_manager._clone_backend.create_clone_at_path(
                str(latest_version),
                str(master_path),
                preserve_attrs=True,
                timeout=cow_timeout,
            )
        except Exception as e:
            logger.error(
                f"Reconciliation: reverse CoW clone failed for {alias_name}: "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
            return False

        # Fix .code-indexer/ paths — non-fatal if cidx is not available
        try:
            subprocess.run(
                ["cidx", "fix-config", "--force"],
                cwd=str(master_path),
                capture_output=True,
                text=True,
                env=build_cidx_subprocess_env(),
                timeout=60,
                check=False,
            )
            logger.info(
                f"Reconciliation: cidx fix-config --force done for {alias_name}"
            )
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

        Checks for golden-repos/cidx-meta/{short_alias}.md and submits work to
        ClaudeCliManager if the file does not exist.

        Args:
            alias_name: Global alias name (e.g., "my-repo-global"). The "-global"
                suffix is stripped to form the filename (e.g., "my-repo.md").
            master_path: Master golden repo path (used as repo_path for generation)
            claude_cli_manager: ClaudeCliManager instance to submit work to

        Returns:
            True if description was queued, False if already exists or error
        """
        cidx_meta_dir = self.golden_repos_dir / "cidx-meta"
        short_alias = alias_name.removesuffix("-global")
        # INVARIANT: cidx-meta descriptions use the SHORT repo alias as filename:
        #   {short_alias}.md  (e.g., JSqlParser.md)
        # The "-global" suffix belongs to the registry alias_name, NOT the filename.
        # DO NOT change this to use "-global" in the filename -- v10.4.9 did this
        # and broke 10 read paths, the UI, and access filtering. Fixed in v10.7.x.
        description_file = cidx_meta_dir / f"{short_alias}.md"

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
            logger.info(
                "Startup reconciliation already completed (marker exists), skipping"
            )
            return

        logger.info("Starting startup reconciliation of golden repos (Story #236)")
        restored_count = 0
        description_queued_count = 0

        try:
            all_repos = self.registry.list_global_repos()
        except Exception as e:
            logger.error(
                f"Failed to list repos for reconciliation: {type(e).__name__}: {e}"
            )
            marker_file.write_text(
                f"Completed (with errors) at {datetime.now().isoformat()}"
            )
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

            repo_name = alias_name.removesuffix("-global")
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
                        f"{type(e).__name__}: {e}",
                        exc_info=True,
                    )
                finally:
                    self.release_write_lock(repo_name, owner_name="reconciliation")

            # AC5: Queue description generation if cidx-meta file is missing
            if claude_cli_manager is not None:
                try:
                    if self._queue_missing_description(
                        alias_name, master_path, claude_cli_manager
                    ):
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

    def _reconcile_global_repos_temporal(
        self, global_alias: str, repo_info: Dict[str, Any], filesystem_temporal: bool
    ) -> bool:
        """Bug #1406: one-way (auto-DISABLE only) reconciliation of
        global_repos.enable_temporal. True->False downgrades when the
        filesystem shows no real data (Bug #1390's fix, preserved).
        False->True is intentionally never applied -- an explicit operator
        disable must never be silently overridden by restored/present data.

        Returns True when this table's auto-enable was suppressed (stored
        value False, filesystem shows real data) so the PARENT method
        (`_reconcile_registry_with_filesystem`) can emit a single
        consolidated suppression INFO log across both tables instead of one
        per table (code-review remediation of #1406 fix-item #3).
        """
        registry_temporal = repo_info.get("enable_temporal", False)

        if registry_temporal is True and filesystem_temporal is False:
            logger.info(
                f"Reconciling temporal flag for {global_alias}: "
                f"registry={registry_temporal} -> filesystem={filesystem_temporal}"
            )
            self.registry.update_enable_temporal(global_alias, False)
        elif registry_temporal is False and filesystem_temporal is True:
            return True
        return False

    def _reconcile_golden_metadata_temporal(
        self, bare_alias: str, filesystem_temporal: bool
    ) -> bool:
        """Bug #1390 cross-table fix + Bug #1406 one-way (auto-DISABLE only)
        rule for golden_repos_metadata.enable_temporal. Checked and updated
        INDEPENDENTLY of the global_repos side -- the two tables can drift
        independently, so global_repos already matching the filesystem must
        not skip this check.

        Returns True when this table's auto-enable was suppressed (stored
        value False, filesystem shows real data) so the PARENT method can
        emit a single consolidated suppression INFO log across both tables
        instead of one per table (code-review remediation of #1406
        fix-item #3).
        """
        try:
            golden_meta_info = self.golden_repo_metadata.get_repo(bare_alias)
        except Exception as exc:
            logger.warning(
                f"Reconciliation: could not read golden_repos_metadata for "
                f"{bare_alias}: {type(exc).__name__}: {exc}"
            )
            return False

        if golden_meta_info is None:
            return False

        golden_meta_temporal = golden_meta_info.get("enable_temporal", False)
        if golden_meta_temporal is True and filesystem_temporal is False:
            logger.info(
                f"Reconciling temporal flag (golden_repos_metadata) for "
                f"{bare_alias}: metadata={golden_meta_temporal} -> "
                f"filesystem={filesystem_temporal}"
            )
            try:
                self.golden_repo_metadata.update_enable_temporal(bare_alias, False)
            except Exception as exc:
                logger.error(
                    f"Reconciliation: failed to update golden_repos_metadata "
                    f"enable_temporal for {bare_alias}: "
                    f"{type(exc).__name__}: {exc}"
                )
        elif golden_meta_temporal is False and filesystem_temporal is True:
            return True
        return False

    def _reconcile_scip_flag(
        self, global_alias: str, repo_info: Dict[str, Any], detected: Dict[str, bool]
    ) -> None:
        """Reconcile SCIP flag -- global_repos only (no golden_repos_metadata
        column). Explicitly OUT OF SCOPE for Bug #1406: stays bidirectional,
        completely unchanged.
        """
        registry_scip = repo_info.get("enable_scip", False)
        filesystem_scip = detected.get("scip", False)

        if registry_scip != filesystem_scip:
            logger.info(
                f"Reconciling SCIP flag for {global_alias}: "
                f"registry={registry_scip} -> filesystem={filesystem_scip}"
            )
            self.registry.update_enable_scip(global_alias, filesystem_scip)

    def _reconcile_registry_with_filesystem(
        self, alias_name: str, detected: Dict[str, bool]
    ) -> None:
        """
        Reconcile registry flags with detected filesystem state.

        Updates enable_temporal and enable_scip flags in the registry to match
        what actually exists on disk. This ensures registry state stays in sync
        with filesystem reality.

        Bug #1390: enable_temporal is tracked in TWO structurally separate
        tables for the same logical repo -- `golden_repos_metadata` (bare-
        alias-keyed) and `global_repos` (-global-suffixed-alias-keyed, via
        `self.registry`). This method used to update ONLY `self.registry`,
        letting the two tables drift independently and permanently. Both
        tables are now reconciled here, using the same bare/`-global` alias
        normalization Bug #1373 established in `_set_enable_temporal_flag`
        (server/mcp/handlers/repos.py): `bare_alias` strips exactly one
        trailing "-global" suffix if present; `global_alias` is always
        re-derived from `bare_alias` (never assumed / blindly re-suffixed).

        Bug #1406: enable_temporal reconciliation is ONE-WAY (auto-DISABLE
        only), identically for both tables -- see
        `_reconcile_global_repos_temporal` / `_reconcile_golden_metadata_temporal`.
        A True->False downgrade (no real data on disk) is preserved from Bug
        #1390. A False->True auto-enable (data restored/present while an
        operator explicitly disabled the feature) is now NEVER applied --
        that direction previously re-armed the scheduled-refresh temporal-
        indexing trigger against operator intent (the production incident
        this bug fixes). Each table's own stored value is still compared
        independently against filesystem truth (the #1390 drift lesson) --
        a True->False downgrade applies to whichever table(s) currently hold
        True, even if the other already holds False.

        enable_scip has no golden_repos_metadata column and stays
        registry-only, bidirectional, and completely unchanged by Bug #1406
        (see `_reconcile_scip_flag`).

        Suppression-log cardinality (code-review remediation of #1406
        fix-item #3): when the auto-enable direction is suppressed (stored
        value False, filesystem shows real data), `_reconcile_global_repos_temporal`
        / `_reconcile_golden_metadata_temporal` do NOT log independently --
        each merely returns a bool signaling suppression. THIS method logs
        exactly ONE consolidated INFO message per invocation if EITHER
        table's auto-enable was suppressed, even when BOTH tables were
        suppressed simultaneously (the exact incident-reproduction
        scenario). The per-table True->False auto-disable log lines are
        unaffected and remain inside each helper.

        Args:
            alias_name: Repository alias name -- called with the -global-
                suffixed form at both existing call sites in
                _execute_refresh(), but normalized defensively here (see
                bare_alias/global_alias derivation) in case a bare alias is
                ever passed instead.
            detected: Dictionary from _detect_existing_indexes() with existence flags
        """
        _GLOBAL_SUFFIX = "-global"
        bare_alias = alias_name.removesuffix(_GLOBAL_SUFFIX)
        global_alias = f"{bare_alias}{_GLOBAL_SUFFIX}"

        # Get current registry state (global_repos, -global-suffixed alias)
        repo_info = self.registry.get_global_repo(global_alias)
        if not repo_info:
            logger.warning(
                f"Cannot reconcile registry for {global_alias}: repo not found in registry"
            )
            return

        filesystem_temporal = detected.get("temporal", False)
        suppressed_global = self._reconcile_global_repos_temporal(
            global_alias, repo_info, filesystem_temporal
        )
        suppressed_golden_meta = self._reconcile_golden_metadata_temporal(
            bare_alias, filesystem_temporal
        )
        if suppressed_global or suppressed_golden_meta:
            logger.info(
                f"Temporal data present on disk for {global_alias} but "
                f"enable_temporal is False -- honoring operator disable "
                f"(Bug #1406), not auto-enabling"
            )
        self._reconcile_scip_flag(global_alias, repo_info, detected)
