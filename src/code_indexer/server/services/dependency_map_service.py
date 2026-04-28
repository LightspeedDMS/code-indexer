"""
Dependency Map Service for Story #192 and #193 (Epic #191).

Orchestrates the full dependency map analysis pipeline:
- Manages staging and atomic swaps (full analysis)
- In-place delta refresh with change detection (incremental updates)
- Tracks analysis state in SQLite
- Coordinates with DependencyMapAnalyzer for Claude CLI execution
- Handles concurrency protection and error recovery
- Scheduler daemon thread for automatic delta refresh

TODO (Code Review M1): File bloat - 977 lines exceeds 500-line module threshold.
Consider extracting scheduler methods or delta analysis methods into separate module.
Deferred to future refactoring to avoid disrupting Story #193 acceptance criteria.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Set, Tuple

from code_indexer.global_repos.dependency_map_analyzer import (
    _DELTA_NOOP,
    _strip_leading_yaml_frontmatter,
)
from code_indexer.global_repos.lifecycle_batch_runner import (
    LifecycleBatchRunner,
    LifecycleFleetScanner,
)

from .activity_journal_service import ActivityJournalService
from .constants import CIDX_META_REPO
from .dep_map_health_detector import DepMapHealthDetector
from .metadata_reader import read_current_commit

logger = logging.getLogger(__name__)

# Constants
SCHEDULER_POLL_INTERVAL_SECONDS = 60  # Story #193: Delta refresh polling interval
THREAD_JOIN_TIMEOUT_SECONDS = 5.0  # Story #193: Daemon thread join timeout
MAX_DOMAIN_RETRIES = 3  # Bug #849: module-level constant for retry loop
_AUTO_REPAIR_JOB_ID_SUFFIX_LEN = (
    8  # Story #927: hex suffix length for auto-repair job IDs
)


class _DomainUpdateResult(Enum):
    """Result of a single domain file update attempt (Bug #849)."""

    WRITTEN = "written"
    NOOP = "noop"
    FAILED = "failed"


class DependencyMapService:
    """
    Service layer orchestrating dependency map analysis pipeline.

    Coordinates analyzer execution, staging directory management,
    atomic swaps, and tracking updates.
    """

    def __init__(
        self,
        golden_repos_manager,
        config_manager,
        tracking_backend,
        analyzer,
        refresh_scheduler=None,
        job_tracker=None,
        description_refresh_tracking_backend=None,
        lifecycle_invoker=None,
        lifecycle_debouncer=None,
        # Story #927: Any is intentional — psycopg2, psycopg3, and asyncpg pool
        # types all differ and no PG driver is imported at this module level.
        # The pool is treated as a duck-typed opaque object: only .connection()
        # context manager and .execute() are called inside _scheduler_decision_lock.
        pg_pool: Optional[Any] = None,
        repair_invoker_fn: Optional[Callable[[str], None]] = None,
        health_check_fn: Optional[Callable[[], Any]] = None,
        storage_mode: str = "sqlite",  # Story #927 Pass 2: anti-fallback guard
    ):
        """
        Initialize dependency map service.

        Args:
            golden_repos_manager: GoldenRepoManager instance
            config_manager: ServerConfigManager instance
            tracking_backend: DependencyMapTrackingBackend instance
            analyzer: DependencyMapAnalyzer instance
            refresh_scheduler: Optional RefreshScheduler for write-lock coordination (Story #227)
            job_tracker: Optional JobTracker for unified job tracking (Story #312)
            description_refresh_tracking_backend: Optional DescriptionRefreshTrackingBackend
                instance for lifecycle backfill (Epic #725). Provides access to the
                description_refresh_tracking table, which is separate from the
                DependencyMapTrackingBackend. Must be supplied for backfill to run.
            lifecycle_invoker: Optional LifecycleClaudeCliInvoker (Story #876 Phase B-1).
                Callable(alias, repo_path) -> UnifiedResult used by LifecycleBatchRunner
                during the pre-flight lifecycle repair step. When None (or when
                job_tracker is None), pre-flight is skipped entirely.
            lifecycle_debouncer: Optional CidxMetaRefreshDebouncer (Story #876 Phase B-1).
                Injected into LifecycleBatchRunner so the runner can signal the
                cidx-meta refresh debouncer once after the pre-flight batch finishes.
                Pre-flight is skipped when this is None.
            pg_pool: Optional PostgreSQL connection pool (Story #927). When provided,
                _scheduler_decision_lock uses PG advisory locks (cluster mode). When
                None, threading.Lock per key is used (solo mode). Type is Any because
                the PG driver (psycopg2/psycopg3/asyncpg) is not imported here.
            repair_invoker_fn: Optional callable(job_id) that starts a repair job
                (Story #927 Phase 3). Injected at app startup. When None, auto-repair
                logs an error and marks the job failed.
            health_check_fn: Optional callable() -> HealthReport-like object
                (Story #927 Phase 3). Must return an object with .anomalies list.
                When None, auto-repair is skipped with a WARNING log.
        """
        self._golden_repos_manager = golden_repos_manager
        self._config_manager = config_manager
        self._tracking_backend = tracking_backend
        self._analyzer = analyzer
        self._lock = threading.Lock()
        self._refresh_scheduler = (
            refresh_scheduler  # Story #227: write-lock coordination
        )
        self._job_tracker = job_tracker  # Story #312: unified job tracking (Epic #261)
        self._description_refresh_tracking_backend = (
            description_refresh_tracking_backend  # Epic #725: lifecycle backfill
        )
        self._lifecycle_invoker = (
            lifecycle_invoker  # Story #876 Phase B-1: lifecycle repair invoker
        )
        self._lifecycle_debouncer = (
            lifecycle_debouncer  # Story #876 Phase B-1: cidx-meta debouncer
        )
        self._activity_journal = (
            ActivityJournalService()
        )  # Story #329: activity journal

        # Story #927: cluster-aware decision lock state
        self._pg_pool = pg_pool  # None = solo mode; not-None = cluster mode
        self._storage_mode = storage_mode  # Story #927 Pass 2: anti-fallback guard
        self._repair_invoker_fn = (
            repair_invoker_fn  # Story #927 Phase 3: repair invoker
        )
        self._health_check_fn = health_check_fn  # Story #927 Phase 3: health check fn
        self._solo_decision_locks: Dict[str, threading.Lock] = {}
        # Guards _solo_decision_locks dict mutations (per-key lock creation)
        self._solo_decision_locks_lock = threading.Lock()

        # Story #193: Scheduler daemon thread state
        self._daemon_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    @property
    def activity_journal(self) -> ActivityJournalService:
        """Return the ActivityJournalService instance (Story #329)."""
        return self._activity_journal  # type: ignore[no-any-return]

    def set_repair_invoker_fn(
        self,
        fn: Optional[Callable[[str], None]],
    ) -> None:
        """Story #927 Codex Pass 4: late-bind the repair invoker after construction.

        Required because the lifespan startup needs to construct DependencyMapService
        BEFORE building the repair invoker closure (the closure must capture the
        constructed service instance, not the pre-construction None placeholder).
        """
        self._repair_invoker_fn = fn

    def _run_verification_pass(
        self,
        *,
        document_path: Path,
        repo_list: List[Dict[str, Any]],
        context_label: str,
    ) -> None:
        """Run verification pass (Story #724 v2 file-edit contract).

        Delegates to DependencyMapAnalyzer.invoke_verification_pass which edits
        the file at document_path in-place.  Emits a single structured log on
        completion.  Raises VerificationFailed if both attempts fail — callers
        must not swallow this exception.

        Story #724 v2.
        """
        ci_config = self._config_manager.get_claude_integration_config()
        self._analyzer.invoke_verification_pass(
            document_path=document_path,
            repo_list=repo_list,
            config=ci_config,
        )

    def is_available(self) -> bool:
        """
        Check if dependency map analysis can be started (Story #195).

        Performs a non-blocking lock probe to determine if the service
        is available for a new analysis.

        Returns:
            True if no analysis is running (lock available)
            False if analysis is already in progress (lock held)
        """
        # Try to acquire lock without blocking
        acquired = self._lock.acquire(blocking=False)

        if acquired:
            # Lock was available - release it immediately and return True
            self._lock.release()
            return True
        else:
            # Lock is held by another operation
            return False

    def run_graph_repair_dry_run(self) -> Dict[str, Any]:
        """Run Phase 3.7 graph-channel repair in dry-run mode (Story #919 AC5).

        Builds a DepMapRepairExecutor against the live dep-map output directory,
        calls _run_phase37(dry_run=True), and returns the DryRunReport as a plain
        dict so MCP handlers can JSON-serialize it directly.

        Returns an empty-safe dict when the dep-map output directory does not exist.
        """
        from dataclasses import asdict
        from datetime import datetime, timezone

        from typing import cast

        from .dep_map_health_detector import DepMapHealthDetector
        from .dep_map_index_regenerator import IndexRegenerator
        from .dep_map_repair_executor import DepMapRepairExecutor, DryRunReport

        dep_map_dir = (
            Path(self._golden_repos_manager.golden_repos_dir)
            / "cidx-meta"
            / "dependency-map"
        )
        if not dep_map_dir.exists():
            return {
                "mode": "dry_run",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_anomalies": 0,
                "per_type_counts": {},
                "per_verdict_counts": {},
                "per_action_counts": {},
                "would_be_writes": [],
                "skipped": [],
                "errors": [
                    f"dependency-map output directory does not exist: {dep_map_dir}"
                ],
            }

        from code_indexer.global_repos.repo_analyzer import invoke_claude_cli

        executor = DepMapRepairExecutor(
            health_detector=DepMapHealthDetector(),
            index_regenerator=IndexRegenerator(),
            enable_graph_channel_repair=True,
            invoke_claude_fn=invoke_claude_cli,
        )
        fixed: List[str] = []
        errors: List[str] = []
        report = executor._run_phase37(dep_map_dir, fixed, errors, dry_run=True)
        # _run_phase37(dry_run=True) with enable_graph_channel_repair=True always returns
        # DryRunReport — the Optional[DryRunReport] return type covers the False/None path
        # (enable_graph_channel_repair=False).  cast narrows the type for mypy only.
        return asdict(cast(DryRunReport, report))

    def run_full_analysis(self, job_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Orchestrate full dependency map analysis pipeline.

        Args:
            job_id: Optional caller-provided job ID for unified job tracking.
                    When None, a new UUID-based ID is generated internally.
                    MCP handler passes its own job_id for consistency (AC4).

        Returns:
            Dict with status, domains_count, repos_analyzed, errors

        Raises:
            RuntimeError: If analysis is already in progress
            DuplicateJobError: If job_tracker detects a concurrent full analysis (AC6)
        """
        # Story #876 Phase B-1 Deliverable 2: cluster-atomic job gate.
        # Replaces the Story #312 TOCTOU pattern (check_operation_conflict +
        # register_job).  register_job_if_no_conflict is backed by the partial
        # unique index idx_active_job_per_repo so duplicate detection happens
        # atomically inside the INSERT — no read-then-write race window across
        # cluster nodes.  DuplicateJobError propagates unchanged (AC6); all
        # other tracker errors are absorbed (pre-Story-#876 defensive behavior).
        from .job_tracker import DuplicateJobError

        _tracked_job_id: Optional[str] = None
        if self._job_tracker is not None:
            _tracked_job_id = job_id or f"dep-map-full-{uuid.uuid4().hex[:8]}"
            try:
                self._job_tracker.register_job_if_no_conflict(
                    job_id=_tracked_job_id,
                    operation_type="dependency_map_full",
                    username="system",
                    repo_alias="server",
                )
            except DuplicateJobError:
                raise  # AC6: Propagate conflict to caller unchanged
            except Exception as tracker_err:
                logger.warning(
                    f"JobTracker register_job_if_no_conflict failed (non-fatal): {tracker_err}"
                )
                _tracked_job_id = None
            if _tracked_job_id is not None:
                try:
                    self._job_tracker.update_status(_tracked_job_id, status="running")
                except Exception as tracker_err:
                    logger.warning(
                        f"JobTracker update_status (running) failed (non-fatal): {tracker_err}"
                    )

        # Story #876 Phase B-1 Deliverable 2: lifecycle fleet pre-flight.
        # Before acquiring the dep-map lock, scan the golden-repo fleet for
        # cidx-meta/<alias>.md files that are missing, malformed, outdated,
        # or poisoned; if any are flagged, run one Claude CLI call per repo
        # via LifecycleBatchRunner to repair them.  Pre-flight requires all
        # four of job_tracker, lifecycle_invoker, lifecycle_debouncer, AND
        # a non-None _tracked_job_id (LifecycleBatchRunner.run's parent_job_id
        # argument is mandatory; if non-duplicate tracker registration failed
        # above and _tracked_job_id is None, there is no job to parent the
        # lifecycle batch against and pre-flight is correctly skipped).
        if (
            self._job_tracker is not None
            and self._lifecycle_invoker is not None
            and self._lifecycle_debouncer is not None
            and _tracked_job_id is not None
        ):
            repo_aliases = [
                r.get("alias")
                for r in self._golden_repos_manager.list_golden_repos()
                if r.get("alias")
            ]
            scanner = LifecycleFleetScanner(
                golden_repos_dir=self._golden_repos_manager.golden_repos_dir,
                repo_aliases=repo_aliases,
            )
            broken = scanner.find_broken_or_missing()
            if broken:
                runner = LifecycleBatchRunner(
                    golden_repos_dir=self._golden_repos_manager.golden_repos_dir,
                    job_tracker=self._job_tracker,
                    refresh_scheduler=self._refresh_scheduler,
                    debouncer=self._lifecycle_debouncer,
                    claude_cli_invoker=self._lifecycle_invoker,
                )
                runner.run(broken, parent_job_id=_tracked_job_id)

        # Non-blocking lock acquire (AC7: Concurrency Protection)
        if not self._lock.acquire(blocking=False):
            # Story #312: Complete the registered job since this run cannot proceed.
            if _tracked_job_id is not None and self._job_tracker is not None:
                try:
                    self._job_tracker.fail_job(
                        _tracked_job_id,
                        error="Analysis already in progress (lock held)",
                    )
                except Exception as tracker_err:
                    logger.warning(
                        f"JobTracker fail_job (lock conflict) failed (non-fatal): {tracker_err}"
                    )
            raise RuntimeError("Dependency map analysis already in progress")

        # Story #227: Acquire write lock so RefreshScheduler skips CoW clone during writes.
        _write_lock_acquired = False
        if self._refresh_scheduler is not None:
            _write_lock_acquired = self._refresh_scheduler.acquire_write_lock(
                "cidx-meta", owner_name="dependency_map_service"
            )

        _analysis_succeeded = False
        try:
            # Story #312 AC5: Progress update during setup
            if _tracked_job_id is not None and self._job_tracker is not None:
                try:
                    self._job_tracker.update_status(
                        _tracked_job_id, progress=5, progress_info="Setting up analysis"
                    )
                except Exception as e:
                    logger.debug(f"Non-fatal: Failed to update progress (setup): {e}")

            # Setup and validation
            setup_result = self._setup_analysis()
            if setup_result.get("early_return"):
                _analysis_succeeded = True
                return setup_result

            config, paths, repo_list = (
                setup_result["config"],
                setup_result["paths"],
                setup_result["repo_list"],
            )

            # Update tracking to running
            self._tracking_backend.update_tracking(
                status="running",
                last_run=datetime.now(timezone.utc).isoformat(),
                error_message=None,  # Bug #437: clear stale error from orphan recovery
            )

            # Story #312 AC5: Progress update during analysis passes
            if _tracked_job_id is not None and self._job_tracker is not None:
                try:
                    self._job_tracker.update_status(
                        _tracked_job_id,
                        progress=20,
                        progress_info="Executing analysis passes",
                    )
                except Exception as e:
                    logger.debug(
                        f"Non-fatal: Failed to update progress (analysis passes): {e}"
                    )
            # Execute analysis passes
            domain_list, errors, pass1_duration_s, pass2_duration_s = (
                self._execute_analysis_passes(config, paths, repo_list, _tracked_job_id)
            )

            # Story #312 AC5: Progress update during finalization
            if _tracked_job_id is not None and self._job_tracker is not None:
                try:
                    self._job_tracker.update_status(
                        _tracked_job_id,
                        progress=80,
                        progress_info="Finalizing analysis",
                    )
                except Exception as e:
                    logger.debug(
                        f"Non-fatal: Failed to update progress (finalization): {e}"
                    )
            try:
                self._activity_journal.log(
                    "Finalizing: generating index and swapping directories"
                )
            except Exception as e:
                logger.debug(f"Non-fatal journal log error: {e}")

            # Finalize and cleanup.
            # Bug #930: finalize_s removed — not a meaningful user-visible phase.
            self._finalize_analysis(
                config,
                paths,
                repo_list,
                domain_list,
                pass1_duration_s,
                pass2_duration_s,
                run_type="full",
            )

            _analysis_succeeded = True
            try:
                self._activity_journal.log(
                    f"Analysis complete: {len(domain_list)} domains analyzed"
                )
            except Exception as e:
                logger.debug(f"Non-fatal journal log error: {e}")
            return {
                "status": "completed",
                "domains_count": len(domain_list),
                "repos_analyzed": len(repo_list),
                "errors": errors,
            }

        except Exception as e:
            self._tracking_backend.update_tracking(
                status="failed", error_message=str(e)
            )
            # Story #312: Report failure to JobTracker (AC8). Defensive - never re-raises.
            if _tracked_job_id is not None and self._job_tracker is not None:
                try:
                    self._job_tracker.fail_job(_tracked_job_id, error=str(e))
                    _tracked_job_id = None  # Prevent double-call in finally
                except Exception as tracker_err:
                    logger.warning(
                        f"JobTracker fail_job failed (non-fatal): {tracker_err}"
                    )
            raise
        finally:
            # Cleanup CLAUDE.md (paths may not be defined if exception occurred early)
            try:
                claude_md = (
                    paths.get("golden_repos_root", Path()) / "CLAUDE.md"
                    if "paths" in locals()
                    else Path(self._golden_repos_manager.golden_repos_dir) / "CLAUDE.md"
                )
                if claude_md.exists():
                    claude_md.unlink()
            except Exception as cleanup_error:
                # Log but don't re-raise - cleanup failure should not prevent lock release or mask original error
                logger.debug(f"CLAUDE.md cleanup failed (non-fatal): {cleanup_error}")

            # Bug #383: Clean up stale staging directory on failure.
            # On success, _stage_then_swap() already consumed the staging dir.
            if not _analysis_succeeded:
                try:
                    staging_path = (
                        paths["staging_dir"]
                        if "paths" in locals() and "staging_dir" in paths
                        else Path(self._golden_repos_manager.golden_repos_dir)
                        / "cidx-meta"
                        / "dependency-map.staging"
                    )
                    if staging_path.exists():
                        shutil.rmtree(staging_path)
                        logger.info(
                            f"Bug #383: Cleaned stale staging dir on failure: {staging_path}"
                        )
                except Exception as staging_cleanup_error:
                    logger.debug(
                        f"Staging dir cleanup failed (non-fatal): {staging_cleanup_error}"
                    )

            self._lock.release()

            # Story #312: Complete job in tracker on success (AC7). Defensive - never re-raises.
            if (
                _analysis_succeeded
                and _tracked_job_id is not None
                and self._job_tracker is not None
            ):
                try:
                    self._job_tracker.complete_job(_tracked_job_id)
                except Exception as tracker_err:
                    logger.warning(
                        f"JobTracker complete_job failed (non-fatal): {tracker_err}"
                    )

            # Story #227: Release write lock so RefreshScheduler can proceed.
            if _write_lock_acquired and self._refresh_scheduler is not None:
                self._refresh_scheduler.release_write_lock(
                    "cidx-meta", owner_name="dependency_map_service"
                )

            # Story #227: Trigger explicit refresh after lock released (only on success).
            # AC2: Writer triggers refresh so RefreshScheduler captures complete data.
            # Must be inside finally so it runs after lock is released, but gated on success
            # to satisfy AC5 (no trigger on exception).
            if _analysis_succeeded and self._refresh_scheduler is not None:
                self._refresh_scheduler.trigger_refresh_for_repo("cidx-meta-global")

    def _setup_analysis(self) -> Dict[str, Any]:
        """
        Setup and validation for analysis run.

        Returns:
            Dict with config, paths, repo_list or early_return indicator
        """
        config = self._config_manager.get_claude_integration_config()
        if not config.dependency_map_enabled:
            return {
                "early_return": True,
                "status": "disabled",
                "message": "Dependency map analysis disabled",
            }

        # Get repo list and paths
        golden_repos_root = self._golden_repos_manager.golden_repos_dir
        cidx_meta_path = Path(golden_repos_root) / "cidx-meta"  # WRITE path (live)
        cidx_meta_read_path = self._get_cidx_meta_read_path()  # READ path (versioned)
        staging_dir = cidx_meta_path / "dependency-map.staging"
        final_dir = cidx_meta_path / "dependency-map"

        paths = {
            "golden_repos_root": Path(golden_repos_root),
            "cidx_meta_path": cidx_meta_path,  # WRITE: used for staging/final dirs
            "cidx_meta_read_path": cidx_meta_read_path,  # READ: versioned .versioned/cidx-meta/v_*/
            "staging_dir": staging_dir,
            "final_dir": final_dir,
        }

        # Get list of golden repos
        repo_list = self._get_activated_repos()
        if not repo_list:
            return {
                "early_return": True,
                "status": "skipped",
                "message": "No activated golden repos",
            }

        # Enrich with repo sizes and sort by size (Iteration 15)
        repo_list = self._enrich_repo_sizes(repo_list)

        return {
            "early_return": False,
            "config": config,
            "paths": paths,
            "repo_list": repo_list,
        }

    def _execute_analysis_passes(
        self,
        config,
        paths: Dict[str, Path],
        repo_list: List[Dict[str, Any]],
        tracked_job_id: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[str], float, float]:
        """
        Execute the three-pass analysis pipeline with journal-based resumability.

        Args:
            config: Claude integration config
            paths: Dict with staging_dir, final_dir, cidx_meta_path, golden_repos_root
            repo_list: List of repository metadata

        Returns:
            Tuple of (domain_list, errors, pass1_duration_s, pass2_duration_s)
        """
        staging_dir = paths["staging_dir"]
        final_dir = paths["final_dir"]
        _cidx_meta_path = paths["cidx_meta_path"]  # noqa: F841
        cidx_meta_read_path = paths["cidx_meta_read_path"]  # READ: versioned path

        # Check for resumable journal (Iteration 15)
        journal = self._should_resume(staging_dir, repo_list)

        if journal is None:
            # Fresh start — clean staging
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            staging_dir.mkdir(parents=True)
            journal = {
                "pipeline_id": f"dep-map-{int(datetime.now(timezone.utc).timestamp())}",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "repo_sizes": {
                    r["alias"]: {
                        "file_count": r.get("file_count", 0),
                        "total_bytes": r.get("total_bytes", 0),
                    }
                    for r in repo_list
                },
                "pass1": {"status": "pending"},
                "pass2": {},
                "pass3": {"status": "pending"},
            }
            # Save journal immediately to prevent loss if crash occurs before Pass 1
            self._save_journal(staging_dir, journal)
            # Story #329: Initialize activity journal AFTER staging dir cleanup
            try:
                self._activity_journal.init(staging_dir)
                self._activity_journal.log(
                    f"Starting full analysis with {len(repo_list)} repositories"
                )
            except Exception as e:
                logger.debug(f"Non-fatal journal init error: {e}")
        else:
            # Resuming — init journal in existing staging dir (don't wipe)
            try:
                self._activity_journal.init(staging_dir)
                self._activity_journal.log("Resuming analysis")
            except Exception as e:
                logger.debug(f"Non-fatal journal init error (resume): {e}")

        # Generate CLAUDE.md (AC2: CLAUDE.md Orientation File)
        self._analyzer.generate_claude_md(repo_list)

        # Pass 1: Synthesis (skip if already completed)
        pass1_duration_s = 0.0
        if journal.get("pass1", {}).get("status") != "completed":
            # Read repo descriptions from cidx-meta (Fix 8: filter stale repos)
            active_aliases = {r.get("alias") for r in repo_list}
            repo_descriptions = self._read_repo_descriptions(
                cidx_meta_read_path,
                active_aliases=active_aliases,  # type: ignore[arg-type]
            )

            pass1_start = time.time()
            domain_list = self._analyzer.run_pass_1_synthesis(
                staging_dir=staging_dir,
                repo_descriptions=repo_descriptions,
                repo_list=repo_list,
                max_turns=config.dependency_map_pass1_max_turns,
            )
            pass1_duration_s = time.time() - pass1_start
            journal["pass1"] = {
                "status": "completed",
                "domains_count": len(domain_list),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            # Initialize pass2 tracking for all domains
            for d in domain_list:
                if d["name"] not in journal["pass2"]:
                    journal["pass2"][d["name"]] = {"status": "pending"}
            self._save_journal(staging_dir, journal)
            try:
                self._activity_journal.log(
                    f"Pass 1 complete: identified {len(domain_list)} domains"
                )
            except Exception as e:
                logger.debug(f"Non-fatal journal log error: {e}")
        else:
            # Load domain_list from _domains.json with boundary check
            domains_file = staging_dir / "_domains.json"
            if not domains_file.exists():
                raise FileNotFoundError(
                    f"Cannot resume: {domains_file} not found despite pass1 completed"
                )
            domain_list = json.loads(domains_file.read_text())
            logger.info(
                f"Pass 1 already completed ({journal['pass1']['domains_count']} domains), skipping"
            )

        # Pass 2: Per-domain (skip completed domains)
        errors: list[str] = []
        pass2_start = time.time()
        total_domains = len(domain_list)
        for domain_idx, domain in enumerate(domain_list):
            domain_name = domain["name"]
            domain_status = journal.get("pass2", {}).get(domain_name, {}).get("status")

            if domain_status == "completed":
                logger.info(f"Pass 2 already completed for '{domain_name}', skipping")
                continue

            try:
                self._activity_journal.log(
                    f"Pass 2: analyzing domain {domain_idx + 1}/{total_domains}: {domain_name}"
                )
            except Exception as e:
                logger.debug(f"Non-fatal journal log error: {e}")

            # Story #329: Per-domain progress update (30-90% range across Pass 2)
            if tracked_job_id is not None and self._job_tracker is not None:
                try:
                    progress_pct = 30 + int(domain_idx * (60.0 / total_domains))
                    self._job_tracker.update_status(
                        tracked_job_id,
                        progress=progress_pct,
                        progress_info=f"Pass 2: domain {domain_idx + 1}/{total_domains}",
                    )
                except Exception as e:
                    logger.debug(
                        f"Non-fatal: Failed to update progress (Pass 2 domain {domain_idx}): {e}"
                    )

            MAX_DOMAIN_RETRIES = 3
            attempt = 0
            chars = 0
            domain_file = staging_dir / f"{domain_name}.md"

            while attempt < MAX_DOMAIN_RETRIES:
                attempt += 1
                self._analyzer.run_pass_2_per_domain(
                    staging_dir=staging_dir,
                    domain=domain,
                    domain_list=domain_list,
                    repo_list=repo_list,
                    max_turns=config.dependency_map_pass2_max_turns,
                    previous_domain_dir=final_dir if final_dir.exists() else None,
                    journal_path=self._activity_journal.journal_path,
                )
                chars = len(domain_file.read_text()) if domain_file.exists() else 0

                if chars > 0:
                    # Story #724 v2: optional post-generation verification pass
                    if config.dep_map_fact_check_enabled:
                        self._run_verification_pass(
                            document_path=domain_file,
                            repo_list=repo_list,
                            context_label=f"pass2:{domain_name}",
                        )
                        chars = (
                            len(domain_file.read_text()) if domain_file.exists() else 0
                        )
                    # Success
                    break

                # 0 chars — retry if attempts remain
                if attempt < MAX_DOMAIN_RETRIES:
                    try:
                        self._activity_journal.log(
                            f"Pass 2: domain {domain_idx + 1}/{total_domains} produced 0 chars, "
                            f"retrying (attempt {attempt + 1}/{MAX_DOMAIN_RETRIES})"
                        )
                    except Exception as e:
                        logger.debug(f"Non-fatal journal log error: {e}")
                    logger.warning(
                        f"Pass 2 domain '{domain_name}' produced 0 chars on attempt {attempt}/{MAX_DOMAIN_RETRIES}, retrying"
                    )
                    # Delete empty/missing file before retry
                    if domain_file.exists():
                        domain_file.unlink()
                else:
                    # All retries exhausted
                    try:
                        self._activity_journal.log(
                            f"Pass 2: domain {domain_idx + 1}/{total_domains} FAILED after {MAX_DOMAIN_RETRIES} attempts (0 chars)"
                        )
                    except Exception as e:
                        logger.debug(f"Non-fatal journal log error: {e}")
                    logger.error(
                        f"Pass 2 domain '{domain_name}' produced 0 chars after {MAX_DOMAIN_RETRIES} attempts"
                    )

            # Record final status in journal
            if chars > 0:
                journal["pass2"][domain_name] = {
                    "status": "completed",
                    "chars": chars,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
                try:
                    self._activity_journal.log(
                        f"Pass 2: domain {domain_idx + 1}/{total_domains} complete ({chars} chars)"
                    )
                except Exception as e:
                    logger.debug(f"Non-fatal journal log error: {e}")
            else:
                journal["pass2"][domain_name] = {
                    "status": "failed",
                    "error": f"0 chars after {MAX_DOMAIN_RETRIES} attempts",
                }

            self._save_journal(staging_dir, journal)  # Save after each domain

        pass2_duration_s = time.time() - pass2_start

        # AC2 (Story #216): Pass 3 (Index generation) is replaced by programmatic
        # _generate_index_md() called in _finalize_analysis(). No Claude CLI call needed.
        # Update journal to reflect pass3 is handled programmatically.
        journal["pass3"] = {
            "status": "completed",
            "method": "programmatic",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_journal(staging_dir, journal)

        return domain_list, errors, pass1_duration_s, pass2_duration_s

    def _finalize_analysis(
        self,
        config,
        paths: Dict[str, Path],
        repo_list: List[Dict[str, Any]],
        domain_list: List[Dict[str, Any]],
        pass1_duration_s: float = 0.0,
        pass2_duration_s: float = 0.0,
        run_type: Optional[str] = None,
        phase_timings_json: Optional[str] = None,
    ) -> None:
        """
        Finalize analysis: swap, reindex, update tracking, cleanup.

        Args:
            config: Claude integration config
            paths: Dict with staging_dir, final_dir, cidx_meta_path, golden_repos_root
            repo_list: List of repository metadata
            domain_list: List of identified domains
            pass1_duration_s: Duration of Pass 1 in seconds
            pass2_duration_s: Duration of Pass 2 in seconds
            run_type: Run classification for metrics (e.g. "full"). Bug #874 Story C.
            phase_timings_json: Pre-serialised JSON with per-phase timing breakdown.
                      Bug #874 Story C. When None and run_type=="full", built here
                      from pass1_duration_s and pass2_duration_s only.
                      Bug #930: finalize_s removed — not a meaningful user-visible phase.
        """
        staging_dir = paths["staging_dir"]
        final_dir = paths["final_dir"]
        _cidx_meta_path = paths["cidx_meta_path"]  # noqa: F841
        _golden_repos_root = paths["golden_repos_root"]  # noqa: F841

        # AC4 (Story #216): Reconcile ghost domains before generating index
        domain_list = self._analyzer._reconcile_domains_json(staging_dir, domain_list)

        # AC2 (Story #216): Generate _index.md programmatically (replaces Claude Pass 3)
        self._analyzer._generate_index_md(staging_dir, domain_list, repo_list)

        # Stage-then-swap (AC4: Stage-then-Swap Atomic Writes)
        try:
            self._stage_then_swap(staging_dir, final_dir)
        except Exception as e:
            raise RuntimeError(
                f"Stage-then-swap failed: {e} -- previous dependency map preserved"
            ) from e

        # Update tracking (AC6: Configuration and Tracking)
        commit_hashes = self._get_commit_hashes(repo_list)
        next_run = (
            datetime.now(timezone.utc)
            + timedelta(hours=config.dependency_map_interval_hours)
        ).isoformat()
        self._tracking_backend.update_tracking(
            status="completed",
            commit_hashes=json.dumps(commit_hashes),
            next_run=next_run,
            error_message=None,
        )

        # AC9 (Story #216): Record run metrics to run_history table.
        # Bug #874 Story C: build phase_timings_json for full runs.
        # Bug #930: finalize_s removed — not a meaningful user-visible phase.
        if run_type == "full" and phase_timings_json is None:
            phase_timings_json = json.dumps(
                {
                    "synth_s": pass1_duration_s,
                    "per_domain_s": pass2_duration_s,
                }
            )
        self._record_run_metrics(
            final_dir,
            domain_list,
            repo_list,
            pass1_duration_s,
            pass2_duration_s,
            run_type=run_type,
            phase_timings_json=phase_timings_json,
        )

    def _stage_then_swap(self, staging_dir: Path, final_dir: Path) -> None:
        """
        Perform atomic stage-then-swap operation.

        Args:
            staging_dir: Staging directory with new content
            final_dir: Final directory to replace
        """
        old_dir = final_dir.parent / "dependency-map.old"

        # Move current final to old (if exists)
        if final_dir.exists():
            if old_dir.exists():
                shutil.rmtree(old_dir)
            final_dir.rename(old_dir)

        # Move staging to final
        staging_dir.rename(final_dir)

        # Cleanup old
        if old_dir.exists():
            shutil.rmtree(old_dir)

        logger.info(f"Stage-then-swap completed: {final_dir}")

    def _record_run_metrics(
        self,
        output_dir: Path,
        domain_list: List[Dict[str, Any]],
        repo_list: List[Dict[str, Any]],
        pass1_duration_s: float = 0.0,
        pass2_duration_s: float = 0.0,
        run_type: Optional[str] = None,
        phase_timings_json: Optional[str] = None,
        repos_skipped: int = 0,
    ) -> None:
        """
        Compute and record run metrics to tracking backend (AC9, Story #216).

        Reads domain file sizes from the output directory to compute total_chars and
        zero_char_domains, counts edge_count from cross-domain graph section
        of _index.md if present, then calls tracking_backend.record_run_metrics().

        Args:
            output_dir: Output directory where domain .md files were written
            domain_list: List of domain dicts from analysis
            repo_list: List of repo dicts that were analyzed
            pass1_duration_s: Duration of Pass 1 in seconds
            pass2_duration_s: Duration of Pass 2 in seconds
            run_type: Optional run classification (e.g. "delta", "full").
                      Bug #874 Story B. NULL for legacy rows until Story C wires it.
            phase_timings_json: Optional pre-serialized JSON string with per-phase
                      timing breakdown. Bug #874 Story B. NULL for legacy rows.
            repos_skipped: Count of repos not touched by this run. Bug #874 Story C.
                      Full runs always pass 0; delta/refinement pass honest values.
        """
        try:
            total_chars = 0
            zero_char_domains = 0
            for domain in domain_list:
                domain_file = output_dir / f"{domain['name']}.md"
                if domain_file.exists():
                    chars = len(domain_file.read_text())
                    total_chars += chars
                    if chars == 0:
                        zero_char_domains += 1
                else:
                    zero_char_domains += 1

            # Count edges from _index.md cross-domain dependencies table
            edge_count = 0
            index_file = output_dir / "_index.md"
            if index_file.exists():
                content = index_file.read_text()
                # Count data rows in cross-domain dependencies table
                # (pipe-delimited rows that aren't headers or separators)
                in_cross_domain = False
                for line in content.splitlines():
                    if (
                        "Cross-Domain Dependencies" in line
                        or "Cross-Domain Dependency Graph" in line
                    ):
                        in_cross_domain = True
                        continue
                    if in_cross_domain:
                        if (
                            line.startswith("| ")
                            and not line.startswith("|---")
                            and not line.startswith("| Source")
                        ):
                            edge_count += 1
                        elif line.startswith("#"):
                            break

            metrics = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "domain_count": len(domain_list),
                "total_chars": total_chars,
                "edge_count": edge_count,
                "zero_char_domains": zero_char_domains,
                "repos_analyzed": len(repo_list),
                "repos_skipped": repos_skipped,  # Bug #874 Story C: caller-supplied
                "pass1_duration_s": pass1_duration_s,
                "pass2_duration_s": pass2_duration_s,
            }

            if hasattr(self._tracking_backend, "record_run_metrics"):
                self._tracking_backend.record_run_metrics(
                    metrics,
                    run_type=run_type,
                    phase_timings_json=phase_timings_json,
                )
                logger.info(
                    f"Recorded run metrics: {len(domain_list)} domains, "
                    f"{len(repo_list)} repos, {total_chars} chars"
                )
            else:
                logger.debug(
                    "Tracking backend does not support record_run_metrics, skipping"
                )

        except Exception as e:
            logger.warning(f"Failed to record run metrics: {e}")

    def _read_repo_descriptions(
        self, cidx_meta_path: Path, active_aliases: Optional[Set[str]] = None
    ) -> Dict[str, str]:
        """
        Read repository descriptions from cidx-meta .md files.

        Args:
            cidx_meta_path: Path to cidx-meta directory
            active_aliases: Optional set of active repo aliases to filter by (Fix 8)

        Returns:
            Dict mapping repo alias to description content
        """
        descriptions = {}
        for md_file in cidx_meta_path.glob("*.md"):
            if md_file.name.startswith("_"):
                continue
            alias = md_file.stem
            # Filter stale repos if active_aliases provided (Fix 8)
            if active_aliases is not None and alias not in active_aliases:
                logger.debug(f"Skipping stale repo description: {alias}")
                continue
            descriptions[alias] = md_file.read_text()
        return descriptions

    def get_activated_repos(self) -> List[Dict[str, Any]]:
        """
        Public accessor: get list of activated golden repos with metadata.

        Returns:
            List of dicts with alias, clone_path, description_summary
        """
        return self._get_activated_repos()

    @property
    def golden_repos_dir(self) -> str:
        """
        Public accessor: return the golden repos directory path.

        Returns:
            Absolute path string to the golden repos directory
        """
        return self._golden_repos_manager.golden_repos_dir  # type: ignore[no-any-return]

    def _get_cidx_meta_read_path(self) -> Path:
        """
        Resolve the cidx-meta path for READ operations.

        Since Story #224 made cidx-meta a versioned golden repo, the live
        golden-repos/cidx-meta/ directory is mostly empty. The actual content
        (_domains.json, _index.md, domain .md files) lives in
        .versioned/cidx-meta/v_*/.

        READS must come from the versioned path.
        WRITES must continue to use the live path so RefreshScheduler detects changes.

        Note: We check .versioned/ directly rather than using get_actual_repo_path()
        because that method returns the live clone_path when it exists, which is
        always true for local repos (the live dir is the write sentinel). This
        causes it to return the empty live dir instead of the versioned content.

        Returns:
            Path to the versioned cidx-meta directory if available,
            otherwise falls back to the live golden-repos/cidx-meta/ path.
        """
        golden_repos_dir = Path(self._golden_repos_manager.golden_repos_dir)

        # Check versioned path directly (bypasses get_actual_repo_path() bug
        # where live dir existence masks versioned content for local repos)
        versioned_base = golden_repos_dir / ".versioned" / "cidx-meta"
        if versioned_base.exists():
            try:
                version_dirs = sorted(
                    [
                        d
                        for d in versioned_base.iterdir()
                        if d.name.startswith("v_") and d.is_dir()
                    ],
                    key=lambda d: d.name,
                    reverse=True,
                )
                if version_dirs:
                    return version_dirs[0]
            except OSError as e:
                logger.warning("Failed to list versioned cidx-meta dirs: %s", e)

        # Fallback: try get_actual_repo_path (handles non-versioned repos)
        try:
            actual_path = self._golden_repos_manager.get_actual_repo_path("cidx-meta")
            if actual_path:
                return Path(actual_path)
        except Exception as e:
            logger.warning(
                "Failed to resolve cidx-meta path, falling back to live: %s", e
            )

        return golden_repos_dir / "cidx-meta"

    @property
    def cidx_meta_read_path(self) -> Path:
        """
        Public property: versioned cidx-meta path for READ operations.

        See _get_cidx_meta_read_path() for full documentation.
        Used by DependencyMapDomainService and other consumers that read
        dependency-map content.
        """
        return self._get_cidx_meta_read_path()

    def _get_activated_repos(self) -> List[Dict[str, Any]]:
        """
        Get list of activated golden repos with metadata.

        Returns:
            List of dicts with alias, clone_path, description_summary
        """
        repos = self._golden_repos_manager.list_golden_repos()

        # Resolve versioned cidx-meta path once before the loop (READ path for Story #224)
        cidx_meta_read_path = self._get_cidx_meta_read_path()

        result = []
        for repo in repos:
            alias = repo.get("alias")
            clone_path = repo.get("clone_path")

            # Skip if missing required fields
            if not alias or not clone_path:
                continue

            # Skip cidx-meta: it's the output target for dependency map results,
            # not a source repository to be analyzed
            if alias == CIDX_META_REPO:
                continue

            # Resolve actual filesystem path — clone_path from metadata may be stale
            # after RefreshScheduler creates .versioned/{alias}/v_*/ structure
            try:
                resolved_path = self._golden_repos_manager.get_actual_repo_path(alias)
                clone_path = resolved_path
            except Exception as e:
                logger.warning(
                    "Skipping repo '%s': could not resolve actual path: %s",
                    alias,
                    e,
                )
                continue

            # Extract description summary (first line of description)
            description_summary = "No description"
            # Use versioned path for reads: cidx-meta is a versioned golden repo since Story #224
            md_file = cidx_meta_read_path / f"{alias}.md"
            if md_file.exists():
                try:
                    content = md_file.read_text()
                    lines = content.split("\n")
                    # Find first non-empty line after frontmatter
                    in_frontmatter = False
                    for line in lines:
                        if line.strip() == "---":
                            in_frontmatter = not in_frontmatter
                            continue
                        if (
                            not in_frontmatter
                            and line.strip()
                            and not line.strip().startswith("#")
                        ):
                            description_summary = line.strip()
                            break
                except Exception as e:
                    logger.warning(f"Failed to read description for {alias}: {e}")

            result.append(
                {
                    "alias": alias,
                    "clone_path": clone_path,
                    "description_summary": description_summary,
                }
            )

        return result

    def _load_journal(self, staging_dir: Path) -> Optional[Dict]:
        """Load existing journal from staging_dir if it exists."""
        journal_path = staging_dir / "_journal.json"
        if journal_path.exists():
            try:
                return json.loads(journal_path.read_text())  # type: ignore[no-any-return]
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    f"Corrupted journal at {journal_path}, starting fresh: {e}"
                )
                return None
        return None

    def _save_journal(self, staging_dir: Path, journal: Dict) -> None:
        """Atomically write journal to staging_dir."""
        journal_path = staging_dir / "_journal.json"
        tmp_path = journal_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(journal, indent=2))
        tmp_path.rename(journal_path)

    def _should_resume(
        self, staging_dir: Path, repo_list: List[Dict[str, Any]]
    ) -> Optional[Dict]:
        """
        Check if a previous run can be resumed.

        Returns journal dict if resumable, None if fresh start needed.
        Resume conditions:
        - staging_dir exists with _journal.json
        - repo_sizes match (no new/changed repos)
        """
        journal = self._load_journal(staging_dir)
        if not journal:
            return None

        # Check if repo set changed
        current_sizes = {r["alias"]: r.get("total_bytes", 0) for r in repo_list}
        journal_sizes = {
            k: v.get("total_bytes", 0) for k, v in journal.get("repo_sizes", {}).items()
        }

        if set(current_sizes.keys()) != set(journal_sizes.keys()):
            logger.info("Journal found but repo set changed — starting fresh")
            return None

        # Check if any repo size changed significantly (>5% difference)
        for alias, current_bytes in current_sizes.items():
            journal_bytes = journal_sizes.get(alias, 0)
            # If either was zero and the other isn't, that's a significant change
            if (journal_bytes == 0) != (current_bytes == 0):
                logger.info(
                    f"Journal found but {alias} size changed from {journal_bytes} to {current_bytes} — starting fresh"
                )
                return None
            if (
                journal_bytes > 0
                and abs(current_bytes - journal_bytes) / journal_bytes > 0.05
            ):
                logger.info(f"Journal found but {alias} size changed — starting fresh")
                return None

        logger.info(
            f"Resuming from journal: pass1={journal.get('pass1', {}).get('status')}"
        )
        return journal

    def _enrich_repo_sizes(
        self,
        repo_list: List[Dict[str, Any]],
        progress_callback=None,
    ) -> List[Dict[str, Any]]:
        """
        Add file_count and total_bytes to each repo dict. Sort by total_bytes descending.

        Args:
            repo_list: List of repo dicts with clone_path
            progress_callback: Optional callable(completed: int, total: int).
                               Called after each repo is enriched. Defaults to None.

        Returns:
            Enriched and sorted repo list
        """
        total = len(repo_list)
        for idx, repo in enumerate(repo_list):
            clone_path = Path(repo.get("clone_path", ""))
            if clone_path.exists():
                file_count = 0
                total_bytes = 0
                for f in clone_path.rglob("*"):
                    # Exclude .git and .code-indexer directories
                    if (
                        f.is_file()
                        and ".git" not in f.parts
                        and ".code-indexer" not in f.parts
                    ):
                        file_count += 1
                        try:
                            total_bytes += f.stat().st_size
                        except OSError:
                            pass  # Broken symlink, permission denied, etc.
                repo["file_count"] = file_count
                repo["total_bytes"] = total_bytes
            else:
                repo["file_count"] = 0
                repo["total_bytes"] = 0

            if progress_callback is not None:
                progress_callback(idx + 1, total)

        # Filter out empty repos (AC8: exclude repos with 0 files — they contribute nothing to analysis)
        non_empty = []
        for repo in repo_list:
            if repo.get("file_count", 0) > 0:
                non_empty.append(repo)
            else:
                logger.warning(
                    "_enrich_repo_sizes: excluding empty repo '%s' (0 files) from analysis",
                    repo.get("alias", "unknown"),
                )

        # Sort descending by total_bytes
        non_empty.sort(key=lambda r: r.get("total_bytes", 0), reverse=True)
        return non_empty

    def _get_commit_hashes(self, repo_list: List[Dict[str, Any]]) -> Dict[str, str]:
        """
        Read provider-aware metadata for each repo to get current_commit (Bug #890).

        Prefers metadata-voyage-ai.json, falls back to legacy metadata.json via
        read_current_commit(). Repos with no readable metadata are omitted from
        the result — callers must not interpret absence as a sentinel value.

        Args:
            repo_list: List of repo dicts with clone_path

        Returns:
            Dict mapping repo alias to real commit SHA (only repos with valid metadata)
        """
        commit_hashes = {}
        for repo in repo_list:
            alias = repo.get("alias")
            clone_path = repo.get("clone_path")

            if not alias or not clone_path:
                continue

            current_commit = read_current_commit(clone_path)
            if current_commit is not None:
                commit_hashes[alias] = current_commit

        return commit_hashes

    # ========================================================================
    # Story #193: Delta Refresh with Change Detection
    # ========================================================================

    def start_scheduler(self) -> None:
        """
        Start daemon thread for scheduled delta refresh (Story #193, AC1).

        Launches a daemon thread that polls every 60 seconds and triggers
        delta analysis when next_run time is reached.

        NOTE (Code Review M3): This diverges from DescriptionRefreshScheduler pattern
        by always starting the daemon thread regardless of enabled state. This is
        intentional to support AC6 runtime toggle - the scheduler loop checks
        dependency_map_enabled on each iteration, allowing users to enable/disable
        delta refresh via Web UI without server restart.
        """
        logger.info("Starting dependency map delta refresh scheduler")
        self._stop_event.clear()
        self._daemon_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._daemon_thread.start()

    def stop_scheduler(self) -> None:
        """
        Stop daemon thread for scheduled delta refresh (Story #193, AC1).

        Sets the stop event and waits for thread to terminate.
        """
        logger.info("Stopping dependency map delta refresh scheduler")
        self._stop_event.set()

        if self._daemon_thread and self._daemon_thread.is_alive():
            self._daemon_thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

    def _is_any_dep_map_job_in_flight(self) -> bool:
        """Story #927: re-entrance guard for the auto-repair scheduler.

        Returns True if any dep-map operation is currently pending or running.
        Covers all 4 dep-map operation types so the auto-repair logic does not
        race against an in-flight full/delta/refinement/repair job.

        Used by `_try_fire_scheduled_delta`, `_try_fire_scheduled_refinement`,
        and `_maybe_run_auto_repair_after_scheduled` (added in phase 2).
        """
        if self._job_tracker is None:
            return False
        active = self._job_tracker.get_active_jobs()
        dep_map_types = {
            "dependency_map_full",
            "dependency_map_delta",
            "dependency_map_refinement",
            "dependency_map_repair",
        }
        return any(job.operation_type in dep_map_types for job in active)

    def _is_cluster_mode(self) -> bool:
        """Story #927: True when a PG pool was injected (cluster deployment).

        Used by `_scheduler_decision_lock` to choose between PG advisory lock
        (cluster) and threading.Lock (solo) for atomic decision-claim windows.
        """
        return self._pg_pool is not None

    def _is_postgres_storage_mode(self) -> bool:
        """Story #927 Pass 2: True when server is configured for cluster (postgres) deployment.

        Distinct from _is_cluster_mode(): this checks the declared storage_mode
        parameter, while _is_cluster_mode() checks whether a pg_pool was actually
        injected. The anti-fallback guard compares the two to detect misconfiguration.
        """
        return self._storage_mode == "postgres"

    @contextmanager
    def _scheduler_decision_lock(self, key: str) -> Generator[bool, None, None]:
        """Story #927: Cluster-aware non-blocking decision lock for scheduler claims.

        Cluster (PG): pg_try_advisory_xact_lock — auto-released on transaction end.
        Solo (SQLite): threading.Lock per (instance, key) — process-local.

        Held only for the atomic claim window (in-flight check + register-job).
        Long-running work runs OUTSIDE the lock; JobTracker entry serves as the
        cross-node in-flight signal afterwards.

        Yields True if lock acquired, False if contended.
        """
        if self._is_cluster_mode():
            assert (
                self._pg_pool is not None
            )  # invariant: _is_cluster_mode() guarantees this
            with self._pg_pool.connection() as conn:
                with conn.transaction():
                    lock_id = self._stable_int_hash(f"dep_map_scheduler_{key}")
                    cur = conn.execute(
                        "SELECT pg_try_advisory_xact_lock(%s)", (lock_id,)
                    )
                    row = cur.fetchone()
                    acquired = bool(row[0]) if row else False
                    yield acquired
        else:
            with self._solo_decision_locks_lock:
                lock = self._solo_decision_locks.setdefault(key, threading.Lock())
            acquired = lock.acquire(blocking=False)
            try:
                yield acquired
            finally:
                if acquired:
                    lock.release()

    @staticmethod
    def _stable_int_hash(s: str) -> int:
        """Story #927: Deterministic 64-bit hash for PG advisory lock IDs.

        Uses MD5 truncated to 64 bits, converted to a signed integer that fits
        within PostgreSQL's bigint range [-2^63, 2^63-1]. Process-stable: same
        input always produces the same result regardless of PYTHONHASHSEED.
        """
        import hashlib

        h = hashlib.md5(s.encode()).hexdigest()[:16]
        val = int(h, 16)
        if val >= 2**63:
            val -= 2**64
        return val

    def _try_fire_scheduled_delta(self) -> None:
        """Story #927: Atomic claim of the scheduled delta trigger window.

        Acquires the decision lock and checks the in-flight re-entrance guard
        inside the lock. Releases the lock BEFORE running run_delta_analysis
        so the long-running work executes outside the atomic claim window.
        The JobTracker entry created by run_delta_analysis then serves as the
        cross-node in-flight signal for subsequent scheduler iterations.

        Design choice: run_delta_analysis manages its own JobTracker entry via
        register_job_if_no_conflict, so this helper does NOT pre-register.
        The decision lock + in-flight guard together form the atomic claim window.
        """
        with self._scheduler_decision_lock("delta") as acquired:
            if not acquired:
                logger.info(
                    "scheduled_delta_skipped_decision_lock_held",
                    extra={"event": "scheduled_delta_skipped_decision_lock_held"},
                )
                return
            if self._is_any_dep_map_job_in_flight():
                logger.info(
                    "scheduled_delta_skipped_reentrance",
                    extra={"event": "scheduled_delta_skipped_reentrance"},
                )
                return
            logger.info(
                "scheduled_delta_fired",
                extra={"event": "scheduled_delta_fired"},
            )
        # Lock released — long-running work runs OUTSIDE the atomic claim window
        self.run_delta_analysis()
        # Story #927 Phase 3: attempt auto-repair after scheduled delta
        self._maybe_run_auto_repair_after_scheduled("delta")

    def _try_fire_scheduled_refinement(self) -> None:
        """Story #927: Atomic claim of the scheduled refinement trigger window.

        Same decision lock + in-flight guard pattern as `_try_fire_scheduled_delta`.
        Releases the lock BEFORE running run_refinement_cycle so the long-running
        work executes outside the atomic claim window.

        Design choice: run_refinement_cycle manages its own concurrency via self._lock
        (non-blocking acquire inside it). This helper adds the cluster-aware decision
        lock layer on top to prevent duplicate fires across nodes.
        """
        with self._scheduler_decision_lock("refinement") as acquired:
            if not acquired:
                logger.info(
                    "scheduled_refinement_skipped_decision_lock_held",
                    extra={"event": "scheduled_refinement_skipped_decision_lock_held"},
                )
                return
            if self._is_any_dep_map_job_in_flight():
                logger.info(
                    "scheduled_refinement_skipped_reentrance",
                    extra={"event": "scheduled_refinement_skipped_reentrance"},
                )
                return
            logger.info(
                "scheduled_refinement_fired",
                extra={"event": "scheduled_refinement_fired"},
            )
        # Lock released — long-running work runs OUTSIDE the atomic claim window
        # Bug #931 Defect 2: delegate to run_tracked_refinement so scheduled runs register with JobTracker.
        self.run_tracked_refinement()
        # Story #927 Phase 3: attempt auto-repair after scheduled refinement
        self._maybe_run_auto_repair_after_scheduled("refinement")

    def _auto_repair_check_health(self, trigger_source: str) -> Optional[List[Any]]:
        """Story #927 Phase 3: health-check gate for auto-repair.

        Returns the anomalies list from the health report, or None when the
        gate should block repair (no health_check_fn, None result, or exception).
        All failure paths log at WARNING level (anti-fallback: never repair
        against unknown anomaly state).
        """
        if self._health_check_fn is None:
            logger.warning(
                "scheduled_auto_repair_skipped_no_health_check_fn",
                extra={"trigger": trigger_source},
            )
            return None

        try:
            health = self._health_check_fn()
        except Exception as exc:
            logger.warning(
                "scheduled_auto_repair_health_check_failed",
                extra={
                    "event": "scheduled_auto_repair_health_check_failed",
                    "trigger": trigger_source,
                    "error": str(exc),
                },
                exc_info=True,
            )
            return None

        if health is None:
            logger.warning(
                "scheduled_auto_repair_health_check_returned_none",
                extra={"trigger": trigger_source},
            )
            return None

        return getattr(health, "anomalies", None)

    def _auto_repair_fail_job(self, job_id: str, error: str) -> None:
        """Story #927 Phase 3: safely mark an auto-repair job as failed.

        Wraps fail_job so failures in the secondary error path are logged
        rather than silently swallowed.
        """
        if self._job_tracker is None:
            return
        try:
            self._job_tracker.fail_job(job_id, error=error)
        except Exception as inner_exc:
            logger.error(
                "scheduled_auto_repair_fail_job_error",
                extra={"job_id": job_id, "error": str(inner_exc)},
            )

    def _auto_repair_try_claim_job(self, trigger_source: str) -> Optional[str]:
        """Story #927 Phase 3: in-lock claim sequence for auto-repair.

        Must be called INSIDE _scheduler_decision_lock("auto_repair").
        Checks in-flight guard, runs health check gate, and registers
        a job if anomalies are present.

        Returns the registered job_id on success, or None when any gate
        blocks repair (logs the reason before returning None).
        """
        if self._is_any_dep_map_job_in_flight():
            logger.info(
                "scheduled_auto_repair_skipped_reentrance",
                extra={
                    "event": "scheduled_auto_repair_skipped_reentrance",
                    "trigger": trigger_source,
                },
            )
            return None

        anomalies = self._auto_repair_check_health(trigger_source)
        if anomalies is None:
            # Health gate blocked (no fn, exception, or None result) — already logged
            return None
        if not anomalies:
            logger.info(
                "scheduled_auto_repair_no_anomalies",
                extra={
                    "event": "scheduled_auto_repair_no_anomalies",
                    "trigger": trigger_source,
                },
            )
            return None

        if self._job_tracker is None:
            logger.warning("scheduled_auto_repair_skipped_no_job_tracker")
            return None

        new_job_id = (
            f"dep-map-auto-repair-{uuid.uuid4().hex[:_AUTO_REPAIR_JOB_ID_SUFFIX_LEN]}"
        )
        registered = self._job_tracker.register_job(
            job_id=new_job_id,
            operation_type="dependency_map_repair",
            username="system",
            metadata={
                "triggered_by": "scheduler_auto_repair",
                "trigger_source": trigger_source,
            },
        )
        logger.info(
            "scheduled_auto_repair_fired",
            extra={
                "event": "scheduled_auto_repair_fired",
                "trigger": trigger_source,
                "anomaly_count": len(anomalies),
                "job_id": registered.job_id,
            },
        )
        return str(registered.job_id)

    def _auto_repair_invoke(self, trigger_source: str, job_id: str) -> None:
        """Story #927 Phase 3: invoke repair fn outside the decision lock.

        Calls repair_invoker_fn(job_id). On missing fn or exception, marks
        the job failed via _auto_repair_fail_job and logs ERROR.
        """
        if self._repair_invoker_fn is None:
            logger.error(
                "scheduled_auto_repair_skipped_no_repair_invoker_fn",
                extra={"job_id": job_id},
            )
            self._auto_repair_fail_job(job_id, error="No repair invoker fn injected")
            return

        try:
            self._repair_invoker_fn(job_id)
            logger.info(
                "scheduled_auto_repair_started",
                extra={
                    "event": "scheduled_auto_repair_started",
                    "trigger": trigger_source,
                    "job_id": job_id,
                },
            )
        except Exception as exc:
            logger.error(
                "scheduled_auto_repair_start_failed",
                extra={
                    "event": "scheduled_auto_repair_start_failed",
                    "trigger": trigger_source,
                    "error": str(exc),
                },
                exc_info=True,
            )
            self._auto_repair_fail_job(job_id, error=str(exc))

    def _maybe_run_auto_repair_after_scheduled(self, trigger_source: str) -> None:
        """Story #927 Phase 3: 4-gate auto-repair after scheduled delta/refinement.

        Gates (order):
          1. Feature flag (dep_map_auto_repair_enabled) — opt-in, default False
          2. Decision lock ("auto_repair" key) — non-blocking, cluster-aware
          3. In-flight guard (no dep-map job pending/running) — inside lock
          4. Health check — non-empty anomalies list — inside lock

        Job is registered atomically inside the lock. Repair invocation runs
        outside the lock via _auto_repair_invoke. Manual triggers bypass this
        helper entirely — only the scheduler calls it.
        """
        config = self._config_manager.get_claude_integration_config()
        if not config or not getattr(config, "dep_map_auto_repair_enabled", False):
            logger.info(
                "scheduled_auto_repair_disabled",
                extra={
                    "event": "scheduled_auto_repair_disabled",
                    "trigger": trigger_source,
                },
            )
            return

        # Story #927 Pass 2: anti-fallback guard — cluster mode without pg_pool means
        # the decision lock silently degrades to a per-node threading.Lock, allowing
        # duplicate auto-repair jobs across nodes. Refuse loudly instead.
        if self._is_postgres_storage_mode() and self._pg_pool is None:
            logger.error(
                "scheduled_auto_repair_misconfigured_cluster_no_pg_pool",
                extra={
                    "trigger": trigger_source,
                    "reason": (
                        "Cluster deployment (storage_mode=postgres) has "
                        "dep_map_auto_repair_enabled=True but no pg_pool injected. "
                        "Decision lock would silently degrade to per-node solo lock, "
                        "allowing duplicate auto-repair jobs across nodes. Refusing to fire."
                    ),
                },
            )
            return

        job_id: Optional[str] = None

        with self._scheduler_decision_lock("auto_repair") as acquired:
            if not acquired:
                logger.info(
                    "scheduled_auto_repair_skipped_decision_lock_held",
                    extra={
                        "event": "scheduled_auto_repair_skipped_decision_lock_held",
                        "trigger": trigger_source,
                    },
                )
                return
            job_id = self._auto_repair_try_claim_job(trigger_source)

        # Lock released — run repair OUTSIDE the atomic claim window
        if job_id is not None:
            self._auto_repair_invoke(trigger_source, job_id)

    def _scheduler_loop(self) -> None:
        """
        Main scheduler loop for delta refresh (Story #193, AC1; refactored Story #927).

        Polls every 60 seconds, checks if delta refresh should run based
        on next_run timestamp and dependency_map_enabled config.

        Story #927: Inline trigger calls replaced with _try_fire_scheduled_delta()
        and _try_fire_scheduled_refinement() which add cluster-aware decision locks
        and re-entrance guards around the actual analysis invocations.
        """
        while not self._stop_event.is_set():
            try:
                # Check if enabled (config may change at runtime - AC6)
                config = self._config_manager.get_claude_integration_config()
                if not config or not config.dependency_map_enabled:
                    logger.debug(
                        "Dependency map disabled, skipping scheduled delta refresh"
                    )
                    self._stop_event.wait(SCHEDULER_POLL_INTERVAL_SECONDS)
                    continue

                # Check if next_run is reached
                tracking = self._tracking_backend.get_tracking()
                next_run_str = tracking.get("next_run")

                if next_run_str:
                    next_run = datetime.fromisoformat(next_run_str)
                    now = datetime.now(timezone.utc)

                    if now >= next_run:
                        self._try_fire_scheduled_delta()

                else:
                    # No next_run set yet — wait for user to trigger manually
                    # or for a successful analysis to schedule the next run.
                    logger.debug(
                        "Delta refresh: no next_run scheduled, waiting for manual trigger"
                    )

                # Story #359: Check refinement schedule (independent of delta analysis)
                try:
                    ref_config = self._config_manager.get_claude_integration_config()
                    if ref_config and ref_config.refinement_enabled:
                        ref_tracking = self._tracking_backend.get_tracking()
                        refinement_next = ref_tracking.get("refinement_next_run")
                        now = datetime.now(timezone.utc)

                        if refinement_next:
                            ref_next_dt = datetime.fromisoformat(refinement_next)
                            if now >= ref_next_dt:
                                # Bug #931: duplicate update_tracking(refinement_next_run=...)
                                # removed here. run_refinement_cycle (called via
                                # run_tracked_refinement inside _try_fire_scheduled_refinement)
                                # now owns the stamp unconditionally on success.
                                self._try_fire_scheduled_refinement()
                        else:
                            # No refinement_next_run set yet. Bug #931 fix: any successful
                            # run_refinement_cycle (manual or scheduled) now seeds the schedule.
                            logger.debug(
                                "Refinement: no refinement_next_run scheduled, waiting for manual trigger"
                            )
                except Exception as ref_e:
                    logger.error(
                        f"Error in refinement scheduler: {ref_e}", exc_info=True
                    )

            except Exception as e:
                logger.error(
                    f"Error in dependency map scheduler loop: {e}", exc_info=True
                )

            # Sleep 60 seconds between checks
            self._stop_event.wait(SCHEDULER_POLL_INTERVAL_SECONDS)

    def detect_changes(
        self,
        progress_callback=None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """
        Detect changed, new, and removed repos via commit hash comparison (Story #193, AC2).

        Compares stored commit hashes from tracking table with current repo commits
        in metadata.json files.

        Args:
            progress_callback: Optional callable(completed: int, total: int) forwarded
                               to _enrich_repo_sizes. Defaults to None.

        Returns:
            Tuple of (changed_repos, new_repos, removed_repos) where:
            - changed_repos: List of repo dicts with alias and clone_path (commit hash changed)
            - new_repos: List of repo dicts (not in stored hashes)
            - removed_repos: List of repo aliases (in stored but not in current repos)
        """
        tracking = self._tracking_backend.get_tracking()
        stored_hashes_json = tracking.get("commit_hashes")

        # Parse stored hashes (may be None for first run)
        stored_hashes = {}
        if stored_hashes_json:
            try:
                stored_hashes = json.loads(stored_hashes_json)
            except json.JSONDecodeError:
                logger.warning(
                    "Failed to parse stored commit hashes, treating as empty"
                )
                stored_hashes = {}

        # Get current repos
        current_repos = self._get_activated_repos()
        # Apply same empty-repo filter as analysis pipeline (_enrich_repo_sizes).
        # Empty repos never get tracked in commit_hashes, so without this filter
        # they perpetually appear as "new" repos triggering degraded health.
        current_repos = self._enrich_repo_sizes(
            current_repos, progress_callback=progress_callback
        )

        changed_repos = []
        new_repos = []

        # Check each current repo
        for repo in current_repos:
            alias = repo.get("alias")
            clone_path = repo.get("clone_path")

            if not alias or not clone_path:
                continue

            # Read current commit hash from provider-aware metadata (Bug #890)
            current_hash = read_current_commit(clone_path)

            # Bug #890 post-deploy note: on the first delta run after this fix
            # lands, every repo will flip to CHANGED because stored_hashes may
            # still carry "local"/"unknown" sentinels from the pre-fix period
            # while current_hash will now hold real SHAs. This is expected and
            # self-healing — the tracking table gets rewritten with real SHAs
            # on that run, and normal behavior resumes thereafter. Rate-limiting
            # in ClaudeCliManager protects against stampede.
            if alias not in stored_hashes:
                # New repo (not in previous analysis)
                new_repos.append(repo)
            elif current_hash and current_hash != stored_hashes.get(alias):
                # Changed repo (different commit hash)
                changed_repos.append(repo)

        # Find removed repos (in stored but not in current)
        current_aliases = {repo.get("alias") for repo in current_repos}
        removed_repos = [
            alias for alias in stored_hashes.keys() if alias not in current_aliases
        ]

        logger.info(
            f"Change detection: {len(changed_repos)} changed, "
            f"{len(new_repos)} new, {len(removed_repos)} removed"
        )

        return changed_repos, new_repos, removed_repos

    def identify_affected_domains(
        self,
        changed_repos: List[Dict[str, Any]],
        new_repos: List[Dict[str, Any]],
        removed_repos: List[str],
    ) -> Set[str]:
        """
        Identify affected domains from _index.md repo-to-domain mapping (Story #193, AC2/3/4).

        Parses the _index.md file to determine which domains need delta refresh
        based on changed, new, or removed repos.

        Args:
            changed_repos: List of changed repo dicts
            new_repos: List of new repo dicts
            removed_repos: List of removed repo aliases

        Returns:
            Set of affected domain names (may include __NEW_REPO_DISCOVERY__ marker)
        """
        # Use versioned path for reads: cidx-meta is a versioned golden repo since Story #224
        cidx_meta_read_path = self._get_cidx_meta_read_path()
        index_file = cidx_meta_read_path / "dependency-map" / "_index.md"

        if not index_file.exists():
            logger.warning("_index.md not found, cannot identify affected domains")
            if new_repos:
                return {"__NEW_REPO_DISCOVERY__"}
            return set()

        # Parse _index.md to build repo-to-domain mapping
        repo_to_domains = self._parse_repo_to_domain_mapping(index_file)

        affected_domains = set()

        # Map changed repos to their domains
        for repo in changed_repos:
            alias = repo.get("alias")
            if alias in repo_to_domains:
                affected_domains.update(repo_to_domains[alias])

        # Map new repos to their domains (or flag for discovery)
        for repo in new_repos:
            alias = repo.get("alias")
            if alias in repo_to_domains:
                affected_domains.update(repo_to_domains[alias])
            else:
                # New repo not in index - needs domain discovery
                affected_domains.add("__NEW_REPO_DISCOVERY__")

        # Map removed repos to their domains (for cleanup)
        for alias in removed_repos:
            if alias in repo_to_domains:
                affected_domains.update(repo_to_domains[alias])

        logger.info(f"Identified {len(affected_domains)} affected domains")

        return affected_domains

    def _parse_repo_to_domain_mapping(self, index_file: Path) -> Dict[str, List[str]]:
        """
        Parse _index.md to extract repo-to-domain mapping.

        Parses both YAML frontmatter (repos_analyzed list) and markdown table
        (Repo-to-Domain Matrix) to build the mapping.

        Args:
            index_file: Path to _index.md

        Returns:
            Dict mapping repo alias to list of domain names
        """
        content = index_file.read_text()

        # Strategy: Parse the "Repo-to-Domain Matrix" table from markdown
        # Table format:
        # | Repository | Domains |
        # |------------|---------|
        # | repo1 | authentication |
        # | repo2 | authentication, data-processing |

        repo_to_domains: dict[str, list[str]] = {}

        # Find table section
        table_match = re.search(
            r"##\s+Repo-to-Domain Matrix\s*\n\n(.*?)(?=\n##|\Z)",
            content,
            re.DOTALL,
        )

        if not table_match:
            logger.warning("Repo-to-Domain Matrix not found in _index.md")
            return repo_to_domains

        table_text = table_match.group(1)

        # Parse table rows (skip header and separator)
        lines = table_text.strip().split("\n")
        for line in lines[2:]:  # Skip header and separator
            line = line.strip()
            if not line or not line.startswith("|"):
                continue

            # Split by | and extract columns
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3:
                continue

            repo_alias = parts[1].strip()
            domains_str = parts[2].strip()

            # Parse comma-separated domains
            domains = [d.strip() for d in domains_str.split(",")]

            repo_to_domains[repo_alias] = domains

        return repo_to_domains

    def _update_frontmatter_timestamp(
        self, existing_content: str, new_body: str, domain_name: str
    ) -> str:
        """
        Update last_analyzed timestamp in YAML frontmatter (Story #193).

        Args:
            existing_content: Original domain file content with frontmatter
            new_body: New content body from Claude CLI
            domain_name: Domain name

        Returns:
            Complete updated content with frontmatter + new body
        """
        now = datetime.now(timezone.utc).isoformat()

        # Parse existing frontmatter
        frontmatter_match = re.match(
            r"^---\n(.*?)\n---\n(.*)$", existing_content, re.DOTALL
        )

        if frontmatter_match:
            # Update last_analyzed in existing frontmatter
            frontmatter_text = frontmatter_match.group(1)
            frontmatter_lines = frontmatter_text.split("\n")
            updated_lines = []
            found_last_analyzed = False

            for line in frontmatter_lines:
                if line.startswith("last_analyzed:"):
                    updated_lines.append(f"last_analyzed: {now}")
                    found_last_analyzed = True
                else:
                    updated_lines.append(line)

            if not found_last_analyzed:
                updated_lines.append(f"last_analyzed: {now}")

            new_frontmatter = "\n".join(updated_lines)
            return f"---\n{new_frontmatter}\n---\n\n{new_body}"
        else:
            # No frontmatter found, create minimal one
            return (
                f"---\ndomain: {domain_name}\nlast_analyzed: {now}\n---\n\n{new_body}"
            )

    def _update_domain_file(
        self,
        domain_name: str,
        domain_file: Path,
        changed_repos: List[str],
        new_repos: List[str],
        removed_repos: List[str],
        domain_list: List[str],
        config,
        read_file: Optional[Path] = None,
    ) -> "_DomainUpdateResult":
        """
        Update a single domain file with delta analysis (Story #193, AC5).

        Args:
            domain_name: Name of the domain
            domain_file: Path to write updated domain .md file (live path)
            changed_repos: List of changed repo aliases
            new_repos: List of new repo aliases
            removed_repos: List of removed repo aliases
            domain_list: Full list of all domain names
            config: Claude integration config
            read_file: Optional path to read existing content from (versioned path).
                       When provided, existing content is read from this path while
                       the updated content is written to domain_file (live path).
                       Falls back to domain_file when None.

        Returns:
            _DomainUpdateResult.WRITTEN  — file was updated successfully
            _DomainUpdateResult.NOOP     — Claude signalled FILE_UNCHANGED (intentional no-op)
            _DomainUpdateResult.FAILED   — invocation failure; caller may retry
        """
        # Read existing content from versioned path if provided, else from write path
        source_file = read_file if read_file is not None else domain_file
        full_content = source_file.read_text()

        # Bug #834 (Step 1): strip frontmatter at the service boundary so that neither
        # the prompt nor the temp file seen by Claude contains frontmatter delimiters.
        # _update_frontmatter_timestamp receives full_content (with frontmatter) to
        # parse/update the timestamp and reconstruct the final file correctly.
        existing_body = _strip_leading_yaml_frontmatter(full_content)

        # Build delta merge prompt (Story #329: pass journal_path for activity journal appendix)
        merge_prompt = self._analyzer.build_delta_merge_prompt(
            domain_name=domain_name,
            existing_content=existing_body,
            changed_repos=changed_repos,
            new_repos=new_repos,
            removed_repos=removed_repos,
            domain_list=domain_list,
            journal_path=self._activity_journal.journal_path,
        )

        # Story #715: File-based delta merge — Claude edits temp file in-place
        result = self._analyzer.invoke_delta_merge_file(
            domain_name=domain_name,
            existing_content=existing_body,
            merge_prompt=merge_prompt,
            timeout=config.dependency_map_pass_timeout_seconds,
            max_turns=config.dependency_map_delta_max_turns,
            temp_dir=domain_file.parent,
        )

        if result is None:
            logger.info(
                f"Delta merge returned no changes for domain '{domain_name}', "
                f"preserving existing content."
            )
            return _DomainUpdateResult.FAILED  # invocation failure — caller may retry

        if result == _DELTA_NOOP:
            logger.info(
                f"Delta merge confirmed no-op for domain '{domain_name}' — FILE_UNCHANGED signal."
            )
            return _DomainUpdateResult.NOOP  # intentional no-op — caller must NOT retry

        # Update frontmatter timestamp — needs full_content (with frontmatter) to parse
        # existing metadata; result is body-only from invoke_delta_merge_file.
        updated_content = self._update_frontmatter_timestamp(
            full_content, result, domain_name
        )

        # Story #724 v2: verify BEFORE writing to the live domain_file
        final_content = updated_content  # default: unverified when flag off
        if config.dep_map_fact_check_enabled:
            import tempfile

            # Build a minimal repo_list from the aliases involved in this delta
            all_aliases = list(dict.fromkeys(changed_repos + new_repos + removed_repos))
            delta_repo_list = [{"alias": a} for a in all_aliases]

            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".md",
                dir=str(domain_file.parent),
                delete=False,
                encoding="utf-8",
            ) as tmp:
                tmp.write(updated_content)
                tmp_path = Path(tmp.name)

            try:
                self._run_verification_pass(
                    document_path=tmp_path,
                    repo_list=delta_repo_list,
                    context_label=f"delta_merge:{domain_name}",
                )
                final_content = tmp_path.read_text(encoding="utf-8")
            finally:
                tmp_path.unlink(missing_ok=True)

        # Single write with (verified or original) content
        domain_file.write_text(final_content)

        logger.info(f"Updated domain file in-place: {domain_file}")
        return _DomainUpdateResult.WRITTEN

    def _update_affected_domains(
        self,
        affected_domains: Set[str],
        dependency_map_dir: Path,
        changed_repos: List[Dict[str, Any]],
        new_repos: List[Dict[str, Any]],
        removed_repos: List[str],
        config,
    ) -> List[str]:
        """
        Update all affected domain files (Story #193, AC5).

        Args:
            affected_domains: Set of domain names to update
            dependency_map_dir: Path to dependency-map directory
            changed_repos: List of changed repo dicts
            new_repos: List of new repo dicts
            removed_repos: List of removed repo aliases
            config: Claude integration config

        Returns:
            List of error messages (empty if all successful)
        """
        errors: list[str] = []
        changed_aliases = [r["alias"] for r in changed_repos]
        new_aliases = [r["alias"] for r in new_repos]

        # Build full domain list from ALL domain files (Code Review H2: cross-domain awareness)
        # Claude needs the complete domain landscape, not just affected domains
        # READ from versioned path: live path is empty after Story #224
        dependency_map_read_dir = self._get_cidx_meta_read_path() / "dependency-map"
        domain_list = (
            [
                f.stem
                for f in dependency_map_read_dir.glob("*.md")
                if not f.name.startswith("_")
            ]
            if dependency_map_read_dir.exists()
            else []
        )

        # Code Review M4: Sort for deterministic processing order
        total_affected = len(affected_domains)
        for domain_idx, domain_name in enumerate(sorted(affected_domains)):
            # READ existence check and content from versioned path (live path is empty after Story #224)
            read_domain_file = dependency_map_read_dir / f"{domain_name}.md"
            # WRITE updated file to live path (so RefreshScheduler detects changes)
            domain_file = dependency_map_dir / f"{domain_name}.md"

            if not read_domain_file.exists():
                logger.warning(f"Domain file not found: {read_domain_file}, skipping")
                continue

            try:
                self._activity_journal.log(
                    f"Delta: updating domain {domain_idx + 1}/{total_affected}: {domain_name}"
                )
            except Exception as e:
                logger.debug(f"Non-fatal journal log error: {e}")

            for attempt in range(1, MAX_DOMAIN_RETRIES + 1):
                update_result = self._update_domain_file(
                    domain_name=domain_name,
                    domain_file=domain_file,
                    changed_repos=changed_aliases,
                    new_repos=new_aliases,
                    removed_repos=removed_repos,
                    domain_list=domain_list,
                    config=config,
                    read_file=read_domain_file,
                )

                if update_result == _DomainUpdateResult.WRITTEN:
                    try:
                        self._activity_journal.log(
                            f"Delta: domain {domain_idx + 1}/{total_affected} complete"
                        )
                    except Exception as e:
                        logger.debug(f"Non-fatal journal log error: {e}")
                    break  # Success

                if update_result == _DomainUpdateResult.NOOP:
                    break

                if update_result == _DomainUpdateResult.FAILED:
                    if attempt < MAX_DOMAIN_RETRIES:
                        try:
                            self._activity_journal.log(
                                f"Delta: domain {domain_idx + 1}/{total_affected} not updated, "
                                f"retrying (attempt {attempt + 1}/{MAX_DOMAIN_RETRIES})"
                            )
                        except Exception as e:
                            logger.debug(f"Non-fatal journal log error: {e}")
                        logger.warning(
                            f"Delta domain '{domain_name}' not updated on attempt {attempt}/{MAX_DOMAIN_RETRIES}, retrying"
                        )
                    else:
                        try:
                            self._activity_journal.log(
                                f"Delta: domain {domain_idx + 1}/{total_affected} not updated after {MAX_DOMAIN_RETRIES} attempts"
                            )
                        except Exception as e:
                            logger.debug(f"Non-fatal journal log error: {e}")
                        logger.warning(
                            f"Delta domain '{domain_name}' not updated after {MAX_DOMAIN_RETRIES} attempts"
                        )

        return errors

    def _discover_and_assign_new_repos(
        self,
        new_repos: List[Dict[str, Any]],
        existing_domains: List[str],
        dependency_map_dir: Path,
        config,
    ) -> Tuple[Set[str], bool]:
        """
        Discover which domains new repos belong to and update _domains.json (AC6, Story #216).

        Invokes Claude CLI with a domain discovery prompt to determine which existing
        domain(s) each new repo belongs to, then updates _domains.json accordingly.

        Args:
            new_repos: List of new repo dicts with alias and clone_path
            existing_domains: List of existing domain names
            dependency_map_dir: Path to dependency-map directory containing _domains.json
            config: Claude integration config

        Returns:
            Tuple of (affected domain names, write_success) where:
            - affected: Set of domain names that need re-analysis
            - write_success: True if _domains.json was written successfully, False on write failure.
              When False, new repos should not be finalized in tracking so they are
              re-detected as new on the next delta run.
        """
        affected: Set[str] = set()

        # Story #329: pass journal_path for activity journal appendix in prompt
        prompt = self._analyzer.build_domain_discovery_prompt(
            new_repos,
            existing_domains,
            journal_path=self._activity_journal.journal_path,
        )

        try:
            result = self._analyzer.invoke_domain_discovery(
                prompt,
                config.dependency_map_pass_timeout_seconds,
                config.dependency_map_delta_max_turns,
            )
            from code_indexer.global_repos.dependency_map_analyzer import (
                DependencyMapAnalyzer,
            )

            assignments = DependencyMapAnalyzer._extract_json(result)
        except Exception as e:
            logger.warning(f"Domain discovery failed for new repos: {e}")
            return affected, True

        if not isinstance(assignments, list):
            logger.warning(
                "Domain discovery returned non-list JSON, skipping assignment"
            )
            return affected, True

        # READ current _domains.json from versioned path (Story #224)
        read_domains_file = (
            self._get_cidx_meta_read_path() / "dependency-map" / "_domains.json"
        )
        if not read_domains_file.exists():
            logger.info(
                "_domains.json not found, starting with empty domain list for new repo assignment"
            )
            domain_list = []
        else:
            try:
                domain_list = json.loads(read_domains_file.read_text())
            except Exception as e:
                logger.warning(
                    f"Failed to read _domains.json for new repo assignment: {e}"
                )
                return affected, True

        # Apply assignments and write updated _domains.json (Fix 2, Bug #687)
        affected = self._apply_domain_assignments(
            assignments=assignments,
            domain_list=domain_list,
            dependency_map_dir=dependency_map_dir,
        )
        logger.info(
            f"Updated _domains.json with {len(new_repos)} new repo(s): "
            f"affected domains: {affected}"
        )
        return affected, True

    def _make_new_domain_entry(
        self, domain_name: str, repo_alias: str
    ) -> Dict[str, Any]:
        """
        Build a minimal domain dict for a brand-new domain (Fix 2, Bug #687).

        Sets needs_reanalysis=True so Check 8 flags it for Phase 3.5 backfill
        rather than leaving description="" silently in _domains.json.
        """
        return {
            "name": domain_name,
            "description": "",
            "participating_repos": [repo_alias],
            "evidence": "",
            "needs_reanalysis": True,
        }

    def _apply_domain_assignments(
        self,
        assignments: List[Dict[str, Any]],
        domain_list: List[Dict[str, Any]],
        dependency_map_dir: Path,
    ) -> Set[str]:
        """
        Apply Claude's assignment list to domain_list and persist _domains.json.

        Adds repos to existing domains or creates new domain entries flagged with
        needs_reanalysis=True (Fix 2, Bug #687). Writes updated _domains.json to disk.

        Returns the set of domain names that were affected.
        """
        affected: Set[str] = set()
        domain_by_name = {d["name"]: d for d in domain_list}

        for assignment in assignments:
            repo_alias = assignment.get("repo")
            assigned_domains = assignment.get("domains", [])
            if not repo_alias or not assigned_domains:
                continue
            for domain_name in assigned_domains:
                if domain_name in domain_by_name:
                    repos = domain_by_name[domain_name].setdefault(
                        "participating_repos", []
                    )
                    if repo_alias not in repos:
                        repos.append(repo_alias)
                    logger.info(
                        f"Assigned repo '{repo_alias}' to domain '{domain_name}'"
                    )
                else:
                    new_entry = self._make_new_domain_entry(domain_name, repo_alias)
                    domain_list.append(new_entry)
                    domain_by_name[domain_name] = new_entry
                    logger.info(
                        f"Created new domain '{domain_name}' for repo '{repo_alias}'"
                    )
                affected.add(domain_name)

        write_file = Path(dependency_map_dir) / "_domains.json"
        try:
            Path(dependency_map_dir).mkdir(parents=True, exist_ok=True)
            write_file.write_text(json.dumps(domain_list, indent=2))
        except OSError as e:
            logger.warning(f"Failed to write _domains.json: {e}")

        return affected

    def _remove_stale_repos_from_domains_json(
        self,
        removed_repos: List[str],
        dependency_map_dir: Path,
    ) -> bool:
        """Remove stale repo aliases from _domains.json participating_repos (Bug #396).

        Deterministic Python cleanup — not LLM-dependent. Called after domain .md
        updates complete, before finalization.

        Args:
            removed_repos: List of removed repo aliases
            dependency_map_dir: Path to live dependency-map directory for writing

        Returns:
            True if cleanup succeeded or was not needed, False on write failure
        """
        if not removed_repos:
            return True

        removed_set = set(removed_repos)

        # READ from versioned path (consistent with other read patterns)
        read_domains_file = (
            self._get_cidx_meta_read_path() / "dependency-map" / "_domains.json"
        )
        if not read_domains_file.exists():
            return True

        try:
            domain_list = json.loads(read_domains_file.read_text())
        except Exception as e:
            logger.warning(f"Failed to read _domains.json for stale repo cleanup: {e}")
            return False

        # Strip removed aliases from every domain's participating_repos
        modified = False
        for domain in domain_list:
            repos = domain.get("participating_repos", [])
            filtered = [r for r in repos if r not in removed_set]
            if len(filtered) != len(repos):
                domain["participating_repos"] = filtered
                removed_from_domain = set(repos) - set(filtered)
                logger.info(
                    f"Removed stale repo(s) {removed_from_domain} from domain "
                    f"'{domain.get('name', '?')}'"
                )
                modified = True

        if not modified:
            return True

        # WRITE to live path
        write_domains_file = dependency_map_dir / "_domains.json"
        try:
            dependency_map_dir.mkdir(parents=True, exist_ok=True)
            write_domains_file.write_text(json.dumps(domain_list, indent=2))
            logger.info(
                f"Cleaned _domains.json: removed {len(removed_set)} stale repo alias(es)"
            )
            return True
        except Exception as e:
            logger.warning(
                f"Failed to write _domains.json after stale repo cleanup: {e}"
            )
            return False

    def _finalize_delta_tracking(
        self,
        config,
        all_repos: List[Dict[str, Any]],
        output_dir: Optional[Path] = None,
        affected_domains: Optional[Set[str]] = None,
        detect_s: float = 0.0,
        merge_s: float = 0.0,
    ) -> None:
        """
        Finalize delta analysis tracking updates (Story #193, AC8).

        Bug #572: Now also records run metrics so delta runs appear in the
        Recent Run Metrics dashboard table.

        Bug #874 Story A: detect_s and merge_s carry real wall-clock timings from
        run_delta_analysis so the Recent Run Metrics dashboard shows honest numbers.
        Column mapping (no schema change — Story B adds phase_timings_json):
          detect_s  -> pass1_duration_s  (change-detection phase)
          merge_s   -> pass2_duration_s  (per-domain Claude-CLI merge phase)

        Args:
            config: Claude integration config
            all_repos: List of all current repos
            output_dir: Dependency map output directory (for metric computation)
            affected_domains: Set of domain names updated in this delta run
            detect_s: Wall-clock seconds spent in detect_changes() (P1-equivalent)
            merge_s: Wall-clock seconds spent in _update_affected_domains() (P2-equivalent);
                     legitimately 0.0 on the no-affected-domains early-return branch
        """
        commit_hashes = self._get_commit_hashes(all_repos) if all_repos else {}
        next_run = (
            datetime.now(timezone.utc)
            + timedelta(hours=config.dependency_map_interval_hours)
        ).isoformat()

        self._tracking_backend.update_tracking(
            status="completed",
            commit_hashes=json.dumps(commit_hashes) if commit_hashes else None,
            next_run=next_run,
            error_message=None,
        )

        # Bug #572: Record run metrics for delta analysis so they appear
        # in the Recent Run Metrics dashboard table.
        # Bug #874 Story A: pass real detect_s/merge_s instead of hardcoded 0.0/0.0.
        # Bug #874 Story C: add run_type="delta", phase_timings_json, and repos_skipped.
        # Bug #930: finalize_s removed — not a meaningful user-visible phase.
        if output_dir is not None and affected_domains is not None:
            domain_list = [{"name": d} for d in affected_domains]
            # TODO #874: repos_skipped could be derived by walking the domains-to-repos
            # mapping; deferred — 0 is a non-None, non-negative int (FR6 contract met).
            self._record_run_metrics(
                output_dir,
                domain_list,
                all_repos,
                detect_s,
                merge_s,
                run_type="delta",
                phase_timings_json=json.dumps(
                    {
                        "detect_s": detect_s,
                        "merge_s": merge_s,
                    }
                ),
                repos_skipped=0,
            )

    def run_delta_analysis(
        self, job_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Run delta analysis to refresh only affected domains (Story #193, AC1-8).

        Args:
            job_id: Optional caller-provided job ID for unified job tracking.
                    When None, a new UUID-based ID is generated internally.
                    MCP handler passes its own job_id for consistency (AC4).

        Returns:
            Dict with status or None if skipped (lock held or disabled)

        Raises:
            DuplicateJobError: If job_tracker detects a concurrent delta analysis (AC6)
        """
        # Story #876 Phase B-1 Deliverable 3: cluster-atomic job gate.
        # Replaces the Story #312 TOCTOU pattern.  register_job_if_no_conflict
        # is backed by the partial unique index idx_active_job_per_repo so
        # duplicate detection is atomic at the DB layer — no read-then-write
        # race window across cluster nodes.  DuplicateJobError propagates
        # unchanged (AC6); all other tracker errors are absorbed.
        from .job_tracker import DuplicateJobError

        _tracked_job_id: Optional[str] = None
        if self._job_tracker is not None:
            _tracked_job_id = job_id or f"dep-map-delta-{uuid.uuid4().hex[:8]}"
            try:
                self._job_tracker.register_job_if_no_conflict(
                    job_id=_tracked_job_id,
                    operation_type="dependency_map_delta",
                    username="system",
                    repo_alias="server",
                )
            except DuplicateJobError:
                raise  # AC6: Propagate conflict to caller unchanged
            except Exception as tracker_err:
                logger.warning(
                    f"JobTracker register_job_if_no_conflict failed (non-fatal): {tracker_err}"
                )
                _tracked_job_id = None
            if _tracked_job_id is not None:
                try:
                    self._job_tracker.update_status(_tracked_job_id, status="running")
                except Exception as tracker_err:
                    logger.warning(
                        f"JobTracker update_status (running) failed (non-fatal): {tracker_err}"
                    )

        # Story #876 Phase B-1 Deliverable 3: lifecycle fleet pre-flight.
        # Mirror image of the run_full_analysis pre-flight: before acquiring
        # the dep-map lock, scan the golden-repo fleet for broken/missing
        # cidx-meta lifecycle metadata; if any aliases are flagged, run one
        # unified Claude CLI call per repo via LifecycleBatchRunner to repair
        # them.  Four conditions are required: job_tracker, lifecycle_invoker,
        # lifecycle_debouncer, AND _tracked_job_id (the latter because
        # LifecycleBatchRunner.run's parent_job_id argument is mandatory).
        if (
            self._job_tracker is not None
            and self._lifecycle_invoker is not None
            and self._lifecycle_debouncer is not None
            and _tracked_job_id is not None
        ):
            repo_aliases = [
                r.get("alias")
                for r in self._golden_repos_manager.list_golden_repos()
                if r.get("alias")
            ]
            scanner = LifecycleFleetScanner(
                golden_repos_dir=self._golden_repos_manager.golden_repos_dir,
                repo_aliases=repo_aliases,
            )
            broken = scanner.find_broken_or_missing()
            if broken:
                runner = LifecycleBatchRunner(
                    golden_repos_dir=self._golden_repos_manager.golden_repos_dir,
                    job_tracker=self._job_tracker,
                    refresh_scheduler=self._refresh_scheduler,
                    debouncer=self._lifecycle_debouncer,
                    claude_cli_invoker=self._lifecycle_invoker,
                )
                runner.run(broken, parent_job_id=_tracked_job_id)

        # Non-blocking lock acquire (AC7: Concurrency Protection)
        if not self._lock.acquire(blocking=False):
            logger.info("Delta analysis skipped - analysis already in progress")
            # Story #312: Complete the registered job since this run is skipped.
            if _tracked_job_id is not None and self._job_tracker is not None:
                try:
                    self._job_tracker.complete_job(_tracked_job_id)
                except Exception as tracker_err:
                    logger.warning(
                        f"JobTracker complete_job (lock skip) failed (non-fatal): {tracker_err}"
                    )
            return None

        # Story #227: Acquire write lock so RefreshScheduler skips CoW clone during writes.
        _write_lock_acquired = False
        if self._refresh_scheduler is not None:
            _write_lock_acquired = self._refresh_scheduler.acquire_write_lock(
                "cidx-meta", owner_name="dependency_map_service"
            )

        _delta_succeeded = False
        try:
            # Check if enabled (AC6: Runtime Configuration Check)
            config = self._config_manager.get_claude_integration_config()
            if not config or not config.dependency_map_enabled:
                logger.debug("Delta analysis skipped - dependency_map_enabled is False")
                _delta_succeeded = (
                    True  # Story #312: Mark succeeded so finally completes the job
                )
                return None

            # Story #329: Initialize activity journal for this delta analysis run
            try:
                delta_journal_dir = Path(
                    os.path.expanduser("~/.tmp/depmap-delta-journal/")
                )
                self._activity_journal.init(delta_journal_dir)
                self._activity_journal.log("Starting delta analysis")
            except Exception as e:
                logger.debug(f"Non-fatal journal init error: {e}")

            # Story #312 AC5: Progress update before change detection
            if _tracked_job_id is not None and self._job_tracker is not None:
                try:
                    self._job_tracker.update_status(
                        _tracked_job_id, progress=10, progress_info="Detecting changes"
                    )
                except Exception as e:
                    logger.debug(
                        f"Non-fatal: Failed to update progress (detecting changes): {e}"
                    )
            try:
                self._activity_journal.log("Detecting changes")
            except Exception as e:
                logger.debug(f"Non-fatal journal log error: {e}")

            # Detect changes (AC2: Change Detection)
            # Bug #874 Story A: time the detect phase so the dashboard shows real numbers.
            # detect_s maps to pass1_duration_s column (Story B will add phase_timings_json).
            t_detect_start = time.time()
            changed_repos, new_repos, removed_repos = self.detect_changes()
            detect_s = time.time() - t_detect_start

            total_changes = len(changed_repos) + len(new_repos) + len(removed_repos)
            try:
                self._activity_journal.log(
                    f"Detected {total_changes} changes: {len(changed_repos)} changed, "
                    f"{len(new_repos)} new, {len(removed_repos)} removed repos"
                )
            except Exception as e:
                logger.debug(f"Non-fatal journal log error: {e}")

            # Story #716: Detect uncovered repos via health check before processing
            uncovered_repo_aliases: Set[str] = set()
            uncov_read_path = self._get_cidx_meta_read_path() / "dependency-map"
            all_activated_repos = self._get_activated_repos()
            if uncov_read_path.exists():
                try:
                    known_aliases = {
                        r.get("alias", r.get("name", "")) for r in all_activated_repos
                    }
                    known_aliases.discard("")
                    if known_aliases:
                        hd = DepMapHealthDetector()
                        hr = hd.detect(uncov_read_path, known_repos=known_aliases)
                        for anomaly in hr.anomalies:
                            if (
                                anomaly.type == "uncovered_repo"
                                and anomaly.missing_repos
                            ):
                                uncovered_repo_aliases.update(anomaly.missing_repos)
                        if uncovered_repo_aliases:
                            logger.info(
                                f"Story #716: Detected {len(uncovered_repo_aliases)} "
                                f"uncovered repos: {sorted(uncovered_repo_aliases)}"
                            )
                except Exception as e:
                    logger.warning(f"Uncovered repo detection failed (non-fatal): {e}")

            # Skip if no changes AND no uncovered repos
            if (
                not changed_repos
                and not new_repos
                and not removed_repos
                and not uncovered_repo_aliases
            ):
                logger.info("No changes detected, skipping delta analysis")
                next_run = (
                    datetime.now(timezone.utc)
                    + timedelta(hours=config.dependency_map_interval_hours)
                ).isoformat()
                self._tracking_backend.update_tracking(
                    status="completed",
                    next_run=next_run,
                    error_message=None,  # Bug #437: clear stale error from orphan recovery
                )
                _delta_succeeded = True
                return {
                    "status": "skipped",
                    "message": "No changes detected",
                }

            # Update tracking to running
            self._tracking_backend.update_tracking(
                status="running",
                last_run=datetime.now(timezone.utc).isoformat(),
                error_message=None,  # Bug #437: clear stale error from orphan recovery
            )

            # Get paths
            # WRITE path: live golden-repos/cidx-meta/ so RefreshScheduler detects changes
            golden_repos_root = Path(self._golden_repos_manager.golden_repos_dir)
            cidx_meta_path = golden_repos_root / "cidx-meta"
            dependency_map_dir = cidx_meta_path / "dependency-map"
            # READ path: versioned cidx-meta (Story #224 made cidx-meta a versioned repo)
            dependency_map_read_dir = self._get_cidx_meta_read_path() / "dependency-map"

            # Identify affected domains (AC3/4)
            affected_domains = self.identify_affected_domains(
                changed_repos, new_repos, removed_repos
            )

            try:
                self._activity_journal.log(
                    f"Identified {len(affected_domains)} affected domains"
                )
            except Exception as e:
                logger.debug(f"Non-fatal journal log error: {e}")

            if not affected_domains:
                logger.info("No affected domains identified")
                # Bug #396: Still clean stale repos from _domains.json even when
                # no affected domains were identified (e.g. stale _index.md
                # prevents domain mapping for removed repos)
                if removed_repos:
                    self._remove_stale_repos_from_domains_json(
                        removed_repos=removed_repos,
                        dependency_map_dir=dependency_map_dir,
                    )
                all_repos = self._get_activated_repos()
                # Bug #874 Story A: pass detect_s (merge never ran on this branch).
                self._finalize_delta_tracking(
                    config, all_repos, detect_s=detect_s, merge_s=0.0
                )
                _delta_succeeded = True
                return {
                    "status": "completed",
                    "affected_domains": 0,
                }

            # Generate CLAUDE.md
            all_repos = self._get_activated_repos()
            self._analyzer.generate_claude_md(all_repos)

            # Handle new repo domain discovery (AC6, Story #216)
            discovery_write_success = True
            if "__NEW_REPO_DISCOVERY__" in affected_domains:
                affected_domains.remove("__NEW_REPO_DISCOVERY__")
                existing_domains = (
                    [
                        f.stem
                        for f in dependency_map_read_dir.glob("*.md")
                        if not f.name.startswith("_")
                    ]
                    if dependency_map_read_dir.exists()
                    else []
                )
                discovered, discovery_write_success = (
                    self._discover_and_assign_new_repos(
                        new_repos=new_repos,
                        existing_domains=existing_domains,
                        dependency_map_dir=dependency_map_dir,
                        config=config,
                    )
                )
                affected_domains.update(discovered)

            # Story #716: Discover and assign uncovered repos
            if uncovered_repo_aliases:
                try:
                    uncovered_repo_dicts = [
                        r
                        for r in all_activated_repos
                        if r.get("alias", r.get("name", "")) in uncovered_repo_aliases
                    ]
                    if uncovered_repo_dicts:
                        existing_domains_list = (
                            [
                                f.stem
                                for f in dependency_map_read_dir.glob("*.md")
                                if not f.name.startswith("_")
                            ]
                            if dependency_map_read_dir.exists()
                            else []
                        )
                        uncov_discovered, _ = self._discover_and_assign_new_repos(
                            new_repos=uncovered_repo_dicts,
                            existing_domains=existing_domains_list,
                            dependency_map_dir=dependency_map_dir,
                            config=config,
                        )
                        affected_domains.update(uncov_discovered)
                        logger.info(
                            f"Story #716: Uncovered repo discovery added "
                            f"{len(uncov_discovered)} domains to affected set"
                        )
                except Exception as e:
                    logger.warning(f"Uncovered repo discovery failed (non-fatal): {e}")

            # Ensure live dependency-map directory exists before writing domain files
            # (versioned cidx-meta repos have empty live path; content is in .versioned/)
            dependency_map_dir.mkdir(parents=True, exist_ok=True)

            # Story #312 AC5: Progress update before domain updates
            if _tracked_job_id is not None and self._job_tracker is not None:
                try:
                    self._job_tracker.update_status(
                        _tracked_job_id,
                        progress=40,
                        progress_info="Updating affected domains",
                    )
                except Exception as e:
                    logger.debug(
                        f"Non-fatal: Failed to update progress (updating domains): {e}"
                    )
            try:
                self._activity_journal.log(
                    f"Updating {len(affected_domains)} affected domains"
                )
            except Exception as e:
                logger.debug(f"Non-fatal journal log error: {e}")

            # Update affected domains (AC5: In-Place Updates)
            # Bug #874 Story A: time the merge phase so the dashboard shows real numbers.
            # merge_s maps to pass2_duration_s column (Story B will add phase_timings_json).
            t_merge_start = time.time()
            errors = self._update_affected_domains(
                affected_domains,
                dependency_map_dir,
                changed_repos,
                new_repos,
                removed_repos,
                config,
            )
            merge_s = time.time() - t_merge_start

            # Bug #396: Remove stale repo aliases from _domains.json
            if removed_repos:
                self._remove_stale_repos_from_domains_json(
                    removed_repos=removed_repos,
                    dependency_map_dir=dependency_map_dir,
                )

            try:
                self._activity_journal.log("Delta analysis complete")
            except Exception as e:
                logger.debug(f"Non-fatal journal log error: {e}")

            # Story #312 AC5: Progress update before finalization
            if _tracked_job_id is not None and self._job_tracker is not None:
                try:
                    self._job_tracker.update_status(
                        _tracked_job_id,
                        progress=80,
                        progress_info="Finalizing delta analysis",
                    )
                except Exception as e:
                    logger.debug(
                        f"Non-fatal: Failed to update progress (finalizing delta): {e}"
                    )

            # Finalize tracking (AC8)
            # When discovery write failed, exclude new repos from finalization so they
            # are re-detected as new on the next delta run (Bug 2 fix).
            if discovery_write_success:
                repos_to_finalize = all_repos
            else:
                new_aliases = {r.get("alias") for r in new_repos}
                repos_to_finalize = [
                    r for r in all_repos if r.get("alias") not in new_aliases
                ]
                logger.warning(
                    f"Discovery write failed: excluding {len(new_repos)} new repo(s) "
                    "from tracking so they are re-detected on next delta run"
                )
            # Bug #874 Story A: pass real detect_s/merge_s timings.
            self._finalize_delta_tracking(
                config,
                repos_to_finalize,
                output_dir=dependency_map_dir,
                affected_domains=affected_domains,
                detect_s=detect_s,
                merge_s=merge_s,
            )

            logger.info(
                f"Delta analysis completed: {len(affected_domains)} domains updated"
            )

            # Story #329: Copy journal to final output directory after successful delta run
            try:
                self._activity_journal.copy_to_final(dependency_map_dir)
            except Exception as e:
                logger.debug(f"Non-fatal journal copy error: {e}")

            _delta_succeeded = True
            return {
                "status": "completed",
                "affected_domains": len(affected_domains),
                "errors": errors,
            }

        except Exception as e:
            logger.error(f"Delta analysis failed: {e}", exc_info=True)
            self._tracking_backend.update_tracking(
                status="failed", error_message=str(e)
            )
            # Story #312: Report failure to JobTracker (AC8). Defensive - never re-raises.
            if _tracked_job_id is not None and self._job_tracker is not None:
                try:
                    self._job_tracker.fail_job(_tracked_job_id, error=str(e))
                    _tracked_job_id = None  # Prevent double-call in finally
                except Exception as tracker_err:
                    logger.warning(
                        f"JobTracker fail_job failed (non-fatal): {tracker_err}"
                    )
            raise

        finally:
            # Cleanup CLAUDE.md
            try:
                claude_md = (
                    Path(self._golden_repos_manager.golden_repos_dir) / "CLAUDE.md"
                )
                if claude_md.exists():
                    claude_md.unlink()
            except Exception as cleanup_error:
                logger.debug(f"CLAUDE.md cleanup failed (non-fatal): {cleanup_error}")

            self._lock.release()

            # Story #312: Complete job in tracker on success (AC7). Defensive - never re-raises.
            if (
                _delta_succeeded
                and _tracked_job_id is not None
                and self._job_tracker is not None
            ):
                try:
                    self._job_tracker.complete_job(_tracked_job_id)
                except Exception as tracker_err:
                    logger.warning(
                        f"JobTracker complete_job failed (non-fatal): {tracker_err}"
                    )

            # Story #227: Release write lock so RefreshScheduler can proceed.
            if _write_lock_acquired and self._refresh_scheduler is not None:
                self._refresh_scheduler.release_write_lock(
                    "cidx-meta", owner_name="dependency_map_service"
                )

            # Story #227: Trigger explicit refresh after lock released (only on success).
            # AC2: Writer triggers refresh so RefreshScheduler captures complete data.
            # Must be inside finally so it runs after lock is released, but gated on success
            # to satisfy AC5 (no trigger on exception).
            if _delta_succeeded and self._refresh_scheduler is not None:
                self._refresh_scheduler.trigger_refresh_for_repo("cidx-meta-global")

    # ---------------------------------------------------------------------------
    # Story #359: Domain Document Refinement
    # ---------------------------------------------------------------------------

    def _select_domain_batch(
        self,
        domain_list_sorted: List[Dict[str, Any]],
        cursor: int,
        domains_per_run: int,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Select a batch of N domains starting from cursor (with wrap-around).

        Args:
            domain_list_sorted: Sorted list of domain dicts
            cursor: Current cursor position (may be past end of list)
            domains_per_run: Number of domains to process per cycle

        Returns:
            Tuple of (batch list, effective_cursor after wrap-around)
        """
        total = len(domain_list_sorted)
        effective_cursor = cursor % total
        batch = []
        for i in range(domains_per_run):
            idx = (effective_cursor + i) % total
            batch.append(domain_list_sorted[idx])
        return batch, effective_cursor

    def run_tracked_refinement(self, job_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Run refinement cycle with job tracking (Bug #371).

        Wraps run_refinement_cycle() with JobTracker registration, conflict
        detection, progress updates, and completion/failure reporting.

        Args:
            job_id: Optional caller-provided job ID. When None, a new
                    dep-map-refinement-XXXX ID is generated.

        Returns:
            Dict with status='completed' on success.

        Raises:
            DuplicateJobError: If job_tracker detects a concurrent refinement.
            Exception: Any exception from run_refinement_cycle() propagates.
        """
        # Conflict detection via JobTracker.
        if self._job_tracker is not None:
            try:
                from .job_tracker import DuplicateJobError

                self._job_tracker.check_operation_conflict("dependency_map_refinement")
            except DuplicateJobError:
                raise
            except Exception as tracker_err:
                logger.warning(
                    f"JobTracker conflict check failed (non-fatal): {tracker_err}"
                )

        # Register job with JobTracker.
        _tracked_job_id: Optional[str] = None
        if self._job_tracker is not None:
            try:
                _tracked_job_id = job_id or f"dep-map-refinement-{uuid.uuid4().hex[:8]}"
                self._job_tracker.register_job(
                    _tracked_job_id,
                    "dependency_map_refinement",
                    username="system",
                    repo_alias="server",
                )
                self._job_tracker.update_status(_tracked_job_id, status="running")
            except Exception as tracker_err:
                logger.warning(
                    f"JobTracker registration failed (non-fatal): {tracker_err}"
                )
                _tracked_job_id = None

        _refinement_succeeded = False
        try:
            if _tracked_job_id is not None and self._job_tracker is not None:
                try:
                    self._job_tracker.update_status(
                        _tracked_job_id,
                        progress=10,
                        progress_info="Starting refinement cycle",
                    )
                except Exception as e:
                    logger.debug(f"Non-fatal: Failed to update progress (start): {e}")

            self.run_refinement_cycle()

            if _tracked_job_id is not None and self._job_tracker is not None:
                try:
                    self._job_tracker.update_status(
                        _tracked_job_id,
                        progress=90,
                        progress_info="Finalizing refinement",
                    )
                except Exception as e:
                    logger.debug(
                        f"Non-fatal: Failed to update progress (finalizing): {e}"
                    )

            _refinement_succeeded = True
            return {"status": "completed"}

        except Exception as e:
            if _tracked_job_id is not None and self._job_tracker is not None:
                try:
                    self._job_tracker.fail_job(_tracked_job_id, error=str(e))
                    _tracked_job_id = None  # Prevent double-call in finally
                except Exception as tracker_err:
                    logger.warning(
                        f"JobTracker fail_job failed (non-fatal): {tracker_err}"
                    )
            raise

        finally:
            if (
                _refinement_succeeded
                and _tracked_job_id is not None
                and self._job_tracker is not None
            ):
                try:
                    self._job_tracker.complete_job(_tracked_job_id)
                except Exception as tracker_err:
                    logger.warning(
                        f"JobTracker complete_job failed (non-fatal): {tracker_err}"
                    )

    def run_refinement_cycle(self) -> None:
        """
        Run one refinement cycle: process N domains from the persistent cursor position.

        Reads domain list from versioned cidx-meta path, selects a batch using the
        cursor, calls refine_or_create_domain() for each, regenerates _index.md if
        any domain changed, and advances the cursor in tracking.

        AC7: Non-blocking lock prevents concurrent writes with delta analysis.
        Acquires RefreshScheduler write lock so CoW clone is skipped during writes.

        Returns None in all cases (early exit when disabled or no domains).
        """
        config = self._config_manager.get_claude_integration_config()
        if not config or not config.refinement_enabled:
            logger.debug("Refinement disabled, skipping refinement cycle")
            try:
                journal_dir = Path(
                    os.path.expanduser("~/.tmp/depmap-refinement-journal/")
                )
                self._activity_journal.init(journal_dir)
                self._activity_journal.log(
                    "Refinement: disabled in config, skipping cycle"
                )
            except Exception as e:
                logger.debug(f"Non-fatal journal log error (disabled path): {e}")
            return None

        # AC7: Non-blocking lock to prevent concurrent writes with delta analysis
        if not self._lock.acquire(blocking=False):
            logger.info("Refinement cycle skipped - analysis already in progress")
            try:
                journal_dir = Path(
                    os.path.expanduser("~/.tmp/depmap-refinement-journal/")
                )
                self._activity_journal.init(journal_dir)
                self._activity_journal.log(
                    "Refinement: skipped - analysis already in progress"
                )
            except Exception as e:
                logger.debug(f"Non-fatal journal log error (lock-skip path): {e}")
            return None

        # Acquire write lock so RefreshScheduler skips CoW clone during writes
        _write_lock_acquired = False
        if self._refresh_scheduler is not None:
            _write_lock_acquired = self._refresh_scheduler.acquire_write_lock(
                "cidx-meta", owner_name="dependency_map_service"
            )

        any_changed = False
        try:
            # Initialize activity journal for this refinement cycle run
            try:
                journal_dir = Path(
                    os.path.expanduser("~/.tmp/depmap-refinement-journal/")
                )
                self._activity_journal.init(journal_dir)
                self._activity_journal.log("Starting refinement cycle")
            except Exception as e:
                logger.debug(f"Non-fatal journal init error: {e}")
            golden_repos_dir = Path(self._golden_repos_manager.golden_repos_dir)
            cidx_meta_read_path = self._get_cidx_meta_read_path()
            dependency_map_read_dir = cidx_meta_read_path / "dependency-map"
            dependency_map_dir = golden_repos_dir / "cidx-meta" / "dependency-map"

            domains_json_path = dependency_map_read_dir / "_domains.json"
            if not domains_json_path.exists():
                logger.info("Refinement: _domains.json not found, skipping cycle")
                try:
                    self._activity_journal.log("Refinement: no domains found, skipping")
                except Exception as e:
                    logger.debug(f"Non-fatal journal log error: {e}")
                return None

            try:
                raw_list = json.loads(domains_json_path.read_text())
            except Exception as e:
                logger.warning("Refinement: Failed to read _domains.json: %s", e)
                try:
                    self._activity_journal.log(
                        f"Refinement: failed to read _domains.json: {e}"
                    )
                except Exception as journal_err:
                    logger.debug(f"Non-fatal journal log error: {journal_err}")
                return None

            if not raw_list:
                logger.info("Refinement: domain list is empty, skipping cycle")
                try:
                    self._activity_journal.log(
                        "Refinement: domain list empty, skipping"
                    )
                except Exception as e:
                    logger.debug(f"Non-fatal journal log error: {e}")
                return None

            # Preserve original JSON order (cursor positions defined by list order in _domains.json)
            domain_list_ordered = raw_list
            tracking = self._tracking_backend.get_tracking()
            cursor = tracking.get("refinement_cursor", 0) or 0

            batch, effective_cursor = self._select_domain_batch(
                domain_list_ordered, cursor, config.refinement_domains_per_run
            )

            try:
                self._activity_journal.log(
                    f"Refinement: processing batch of {len(batch)} domains (cursor at {effective_cursor})"
                )
            except Exception as e:
                logger.debug(f"Non-fatal journal log error: {e}")

            domains_processed = 0
            domains_changed = 0
            domains_failed = 0
            # Bug #874 Story C: time the refinement work so phase_timings_json has honest refine_s.
            t_refine_start = time.time()
            for domain_idx, domain_info in enumerate(batch):
                domain_name = domain_info.get("name", "")
                if not domain_name:
                    continue
                try:
                    self._activity_journal.log(
                        f"Refining domain '{domain_name}' ({domain_idx + 1}/{len(batch)})"
                    )
                except Exception as e:
                    logger.debug(f"Non-fatal journal log error: {e}")
                try:
                    changed = self.refine_or_create_domain(
                        domain_name=domain_name,
                        domain_info=domain_info,
                        dependency_map_dir=dependency_map_dir,
                        dependency_map_read_dir=dependency_map_read_dir,
                        config=config,
                    )
                    domains_processed += 1
                    if changed:
                        any_changed = True
                        domains_changed += 1
                        try:
                            self._activity_journal.log(
                                f"Domain '{domain_name}' refined successfully (changed)"
                            )
                        except Exception as e:
                            logger.debug(f"Non-fatal journal log error: {e}")
                    else:
                        try:
                            self._activity_journal.log(
                                f"Domain '{domain_name}' refined (no changes)"
                            )
                        except Exception as e:
                            logger.debug(f"Non-fatal journal log error: {e}")
                except Exception as e:
                    domains_processed += 1
                    domains_failed += 1
                    try:
                        self._activity_journal.log(
                            f"Domain '{domain_name}' refinement failed: {str(e)[:80]}"
                        )
                    except Exception as journal_err:
                        logger.debug(f"Non-fatal journal log error: {journal_err}")
                    logger.warning(
                        "Refinement: Failed to refine domain '%s': %s", domain_name, e
                    )
            refine_s = time.time() - t_refine_start

            if any_changed:
                try:
                    self._activity_journal.log("Regenerating _index.md")
                except Exception as e:
                    logger.debug(f"Non-fatal journal log error: {e}")
                try:
                    self._analyzer._generate_index_md(
                        dependency_map_dir, domain_list_ordered, []
                    )
                except Exception as e:
                    logger.warning("Refinement: Failed to regenerate _index.md: %s", e)

            new_cursor = effective_cursor + config.refinement_domains_per_run
            self._tracking_backend.update_tracking(refinement_cursor=new_cursor)

            # Bug #874 Story C FR5: first-ever _record_run_metrics call from refinement path.
            # domain_list uses batch (what this cycle touched), not domain_list_ordered (total).
            # repos_skipped = 0 (refinement has no per-repo skipping concept at this scope).
            self._record_run_metrics(
                dependency_map_dir,
                [{"name": d["name"]} for d in batch],
                [],
                run_type="refinement",
                phase_timings_json=json.dumps({"refine_s": refine_s}),
                repos_skipped=0,
            )

            # Bug #931 Defect 1: stamp refinement_next_run unconditionally on success.
            # Previously written only in the scheduler's own success branch — creating a
            # chicken-and-egg bootstrap gap where the manual trigger (run_tracked_refinement)
            # never seeded the schedule, leaving refinement_next_run=NULL indefinitely.
            # Mirrors run_delta_analysis (lines ~877, ~2547): the cycle method owns the
            # next-run timestamp so every caller (manual OR scheduled) seeds the schedule.
            # `config` is the same object retrieved at method entry (line ~3199); no second
            # config lookup needed and no fallback introduced.
            self._tracking_backend.update_tracking(
                refinement_next_run=(
                    datetime.now(timezone.utc)
                    + timedelta(hours=config.refinement_interval_hours)
                ).isoformat()
            )

            try:
                self._activity_journal.log(
                    f"Refinement cycle complete: {domains_processed} domains processed, "
                    f"{domains_changed} changed, {domains_failed} failed"
                )
            except Exception as e:
                logger.debug(f"Non-fatal journal log error: {e}")

        finally:
            self._lock.release()
            if _write_lock_acquired and self._refresh_scheduler is not None:
                self._refresh_scheduler.release_write_lock(
                    "cidx-meta", owner_name="dependency_map_service"
                )
            if any_changed and self._refresh_scheduler is not None:
                self._refresh_scheduler.trigger_refresh_for_repo("cidx-meta-global")

        return None

    def _build_refinement_frontmatter(
        self, existing_content: str, new_body: str, domain_name: str
    ) -> str:
        """
        Build updated file content with frontmatter for a refined domain (Story #359).

        Preserves all existing frontmatter fields (including last_analyzed).
        Adds or updates last_refined to the current UTC timestamp.

        Args:
            existing_content: Original domain file content (may include frontmatter)
            new_body: Refined document body from Claude CLI
            domain_name: Domain name (used when no frontmatter exists)

        Returns:
            Complete content string: YAML frontmatter block + body
        """
        now = datetime.now(timezone.utc).isoformat()
        frontmatter_match = re.match(
            r"^---\n(.*?)\n---\n(.*)$", existing_content, re.DOTALL
        )
        if not frontmatter_match:
            return f"---\ndomain: {domain_name}\nlast_refined: {now}\n---\n\n{new_body}"

        lines = frontmatter_match.group(1).split("\n")
        updated, found_refined = [], False
        for line in lines:
            if line.startswith("last_refined:"):
                updated.append(f"last_refined: {now}")
                found_refined = True
            else:
                updated.append(line)  # Preserve last_analyzed and all other fields
        if not found_refined:
            updated.append(f"last_refined: {now}")
        frontmatter_body = "\n".join(updated)
        return f"---\n{frontmatter_body}\n---\n\n{new_body}"

    def _refine_existing_domain(
        self,
        domain_name: str,
        existing_content: str,
        participating_repos: List[str],
        write_file: Path,
        dependency_map_dir: Path,
        config,
    ) -> bool:
        """
        Refine an existing domain file via Claude CLI (Story #359).

        Applies truncation guard and no-op check before writing.

        Returns:
            True if file was updated, False if guard fired or content identical.
        """
        frontmatter_match = re.match(
            r"^---\n(.*?)\n---\n(.*)$", existing_content, re.DOTALL
        )
        existing_body = (
            frontmatter_match.group(2).lstrip("\n")
            if frontmatter_match
            else existing_content
        )

        prompt = self._analyzer.build_refinement_prompt(
            domain_name=domain_name,
            existing_body=existing_body,
            participating_repos=participating_repos,
        )

        # Story #715: File-based refinement — Claude edits temp file in-place
        result_body = self._analyzer.invoke_refinement_file(
            domain_name=domain_name,
            existing_content=existing_body,
            refinement_prompt=prompt,
            timeout=config.dependency_map_pass_timeout_seconds,
            max_turns=config.dependency_map_delta_max_turns,
            temp_dir=dependency_map_dir,
        )

        if result_body is None:
            logger.debug("Refinement produced no changes for domain '%s'", domain_name)
            return False

        updated_content = self._build_refinement_frontmatter(
            existing_content=existing_content,
            new_body=result_body,
            domain_name=domain_name,
        )
        dependency_map_dir.mkdir(parents=True, exist_ok=True)
        write_file.write_text(updated_content)
        return True

    def refine_or_create_domain(
        self,
        domain_name: str,
        domain_info: Dict[str, Any],
        dependency_map_dir: Path,
        dependency_map_read_dir: Path,
        config,
    ) -> bool:
        """
        Refine an existing domain file or create it if missing (Story #359).

        Reads from dependency_map_read_dir (versioned/read path).
        Writes to dependency_map_dir (live/write path).

        For missing files: uses build_new_domain_prompt + invoke_new_domain_generation.
        For existing files: delegates to _refine_existing_domain() for file-based
            refinement via invoke_refinement_file().

        Returns:
            True if the domain file was created or updated, False otherwise.
        """
        participating_repos = domain_info.get("participating_repos", [])
        timeout = config.dependency_map_pass_timeout_seconds
        max_turns = config.dependency_map_delta_max_turns

        read_file = dependency_map_read_dir / f"{domain_name}.md"
        write_file = dependency_map_dir / f"{domain_name}.md"

        if not read_file.exists() and not write_file.exists():
            # Truly orphaned - create new from scratch
            prompt = self._analyzer.build_new_domain_prompt(
                domain_name=domain_name,
                participating_repos=participating_repos,
            )
            result_body = self._analyzer.invoke_new_domain_generation(
                prompt, timeout, max_turns
            )
            now = datetime.now(timezone.utc).isoformat()
            new_content = (
                f"---\ndomain: {domain_name}\nlast_refined: {now}\n---\n\n{result_body}"
            )
            dependency_map_dir.mkdir(parents=True, exist_ok=True)
            write_file.write_text(new_content)
            return True
        elif not read_file.exists() and write_file.exists():
            # Write path has content from a prior cycle not yet snapshotted - refine from that
            existing_content = write_file.read_text()
            return self._refine_existing_domain(
                domain_name=domain_name,
                existing_content=existing_content,
                participating_repos=participating_repos,
                write_file=write_file,
                dependency_map_dir=dependency_map_dir,
                config=config,
            )

        existing_content = read_file.read_text()
        return self._refine_existing_domain(
            domain_name=domain_name,
            existing_content=existing_content,
            participating_repos=participating_repos,
            write_file=write_file,
            dependency_map_dir=dependency_map_dir,
            config=config,
        )
