"""
Description Refresh Scheduler (Story #190).

Manages periodic description regeneration for golden repositories using
hash-based bucket scheduling with jitter to distribute load evenly.
"""

import concurrent.futures
import hashlib
import json
import logging
import random
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING, cast

from code_indexer.global_repos.lifecycle_batch_runner import LifecycleBatchRunner
from code_indexer.server.services.dep_map_dispatcher_factory import (
    build_dep_map_dispatcher,
)
from code_indexer.server.storage.sqlite_backends import (
    DescriptionRefreshTrackingBackend,
    GoldenRepoMetadataSqliteBackend,
)

if TYPE_CHECKING:
    from code_indexer.server.services.cli_dispatcher import CliDispatcher

logger = logging.getLogger(__name__)

# Claude CLI timeout constants (Story #727).
# NOTE: This module assumes a Unix-like host with ``script``, ``timeout``, and
# ``claude`` available in PATH.  The ``script`` utility provides a pseudo-TTY
# required for Claude CLI in non-interactive environments.
_CLAUDE_CLI_SOFT_TIMEOUT_SECONDS = 90  # inner shell ``timeout`` budget
_CLAUDE_CLI_HARD_TIMEOUT_SECONDS = 120  # Python subprocess.run cap

# Bug #953: circuit-breaker threshold for consecutive prompt-generation failures.
# After this many consecutive None-prompt results for the same repo, the scheduler
# stops rescheduling (quarantines) and logs one ERROR.  Reset to 0 on success.
PROMPT_FAILURE_QUARANTINE_THRESHOLD = 3

# v10.4.14 stub-healing: descriptions with body shorter than this threshold or
# missing `last_analyzed` are treated as stubs (artifact of v10.4.9 wipe bug or
# pre-v10.4.13 README/static-regex fallback) and trigger full re-analysis via
# the same path on_repo_added uses, instead of incremental refresh.
_STUB_BODY_CHAR_THRESHOLD: int = 800


def _build_claude_env() -> dict:
    """
    Build a sanitised copy of ``os.environ`` for Claude CLI subprocess.

    Strips ``CLAUDECODE`` (prevents nested-session errors) and optionally
    ``ANTHROPIC_API_KEY`` when ``CLAUDECODE`` is set.

    Returns:
        Dict of environment variables for the subprocess.
    """
    import os

    keys_to_strip = {"CLAUDECODE"}
    if "CLAUDECODE" in os.environ:
        keys_to_strip.add("ANTHROPIC_API_KEY")
    filtered = {k: v for k, v in os.environ.items() if k not in keys_to_strip}
    filtered["NO_COLOR"] = "1"
    return filtered


def _normalize_claude_output(raw: str) -> str:
    """
    Strip terminal control sequences and normalise line endings from Claude CLI stdout.

    Removes CSI, OSC and bare ESC sequences emitted by the ``script`` wrapper,
    normalises CR/LF line endings, and trims chain-of-thought text that may
    precede the opening ``---`` YAML frontmatter delimiter.

    Args:
        raw: Raw stdout string from the subprocess.

    Returns:
        Cleaned string ready for YAML frontmatter parsing.
    """
    import re

    output = raw
    # CSI sequences: full ECMA-48 grammar — parameter bytes [0-?], intermediate bytes [ -/],
    # final bytes [@-~] (covers colors, cursor, private modes, intermediate byte variants).
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    # OSC sequences: ESC ] ... BEL or ESC ] ... ST
    output = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?", "", output)
    # Other ESC sequences (ESC followed by single char)
    output = re.sub(r"\x1b[^[\]()]", "", output)
    # Stray control artifacts from script command
    output = re.sub(r"\[<u", "", output)
    # Strip any remaining bare ESC bytes
    output = output.replace("\x1b", "")
    # Normalize line endings
    output = output.replace("\r\n", "\n").replace("\r", "")
    output = output.strip()
    # Strip chain-of-thought text before YAML frontmatter
    frontmatter_match = re.search(r"^---\s*$", output, re.MULTILINE)
    if frontmatter_match and frontmatter_match.start() > 0:
        output = output[frontmatter_match.start() :]
    return output


class DescriptionRefreshScheduler:
    """
    Scheduler for periodic repository description refresh.

    Implements hash-based bucket scheduling with jitter to distribute
    refresh jobs evenly across time intervals, preventing thundering herd.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        config_manager=None,
        claude_cli_manager=None,
        meta_dir: Optional[Path] = None,
        analysis_model: str = "opus",
        job_tracker=None,
        mcp_registration_service=None,
        tracking_backend=None,
        golden_backend=None,
        lifecycle_invoker=None,
        golden_repos_dir: Optional[Path] = None,
        lifecycle_debouncer=None,
        refresh_scheduler=None,
        cli_dispatcher=None,
        background_job_manager=None,
    ) -> None:
        """
        Initialize the scheduler.

        Args:
            db_path: Path to SQLite database. Required unless both tracking_backend and
                golden_backend are provided directly (injectable backend mode for tests).
            config_manager: ServerConfigManager instance
            claude_cli_manager: Optional ClaudeCliManager instance (for submitting work)
            meta_dir: Path to cidx-meta directory (for reading existing .md files)
            analysis_model: Claude model to use ("opus" or "sonnet", default: "opus")
            job_tracker: Optional JobTracker instance for unified job tracking (Story #313)
            mcp_registration_service: Optional MCPSelfRegistrationService instance (Story #727)
            tracking_backend: Optional pre-constructed DescriptionRefreshTrackingBackend.
                When provided together with golden_backend, db_path is not required.
            golden_backend: Optional pre-constructed GoldenRepoMetadataSqliteBackend.
                When provided together with tracking_backend, db_path is not required.
            lifecycle_invoker: Optional LifecycleClaudeCliInvoker callable (Story #876 D3).
                When wired along with golden_repos_dir, lifecycle_debouncer and
                refresh_scheduler, the scheduler hands each refresh event to
                LifecycleBatchRunner.
                Messi Rule #2 anti-fallback: missing wiring emits a WARNING and skips
                the runner — no silent legacy-path execution.
            golden_repos_dir: Optional filesystem root of golden repos (Story #876 D3).
                Passed to LifecycleBatchRunner so it can resolve cidx-meta/<alias>.md
                paths under a single controlled root.
            lifecycle_debouncer: Optional MetaWriteDebouncer instance (Story #876 D3).
                Forwarded to LifecycleBatchRunner for cidx-meta write coalescing.
            refresh_scheduler: Optional global RefreshScheduler instance (Story #876 D3).
                Provides the write-lock acquire/release interface the batch runner
                uses to serialise cidx-meta updates across the fleet.
            cli_dispatcher: Optional CliDispatcher instance (Story #847).
                When provided, _invoke_claude_cli routes through this dispatcher
                instead of building one from config on each call.  Used by tests
                to inject a mock dispatcher for deterministic behaviour.
        """
        if db_path is None and (tracking_backend is None or golden_backend is None):
            raise ValueError(
                "Either db_path or both tracking_backend and golden_backend must be provided"
            )
        self._db_path = db_path
        self._config_manager = config_manager
        self._claude_cli_manager = claude_cli_manager
        self._meta_dir = meta_dir
        self._analysis_model = analysis_model
        self._job_tracker = job_tracker
        self._mcp_registration_service = mcp_registration_service
        # Story #876 D3 wiring — four optional collaborators that together
        # activate the LifecycleBatchRunner refresh path.  Any None sinks the
        # refresh back to a WARNING-guarded skip (Messi Rule #2 anti-fallback).
        self._lifecycle_invoker = lifecycle_invoker
        self._golden_repos_dir = golden_repos_dir
        self._lifecycle_debouncer = lifecycle_debouncer
        self._refresh_scheduler = refresh_scheduler
        # Story #847: injectable CliDispatcher for testing; None means build from config.
        self._cli_dispatcher = cli_dispatcher
        # v10.4.14 stub-heal: BackgroundJobManager wired by lifespan startup so the
        # stub-heal dispatch goes through the standard background-job pipeline
        # (dashboard visibility, JobTracker registration, DuplicateJobError dedup,
        # operation_type="description_stub_heal").  Optional for unit tests.
        self._background_job_manager = background_job_manager
        if tracking_backend is not None:
            self._tracking_backend = tracking_backend
        else:
            assert db_path is not None  # guarded above
            self._tracking_backend = DescriptionRefreshTrackingBackend(db_path)
        if golden_backend is not None:
            self._golden_backend = golden_backend
        else:
            assert db_path is not None  # guarded above
            self._golden_backend = GoldenRepoMetadataSqliteBackend(db_path)
        self._shutdown_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Bug #953: per-repo consecutive prompt-failure counter.
        # Incremented each time _get_refresh_prompt() returns None.
        # Reset to 0 by on_refresh_complete(success=True).
        # When the count reaches PROMPT_FAILURE_QUARANTINE_THRESHOLD the repo is
        # quarantined (not rescheduled) and one ERROR log is emitted.
        self._prompt_failure_counts: Dict[str, int] = defaultdict(int)
        # Bug #984: track which repos have already had "missing description or
        # last_analyzed" WARNING emitted in the current scheduler instance lifetime.
        # Subsequent passes for the same repo downgrade the log to DEBUG so the
        # warning fires at most once per repo (re-armed after a successful refresh).
        self._warned_missing_desc: set = set()
        # v10.4.14 stub-heal regression guard: aliases placed here by
        # _get_refresh_prompt when it returns None from the stub-heal branch
        # (either heal succeeded or preconditions failed).  _run_loop_single_pass
        # checks this set before incrementing the prompt-failure quarantine counter
        # so stub-heal None returns never count toward the quarantine threshold and
        # never block lifecycle backfill.  Entries are consumed (discarded) on read.
        self._stub_heal_no_quarantine_aliases: set = set()

        # Story #728 AC5: Bounded thread pool sized by max_concurrent_claude_cli config.
        # Prevents mass backfill from spawning N unbounded concurrent Claude CLI processes.
        # Canonical default comes from ClaudeIntegrationConfig to avoid duplication.
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        _default_max_workers = ClaudeIntegrationConfig().max_concurrent_claude_cli
        config = config_manager.load_config() if config_manager is not None else None
        _configured = (
            config.claude_integration_config.max_concurrent_claude_cli
            if config and config.claude_integration_config
            else _default_max_workers
        )
        # Clamp to >= 1: ThreadPoolExecutor raises ValueError for 0 or negative values.
        max_workers = max(1, _configured)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

    def start(self) -> None:
        """Start daemon thread if enabled in config."""
        config = self._config_manager.load_config()
        if not config or not config.claude_integration_config:
            logger.debug("Skipping description refresh: config not initialized")
            return

        if not config.claude_integration_config.description_refresh_enabled:
            logger.debug(
                "Skipping description refresh: description_refresh_enabled is false"
            )
            return

        interval_hours = (
            config.claude_integration_config.description_refresh_interval_hours
        )

        # Calculate number of buckets (one per hour in interval)
        buckets = interval_hours

        logger.info(
            f"Description refresh scheduler started (interval: {interval_hours}h, {buckets} buckets)"
        )

        self.reconcile_orphan_tracking()
        self.reconcile_broken_lifecycle_metadata()

        # Start daemon thread
        self._shutdown_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop daemon thread."""
        logger.info("Stopping description refresh scheduler")
        self._shutdown_event.set()
        # Story #728 AC5b: Drain queued tasks before shutdown so no refresh leaks
        # into the next start cycle.  Must be called AFTER _shutdown_event.set()
        # so any new submission guards in _run_loop_single_pass see the event first.
        self._executor.shutdown(wait=True)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def calculate_next_run(
        self, alias: str, interval_hours: Optional[int] = None
    ) -> str:
        """
        Calculate next run time using hash-based bucket scheduling with jitter.

        Args:
            alias: Repository alias (used for consistent bucketing)
            interval_hours: Interval in hours (defaults to config value)

        Returns:
            ISO 8601 timestamp for next run
        """
        if interval_hours is None:
            interval_hours = self._get_interval_hours()

        # Hash-based bucket assignment (deterministic for same alias)
        bucket = int(hashlib.md5(alias.encode()).hexdigest(), 16) % interval_hours

        # Calculate next hour boundary
        now = datetime.now(timezone.utc)
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

        # Base time = next_hour + bucket offset
        base_time = next_hour + timedelta(hours=bucket)

        # Add jitter (0-30% of bucket size = 0-18 minutes for 1-hour buckets)
        jitter_seconds = random.uniform(0, 3600 * 0.3)

        final_time = base_time + timedelta(seconds=jitter_seconds)

        logger.debug(f"Repo {alias} assigned to bucket {bucket}/{interval_hours}")

        return final_time.isoformat()

    def has_changes_since_last_run(
        self, repo_path: str, tracking_record: Dict[str, Any]
    ) -> bool:
        """
        Check if repository has changes since last refresh.

        Args:
            repo_path: Path to repository
            tracking_record: Tracking record from database

        Returns:
            True if repository has changes (or no metadata), False if unchanged
        """
        metadata_path = Path(repo_path) / ".code-indexer" / "metadata.json"

        if not metadata_path.exists():
            logger.debug(f"No metadata.json in {repo_path}, assuming changes")
            return True

        try:
            with open(metadata_path) as f:
                metadata = json.load(f)

            # Git repository: compare current_commit
            if "current_commit" in metadata:
                last_known_commit = tracking_record.get("last_known_commit")
                current_commit = metadata["current_commit"]

                if last_known_commit == current_commit:
                    logger.debug(
                        f"Skipping {repo_path}: no changes since last run (commit: {current_commit})"
                    )
                    return False
                else:
                    logger.debug(
                        f"Changes detected in {repo_path}: {last_known_commit} -> {current_commit}"
                    )
                    return True

            # Langfuse repository: compare files_processed
            if "files_processed" in metadata:
                last_known_files = tracking_record.get("last_known_files_processed")
                current_files = metadata["files_processed"]

                if last_known_files == current_files:
                    logger.debug(
                        f"Skipping {repo_path}: no changes since last run (files: {current_files})"
                    )
                    return False
                else:
                    logger.debug(
                        f"Changes detected in {repo_path}: {last_known_files} -> {current_files} files"
                    )
                    return True

            # Unknown metadata format - assume changes
            logger.debug(f"Unknown metadata format in {repo_path}, assuming changes")
            return True

        except Exception as e:
            logger.warning(
                f"Failed to read metadata from {repo_path}: {e}", exc_info=True
            )
            # Safe default: assume changes
            return True

    def get_stale_repos(self) -> List[Dict[str, Any]]:
        """
        Query repos where next_run <= now AND status != 'queued'.

        Returns:
            List of stale repo records with path info from golden_repos_metadata
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # Get stale tracking records
        stale_tracking = self._tracking_backend.get_stale_repos(now_iso)

        # Join with golden repos to get clone_path
        result = []
        for tracking in stale_tracking:
            alias = tracking["repo_alias"]
            golden_repo = self._golden_backend.get_repo(alias)

            if golden_repo:
                # Merge tracking and golden repo data
                merged = {**tracking, "clone_path": golden_repo["clone_path"]}
                result.append(merged)
            else:
                try:
                    self._tracking_backend.delete_tracking(alias)
                    logger.info(
                        "Pruned orphan tracking row for %s (golden repo not found)",
                        alias,
                    )
                except Exception:
                    logger.error(
                        "Failed to prune orphan tracking row for %s",
                        alias,
                        exc_info=True,
                    )

        return result

    def reconcile_orphan_tracking(self) -> int:
        """One-shot sweep: delete tracking rows whose golden_repo is missing.

        Returns the number of rows pruned. Self-defensive: any exception in the
        sweep is logged and swallowed so scheduler startup cannot be blocked.
        """
        deleted = 0
        try:
            rows = self._tracking_backend.get_all_tracking()
        except Exception:
            logger.error(
                "Orphan tracking reconciliation: get_all_tracking failed",
                exc_info=True,
            )
            return 0

        for row in rows:
            alias = row.get("repo_alias")
            if not alias:
                continue
            try:
                golden = self._golden_backend.get_repo(alias)
            except Exception:
                logger.error(
                    "Orphan tracking reconciliation: get_repo failed for %s",
                    alias,
                    exc_info=True,
                )
                continue
            if golden:
                continue
            try:
                self._tracking_backend.delete_tracking(alias)
                deleted += 1
            except Exception:
                logger.error(
                    "Orphan tracking reconciliation: delete failed for %s",
                    alias,
                    exc_info=True,
                )

        logger.info("Orphan tracking reconciliation: pruned %d rows", deleted)
        return deleted

    def reconcile_broken_lifecycle_metadata(self) -> int:
        """One-shot backfill sweep: scan all golden repos for broken cidx-meta
        lifecycle frontmatter and asynchronously route them through
        LifecycleBatchRunner for repair.

        Runs once at start(), after reconcile_orphan_tracking() and before the
        periodic daemon thread spawns.  Closes Story #876 gap: pre-existing
        aliases with stale v2 or 'confidence: unknown' metadata are never repaired
        by any event-driven code path.

        Returns the number of broken aliases queued for async repair, or 0 on
        any error or empty result.  Self-defensive: all failures are logged and
        swallowed so scheduler startup cannot be blocked.
        """
        if not self._check_lifecycle_backfill_wiring():
            return 0

        aliases = self._list_golden_aliases()
        if aliases is None:
            return 0
        if not aliases:
            logger.info("Lifecycle backfill: no golden repos to scan")
            return 0

        broken = self._find_broken_lifecycle_aliases(aliases)
        if broken is None:
            return 0
        if not broken:
            logger.info(
                "Lifecycle backfill: no broken lifecycle metadata found "
                "(%d aliases clean)",
                len(aliases),
            )
            return 0

        logger.info(
            "Lifecycle backfill: identified %d broken aliases — "
            "dispatching async repair thread",
            len(broken),
        )
        try:
            self._dispatch_lifecycle_backfill_thread(broken)
        except Exception:
            logger.error(
                "Lifecycle backfill: failed to dispatch async repair thread",
                exc_info=True,
            )
            return 0
        return len(broken)

    def _check_lifecycle_backfill_wiring(self) -> bool:
        """Return True if all five lifecycle collaborators are wired; log WARNING and
        return False for the first missing one (Messi Rule #2 — no silent fallback)."""
        for name, value in (
            ("lifecycle_invoker", self._lifecycle_invoker),
            ("golden_repos_dir", self._golden_repos_dir),
            ("lifecycle_debouncer", self._lifecycle_debouncer),
            ("refresh_scheduler", self._refresh_scheduler),
            ("job_tracker", self._job_tracker),
        ):
            if value is None:
                logger.warning(
                    "Lifecycle backfill skipped at startup: %s not wired",
                    name,
                )
                return False
        return True

    def _list_golden_aliases(self) -> Optional[List[str]]:
        """Return a list of valid alias strings from the golden backend.

        Returns None on error (caller should skip the sweep).
        Returns [] when the backend has no repos yet.
        Alias filter: must be a non-empty str (excludes None, int, empty string).
        """
        try:
            repos = self._golden_backend.list_repos() or []
        except Exception:
            logger.error(
                "Lifecycle backfill: list_repos failed — skipping startup sweep",
                exc_info=True,
            )
            return None

        aliases: List[str] = []
        for repo in repos:
            alias = repo.get("alias") if isinstance(repo, dict) else None
            if isinstance(alias, str) and alias:
                aliases.append(alias)
        return aliases

    def _find_broken_lifecycle_aliases(self, aliases: List[str]) -> Optional[List[str]]:
        """Run LifecycleFleetScanner over *aliases* and return the broken subset.

        Returns None on scanner error (caller should skip the sweep).
        Returns [] when all aliases have valid lifecycle metadata.
        """
        try:
            from code_indexer.global_repos.lifecycle_batch_runner import (
                LifecycleFleetScanner,
            )

            scanner: LifecycleFleetScanner = LifecycleFleetScanner(
                golden_repos_dir=self._golden_repos_dir,
                repo_aliases=aliases,
            )
            # cast needed: LifecycleFleetScanner is imported inside try block so mypy
            # infers scanner as Any; find_broken_or_missing() is declared -> List[str].
            return cast(List[str], scanner.find_broken_or_missing())
        except Exception:
            logger.error(
                "Lifecycle backfill: fleet scan failed — skipping startup sweep",
                exc_info=True,
            )
            return None

    def _dispatch_lifecycle_backfill_thread(self, broken: List[str]) -> None:
        """Spawn a daemon thread to run LifecycleBatchRunner on *broken* aliases."""
        thread = threading.Thread(
            target=self._run_lifecycle_backfill_async,
            args=(list(broken),),
            daemon=True,
            name="lifecycle-backfill",
        )
        thread.start()

    def _run_lifecycle_backfill_async(self, aliases: List[str]) -> None:
        """Background worker: run LifecycleBatchRunner on broken aliases.

        Generates a UUID job_id, registers it with JobTracker (operation_type
        'lifecycle_backfill', username 'system'), then invokes
        LifecycleBatchRunner.run(aliases, parent_job_id=job_id) — which itself
        handles sub-batching, concurrency, per-alias locking, debouncer signal,
        and job_tracker.complete_job.

        All exceptions are logged and swallowed — a sweep failure must not crash
        the daemon thread or leak back into scheduler startup.
        """
        try:
            if self._job_tracker is None:
                logger.error(
                    "Lifecycle backfill async: job_tracker missing — "
                    "cannot route repair through LifecycleBatchRunner"
                )
                return

            import uuid

            job_id = str(uuid.uuid4())
            self._job_tracker.register_job(
                job_id=job_id,
                operation_type="lifecycle_backfill",
                username="system",
                metadata={"total": len(aliases), "source": "startup_backfill"},
            )

            runner = LifecycleBatchRunner(
                golden_repos_dir=self._golden_repos_dir,
                job_tracker=self._job_tracker,
                refresh_scheduler=self._refresh_scheduler,
                debouncer=self._lifecycle_debouncer,
                claude_cli_invoker=self._lifecycle_invoker,
                tracking_backend=self._tracking_backend,
            )
            runner.run(aliases, parent_job_id=job_id)
            logger.info(
                "Lifecycle backfill async: completed repair for %d aliases",
                len(aliases),
            )
        except Exception:
            logger.error(
                "Lifecycle backfill async: repair thread failed",
                exc_info=True,
            )

    def on_refresh_complete(
        self,
        repo_alias: str,
        repo_path: str,
        success: bool,
        result: Optional[Dict[str, Any]] = None,
        job_id: Optional[str] = None,
    ) -> None:
        """
        Callback for ClaudeCliManager when refresh completes.

        Updates tracking record with success/failure status and change markers.
        If a job_id is provided and a job_tracker is configured, updates the
        job status accordingly (AC2, AC7, AC8 - Story #313).

        Args:
            repo_alias: Repository alias
            repo_path: Path to repository
            success: Whether refresh succeeded
            result: Result data from Claude CLI (may contain error info)
            job_id: Optional job ID for unified job tracking (Story #313)
        """
        now = datetime.now(timezone.utc).isoformat()

        # Read current metadata to save change markers
        metadata_path = Path(repo_path) / ".code-indexer" / "metadata.json"
        change_markers = {}

        if metadata_path.exists():
            try:
                with open(metadata_path) as f:
                    metadata = json.load(f)

                if "current_commit" in metadata:
                    change_markers["last_known_commit"] = metadata["current_commit"]

                if "files_processed" in metadata:
                    change_markers["last_known_files_processed"] = metadata[
                        "files_processed"
                    ]

                if "indexed_at" in metadata:
                    change_markers["last_known_indexed_at"] = metadata["indexed_at"]

            except Exception as e:
                logger.warning(
                    f"Failed to read metadata from {repo_path}: {e}", exc_info=True
                )

        # Update tracking record
        if success:
            # Bug #953: reset circuit-breaker counter on any successful refresh.
            self._prompt_failure_counts[repo_alias] = 0
            # Bug #984: re-arm warning so future legitimate failures warn again.
            self._warned_missing_desc.discard(repo_alias)
            self._tracking_backend.upsert_tracking(
                repo_alias=repo_alias,
                status="completed",
                last_run=now,
                next_run=self.calculate_next_run(repo_alias),
                error=None,
                updated_at=now,
                **change_markers,
            )
            logger.info(f"Description refresh completed for {repo_alias}")
        else:
            error_msg = result.get("error") if result else "Unknown error"
            self._tracking_backend.upsert_tracking(
                repo_alias=repo_alias,
                status="failed",
                last_run=now,
                next_run=self.calculate_next_run(repo_alias),
                error=error_msg,
                updated_at=now,
            )
            logger.warning(f"Description refresh failed for {repo_alias}: {error_msg}")

        # Update unified job tracker if configured (Story #313, AC2, AC7, AC8)
        if job_id is not None and self._job_tracker is not None:
            try:
                if success:
                    self._job_tracker.complete_job(job_id)
                else:
                    error_message = result.get("error") if result else "Unknown error"
                    self._job_tracker.fail_job(job_id, error=str(error_message))
            except Exception as e:
                logger.warning(f"Failed to update job tracker for job {job_id}: {e}")

    def _run_loop(self) -> None:
        """
        Main scheduler loop (runs in daemon thread).

        Periodically checks for stale repos and submits refresh jobs.
        Delegates per-iteration logic to _run_loop_single_pass().
        """
        while not self._shutdown_event.is_set():
            try:
                # Check if enabled (config may change while running)
                config = self._config_manager.load_config()
                if (
                    not config
                    or not config.claude_integration_config
                    or not config.claude_integration_config.description_refresh_enabled
                ):
                    logger.debug("Description refresh disabled, sleeping")
                    self._shutdown_event.wait(60)
                    continue

                self._run_loop_single_pass()

            except Exception as e:
                logger.error(
                    f"Error in description refresh scheduler loop: {e}", exc_info=True
                )

            # Sleep between checks
            self._shutdown_event.wait(60)

    def _run_loop_single_pass(self) -> None:
        """
        Execute one pass of the scheduler loop: scan for stale repos and spawn refresh tasks.

        Extracted from _run_loop() to enable unit testing of job registration behavior.
        For each stale repo with changes, registers a description_refresh job in the
        job_tracker (if configured) and spawns a background thread (AC2, Story #313).
        """
        import uuid

        stale_repos = self.get_stale_repos()

        for repo in stale_repos:
            alias = repo["repo_alias"]
            clone_path = repo["clone_path"]

            # Submit refresh job (if ClaudeCliManager available)
            if self._claude_cli_manager:
                logger.info(f"Submitting description refresh for {alias}")

                # Bug #984: check quarantine state BEFORE calling _get_refresh_prompt()
                # so that already-quarantined repos never trigger the warning inside it.
                if (
                    self._prompt_failure_counts[alias]
                    >= PROMPT_FAILURE_QUARANTINE_THRESHOLD
                ):
                    logger.debug(
                        "Repo %s is quarantined (failure count %d >= %d), skipping",
                        alias,
                        self._prompt_failure_counts[alias],
                        PROMPT_FAILURE_QUARANTINE_THRESHOLD,
                    )
                    continue

                # Get refresh prompt using RepoAnalyzer
                prompt = self._get_refresh_prompt(alias, clone_path)
                if prompt is None:
                    # v10.4.14: stub-heal returns None opportunistically (heal attempted
                    # or preconditions not met).  Do NOT count toward quarantine and do
                    # NOT reschedule/continue — fall through so the lifecycle runner fires
                    # normally.  Heal is opportunistic; a missing precondition (e.g. no
                    # cli_manager in tests) simply defers to the next cycle.
                    if alias in self._stub_heal_no_quarantine_aliases:
                        self._stub_heal_no_quarantine_aliases.discard(alias)
                        continue
                    else:
                        # Bug #953: circuit-breaker — count consecutive prompt failures.
                        self._prompt_failure_counts[alias] += 1
                        failure_count = self._prompt_failure_counts[alias]
                        if failure_count >= PROMPT_FAILURE_QUARANTINE_THRESHOLD:
                            # Log exactly once at the quarantine boundary; subsequent
                            # passes are silently skipped to avoid log spam.
                            if failure_count == PROMPT_FAILURE_QUARANTINE_THRESHOLD:
                                logger.error(
                                    "Repo %s entered quarantine after %d consecutive "
                                    "prompt-generation failures — will not reschedule until "
                                    "a successful refresh resets the counter.",
                                    alias,
                                    failure_count,
                                )
                            # Do NOT call upsert_tracking — leave next_run stale so the
                            # repo stays quarantined until the counter is reset externally.
                            continue
                        logger.warning(
                            "Cannot refresh %s: failed to generate prompt, rescheduling"
                            " (failure %d/%d)",
                            alias,
                            failure_count,
                            PROMPT_FAILURE_QUARANTINE_THRESHOLD,
                        )
                        # Reschedule to next cycle to avoid infinite retry loop (Finding N3)
                        now = datetime.now(timezone.utc).isoformat()
                        self._tracking_backend.upsert_tracking(
                            repo_alias=alias,
                            next_run=self.calculate_next_run(alias),
                            updated_at=now,
                        )
                        continue

                # Mark as queued before submitting
                now = datetime.now(timezone.utc).isoformat()
                self._tracking_backend.upsert_tracking(
                    repo_alias=alias,
                    status="queued",
                    updated_at=now,
                )

                # Register job in unified tracker before spawning thread (AC2, Story #313)
                tracked_job_id: Optional[str] = None
                if self._job_tracker is not None:
                    try:
                        tracked_job_id = f"desc-refresh-{alias}-{uuid.uuid4().hex[:8]}"
                        self._job_tracker.register_job(
                            tracked_job_id,
                            "description_refresh",
                            username="system",
                            repo_alias=alias,
                        )
                    except Exception as e:
                        logger.warning(
                            f"JobTracker registration failed for {alias}: {e}"
                        )
                        tracked_job_id = None

                # Run two-phase task in background thread to avoid blocking scheduler loop
                def refresh_task(
                    alias=alias,
                    clone_path=clone_path,
                    job_id=tracked_job_id,
                ):
                    # Story #313 AC7: Transition to "running" when thread starts
                    if job_id is not None and self._job_tracker is not None:
                        try:
                            self._job_tracker.update_status(job_id, status="running")
                        except Exception as e:
                            logger.debug(
                                f"Non-fatal: Failed to update job {job_id} to running: {e}"
                            )
                    success = True
                    error_dict = None
                    try:
                        # Story #876 D3: route through LifecycleBatchRunner.
                        # Wiring guards emit a WARNING and skip the runner when
                        # any collaborator is missing (Messi Rule #2 anti-fallback).
                        self._run_lifecycle_via_batch_runner(alias, job_id)
                    except Exception as e:
                        logger.error(
                            f"Lifecycle batch runner failed for {alias}: {e}",
                            exc_info=True,
                        )
                        success = False
                        error_dict = {"error": str(e)}
                    self.on_refresh_complete(
                        alias, clone_path, success, error_dict, job_id=job_id
                    )

                # Story #728 AC5: Use bounded executor instead of unbounded threading.Thread.
                # Guard against submission after shutdown to prevent tasks leaking
                # into a draining executor after stop() has been called.
                if not self._shutdown_event.is_set():
                    future = self._executor.submit(refresh_task)

                    def _log_future_exception(
                        f: concurrent.futures.Future, _alias: str = alias
                    ) -> None:
                        exc = f.exception()
                        if exc is not None:
                            logger.error(
                                "Unhandled exception in refresh task for %s: %s",
                                _alias,
                                exc,
                                exc_info=exc,
                            )

                    future.add_done_callback(_log_future_exception)
                    logger.debug(f"Submitted refresh task for {alias}")
                else:
                    logger.debug(
                        f"Skipping refresh task for {alias}: shutdown in progress"
                    )

            else:
                logger.debug(f"ClaudeCliManager not available, skipping {alias}")

    def _run_lifecycle_via_batch_runner(
        self, alias: str, job_id: Optional[str]
    ) -> None:
        """
        Route one refresh event through LifecycleBatchRunner (Story #876 D3).

        Any missing wiring slot (lifecycle_invoker, golden_repos_dir,
        lifecycle_debouncer, refresh_scheduler, job_tracker, job_id) emits a
        WARNING and skips the runner — never a silent fallback (Messi Rule #2).
        Runner exceptions propagate to refresh_task for sidecar handling.
        """
        wiring = {
            "lifecycle_invoker": self._lifecycle_invoker,
            "golden_repos_dir": self._golden_repos_dir,
            "lifecycle_debouncer": self._lifecycle_debouncer,
            "refresh_scheduler": self._refresh_scheduler,
            "job_tracker": self._job_tracker,
            "job_id": job_id,
        }
        for name, value in wiring.items():
            if value is None:
                logger.warning(
                    "Skipping lifecycle refresh for %s: %s not wired",
                    alias,
                    name,
                )
                return

        runner = LifecycleBatchRunner(
            golden_repos_dir=self._golden_repos_dir,
            job_tracker=self._job_tracker,
            refresh_scheduler=self._refresh_scheduler,
            debouncer=self._lifecycle_debouncer,
            claude_cli_invoker=self._lifecycle_invoker,
        )
        runner.run([alias], parent_job_id=job_id)

    def _get_interval_hours(self) -> int:
        """Get refresh interval from config."""
        config = self._config_manager.load_config()
        if not config or not config.claude_integration_config:
            return 24  # Default

        return int(config.claude_integration_config.description_refresh_interval_hours)

    def _read_existing_description(
        self, repo_alias: str
    ) -> Optional[Dict[str, Optional[str]]]:
        """
        Read existing .md file from cidx-meta and extract description and last_analyzed.

        Args:
            repo_alias: Repository alias

        Returns:
            Dict with 'description' and 'last_analyzed' keys, or None if file not found
        """
        if not self._meta_dir:
            logger.warning("Meta directory not set, cannot read existing description")
            return None

        md_file = self._meta_dir / f"{repo_alias}.md"
        if not md_file.exists():
            logger.debug(f"No .md file found for {repo_alias}, cannot refresh")
            return None

        try:
            content = md_file.read_text()

            # Parse YAML frontmatter to extract last_analyzed
            # Format: ---\nfield: value\n---\n<body>
            frontmatter_match = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
            if not frontmatter_match:
                logger.warning(f"No YAML frontmatter found in {md_file}")
                return {"description": content, "last_analyzed": None}

            frontmatter_text = frontmatter_match.group(1)
            _body = frontmatter_match.group(2)

            # Extract last_analyzed from frontmatter
            last_analyzed = None
            for line in frontmatter_text.split("\n"):
                if line.startswith("last_analyzed:"):
                    last_analyzed = line.split(":", 1)[1].strip()
                    break

            return {"description": content, "last_analyzed": last_analyzed}

        except Exception as e:
            logger.error(
                f"Failed to read existing description for {repo_alias}: {e}",
                exc_info=True,
            )
            return None

    def _validate_refresh_inputs(
        self, repo_alias: str, repo_path: str
    ) -> Optional[Path]:
        """Validate refresh inputs; return resolved repo Path or None on failure."""
        if not repo_alias or not isinstance(repo_alias, str):
            logger.warning("_get_refresh_prompt: repo_alias must be a non-empty string")
            return None
        if not repo_path or not isinstance(repo_path, str):
            logger.warning("_get_refresh_prompt: repo_path must be a non-empty string")
            return None
        resolved = Path(repo_path).resolve()
        if not resolved.exists() or not resolved.is_dir():
            logger.warning(
                "_get_refresh_prompt: repo_path does not resolve to a directory: %s",
                repo_path,
            )
            return None
        return resolved

    def _stage_and_build_prompt(
        self, description: str, last_analyzed: str, repo_path_obj: Path
    ) -> Optional[str]:
        """
        Stage *description* to a temp file and build a file-reference refresh prompt.

        Creates a unique temp dir under *repo_path_obj*, writes ``existing_desc.md``,
        calls RepoAnalyzer.get_prompt with ``temp_file_path``, and returns the prompt.
        Cleans up the temp dir on any error; on success the dir persists for the CLI
        subprocess (caller is responsible for cleanup after the CLI call).
        """
        import shutil
        import tempfile
        from code_indexer.global_repos.repo_analyzer import RepoAnalyzer

        tmp_dir_str = tempfile.mkdtemp(dir=repo_path_obj)
        try:
            temp_file = Path(tmp_dir_str) / "existing_desc.md"
            temp_file.write_text(description, encoding="utf-8")
            analyzer = RepoAnalyzer(str(repo_path_obj))
            return cast(
                Optional[str],
                analyzer.get_prompt(
                    mode="refresh",
                    last_analyzed=last_analyzed,
                    temp_file_path=temp_file,
                ),
            )
        except Exception as e:
            shutil.rmtree(tmp_dir_str, ignore_errors=True)
            logger.error("_stage_and_build_prompt failed: %s", e, exc_info=True)
            return None

    def _extract_body_from_description(self, full_content: str) -> str:
        """
        Extract the markdown body (content after YAML frontmatter) from a .md file string.

        Falls back to full content when frontmatter is absent, matching
        _read_existing_description's no-frontmatter fallback behaviour.
        """
        fm_match = re.match(r"^---\n.*?\n---\n(.*)$", full_content, re.DOTALL)
        if fm_match:
            return fm_match.group(1)
        return full_content

    def _is_stub_description(self, desc_data: Dict[str, Optional[str]]) -> bool:
        """
        Return True when desc_data meets v10.4.14 stub-detection criteria (logical OR):
          (a) last_analyzed is None or empty string
          (b) body length (chars after YAML frontmatter) < _STUB_BODY_CHAR_THRESHOLD
        """
        if not desc_data.get("last_analyzed"):
            return True
        body = self._extract_body_from_description(desc_data.get("description") or "")
        return len(body) < _STUB_BODY_CHAR_THRESHOLD

    def _lookup_repo_url_for_heal(self, repo_alias: str) -> Optional[str]:
        """
        Look up repo_url from golden_backend for stub-heal dispatch (v10.4.14).

        Returns URL or None; logs ERROR 006 on exception, 007 when empty.
        """
        try:
            golden = self._golden_backend.get_repo(repo_alias)
            repo_url: Optional[str] = golden.get("repo_url") if golden else None
        except Exception as exc:
            logger.error(
                "DESC-REFRESH-STUB-HEAL-006 repo_url lookup failed for %s: %s",
                repo_alias,
                exc,
                exc_info=True,
            )
            return None
        if not repo_url:
            logger.error("DESC-REFRESH-STUB-HEAL-007 repo_url empty for %s", repo_alias)
            return None
        return repo_url

    def _validate_heal_preconditions(
        self, repo_alias: str, repo_path_obj: Path
    ) -> bool:
        """
        Validate preconditions for stub-heal dispatch (v10.4.14).

        Checks alias format (no separators/traversal), meta_dir presence, and
        repo_path_obj is an existing directory. Logs ERROR 008/009 on failure.
        """
        import re as _re

        if not repo_alias or _re.search(r"[/\\]|\.\.", repo_alias):
            logger.error("DESC-REFRESH-STUB-HEAL-009 invalid repo_alias %r", repo_alias)
            return False
        if self._meta_dir is None:
            logger.error(
                "DESC-REFRESH-STUB-HEAL-008 meta_dir not set for %s", repo_alias
            )
            return False
        try:
            valid_path = isinstance(repo_path_obj, Path) and repo_path_obj.is_dir()
        except Exception as exc:
            logger.error(
                "DESC-REFRESH-STUB-HEAL-009 repo_path_obj check failed for %s: %s",
                repo_alias,
                exc,
            )
            return False
        if not valid_path:
            logger.error(
                "DESC-REFRESH-STUB-HEAL-009 repo_path_obj not a directory for %s",
                repo_alias,
            )
            return False
        return True

    def _get_available_cli_manager_for_heal(self, repo_alias: str):
        """
        Acquire ClaudeCliManager for stub-heal dispatch (v10.4.14).

        Lazy-imports get_claude_cli_manager. Returns the manager when available,
        or None after logging ERROR 004 when unavailable or CLI not on PATH.
        """
        from code_indexer.server.services.claude_cli_manager import (
            get_claude_cli_manager,
        )

        cli_manager = get_claude_cli_manager()
        if cli_manager is None or not cli_manager.check_cli_available():
            reason = (
                "ClaudeCliManager not initialized"
                if cli_manager is None
                else "Claude CLI not available on PATH"
            )
            # WARNING (not ERROR): missing precondition is transient — heal defers to
            # next cycle.  ERROR is reserved for unexpected failures after preconditions
            # are satisfied (see HEAL-005, HEAL-010).
            logger.warning(
                "DESC-REFRESH-STUB-HEAL-004 cannot heal %s: %s", repo_alias, reason
            )
            return None
        return cli_manager

    def _heal_stub_description(
        self, repo_alias: str, repo_path_obj: Path
    ) -> Optional[bool]:
        """
        Dispatch full re-analysis for a stub description (v10.4.14).

        Orchestrates _validate_heal_preconditions, _lookup_repo_url_for_heal,
        _get_available_cli_manager_for_heal, then calls _generate_repo_description
        and atomic_write_description. Lazy imports preserve CLI startup discipline.
        Log codes: INFO 001/002/003; ERROR 004/005/006/007/008/009/010.

        Return values:
          True  — heal completed successfully (full regen written).
          False — preconditions not met (cli_manager absent, repo_url missing, etc.);
                  caller may fall through to incremental refresh if last_analyzed present.
          None  — preconditions met but heal failed at runtime (HEAL-005/010 logged);
                  caller should NOT fall through (avoid masking the error silently).
        """
        from code_indexer.global_repos.meta_description_hook import (
            _generate_repo_description,
            atomic_write_description,
        )

        logger.info("DESC-REFRESH-STUB-HEAL-001 stub detected for %s", repo_alias)
        if not self._validate_heal_preconditions(repo_alias, repo_path_obj):
            return False
        repo_url = self._lookup_repo_url_for_heal(repo_alias)
        if not repo_url:
            return False
        cli_manager = self._get_available_cli_manager_for_heal(repo_alias)
        if not cli_manager:
            return False

        logger.info(
            "DESC-REFRESH-STUB-HEAL-002 dispatching full regen for %s", repo_alias
        )
        try:
            md_content = _generate_repo_description(
                repo_alias, repo_url, str(repo_path_obj), cli_manager
            )
        except Exception as exc:
            logger.error(
                "DESC-REFRESH-STUB-HEAL-005 generation failed for %s: %s",
                repo_alias,
                exc,
                exc_info=True,
            )
            return None  # attempted but failed at runtime

        assert self._meta_dir is not None  # guarded by _validate_heal_preconditions
        # CRITICAL: heal must write to the SAME file _read_existing_description
        # reads from (line 989: f"{repo_alias}.md"). The tracking table stores
        # repo_alias = repo_name (bare), set by meta_description_hook.upsert_tracking
        # at line 311-312. If heal writes a different filename (e.g. with -global
        # suffix), the scheduler's next tick reads the OLD file, still detects
        # the stub, and re-dispatches the heal job — infinite loop.
        try:
            atomic_write_description(self._meta_dir / f"{repo_alias}.md", md_content)
        except Exception as exc:
            logger.error(
                "DESC-REFRESH-STUB-HEAL-010 atomic write failed for %s: %s",
                repo_alias,
                exc,
                exc_info=True,
            )
            return None  # attempted but failed at runtime
        logger.info(
            "DESC-REFRESH-STUB-HEAL-003 stub healing complete for %s", repo_alias
        )
        return True

    def _heal_stub_description_worker(
        self, repo_alias: str, repo_path_str: str
    ) -> Dict[str, Any]:
        """BackgroundJobManager-compliant worker wrapping _heal_stub_description (v10.4.14).

        Invoked from `_dispatch_heal_via_background_job` via
        `BackgroundJobManager.submit_job`.  Adapts the tri-valued
        _heal_stub_description return (True/False/None) into the dict shape
        BJM expects (status string for dashboard visibility).  Raises on
        runtime-failure so BJM marks the job FAILED, not SUCCESS.
        """
        result = self._heal_stub_description(repo_alias, Path(repo_path_str))
        if result is True:
            return {
                "status": "success",
                "operation_type": "description_stub_heal",
                "repo_alias": repo_alias,
            }
        if result is False:
            return {
                "status": "preconditions_unmet",
                "operation_type": "description_stub_heal",
                "repo_alias": repo_alias,
            }
        # result is None — runtime failure already logged via HEAL-005 or HEAL-010.
        # Raise so BJM marks the job FAILED for dashboard visibility.
        raise RuntimeError(
            f"description_stub_heal runtime failure for {repo_alias} "
            f"(see DESC-REFRESH-STUB-HEAL-005 or HEAL-010 in logs)"
        )

    def _dispatch_heal_via_background_job(
        self, repo_alias: str, repo_path_obj: Path
    ) -> bool:
        """Submit stub-heal as a tracked BackgroundJobManager job (v10.4.14).

        Called from `_get_refresh_prompt` when `_is_stub_description` returns
        True.  Returns True when the heal was queued (new job) or already in
        flight (DuplicateJobError); False when BJM unavailable or submit
        raised an unexpected exception (caller falls through if last_analyzed
        is present).

        Log codes:
          INFO  HEAL-014 — job submitted (job_id + repo_alias)
          INFO  HEAL-011 — heal already in flight (DuplicateJobError)
          ERROR HEAL-012 — submit_job raised unexpected exception
          WARNING HEAL-013 — BackgroundJobManager not wired (lifespan misconfig)
        """
        if self._background_job_manager is None:
            logger.warning(
                "DESC-REFRESH-STUB-HEAL-013 BackgroundJobManager not wired; "
                "cannot dispatch heal for %s",
                repo_alias,
            )
            return False
        try:
            from code_indexer.server.repositories.background_jobs import (
                DuplicateJobError,
            )

            job_id = self._background_job_manager.submit_job(
                "description_stub_heal",
                self._heal_stub_description_worker,
                repo_alias,
                str(repo_path_obj),
                submitter_username="system",
                repo_alias=repo_alias,
            )
        except DuplicateJobError as dup:
            logger.info(
                "DESC-REFRESH-STUB-HEAL-011 heal already in flight for %s "
                "(existing job %s); skipping dispatch",
                repo_alias,
                dup.existing_job_id,
            )
            return True
        except Exception as exc:
            logger.error(
                "DESC-REFRESH-STUB-HEAL-012 submit_job failed for %s: %s",
                repo_alias,
                exc,
                exc_info=True,
            )
            return False
        logger.info(
            "DESC-REFRESH-STUB-HEAL-014 stub-heal background job submitted "
            "for %s (job_id=%s)",
            repo_alias,
            job_id,
        )
        return True

    def _get_refresh_prompt(self, repo_alias: str, repo_path: str) -> Optional[str]:
        """
        Get refresh prompt staging the existing description to a temp file (Bug #840 Site #5).

        Returns a prompt string with the temp file path embedded, or None on failure.
        The temp dir persists until the calling thread completes the CLI invocation.

        v10.4.14: when no .md file exists, falls through to existing warn-and-bail path.
        When desc_data is present but is_stub, attempts full re-analysis via
        _heal_stub_description (optimistic enhancement):
          - True  (heal succeeded): regen complete, no incremental needed — return None
            and mark alias as no-quarantine.
          - False (preconditions unmet, e.g. cli_manager absent) AND last_analyzed
            present: fall through to incremental refresh — better than nothing.
          - False AND last_analyzed absent: nothing to refresh — return None,
            mark as no-quarantine.
          - None  (heal attempted but failed at runtime): return None and mark as
            no-quarantine; do NOT fall through so the runtime error is not masked.
        """
        repo_path_obj = self._validate_refresh_inputs(repo_alias, repo_path)
        if repo_path_obj is None:
            return None
        desc_data = self._read_existing_description(repo_alias)
        if not desc_data:
            # Bug #984: warn at WARNING level only on the first occurrence per repo
            # per scheduler instance lifetime; downgrade to DEBUG on repeats so
            # missing-file repos do not flood the log on every scheduler pass.
            if repo_alias not in self._warned_missing_desc:
                logger.warning(
                    "Cannot generate refresh prompt for %s: missing description or last_analyzed",
                    repo_alias,
                )
                self._warned_missing_desc.add(repo_alias)
            else:
                logger.debug(
                    "Cannot generate refresh prompt for %s: missing description or last_analyzed (suppressed repeat)",
                    repo_alias,
                )
            return None
        # v10.4.14: detect stubs and dispatch full re-analysis as a proper
        # background job via BackgroundJobManager (operation_type="description_stub_heal").
        # The dispatch is NON-BLOCKING — scheduler tick continues in milliseconds
        # while the heal runs concurrently in BJM's worker pool with dashboard
        # visibility, JobTracker registration, and DuplicateJobError dedup.
        if self._is_stub_description(desc_data):
            dispatched = self._dispatch_heal_via_background_job(
                repo_alias, repo_path_obj
            )
            has_last_analyzed = bool(desc_data.get("last_analyzed"))
            # Four explicit cases over (dispatched, has_last_analyzed):
            if dispatched and has_last_analyzed:
                # Heal in flight will replace the stub; skip incremental refresh
                # to avoid race where the regular refresh overwrites the healed
                # description.  Mark no-quarantine: heal is the real fix.
                self._stub_heal_no_quarantine_aliases.add(repo_alias)
                return None
            if not dispatched and has_last_analyzed:
                # BJM unavailable / submit error (HEAL-013/012 logged); no heal
                # in flight.  Try incremental refresh.  Do NOT mark no-quarantine
                # — if incremental also fails, normal quarantine semantics should
                # apply so operators see the misconfiguration.
                return self._stage_and_build_prompt(
                    desc_data.get("description") or "",
                    desc_data["last_analyzed"] or "",
                    repo_path_obj,
                )
            if dispatched and not has_last_analyzed:
                # Heal in flight; nothing to incrementally refresh.  Mark
                # no-quarantine: heal will replace the stub on completion.
                self._stub_heal_no_quarantine_aliases.add(repo_alias)
                return None
            # not dispatched and not has_last_analyzed:
            # Stub with no last_analyzed AND no heal in flight.  Nothing
            # actionable this cycle.  Mark no-quarantine because the underlying
            # issue is the stub itself, not a prompt-generation failure.
            self._stub_heal_no_quarantine_aliases.add(repo_alias)
            return None
        return self._stage_and_build_prompt(
            desc_data.get("description") or "",
            desc_data["last_analyzed"] or "",
            repo_path_obj,
        )

    def _build_cli_dispatcher(self, config) -> "CliDispatcher":  # noqa: F821
        """
        Build a CliDispatcher from *config* (Story #847).

        Delegates to build_dep_map_dispatcher (the single source of truth for
        CliDispatcher construction — Bug #936 consolidation), forwarding
        analysis_model and the per-scheduler soft-timeout constant.

        Args:
            config: ServerConfig returned by config_manager.load_config().

        Returns:
            A fully initialised CliDispatcher.
        """
        return build_dep_map_dispatcher(
            config,
            analysis_model=self._analysis_model,
            claude_soft_timeout_seconds=_CLAUDE_CLI_SOFT_TIMEOUT_SECONDS,
        )

    def _invoke_claude_cli(self, repo_path: str, prompt: str) -> tuple[bool, str]:
        """
        Invoke the CLI dispatcher with the given prompt (Story #847).

        Uses the injected CliDispatcher when available; otherwise builds one
        from the current ServerConfig on each call.  Logs an INFO record when
        failover fired so operators can see which CLI handled the job.

        Args:
            repo_path: Path to repository (used as subprocess cwd).
            prompt: Prompt to send to the CLI.

        Returns:
            Tuple of (success: bool, result: str) where result is the output
            or error message — identical shape to the pre-wiring behaviour.
        """
        if self._cli_dispatcher is not None:
            dispatcher = self._cli_dispatcher
        else:
            config = (
                self._config_manager.load_config() if self._config_manager else None
            )
            dispatcher = self._build_cli_dispatcher(config)

        result = dispatcher.dispatch(
            flow="description_refresh",
            cwd=repo_path,
            prompt=prompt,
            timeout=_CLAUDE_CLI_HARD_TIMEOUT_SECONDS,
        )

        if result.was_failover:
            logger.info(
                "CLI failover fired: cli_used=%s was_failover=True",
                result.cli_used,
            )

        if not result.success:
            return False, result.error

        # Validate output quality (detect error messages masquerading as content)
        if not self._validate_cli_output(result.output):
            error_msg = (
                f"CLI output appears to be an error message"
                f" (length={len(result.output)}): {result.output[:200]}"
            )
            logger.warning(error_msg)
            return False, error_msg

        return True, result.output

    def _validate_cli_output(self, output: str) -> bool:
        """
        Validate that Claude CLI output is a real description, not an error message.

        Args:
            output: Cleaned output from Claude CLI

        Returns:
            True if output looks like a valid description, False if it appears to be an error
        """
        # Empty or very short output is invalid
        if not output or len(output) < 100:
            output_len = len(output) if output else 0
            logger.warning(
                f"CLI output too short ({output_len} chars), likely an error"
            )
            return False

        # Infrastructure error patterns — always checked.
        # These strings can never appear in valid YAML description content.
        infrastructure_errors = [
            "Invalid API key",
            "Fix external API key",
            "cannot be launched inside another",
            "Nested sessions share runtime",
            "CLAUDECODE environment variable",
            "Authentication failed",
        ]

        output_lower = output.lower()
        for pattern in infrastructure_errors:
            if pattern.lower() in output_lower:
                logger.warning(f"CLI output contains error pattern: '{pattern}'")
                return False

        # Content-ambiguous patterns — only checked when output lacks YAML frontmatter.
        # Valid descriptions always start with "---" (YAML frontmatter).
        # Real API errors (rate limit, quota exceeded, Error:) arrive as plain text
        # without frontmatter.  When frontmatter IS present the output is a real
        # description even if it mentions these terms as domain concepts (Bug #382).
        has_frontmatter = output.startswith("---")
        if not has_frontmatter:
            content_ambiguous_errors = [
                "rate limit",
                "quota exceeded",
                "Error:",
            ]
            for pattern in content_ambiguous_errors:
                if pattern.lower() in output_lower:
                    logger.warning(f"CLI output contains error pattern: '{pattern}'")
                    return False

        return True

    def _update_description_file(self, repo_alias: str, content: str) -> None:
        """
        Update the .md description file for a repository.

        Args:
            repo_alias: Repository alias
            content: New content for the .md file (YAML frontmatter + markdown)
        """
        if not self._meta_dir:
            logger.warning(
                f"Meta directory not set, cannot update description for {repo_alias}"
            )
            return

        md_file = self._meta_dir / f"{repo_alias}.md"

        try:
            md_file.write_text(content)
            logger.info(f"Updated description file: {md_file}")

        except Exception as e:
            logger.error(
                f"Failed to update description file for {repo_alias}: {e}",
                exc_info=True,
            )

    def close(self) -> None:
        """Clean up resources."""
        self.stop()
        self._tracking_backend.close()
        self._golden_backend.close()
