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
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Constants
CIDX_REINDEX_TIMEOUT_SECONDS = 120
SCHEDULER_POLL_INTERVAL_SECONDS = 60  # Story #193: Delta refresh polling interval
THREAD_JOIN_TIMEOUT_SECONDS = 5.0  # Story #193: Daemon thread join timeout


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
    ):
        """
        Initialize dependency map service.

        Args:
            golden_repos_manager: GoldenRepoManager instance
            config_manager: ServerConfigManager instance
            tracking_backend: DependencyMapTrackingBackend instance
            analyzer: DependencyMapAnalyzer instance
        """
        self._golden_repos_manager = golden_repos_manager
        self._config_manager = config_manager
        self._tracking_backend = tracking_backend
        self._analyzer = analyzer
        self._lock = threading.Lock()

        # Story #193: Scheduler daemon thread state
        self._daemon_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

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

    def run_full_analysis(self) -> Dict[str, Any]:
        """
        Orchestrate full dependency map analysis pipeline.

        Returns:
            Dict with status, domains_count, repos_analyzed, errors

        Raises:
            RuntimeError: If analysis is already in progress
        """
        # Non-blocking lock acquire (AC7: Concurrency Protection)
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Dependency map analysis already in progress")

        try:
            # Setup and validation
            setup_result = self._setup_analysis()
            if setup_result.get("early_return"):
                return setup_result

            config, paths, repo_list = (
                setup_result["config"],
                setup_result["paths"],
                setup_result["repo_list"],
            )

            # Update tracking to running
            self._tracking_backend.update_tracking(
                status="running", last_run=datetime.now(timezone.utc).isoformat()
            )

            # Execute analysis passes
            domain_list, errors = self._execute_analysis_passes(
                config, paths, repo_list
            )

            # Finalize and cleanup
            self._finalize_analysis(config, paths, repo_list, domain_list)

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

            self._lock.release()

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
        cidx_meta_path = Path(golden_repos_root) / "cidx-meta"
        staging_dir = cidx_meta_path / "dependency-map.staging"
        final_dir = cidx_meta_path / "dependency-map"

        paths = {
            "golden_repos_root": Path(golden_repos_root),
            "cidx_meta_path": cidx_meta_path,
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
        self, config, paths: Dict[str, Path], repo_list: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """
        Execute the three-pass analysis pipeline with journal-based resumability.

        Args:
            config: Claude integration config
            paths: Dict with staging_dir, final_dir, cidx_meta_path, golden_repos_root
            repo_list: List of repository metadata

        Returns:
            Tuple of (domain_list, errors)
        """
        staging_dir = paths["staging_dir"]
        final_dir = paths["final_dir"]
        cidx_meta_path = paths["cidx_meta_path"]

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
                    r["alias"]: {"file_count": r.get("file_count", 0), "total_bytes": r.get("total_bytes", 0)}
                    for r in repo_list
                },
                "pass1": {"status": "pending"},
                "pass2": {},
                "pass3": {"status": "pending"},
            }
            # Save journal immediately to prevent loss if crash occurs before Pass 1
            self._save_journal(staging_dir, journal)

        # Generate CLAUDE.md (AC2: CLAUDE.md Orientation File)
        self._analyzer.generate_claude_md(repo_list)

        # Pass 1: Synthesis (skip if already completed)
        if journal.get("pass1", {}).get("status") != "completed":
            # Read repo descriptions from cidx-meta (Fix 8: filter stale repos)
            active_aliases = {r.get("alias") for r in repo_list}
            repo_descriptions = self._read_repo_descriptions(cidx_meta_path, active_aliases=active_aliases)

            domain_list = self._analyzer.run_pass_1_synthesis(
                staging_dir=staging_dir,
                repo_descriptions=repo_descriptions,
                repo_list=repo_list,
                max_turns=config.dependency_map_pass1_max_turns,
            )
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
        else:
            # Load domain_list from _domains.json with boundary check
            domains_file = staging_dir / "_domains.json"
            if not domains_file.exists():
                raise FileNotFoundError(
                    f"Cannot resume: {domains_file} not found despite pass1 completed"
                )
            domain_list = json.loads(domains_file.read_text())
            logger.info(f"Pass 1 already completed ({journal['pass1']['domains_count']} domains), skipping")

        # Pass 2: Per-domain (skip completed domains)
        errors = []
        for domain in domain_list:
            domain_name = domain["name"]
            domain_status = journal.get("pass2", {}).get(domain_name, {}).get("status")

            if domain_status == "completed":
                logger.info(f"Pass 2 already completed for '{domain_name}', skipping")
                continue

            try:
                self._analyzer.run_pass_2_per_domain(
                    staging_dir=staging_dir,
                    domain=domain,
                    domain_list=domain_list,
                    repo_list=repo_list,
                    max_turns=config.dependency_map_pass2_max_turns,
                    previous_domain_dir=final_dir if final_dir.exists() else None,
                )
                # Read output size
                domain_file = staging_dir / f"{domain_name}.md"
                chars = len(domain_file.read_text()) if domain_file.exists() else 0
                journal["pass2"][domain_name] = {
                    "status": "completed",
                    "chars": chars,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as e:
                errors.append(f"Domain '{domain_name}': {e}")
                logger.warning(f"Pass 2 failed for domain '{domain_name}': {e}")
                journal["pass2"][domain_name] = {"status": "failed", "error": str(e)}

            self._save_journal(staging_dir, journal)  # Save after each domain

        # Pass 3: Index generation (skip if already completed)
        if journal.get("pass3", {}).get("status") != "completed":
            self._analyzer.run_pass_3_index(
                staging_dir=staging_dir,
                domain_list=domain_list,
                repo_list=repo_list,
                max_turns=config.dependency_map_pass3_max_turns,
            )
            journal["pass3"] = {
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_journal(staging_dir, journal)
        else:
            logger.info("Pass 3 already completed, skipping")

        return domain_list, errors

    def _finalize_analysis(
        self,
        config,
        paths: Dict[str, Path],
        repo_list: List[Dict[str, Any]],
        domain_list: List[Dict[str, Any]],
    ) -> None:
        """
        Finalize analysis: swap, reindex, update tracking, cleanup.

        Args:
            config: Claude integration config
            paths: Dict with staging_dir, final_dir, cidx_meta_path, golden_repos_root
            repo_list: List of repository metadata
            domain_list: List of identified domains
        """
        staging_dir = paths["staging_dir"]
        final_dir = paths["final_dir"]
        cidx_meta_path = paths["cidx_meta_path"]
        golden_repos_root = paths["golden_repos_root"]

        # Stage-then-swap (AC4: Stage-then-Swap Atomic Writes)
        try:
            self._stage_then_swap(staging_dir, final_dir)
        except Exception as e:
            raise RuntimeError(
                f"Stage-then-swap failed: {e} -- previous dependency map preserved"
            ) from e

        # Re-index cidx-meta (AC5: cidx-meta Re-indexing)
        self._reindex_cidx_meta(cidx_meta_path)

        # Update tracking (AC6: Configuration and Tracking)
        commit_hashes = self._get_commit_hashes(repo_list)
        next_run = (
            datetime.now(timezone.utc) + timedelta(hours=config.dependency_map_interval_hours)
        ).isoformat()
        self._tracking_backend.update_tracking(
            status="completed",
            commit_hashes=json.dumps(commit_hashes),
            next_run=next_run,
            error_message=None,
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

    def _reindex_cidx_meta(self, cidx_meta_path: Path) -> None:
        """
        Re-index cidx-meta using cidx CLI.

        Args:
            cidx_meta_path: Path to cidx-meta directory
        """
        try:
            subprocess.run(
                ["cidx", "index"],
                cwd=str(cidx_meta_path),
                capture_output=True,
                text=True,
                timeout=CIDX_REINDEX_TIMEOUT_SECONDS,
            )
            logger.info("Re-indexed cidx-meta after dependency map update")
        except Exception as e:
            logger.warning(f"cidx index re-indexing failed: {e}")

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

    def _get_activated_repos(self) -> List[Dict[str, Any]]:
        """
        Get list of activated golden repos with metadata.

        Returns:
            List of dicts with alias, clone_path, description_summary
        """
        repos = self._golden_repos_manager.list_golden_repos()

        result = []
        for repo in repos:
            alias = repo.get("alias")
            clone_path = repo.get("clone_path")

            # Skip if missing required fields
            if not alias or not clone_path:
                continue

            # Extract description summary (first line of description)
            description_summary = "No description"
            cidx_meta_path = (
                Path(self._golden_repos_manager.golden_repos_dir) / "cidx-meta"
            )
            md_file = cidx_meta_path / f"{alias}.md"
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
                        if not in_frontmatter and line.strip() and not line.strip().startswith('#'):
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
                return json.loads(journal_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Corrupted journal at {journal_path}, starting fresh: {e}")
                return None
        return None

    def _save_journal(self, staging_dir: Path, journal: Dict) -> None:
        """Atomically write journal to staging_dir."""
        journal_path = staging_dir / "_journal.json"
        tmp_path = journal_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(journal, indent=2))
        tmp_path.rename(journal_path)

    def _should_resume(self, staging_dir: Path, repo_list: List[Dict[str, Any]]) -> Optional[Dict]:
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
        journal_sizes = {k: v.get("total_bytes", 0) for k, v in journal.get("repo_sizes", {}).items()}

        if set(current_sizes.keys()) != set(journal_sizes.keys()):
            logger.info("Journal found but repo set changed — starting fresh")
            return None

        # Check if any repo size changed significantly (>5% difference)
        for alias, current_bytes in current_sizes.items():
            journal_bytes = journal_sizes.get(alias, 0)
            # If either was zero and the other isn't, that's a significant change
            if (journal_bytes == 0) != (current_bytes == 0):
                logger.info(f"Journal found but {alias} size changed from {journal_bytes} to {current_bytes} — starting fresh")
                return None
            if journal_bytes > 0 and abs(current_bytes - journal_bytes) / journal_bytes > 0.05:
                logger.info(f"Journal found but {alias} size changed — starting fresh")
                return None

        logger.info(f"Resuming from journal: pass1={journal.get('pass1', {}).get('status')}")
        return journal

    def _enrich_repo_sizes(self, repo_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Add file_count and total_bytes to each repo dict. Sort by total_bytes descending.

        Args:
            repo_list: List of repo dicts with clone_path

        Returns:
            Enriched and sorted repo list
        """
        for repo in repo_list:
            clone_path = Path(repo.get("clone_path", ""))
            if clone_path.exists():
                file_count = 0
                total_bytes = 0
                for f in clone_path.rglob("*"):
                    # Exclude .git and .code-indexer directories
                    if f.is_file() and ".git" not in f.parts and ".code-indexer" not in f.parts:
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

        # Sort descending by total_bytes
        repo_list.sort(key=lambda r: r.get("total_bytes", 0), reverse=True)
        return repo_list

    def _get_commit_hashes(self, repo_list: List[Dict[str, Any]]) -> Dict[str, str]:
        """
        Read metadata.json for each repo to get current_commit.

        Args:
            repo_list: List of repo dicts with clone_path

        Returns:
            Dict mapping repo alias to commit hash
        """
        commit_hashes = {}
        for repo in repo_list:
            alias = repo.get("alias")
            clone_path = repo.get("clone_path")

            if not alias or not clone_path:
                continue

            metadata_path = Path(clone_path) / ".code-indexer" / "metadata.json"
            if metadata_path.exists():
                try:
                    with open(metadata_path) as f:
                        metadata = json.load(f)
                    current_commit = metadata.get("current_commit", "unknown")
                    commit_hashes[alias] = current_commit
                except Exception as e:
                    logger.warning(f"Failed to read metadata for {alias}: {e}")
                    commit_hashes[alias] = "unknown"
            else:
                commit_hashes[alias] = "local"

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
        self._daemon_thread = threading.Thread(
            target=self._scheduler_loop, daemon=True
        )
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

    def _scheduler_loop(self) -> None:
        """
        Main scheduler loop for delta refresh (Story #193, AC1).

        Polls every 60 seconds, checks if delta refresh should run based
        on next_run timestamp and dependency_map_enabled config.
        """
        while not self._stop_event.is_set():
            try:
                # Check if enabled (config may change at runtime - AC6)
                config = self._config_manager.get_claude_integration_config()
                if not config or not config.dependency_map_enabled:
                    logger.debug("Dependency map disabled, skipping scheduled delta refresh")
                    self._stop_event.wait(SCHEDULER_POLL_INTERVAL_SECONDS)
                    continue

                # Check if next_run is reached
                tracking = self._tracking_backend.get_tracking()
                next_run_str = tracking.get("next_run")

                if next_run_str:
                    next_run = datetime.fromisoformat(next_run_str)
                    now = datetime.now(timezone.utc)

                    if now >= next_run:
                        logger.info("Scheduled delta refresh triggered")
                        self.run_delta_analysis()

            except Exception as e:
                logger.error(f"Error in dependency map scheduler loop: {e}", exc_info=True)

            # Sleep 60 seconds between checks
            self._stop_event.wait(SCHEDULER_POLL_INTERVAL_SECONDS)

    def detect_changes(
        self,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """
        Detect changed, new, and removed repos via commit hash comparison (Story #193, AC2).

        Compares stored commit hashes from tracking table with current repo commits
        in metadata.json files.

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
                logger.warning("Failed to parse stored commit hashes, treating as empty")
                stored_hashes = {}

        # Get current repos
        current_repos = self._get_activated_repos()

        changed_repos = []
        new_repos = []

        # Check each current repo
        for repo in current_repos:
            alias = repo.get("alias")
            clone_path = repo.get("clone_path")

            if not alias or not clone_path:
                continue

            # Read current commit hash from metadata.json
            metadata_path = Path(clone_path) / ".code-indexer" / "metadata.json"
            current_hash = None

            if metadata_path.exists():
                try:
                    with open(metadata_path) as f:
                        metadata = json.load(f)
                    current_hash = metadata.get("current_commit")
                except Exception as e:
                    logger.warning(f"Failed to read metadata for {alias}: {e}")

            # Compare with stored hash
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
        cidx_meta_path = Path(self._golden_repos_manager.golden_repos_dir) / "cidx-meta"
        index_file = cidx_meta_path / "dependency-map" / "_index.md"

        if not index_file.exists():
            logger.warning("_index.md not found, cannot identify affected domains")
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

        repo_to_domains = {}

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
            return f"---\ndomain: {domain_name}\nlast_analyzed: {now}\n---\n\n{new_body}"

    def _update_domain_file(
        self,
        domain_name: str,
        domain_file: Path,
        changed_repos: List[str],
        new_repos: List[str],
        removed_repos: List[str],
        domain_list: List[str],
        config,
    ) -> None:
        """
        Update a single domain file with delta analysis (Story #193, AC5).

        Args:
            domain_name: Name of the domain
            domain_file: Path to domain .md file
            changed_repos: List of changed repo aliases
            new_repos: List of new repo aliases
            removed_repos: List of removed repo aliases
            domain_list: Full list of all domain names
            config: Claude integration config

        Raises:
            Exception: If Claude CLI invocation or file write fails
        """
        # Read existing content
        existing_content = domain_file.read_text()

        # Build delta merge prompt
        merge_prompt = self._analyzer.build_delta_merge_prompt(
            domain_name=domain_name,
            existing_content=existing_content,
            changed_repos=changed_repos,
            new_repos=new_repos,
            removed_repos=removed_repos,
            domain_list=domain_list,
        )

        # Invoke Claude CLI via public method (Code Review H1: proper encapsulation)
        result = self._analyzer.invoke_delta_merge(
            prompt=merge_prompt,
            timeout=config.dependency_map_pass_timeout_seconds,
            max_turns=config.dependency_map_delta_max_turns,
        )

        # Update frontmatter timestamp and write in-place
        updated_content = self._update_frontmatter_timestamp(
            existing_content, result, domain_name
        )

        domain_file.write_text(updated_content)
        logger.info(f"Updated domain file in-place: {domain_file}")

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
        errors = []
        changed_aliases = [r["alias"] for r in changed_repos]
        new_aliases = [r["alias"] for r in new_repos]

        # Build full domain list from ALL domain files (Code Review H2: cross-domain awareness)
        # Claude needs the complete domain landscape, not just affected domains
        domain_list = [
            f.stem for f in dependency_map_dir.glob("*.md")
            if not f.name.startswith("_")
        ]

        # Code Review M4: Sort for deterministic processing order
        for domain_name in sorted(affected_domains):
            domain_file = dependency_map_dir / f"{domain_name}.md"

            if not domain_file.exists():
                logger.warning(f"Domain file not found: {domain_file}, skipping")
                continue

            try:
                self._update_domain_file(
                    domain_name=domain_name,
                    domain_file=domain_file,
                    changed_repos=changed_aliases,
                    new_repos=new_aliases,
                    removed_repos=removed_repos,
                    domain_list=domain_list,
                    config=config,
                )
            except Exception as e:
                errors.append(f"Domain '{domain_name}': {e}")
                logger.warning(
                    f"Delta analysis failed for domain '{domain_name}': {e}"
                )

        return errors

    def _finalize_delta_tracking(
        self, config, all_repos: List[Dict[str, Any]]
    ) -> None:
        """
        Finalize delta analysis tracking updates (Story #193, AC8).

        Args:
            config: Claude integration config
            all_repos: List of all current repos
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

    def run_delta_analysis(self) -> Optional[Dict[str, Any]]:
        """
        Run delta analysis to refresh only affected domains (Story #193, AC1-8).

        Returns:
            Dict with status or None if skipped (lock held or disabled)
        """
        # Non-blocking lock acquire (AC7: Concurrency Protection)
        if not self._lock.acquire(blocking=False):
            logger.info("Delta analysis skipped - analysis already in progress")
            return None

        try:
            # Check if enabled (AC6: Runtime Configuration Check)
            config = self._config_manager.get_claude_integration_config()
            if not config or not config.dependency_map_enabled:
                logger.debug("Delta analysis skipped - dependency_map_enabled is False")
                return None

            # Detect changes (AC2: Change Detection)
            changed_repos, new_repos, removed_repos = self.detect_changes()

            # Skip if no changes
            if not changed_repos and not new_repos and not removed_repos:
                logger.info("No changes detected, skipping delta analysis")
                next_run = (
                    datetime.now(timezone.utc)
                    + timedelta(hours=config.dependency_map_interval_hours)
                ).isoformat()
                self._tracking_backend.update_tracking(next_run=next_run)
                return {"status": "skipped", "message": "No changes detected"}

            # Update tracking to running
            self._tracking_backend.update_tracking(
                status="running", last_run=datetime.now(timezone.utc).isoformat()
            )

            # Get paths
            golden_repos_root = Path(self._golden_repos_manager.golden_repos_dir)
            cidx_meta_path = golden_repos_root / "cidx-meta"
            dependency_map_dir = cidx_meta_path / "dependency-map"

            # Identify affected domains (AC3/4)
            affected_domains = self.identify_affected_domains(
                changed_repos, new_repos, removed_repos
            )

            if not affected_domains:
                logger.info("No affected domains identified")
                all_repos = self._get_activated_repos()
                self._finalize_delta_tracking(config, all_repos)
                return {"status": "completed", "affected_domains": 0}

            # Generate CLAUDE.md
            all_repos = self._get_activated_repos()
            self._analyzer.generate_claude_md(all_repos)

            # Handle new repo domain discovery
            # Code Review M2: Known limitation - new repos not in _index.md are skipped
            # The build_domain_discovery_prompt() and build_new_domain_prompt() methods
            # exist but the full discovery flow is not yet implemented.
            # WORKAROUND: Users should trigger a full dependency map re-analysis to
            # incorporate new repos into the domain structure.
            if "__NEW_REPO_DISCOVERY__" in affected_domains:
                affected_domains.remove("__NEW_REPO_DISCOVERY__")
                logger.warning(
                    "New repo domain discovery not yet implemented - "
                    "new repos not in _index.md are skipped. "
                    "Trigger a full re-analysis to incorporate new repos."
                )

            # Update affected domains (AC5: In-Place Updates)
            errors = self._update_affected_domains(
                affected_domains,
                dependency_map_dir,
                changed_repos,
                new_repos,
                removed_repos,
                config,
            )

            # Re-index cidx-meta
            self._reindex_cidx_meta(cidx_meta_path)

            # Finalize tracking (AC8)
            self._finalize_delta_tracking(config, all_repos)

            logger.info(f"Delta analysis completed: {len(affected_domains)} domains updated")

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
