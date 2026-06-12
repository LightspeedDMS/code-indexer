"""
Description Refresh Scheduler (Story #190).

Manages periodic description regeneration for golden repositories using
uniform-random scheduling across the full interval to distribute load evenly.
"""

import concurrent.futures
import json
import logging
import random
import re
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from code_indexer.global_repos.lifecycle_batch_runner import LifecycleBatchRunner
from code_indexer.server.services.job_tracker import DuplicateJobError
from code_indexer.server.storage.sqlite_backends import (
    DescriptionRefreshTrackingBackend,
    GoldenRepoMetadataSqliteBackend,
)

logger = logging.getLogger(__name__)

# Claude CLI timeout constants (Story #727).
# NOTE: This module assumes a Unix-like host with ``script``, ``timeout``, and
# ``claude`` available in PATH.  The ``script`` utility provides a pseudo-TTY
# required for Claude CLI in non-interactive environments.
_CLAUDE_CLI_SOFT_TIMEOUT_SECONDS = 1800  # inner shell ``timeout`` budget (30 min)
_CLAUDE_CLI_HARD_TIMEOUT_SECONDS = 1860  # Python subprocess.run cap (shell + 60s grace)

# Bug #953: circuit-breaker threshold for consecutive prompt-generation failures.
# After this many consecutive None-prompt results for the same repo, the scheduler
# stops rescheduling (quarantines) and logs one ERROR.  Reset to 0 on success.
PROMPT_FAILURE_QUARANTINE_THRESHOLD = 3

# Description body length threshold for the startup backfill sweep.
# Aliases whose cidx-meta body (stripped) is at or below this limit are
# considered terse and queued for regeneration via LifecycleBatchRunner.
# Bug #1064: lowered from 500 to 200. At 500 the detector re-flagged
# legitimately-concise small-repo descriptions (300-500 chars) on every
# startup, causing an infinite regeneration loop. At 200 (barely a sentence)
# only genuine stubs or failed generations are queued for regeneration.
TERSE_DESCRIPTION_MAX_CHARS = 200


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

    Implements uniform-random scheduling across the full interval to distribute
    refresh jobs evenly across time, preventing thundering herd.
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
        # Bug #953: per-repo consecutive refresh-failure counter.
        # Reset to 0 by on_refresh_complete(success=True).
        # When the count reaches PROMPT_FAILURE_QUARANTINE_THRESHOLD the repo is
        # quarantined (not rescheduled) and one ERROR log is emitted.
        self._prompt_failure_counts: Dict[str, int] = defaultdict(int)
        # Bug #1096 review fix: on-disk commit fingerprint recorded at failure time.
        # Quarantine auto-clear compares the CURRENT on-disk fingerprint to this value.
        # Using has_changes_since_last_run for the clear decision is wrong because
        # last_known_commit stays NULL for repos that never succeeded, making
        # has_changes always return True — defeating quarantine for the worst case.
        self._failure_commit: Dict[str, Optional[str]] = {}
        # Story #1062: two separate events replacing the old single _backfill_in_progress.
        # Each async path sets/clears its OWN event so one finishing does not clear the other.
        self._lifecycle_backfill_running = threading.Event()
        self._description_backfill_running = threading.Event()
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

        logger.info(
            f"Description refresh scheduler started "
            f"(interval: {interval_hours}h, uniform random across full interval)"
        )

        self._migrate_global_suffix_filenames()
        self.reconcile_orphan_tracking()
        self._reconcile_stale_next_run_rows()
        self.reconcile_broken_lifecycle_metadata()
        self.reconcile_terse_descriptions()

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
        Calculate next run time using uniform random across the full interval.

        Distributes refreshes evenly across the entire interval window so that
        no thundering herd forms — not at steady-state, and not on first-enable
        (when _reconcile_stale_next_run_rows re-slots all stale rows).

        Args:
            alias: Repository alias (kept for API/log-line compatibility; unused in body)
            interval_hours: Interval in hours (defaults to config value)

        Returns:
            ISO 8601 timestamp for next run
        """
        if interval_hours is None:
            interval_hours = self._get_interval_hours()

        offset_seconds = random.uniform(0, interval_hours * 3600)
        return (
            datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
        ).isoformat()

    def _reconcile_stale_next_run_rows(self) -> int:
        """One-shot sweep: recompute next_run for tracking rows that are NULL or in the past.

        Called from start() AFTER reconcile_orphan_tracking() so orphan rows are
        already gone before we iterate.  Rows with a valid FUTURE next_run are
        preserved unchanged — this avoids disturbing repos that were already
        spread across the interval by a previous start cycle.

        Returns the number of rows recomputed. Self-defensive: any exception in the
        sweep is logged and swallowed so scheduler startup cannot be blocked.
        """
        recomputed = 0
        try:
            rows = self._tracking_backend.get_all_tracking()
        except Exception:
            logger.error(
                "Stale next_run reconciliation: get_all_tracking failed",
                exc_info=True,
            )
            return 0

        now = datetime.now(timezone.utc)

        for row in rows:
            alias = row.get("repo_alias")
            if not alias:
                continue
            next_run = row.get("next_run")
            # Recompute if NULL or already past (tz-aware comparison to avoid
            # lexicographic-compare footgun with Postgres TIMESTAMPTZ in cluster mode).
            if next_run is None:
                is_stale = True
            else:
                try:
                    next_run_dt = datetime.fromisoformat(str(next_run))
                    if next_run_dt.tzinfo is None:
                        next_run_dt = next_run_dt.replace(tzinfo=timezone.utc)
                    is_stale = next_run_dt <= now
                except (ValueError, TypeError):
                    logger.warning(
                        "Stale next_run reconciliation: unparseable next_run=%r for %s, recomputing",
                        next_run,
                        alias,
                    )
                    is_stale = True
            if is_stale:
                try:
                    new_next_run = self.calculate_next_run(alias)
                    self._tracking_backend.upsert_tracking(
                        repo_alias=alias,
                        next_run=new_next_run,
                        updated_at=datetime.now(timezone.utc).isoformat(),
                    )
                    recomputed += 1
                except Exception:
                    logger.error(
                        "Stale next_run reconciliation: upsert failed for %s",
                        alias,
                        exc_info=True,
                    )

        logger.info(
            "Stale next_run reconciliation: recomputed %d rows (uniform random across interval)",
            recomputed,
        )
        return recomputed

    def _read_current_fingerprint(self, repo_path: str) -> Optional[str]:
        """
        Read the on-disk commit (or files_processed) fingerprint from the repo's
        metadata file — the SAME source has_changes_since_last_run reads.

        Returns the fingerprint string, or None when no metadata file exists or
        the metadata cannot be parsed.  The returned value is suitable for stable
        equality comparison: same string → same code state; different string →
        code has changed.

        Used by:
        - has_changes_since_last_run (change detection at schedule time)
        - on_refresh_complete failure branch (record fingerprint at failure time)
        - _run_loop_single_pass quarantine gate (compare current vs failure fingerprint)

        Extracting this helper removes the triple-read of metadata that previously
        existed across these three call sites (Messi Rule #4 anti-duplication).
        """
        metadata_dir = Path(repo_path) / ".code-indexer"
        metadata_path = metadata_dir / "metadata.json"

        if not metadata_path.exists():
            provider_files = sorted(metadata_dir.glob("metadata-*.json"))
            if provider_files:
                metadata_path = provider_files[0]
            else:
                return None

        try:
            with open(metadata_path) as f:
                metadata = json.load(f)

            if "current_commit" in metadata:
                return str(metadata["current_commit"])

            if "files_processed" in metadata:
                # Use files_processed + indexed_at as a combined fingerprint for
                # non-git repos so that any re-index is detectable.
                files = metadata["files_processed"]
                indexed_at = metadata.get("indexed_at", "")
                return f"{files}:{indexed_at}"

            return None

        except Exception as e:
            logger.warning(
                f"Failed to read metadata fingerprint from {repo_path}: {e}",
                exc_info=True,
            )
            return None

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
        metadata_dir = Path(repo_path) / ".code-indexer"
        metadata_path = metadata_dir / "metadata.json"

        if not metadata_path.exists():
            # Golden repos use provider-specific filenames: metadata-{provider}.json
            provider_files = sorted(metadata_dir.glob("metadata-*.json"))
            if provider_files:
                metadata_path = provider_files[0]
            else:
                logger.debug(f"No metadata in {repo_path}, assuming changes")
                return True

        try:
            with open(metadata_path) as f:
                metadata = json.load(f)

            # Git repository: compare current_commit
            if "current_commit" in metadata:
                last_known_commit = tracking_record.get("last_known_commit")
                current_commit = metadata["current_commit"]

                # #1094 (reverts #1093 Fix A): a NULL last_known_commit means we
                # have no commit marker yet, so a refresh MUST fire — it is the
                # signal the marker still needs establishing.  When an existing
                # .md is present the refresh REFINES it (and stamps last_analyzed)
                # rather than skipping; either way we must not suppress it here.
                if last_known_commit is None:
                    logger.debug(
                        f"Changes detected in {repo_path}: no last_known_commit "
                        f"marker — refresh needed to establish it"
                    )
                    return True

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

    def reconcile_terse_descriptions(self) -> int:
        """One-shot backfill sweep: scan all golden repos for terse cidx-meta
        descriptions and asynchronously route them through LifecycleBatchRunner
        for regeneration.

        Runs once at start(), after reconcile_broken_lifecycle_metadata() and
        before the periodic daemon thread spawns.  Closes the production gap
        where repos with body <= TERSE_DESCRIPTION_MAX_CHARS chars in their
        cidx-meta .md files are never regenerated by any event-driven code path.

        Returns the number of terse aliases queued for async regeneration, or 0
        on any error or empty result.  Self-defensive: all failures are logged
        and swallowed so scheduler startup cannot be blocked.
        """
        if not self._check_lifecycle_backfill_wiring():
            return 0

        aliases = self._list_golden_aliases()
        if aliases is None:
            return 0
        if not aliases:
            logger.info("Description backfill: no golden repos to scan")
            return 0

        terse = self._find_terse_description_aliases(aliases)
        if terse is None:
            return 0
        if not terse:
            logger.info(
                "Description backfill: no terse descriptions found "
                "(%d aliases adequate)",
                len(aliases),
            )
            return 0

        logger.info(
            "Description backfill: identified %d terse aliases — "
            "dispatching async regeneration thread",
            len(terse),
        )
        try:
            self._dispatch_description_backfill_thread(terse)
        except Exception:
            logger.error(
                "Description backfill: failed to dispatch async regeneration thread",
                exc_info=True,
            )
            return 0
        return len(terse)

    def _migrate_global_suffix_filenames(self) -> int:
        """One-time migration: rename {alias}-global.md -> {alias}.md in cidx-meta.

        v10.4.9 introduced -global suffix in WRITE paths. v10.8.0 reversed this.
        Must run BEFORE any description scan to preserve existing frontmatter.
        Skips rename when target {alias}.md already exists.

        # INVARIANT: cidx-meta descriptions use the SHORT repo alias as filename:
        #   {short_alias}.md  (e.g., JSqlParser.md)
        # The "-global" suffix belongs to the registry alias_name, NOT the filename.
        """
        if self._meta_dir is None or not self._meta_dir.exists():
            return 0
        try:
            aliases = self._list_golden_aliases()
            if not aliases:
                return 0
            count = 0
            for alias in aliases:
                old_file = self._meta_dir / f"{alias}-global.md"
                new_file = self._meta_dir / f"{alias}.md"
                if old_file.exists() and not new_file.exists():
                    old_file.rename(new_file)
                    logger.info(
                        "Filename migration: renamed %s -> %s",
                        old_file.name,
                        new_file.name,
                    )
                    count += 1
            if count:
                logger.info("Filename migration: renamed %d files", count)
            return count
        except Exception:
            logger.error(
                "Filename migration: failed — skipping (non-fatal)",
                exc_info=True,
            )
            return 0

    def _find_terse_description_aliases(
        self, aliases: List[str]
    ) -> Optional[List[str]]:
        """Scan cidx-meta files for each alias and return those with terse bodies.

        A description is considered terse when the body section of the markdown
        file (everything after the closing ``---`` frontmatter delimiter) has a
        stripped length of <= TERSE_DESCRIPTION_MAX_CHARS characters.  Files
        without YAML frontmatter are treated as pure-body files and their full
        content is checked.

        The alias ``cidx-meta`` is always skipped (self-referential).
        Aliases whose cidx-meta file does not exist are silently skipped.

        Returns None on exception (caller skips the sweep).
        Returns [] if all descriptions are adequate.
        """
        try:
            terse: List[str] = []
            for alias in aliases:
                if alias == "cidx-meta":
                    continue
                if self._meta_dir is None:
                    continue
                # INVARIANT: cidx-meta filenames use SHORT alias ({alias}.md), NOT -global.md
                md_file = self._meta_dir / f"{alias}.md"
                if not md_file.exists():
                    continue
                content = md_file.read_text()
                frontmatter_match = re.match(
                    r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL
                )
                if frontmatter_match:
                    body = frontmatter_match.group(2)
                else:
                    # No frontmatter — whole content is the body
                    body = content
                if len(body.strip()) <= TERSE_DESCRIPTION_MAX_CHARS:
                    terse.append(alias)
            return terse
        except Exception:
            logger.error(
                "Description backfill: file scan failed — skipping startup sweep",
                exc_info=True,
            )
            return None

    def _dispatch_description_backfill_thread(self, terse: List[str]) -> None:
        """Spawn a daemon thread to run LifecycleBatchRunner on *terse* aliases."""
        thread = threading.Thread(
            target=self._run_description_backfill_async,
            args=(list(terse),),
            daemon=True,
            name="description-backfill",
        )
        thread.start()

    def _run_description_backfill_async(self, aliases: List[str]) -> None:
        """Background worker: run LifecycleBatchRunner on terse-description aliases.

        Story #1062: uses _description_backfill_running (not the removed _backfill_in_progress)
        and drives BackfillJournalService for NFS-shared observability.

        Generates a UUID job_id, registers it with JobTracker (operation_type
        'description_backfill', username 'system'), then invokes
        LifecycleBatchRunner.run(aliases, parent_job_id=job_id) — which itself
        handles sub-batching, concurrency, per-alias locking, debouncer signal,
        and job_tracker.complete_job.

        All exceptions are logged and swallowed — a sweep failure must not crash
        the daemon thread or leak back into scheduler startup.
        """
        self._description_backfill_running.set()

        # Story #1062: per-namespace NFS journal under {golden_repos_dir}/.scratch/
        journal_svc = self._init_backfill_journal("description")
        try:
            journal_svc.start(total=len(aliases))
        except Exception as exc:
            logger.warning(
                "Description backfill: journal start failed (degraded): %s", exc
            )

        try:
            if self._job_tracker is None:
                logger.error(
                    "Description backfill async: job_tracker missing — "
                    "cannot route regeneration through LifecycleBatchRunner"
                )
                return

            import uuid

            job_id = str(uuid.uuid4())
            self._job_tracker.register_job_if_no_conflict(
                job_id=job_id,
                operation_type="description_backfill",
                username="system",
                repo_alias="server",
                metadata={
                    "total": len(aliases),
                    "source": "startup_description_backfill",
                },
            )

            def _journal_cb(alias: str, outcome: str) -> None:
                try:
                    success = not outcome.startswith("failed")
                    reason = (
                        outcome[len("failed: ") :]
                        if outcome.startswith("failed: ")
                        else None
                    )
                    journal_svc.update_alias(alias, success=success, reason=reason)
                except Exception as cb_exc:
                    logger.debug(
                        "Description backfill: journal_cb failed (non-fatal): %s",
                        cb_exc,
                    )

            runner = LifecycleBatchRunner(
                golden_repos_dir=self._golden_repos_dir,
                job_tracker=self._job_tracker,
                refresh_scheduler=self._refresh_scheduler,
                debouncer=self._lifecycle_debouncer,
                claude_cli_invoker=self._lifecycle_invoker,
                tracking_backend=self._tracking_backend,
                concurrency=self._get_lifecycle_concurrency(),
                journal_callback=_journal_cb,
            )
            runner.run(aliases, parent_job_id=job_id)
            logger.info(
                "Description backfill async: completed regeneration for %d aliases",
                len(aliases),
            )
        except DuplicateJobError as dup:
            logger.info(
                "Description backfill: duplicate job already active — skipping "
                "(existing_job_id=%s)",
                dup.existing_job_id,
            )
            return
        except Exception:
            logger.error(
                "Description backfill async: regeneration thread failed",
                exc_info=True,
            )
        finally:
            try:
                journal_svc.complete()
            except Exception as exc:
                logger.debug(
                    "Description backfill: journal complete failed (non-fatal): %s", exc
                )
            self._description_backfill_running.clear()

    _VALID_BACKFILL_NAMESPACES = frozenset({"lifecycle", "description"})

    def _init_backfill_journal(self, namespace: str):
        """Return a BackfillJournalService for *namespace* pointing to the shared NFS scratch dir.

        Journal path: {golden_repos_dir}/.scratch/{namespace}-backfill-journal/
        Mirrors the dep-map precedent: Bug #1041.

        Namespace must be one of {"lifecycle", "description"} — validated before path
        construction to prevent directory traversal or injection.

        When golden_repos_dir is None/invalid (e.g. early startup), falls back to a path
        under tempfile.gettempdir() so the caller never raises. The BackfillJournalService
        init() failure-swallow contract (Scenario 6) then handles any NFS-gone errors
        without aborting the backfill.
        """
        from code_indexer.server.services.backfill_journal_service import (
            BackfillJournalService,
        )
        import tempfile

        if namespace not in self._VALID_BACKFILL_NAMESPACES:
            raise ValueError(
                f"Invalid backfill namespace {namespace!r}. "
                f"Must be one of {sorted(self._VALID_BACKFILL_NAMESPACES)}"
            )

        try:
            grd = self._golden_repos_dir
            if grd is None:
                raise AttributeError("golden_repos_dir is None")
            journal_dir = Path(grd) / ".scratch" / f"{namespace}-backfill-journal"
        except (TypeError, AttributeError):
            # golden_repos_dir is None or non-path — degrade to a temp-dir fallback
            journal_dir = (
                Path(tempfile.gettempdir()) / f"cidx-{namespace}-backfill-journal"
            )
            logger.warning(
                "%s backfill: golden_repos_dir not set — journal will use fallback path %s",
                namespace.capitalize(),
                journal_dir,
            )

        return BackfillJournalService(namespace=namespace, journal_dir=journal_dir)

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

        Story #1062: uses _lifecycle_backfill_running (not the removed _backfill_in_progress)
        and drives BackfillJournalService for NFS-shared observability.

        Generates a UUID job_id, registers it with JobTracker (operation_type
        'lifecycle_backfill', username 'system'), then invokes
        LifecycleBatchRunner.run(aliases, parent_job_id=job_id) — which itself
        handles sub-batching, concurrency, per-alias locking, debouncer signal,
        and job_tracker.complete_job.

        All exceptions are logged and swallowed — a sweep failure must not crash
        the daemon thread or leak back into scheduler startup.
        """
        self._lifecycle_backfill_running.set()

        # Story #1062: per-namespace NFS journal under {golden_repos_dir}/.scratch/
        journal_svc = self._init_backfill_journal("lifecycle")
        try:
            journal_svc.start(total=len(aliases))
        except Exception as exc:
            logger.warning(
                "Lifecycle backfill: journal start failed (degraded): %s", exc
            )

        try:
            if self._job_tracker is None:
                logger.error(
                    "Lifecycle backfill async: job_tracker missing — "
                    "cannot route repair through LifecycleBatchRunner"
                )
                return

            import uuid

            job_id = str(uuid.uuid4())
            self._job_tracker.register_job_if_no_conflict(
                job_id=job_id,
                operation_type="lifecycle_backfill",
                username="system",
                repo_alias="server",
                metadata={"total": len(aliases), "source": "startup_backfill"},
            )

            def _journal_cb(alias: str, outcome: str) -> None:
                try:
                    success = not outcome.startswith("failed")
                    reason = (
                        outcome[len("failed: ") :]
                        if outcome.startswith("failed: ")
                        else None
                    )
                    journal_svc.update_alias(alias, success=success, reason=reason)
                except Exception as cb_exc:
                    logger.debug(
                        "Lifecycle backfill: journal_cb failed (non-fatal): %s", cb_exc
                    )

            runner = LifecycleBatchRunner(
                golden_repos_dir=self._golden_repos_dir,
                job_tracker=self._job_tracker,
                refresh_scheduler=self._refresh_scheduler,
                debouncer=self._lifecycle_debouncer,
                claude_cli_invoker=self._lifecycle_invoker,
                tracking_backend=self._tracking_backend,
                concurrency=self._get_lifecycle_concurrency(),
                journal_callback=_journal_cb,
            )
            runner.run(aliases, parent_job_id=job_id)
            logger.info(
                "Lifecycle backfill async: completed repair for %d aliases",
                len(aliases),
            )
        except DuplicateJobError as dup:
            logger.info(
                "Lifecycle backfill: duplicate job already active — skipping "
                "(existing_job_id=%s)",
                dup.existing_job_id,
            )
            return
        except Exception:
            logger.error(
                "Lifecycle backfill async: repair thread failed",
                exc_info=True,
            )
        finally:
            try:
                journal_svc.complete()
            except Exception as exc:
                logger.debug(
                    "Lifecycle backfill: journal complete failed (non-fatal): %s", exc
                )
            self._lifecycle_backfill_running.clear()

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

        if not metadata_path.exists():
            # Bug #1093 Fix B: golden repos use metadata-{provider}.json, not metadata.json.
            # Mirror the same fallback already used in has_changes_since_last_run (lines ~390-394).
            provider_files = sorted(
                (Path(repo_path) / ".code-indexer").glob("metadata-*.json")
            )
            if provider_files:
                metadata_path = provider_files[0]

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
            # Bug #1096 review fix: record the on-disk commit fingerprint at failure
            # time so the quarantine gate can detect a GENUINE commit transition,
            # independent of last_known_commit being NULL (which stays NULL forever
            # for repos that never succeed).
            self._failure_commit[repo_alias] = self._read_current_fingerprint(repo_path)
            # Bug #1096: increment circuit-breaker counter on each prompt failure.
            self._prompt_failure_counts[repo_alias] += 1
            if (
                self._prompt_failure_counts[repo_alias]
                == PROMPT_FAILURE_QUARANTINE_THRESHOLD
            ):
                logger.error(
                    "Repo %s has reached prompt failure quarantine threshold (%d consecutive "
                    "failures). Quarantining until a new commit is detected or a success occurs.",
                    repo_alias,
                    PROMPT_FAILURE_QUARANTINE_THRESHOLD,
                )

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
        # Story #1062: guard checks BOTH separate events — skip if EITHER backfill is running.
        if (
            self._lifecycle_backfill_running.is_set()
            or self._description_backfill_running.is_set()
        ):
            logger.debug("Description refresh pass skipped: backfill in progress")
            return

        import uuid

        stale_repos = self.get_stale_repos()

        for repo in stale_repos:
            alias = repo["repo_alias"]
            clone_path = repo["clone_path"]

            # Submit refresh job (if ClaudeCliManager available)
            if self._claude_cli_manager:
                logger.info(f"Submitting description refresh for {alias}")

                # Bug #984: check quarantine state FIRST, before dispatching any
                # refresh work through the LifecycleBatchRunner path, so that
                # already-quarantined repos are skipped without further processing.
                if (
                    self._prompt_failure_counts[alias]
                    >= PROMPT_FAILURE_QUARANTINE_THRESHOLD
                ):
                    # Bug #1096 review fix: auto-clear ONLY on a genuine commit
                    # TRANSITION — compare the CURRENT on-disk fingerprint against
                    # the fingerprint recorded at failure time (_failure_commit).
                    #
                    # Using has_changes_since_last_run here is wrong: it returns True
                    # when last_known_commit is None (the #1094 revert), but
                    # last_known_commit stays NULL forever for repos that never
                    # succeed.  That causes the auto-clear to fire every cycle,
                    # defeating quarantine for the worst case (persistently broken
                    # repos — the exact money-burn #1096 exists to stop).
                    #
                    # Invariant: quarantine HOLDS while the on-disk commit is
                    # unchanged; clears ONLY when the fingerprint genuinely changes.
                    current_fp = self._read_current_fingerprint(clone_path)
                    # Auto-clear ONLY when a failure fingerprint was actually
                    # recorded AND the on-disk commit has genuinely changed.
                    # If no failure fingerprint was recorded (key absent from
                    # _failure_commit), quarantine holds — conservative default.
                    failure_fp = self._failure_commit.get(alias)
                    if alias in self._failure_commit and current_fp != failure_fp:
                        logger.info(
                            "Repo %s was quarantined but commit has changed "
                            "(%r -> %r) — resetting failure counter and retrying",
                            alias,
                            failure_fp,
                            current_fp,
                        )
                        self._prompt_failure_counts[alias] = 0
                        self._failure_commit.pop(alias, None)
                        # Fall through to normal dispatch below
                    else:
                        logger.debug(
                            "Repo %s is quarantined (failure count %d >= %d, "
                            "commit unchanged: %r), skipping",
                            alias,
                            self._prompt_failure_counts[alias],
                            PROMPT_FAILURE_QUARANTINE_THRESHOLD,
                            current_fp,
                        )
                        continue

                if not self.has_changes_since_last_run(clone_path, repo):
                    now = datetime.now(timezone.utc).isoformat()
                    self._tracking_backend.upsert_tracking(
                        repo_alias=alias,
                        next_run=self.calculate_next_run(alias),
                        updated_at=now,
                    )
                    continue

                # Lightweight existence gate: skip repos that have never been
                # analyzed (no .md yet) without spending a Claude invocation.
                # The refresh itself (refine-existing vs create-from-scratch) is
                # decided downstream by LifecycleBatchRunner._process_one_repo.
                if not self._has_existing_description(alias):
                    # No description yet — repo hasn't been analyzed.  Skip without
                    # incrementing the failure counter (not a prompt-generation failure).
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
            concurrency=self._get_lifecycle_concurrency(),
        )
        runner.run([alias], parent_job_id=job_id)

    def _get_interval_hours(self) -> int:
        """Get refresh interval from config."""
        config = self._config_manager.load_config()
        if not config or not config.claude_integration_config:
            return 24  # Default

        return int(config.claude_integration_config.description_refresh_interval_hours)

    def _get_lifecycle_concurrency(self) -> int:
        """Read max_concurrent_claude_cli from config for LifecycleBatchRunner."""
        config_manager = getattr(self, "_config_manager", None)
        config = config_manager.load_config() if config_manager else None
        if config and config.claude_integration_config:
            return max(1, config.claude_integration_config.max_concurrent_claude_cli)  # type: ignore[no-any-return]
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        return ClaudeIntegrationConfig().max_concurrent_claude_cli  # type: ignore[no-any-return]

    def _has_existing_description(self, alias: str) -> bool:
        """
        Lightweight check: does a non-empty .md file exist for *alias* in _meta_dir?

        Used by _run_loop_single_pass to skip repos that have not been analyzed
        yet (no description generated), without any Claude invocation.

        Returns:
            True if _meta_dir is set and {alias}.md exists and is non-empty.
            False in all other cases (no meta_dir, file absent, file empty/whitespace).
        """
        if not self._meta_dir:
            return False
        md_file = self._meta_dir / f"{alias}.md"
        if not md_file.exists():
            return False
        return bool(md_file.read_text(encoding="utf-8", errors="replace").strip())

    def _update_description_file(self, repo_alias: str, content: str) -> None:
        """DEPRECATED: Use atomic_write_description() or write_meta_md() instead."""
        raise NotImplementedError(
            "_update_description_file is deprecated. "
            "Use atomic_write_description() from meta_description_hook or "
            "write_meta_md() from lifecycle_batch_runner instead."
        )

    def close(self) -> None:
        """Clean up resources."""
        self.stop()
        self._tracking_backend.close()
        self._golden_backend.close()
