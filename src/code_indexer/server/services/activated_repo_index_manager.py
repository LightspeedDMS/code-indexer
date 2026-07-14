"""
Activated Repository Index Manager for CIDX Server.

Manages manual re-indexing operations for activated repositories,
supporting semantic, FTS, temporal, and SCIP indexes.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.logging_utils import format_error_log, get_log_extra

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable

from ..repositories.background_jobs import BackgroundJobManager
from ..repositories.activated_repo_manager import ActivatedRepoManager
from .config_service import get_config_service
from code_indexer.utils.subprocess_env import build_cidx_subprocess_env
from ..utils.cancellable_subprocess import (
    SHORT_POLL_SECONDS,
    SubprocessCancelledError,
    run_cancellable_subprocess,
)


class IndexingError(Exception):
    """Exception raised when indexing operations fail."""

    pass


logger = logging.getLogger(__name__)


class ActivatedRepoIndexManager:
    """
    Manages manual re-indexing operations for activated repositories.

    Provides on-demand index updates for semantic, FTS, temporal, and SCIP
    indexes after file modifications or for maintenance purposes.
    """

    # Valid index types
    VALID_INDEX_TYPES = ["semantic", "fts", "temporal", "scip"]

    # Status detection constants
    STALE_THRESHOLD_DAYS = 7  # Temporal index stale after 7 days
    BYTES_PER_MB = 1024 * 1024  # Bytes to megabytes conversion

    # Concurrent job prevention
    MAX_JOBS_TO_CHECK = 100  # Maximum jobs to check for concurrency conflicts

    def __init__(
        self,
        data_dir: Optional[str] = None,
        background_job_manager: Optional[BackgroundJobManager] = None,
        activated_repo_manager: Optional[ActivatedRepoManager] = None,
    ):
        """
        Initialize activated repository index manager.

        Args:
            data_dir: Data directory path (defaults to CIDX_SERVER_DATA_DIR/data if
                the env var is set, otherwise ~/.cidx-server/data)
            background_job_manager: Background job manager instance
            activated_repo_manager: Activated repository manager instance
        """
        if data_dir:
            self.data_dir = data_dir
        else:
            env_server_dir = os.environ.get("CIDX_SERVER_DATA_DIR")
            if env_server_dir:
                self.data_dir = str(Path(env_server_dir) / "data")
            else:
                self.data_dir = str(Path.home() / ".cidx-server" / "data")

        self.logger = logging.getLogger(__name__)

        # Set dependencies
        self.background_job_manager = background_job_manager or BackgroundJobManager()
        self.activated_repo_manager = activated_repo_manager or ActivatedRepoManager(
            self.data_dir
        )

        # Story #3 Phase 2 AC9-AC11: Load SCIP settings from ConfigService
        self._load_scip_config()

    def _load_scip_config(self) -> None:
        """
        Load SCIP configuration from ConfigService (Story #3 Phase 2 AC11).

        Updates instance-level threshold attributes from ConfigService.
        Falls back to class-level defaults if ConfigService is unavailable.

        Note (Bug #1218): indexing_timeout_seconds and scip_generation_timeout_seconds
        have been removed — no whole-job timeout is applied on the indexing path.
        """
        try:
            config_service = get_config_service()
            config = config_service.get_config()
            if config.scip_config is not None:
                self.STALE_THRESHOLD_DAYS = (
                    config.scip_config.temporal_stale_threshold_days
                )
        except Exception as e:
            logger.warning(
                format_error_log(
                    "SVC-MIGRATE-001",
                    f"Could not read SCIP config, using defaults: {e}",
                ),
                extra=get_log_extra("SVC-MIGRATE-001"),
            )

    def _compute_allowed_repo_roots(self) -> List[Path]:
        """
        Compute the set of allowed resolved root paths for repository confinement.

        On a local backend, data_dir/activated-repos and data_dir/golden-repos are
        real subdirectories, so data_dir.resolve() already covers them.

        On a cow-daemon backend (Bug #1052), data_dir/activated-repos and
        data_dir/golden-repos are SYMLINKS to the cow-daemon mount. Including the
        resolved symlink targets as extra allowed roots lets paths that resolve via
        the symlink pass the confinement check (Bug #1246), while a genuine traversal
        path (e.g. ../../etc) is under none of the roots and is still rejected.

        Returns:
            List of resolved Path objects that are valid containment roots.
        """
        allowed: List[Path] = [Path(self.data_dir).resolve()]
        for sub in ("activated-repos", "golden-repos"):
            p = Path(self.data_dir) / sub
            if p.exists():
                resolved = p.resolve()
                if resolved not in allowed:
                    allowed.append(resolved)
        return allowed

    @staticmethod
    def _path_is_within_any(repo_path: Path, roots: List[Path]) -> bool:
        """
        Return True if repo_path is a sub-path of at least one root in roots.

        Uses Path.relative_to() per root; immune to prefix-string attacks (e.g.
        /data-extra would not match /data).

        Args:
            repo_path: Fully resolved absolute path to test.
            roots: Sequence of resolved absolute paths that are allowed parents.

        Returns:
            True if repo_path is under any root; False otherwise.
        """
        for root in roots:
            try:
                repo_path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def trigger_reindex(
        self,
        repo_alias: str,
        index_types: List[str],
        clear: bool,
        username: str,
    ) -> str:
        """
        Trigger manual re-indexing job for activated repository.

        Args:
            repo_alias: Repository alias to reindex
            index_types: List of index types to rebuild (semantic, fts, temporal, scip)
            clear: If True, rebuild from scratch; if False, incremental update
            username: Username requesting reindex

        Returns:
            Job ID for tracking progress

        Raises:
            ValueError: If index_types contains invalid types or is empty
            FileNotFoundError: If repository not found
        """
        # Validate index_types
        if not index_types:
            raise ValueError("At least one index type required")

        invalid_types = [t for t in index_types if t not in self.VALID_INDEX_TYPES]
        if invalid_types:
            raise ValueError(
                f"Invalid index type(s): {', '.join(invalid_types)}. "
                f"Valid types: {', '.join(self.VALID_INDEX_TYPES)}"
            )

        # Validate repository exists
        try:
            repo_path = self.activated_repo_manager.get_activated_repo_path(
                username, repo_alias
            )
        except Exception as e:
            if "not found" in str(e).lower():
                raise FileNotFoundError(
                    f"Repository '{repo_alias}' not found for user '{username}'"
                )
            raise

        # Security: Validate path doesn't escape managed storage roots.
        # On cow-daemon backends, data_dir/activated-repos and data_dir/golden-repos
        # are SYMLINKS to the cow-daemon mount (Bug #1052). Path.resolve() follows
        # symlinks, so a repo path under data_dir/activated-repos resolves OUTSIDE
        # data_dir itself. We must accept paths under any resolved allowed root,
        # not just data_dir (Bug #1246).
        repo_path_obj = Path(repo_path).resolve()
        if not self._path_is_within_any(
            repo_path_obj, self._compute_allowed_repo_roots()
        ):
            raise ValueError(
                "Security violation: Repository path escapes data directory"
            )

        if not os.path.exists(repo_path):
            raise FileNotFoundError(f"Repository directory not found: {repo_path}")

        # Check for concurrent reindex jobs to prevent resource conflicts
        # Note: BackgroundJobManager doesn't store job parameters, so we check per-user, not per-repo
        running_jobs = self.background_job_manager.list_jobs(
            username=username,
            status_filter="running",
            limit=self.MAX_JOBS_TO_CHECK,
        )
        pending_jobs = self.background_job_manager.list_jobs(
            username=username,
            status_filter="pending",
            limit=self.MAX_JOBS_TO_CHECK,
        )

        # Defensive dict access
        running_jobs_list = running_jobs.get("jobs", [])
        pending_jobs_list = pending_jobs.get("jobs", [])
        all_active_jobs = running_jobs_list + pending_jobs_list

        # Check if any active job is a reindex operation
        for job in all_active_jobs:
            if job.get("operation_type") == "reindex":
                raise ValueError(
                    f"Another reindex job is already running/pending (job {job.get('job_id')}). "
                    f"Please wait for it to complete before starting a new reindex."
                )

        # Submit background job
        # BackgroundJobManager accepts *args/**kwargs despite signature showing Callable[[], Dict[str, Any]]
        # The implementation uses inspect.signature() to detect and inject progress_callback parameter.
        # Bug #1154: worker params must be passed as positional *args so they reach func(*args, ...).
        # Passing them as **kwargs would have repo_alias= consumed by submit_job's own tracking kwarg.
        # Positional order matches _execute_indexing_job(repo_alias, repo_path, index_types, clear).
        job_id = self.background_job_manager.submit_job(
            "reindex",
            self._execute_indexing_job,  # type: ignore[arg-type]
            repo_alias,  # *args[0] -> worker's positional repo_alias
            repo_path,  # *args[1] -> worker's positional repo_path
            index_types,  # *args[2] -> worker's positional index_types
            clear,  # *args[3] -> worker's positional clear
            submitter_username=username,
            repo_alias=repo_alias,  # submit_job tracking kwarg (keyword-only, no conflict with positional)
        )

        self.logger.info(
            f"Reindex job {job_id} submitted for repository '{repo_alias}' "
            f"(types: {index_types}, clear: {clear})",
            extra={"correlation_id": get_correlation_id()},
        )

        return job_id

    def get_index_status(
        self,
        repo_alias: str,
        username: str,
    ) -> Dict[str, Any]:
        """
        Get indexing status for all index types.

        Args:
            repo_alias: Repository alias
            username: Username requesting status

        Returns:
            Dictionary with status for each index type

        Raises:
            FileNotFoundError: If repository not found
        """
        # Get repository path
        try:
            repo_path_str = self.activated_repo_manager.get_activated_repo_path(
                username, repo_alias
            )
        except Exception as e:
            if "not found" in str(e).lower():
                raise FileNotFoundError(
                    f"Repository '{repo_alias}' not found for user '{username}'"
                )
            raise

        repo_path = Path(repo_path_str)

        if not repo_path.exists():
            raise FileNotFoundError(f"Repository directory not found: {repo_path}")

        # Get status for each index type
        status = {
            "semantic": self._get_semantic_status(repo_path),
            "fts": self._get_fts_status(repo_path),
            "temporal": self._get_temporal_status(repo_path),
            "scip": self._get_scip_status(repo_path),
        }

        return status

    def _execute_indexing_job(
        self,
        repo_alias: str,
        repo_path: str,
        index_types: List[str],
        clear: bool,
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> Dict[str, Any]:
        """
        Execute indexing job for specified index types.

        Story #482 PATH E: Uses ProgressPhaseAllocator for dynamic phase-aware
        progress reporting instead of hardcoded 10/50/90 milestones.

        Args:
            repo_alias: Repository alias
            repo_path: Repository filesystem path
            index_types: List of index types to rebuild
            clear: Rebuild from scratch vs incremental
            progress_callback: Optional progress callback(pct, phase=..., detail=...)

        Returns:
            Result dictionary with success status and details
        """
        from code_indexer.services.progress_subprocess_runner import (
            gather_repo_metrics,
        )
        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )

        # Build allocator for phase-aware progress (Story #482 PATH E)
        file_count, commit_count = gather_repo_metrics(repo_path)
        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=index_types,
            file_count=file_count,
            commit_count=commit_count,
        )

        def update_progress(
            percent: int, message: str = "", phase: Optional[str] = None
        ) -> None:
            """Helper to update progress with logging and phase info."""
            if progress_callback:
                progress_callback(percent, phase=phase, detail=message)
            if message:
                self.logger.info(
                    f"Reindex progress ({percent}%): {message}",
                    extra={"correlation_id": get_correlation_id()},
                )

        try:
            # Report start of first phase using allocator (replaces hardcoded 10%)
            first_phase = index_types[0] if index_types else "semantic"
            start_pct = int(allocator.phase_start(first_phase))
            update_progress(
                start_pct,
                f"Starting reindex for '{repo_alias}' (types: {index_types}, clear: {clear})",
                phase=first_phase,
            )

            # Execute each index type and collect results
            # Pass a phase-aware update_progress wrapper
            def phase_update(percent: int, message: str = "") -> None:
                update_progress(percent, message)

            results = self._execute_all_index_types(
                repo_path, index_types, clear, phase_update, allocator
            )

            # Determine overall success
            all_success = all(r.get("success", False) for r in results.values())
            failed_types = [
                t for t, r in results.items() if not r.get("success", False)
            ]

            if all_success:
                message = f"Successfully reindexed all types: {', '.join(index_types)}"
            else:
                message = f"Reindex completed with failures: {', '.join(failed_types)}"

            update_progress(100, message, phase="complete")

            return {
                "success": all_success,
                "message": message,
                "results": results,
                "failed_types": failed_types if failed_types else None,
            }

        except Exception as e:
            error_msg = f"Failed to execute reindex job for '{repo_alias}': {str(e)}"
            self.logger.error(
                format_error_log("SVC-MIGRATE-009", error_msg),
                extra=get_log_extra("SVC-MIGRATE-009"),
            )

            if progress_callback:
                progress_callback(0, phase="error", detail=error_msg)

            return {
                "success": False,
                "message": error_msg,
                "results": {},
                "error": str(e),
            }

    def _execute_all_index_types(
        self,
        repo_path: str,
        index_types: List[str],
        clear: bool,
        update_progress: Callable,
        allocator=None,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Execute indexing for all requested index types.

        Story #482 PATH E: Uses ProgressPhaseAllocator when provided to compute
        phase-aware start/end progress values instead of hardcoded arithmetic.
        Falls back to evenly-spaced values only when allocator is None.

        Args:
            repo_path: Repository path
            index_types: List of index types to process
            clear: Clear flag
            update_progress: Progress callback function
            allocator: Optional ProgressPhaseAllocator for phase-aware progress.
                When provided, phase_start/phase_end values are used instead of
                the old hardcoded `10 + int((idx / total_types) * 80)` formula.

        Returns:
            Dictionary mapping index type to result
        """
        results = {}
        total_types = len(index_types)

        for idx, index_type in enumerate(index_types):
            # Use allocator if available; fall back to evenly-spaced arithmetic only
            # when allocator is absent (legacy callers without phase weights).
            if allocator is not None:
                try:
                    base_progress = int(allocator.phase_start(index_type))
                    next_progress = int(allocator.phase_end(index_type))
                except (ValueError, AttributeError) as e:
                    # Unknown phase in allocator — log and fall back gracefully
                    logger.warning(
                        "Allocator phase lookup failed for '%s': %s. "
                        "Falling back to evenly-spaced progress.",
                        index_type,
                        e,
                    )
                    base_progress = 10 + int((idx / total_types) * 80)
                    next_progress = 10 + int(((idx + 1) / total_types) * 80)
            else:
                base_progress = 10 + int((idx / total_types) * 80)
                next_progress = 10 + int(((idx + 1) / total_types) * 80)

            update_progress(
                base_progress,
                f"Processing {index_type} index ({idx + 1}/{total_types})",
            )

            try:
                result = self._execute_single_index_type(repo_path, index_type, clear)
                results[index_type] = result

                if not result.get("success", False):
                    error_msg = result.get("error", "Unknown error")
                    logger.error(
                        format_error_log(
                            "SVC-MIGRATE-003",
                            f"Failed to index {index_type}: {error_msg}",
                        ),
                        extra=get_log_extra("SVC-MIGRATE-003"),
                    )
            except Exception as e:
                error_msg = f"Exception during {index_type} indexing: {str(e)}"
                self.logger.error(
                    format_error_log("SVC-MIGRATE-010", error_msg),
                    extra=get_log_extra("SVC-MIGRATE-010"),
                )
                results[index_type] = {"success": False, "error": error_msg}

            update_progress(next_progress, f"Completed {index_type} index")

        return results

    def _execute_single_index_type(
        self, repo_path: str, index_type: str, clear: bool
    ) -> Dict[str, Any]:
        """
        Execute indexing for a single index type.

        Args:
            repo_path: Repository path
            index_type: Index type to process
            clear: Clear flag

        Returns:
            Result dictionary
        """
        if index_type == "semantic":
            return self._execute_semantic_indexing(repo_path, clear)
        elif index_type == "fts":
            return self._execute_fts_indexing(repo_path, clear)
        elif index_type == "temporal":
            return self._execute_temporal_indexing(repo_path, clear)
        elif index_type == "scip":
            return self._execute_scip_indexing(repo_path, clear)
        else:
            return {"success": False, "error": f"Unknown index type: {index_type}"}

    def _seed_telemetry(self, repo_path: str) -> None:
        """Bug #678: Seed provider config into the repo .code-indexer dir before indexing.

        Fire-and-forget: failures are logged at DEBUG and never interrupt indexing.
        """
        try:
            from code_indexer.server.services.config_seeding import seed_provider_config

            seed_provider_config(repo_path)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(
                "Bug #678: seed_provider_config failed (non-fatal): %s", exc
            )

    def _drain_telemetry(self, repo_path: str) -> None:
        """Bug #678: Drain health events written by the cidx index subprocess.

        Fire-and-forget: failures are logged at DEBUG and never interrupt indexing.
        """
        try:
            from code_indexer.services.provider_health_bridge import (
                drain_and_feed_monitor,
            )

            drain_and_feed_monitor(repo_path)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(
                "Bug #678: drain_and_feed_monitor failed (non-fatal): %s", exc
            )

    def _run_subprocess_with_telemetry(
        self,
        args: List[str],
        repo_path: str,
        env: Optional[dict] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        poll_interval: float = SHORT_POLL_SECONDS,
    ) -> "subprocess.CompletedProcess[str]":
        """Run a cidx subprocess with provider-config seeding and health-event draining.

        Bug #678: Seeds config before the subprocess starts and drains health events
        in a finally block so telemetry is collected even when the command fails.

        Bug #1313 round-4 (Finding 3): env is forwarded to the subprocess so
        the temporal (--index-commits) child subprocess can be handed
        CIDX_TEMPORAL_PG_BOOTSTRAP_DIR in cluster/postgres mode -- see
        _execute_temporal_indexing.

        Bug #1325: the resolved env is ALWAYS sanitized via
        build_cidx_subprocess_env before being passed to the subprocess, so
        any relative PYTHONPATH entry inherited from the server process is
        absolutized before the child changes cwd to repo_path (otherwise a
        relative PYTHONPATH re-anchors into the repo and can shadow an
        installed dependency with a repo-local package of the same name).
        When the caller passes env=None (the semantic/FTS default case), a
        fresh sanitized copy of os.environ is used. When the caller passes a
        temporal env dict (built by build_temporal_child_env), that dict's
        PYTHONPATH is absolutized too, preserving the PG bootstrap var.

        Bug #1342: the subprocess now runs via run_cancellable_subprocess
        instead of a plain blocking subprocess.run. When cancel_check is
        provided and returns True, the ENTIRE child process group (the
        `cidx index` subprocess and any of its own children) is killed
        (SIGTERM, brief grace, SIGKILL) and SubprocessCancelledError is
        raised -- callers up the stack (run_branch_delta_index ->
        ActivatedRepoManager._run_branch_delta_index) turn this into the
        existing ActivatedRepoError + cleanup path.

        Note (Bug #1218): no whole-job timeout is applied. poll_interval only
        controls how often cancel_check() is consulted -- it is NOT a
        wall-clock deadline on the subprocess itself. The only legitimate
        fixed timeout in the system is the per-request outbound embedding-
        provider HTTP call.
        """
        resolved_env = (
            build_cidx_subprocess_env(env)
            if env is not None
            else build_cidx_subprocess_env()
        )
        self._seed_telemetry(repo_path)
        try:
            return run_cancellable_subprocess(
                args,
                cwd=repo_path,
                env=resolved_env,
                cancel_check=cancel_check,
                poll_interval=poll_interval,
            )
        finally:
            self._drain_telemetry(repo_path)

    def _execute_semantic_indexing(
        self,
        repo_path: str,
        clear: bool,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Execute semantic indexing using SmartIndexer.

        Bug #1342: cancel_check is forwarded to the subprocess helper so a
        cancelled activation/switch/sync job can kill the `cidx index`
        child promptly instead of blocking to completion. None (the default,
        used by the unrelated manual-reindex job path) disables cancellation.
        """
        try:
            repo_path_obj = Path(repo_path)
            index_dir = repo_path_obj / ".code-indexer" / "index"

            # Clear index if requested
            if clear and index_dir.exists():
                self.logger.info(
                    f"Clearing semantic index: {index_dir}",
                    extra={"correlation_id": get_correlation_id()},
                )
                shutil.rmtree(index_dir)

            result = self._run_subprocess_with_telemetry(
                ["cidx", "index"],
                repo_path,
                cancel_check=cancel_check,
            )

            if result.returncode != 0:
                return {
                    "success": False,
                    "error": f"Semantic indexing failed: {result.stderr}",
                }

            return {"success": True, "message": "Semantic indexing completed"}

        except SubprocessCancelledError:
            # Bug #1346: a user-initiated cancel must propagate with its
            # original type intact so ActivatedRepoManager._run_branch_delta_index
            # can recognize it via isinstance() and log it as a cancellation
            # (INFO) rather than a genuine failure (ERROR). Swallowing it into
            # the generic result-dict shape below would lose that type.
            raise
        except Exception as e:
            return {"success": False, "error": f"Semantic indexing error: {str(e)}"}

    def run_branch_delta_index(
        self,
        repo_path: str,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Run a synchronous branch-aware incremental semantic reindex.

        Called by ActivatedRepoManager after activation/switch/sync on a
        non-default branch (Bug #1203).  Uses clear=False so the existing CoW
        clone index is treated as a starting point and only changed files are
        re-embedded.

        Args:
            repo_path: Absolute path to the activated repo clone.
            cancel_check: Bug #1342 -- optional zero-arg callable returning
                True when the owning job has been cancelled. Forwarded down
                to the `cidx index` subprocess so cancellation kills the
                child promptly instead of waiting for it to finish. A
                cancellation surfaces here as a RuntimeError (same as any
                other subprocess failure), preserving this method's existing
                contract for callers.

        Raises:
            RuntimeError: If the indexing subprocess fails, is cancelled, or
                times out.
        """
        result = self._execute_semantic_indexing(
            repo_path, False, cancel_check=cancel_check
        )
        if not result.get("success", False):
            error_detail = result.get("error", "unknown error")
            raise RuntimeError(
                f"Branch-delta reindex failed for '{repo_path}': {error_detail}"
            )

    def _execute_fts_indexing(self, repo_path: str, clear: bool) -> Dict[str, Any]:
        """Execute FTS indexing using TantivyIndexManager."""
        try:
            args = ["cidx", "index", "--fts"]
            if clear:
                args.append("--clear")

            result = self._run_subprocess_with_telemetry(
                args,
                repo_path,
            )

            if result.returncode != 0:
                return {
                    "success": False,
                    "error": f"FTS indexing failed: {result.stderr}",
                }

            return {"success": True, "message": "FTS indexing completed"}

        except Exception as e:
            return {"success": False, "error": f"FTS indexing error: {str(e)}"}

    def _execute_temporal_indexing(self, repo_path: str, clear: bool) -> Dict[str, Any]:
        """Execute temporal indexing using GitCommitIndexer.

        Bug #1313 round-4 (Finding 3): in postgres/cluster mode, hand the
        child subprocess CIDX_TEMPORAL_PG_BOOTSTRAP_DIR so it installs the
        PostgreSQL temporal-metadata backend instead of silently falling
        back to SQLite-on-NFS. sqlite/solo mode yields env=None --
        byte-unchanged existing behavior.
        """
        try:
            from code_indexer.server.storage.postgres.temporal_child_wiring import (
                build_temporal_child_env,
            )

            args = ["cidx", "index", "--index-commits"]
            if clear:
                args.append("--clear")

            _temporal_env = build_temporal_child_env(get_config_service().get_config())

            result = self._run_subprocess_with_telemetry(
                args,
                repo_path,
                env=_temporal_env,
            )

            if result.returncode != 0:
                return {
                    "success": False,
                    "error": f"Temporal indexing failed: {result.stderr}",
                }

            return {"success": True, "message": "Temporal indexing completed"}

        except Exception as e:
            return {"success": False, "error": f"Temporal indexing error: {str(e)}"}

    def _execute_scip_indexing(self, repo_path: str, clear: bool) -> Dict[str, Any]:
        """Execute SCIP indexing using cidx scip generate."""
        try:
            args = ["cidx", "scip", "generate", "--project", repo_path]
            if clear:
                args.append("--clear")

            result = subprocess.run(
                args,
                cwd=repo_path,
                capture_output=True,
                text=True,
                env=build_cidx_subprocess_env(),
            )

            if result.returncode != 0:
                return {
                    "success": False,
                    "error": f"SCIP generation failed: {result.stderr}",
                }

            return {"success": True, "message": "SCIP generation completed"}

        except Exception as e:
            return {"success": False, "error": f"SCIP generation error: {str(e)}"}

    def _get_semantic_status(self, repo_path: Path) -> Dict[str, Any]:
        """Get semantic index status."""
        index_dir = repo_path / ".code-indexer" / "index"

        if not index_dir.exists():
            return {"status": "not_indexed"}

        metadata_file = index_dir / "metadata.json"
        if not metadata_file.exists():
            return {"status": "not_indexed"}

        try:
            with open(metadata_file) as f:
                metadata = json.load(f)

            # Calculate index size
            index_size_bytes = sum(
                f.stat().st_size for f in index_dir.rglob("*") if f.is_file()
            )
            index_size_mb = round(index_size_bytes / self.BYTES_PER_MB, 2)

            return {
                "last_indexed": metadata.get("last_indexed"),
                "file_count": metadata.get("file_count", 0),
                "index_size_mb": index_size_mb,
                "status": "up_to_date",
            }
        except Exception:
            logger.warning(
                format_error_log(
                    "SVC-MIGRATE-005", "Failed to read semantic index metadata: {e}"
                ),
                extra=get_log_extra("SVC-MIGRATE-005"),
            )
            return {"status": "not_indexed"}

    def _get_fts_status(self, repo_path: Path) -> Dict[str, Any]:
        """Get FTS index status."""
        fts_dir = repo_path / ".code-indexer" / "tantivy"

        if not fts_dir.exists():
            return {"status": "not_indexed"}

        try:
            # Count document files (rough approximation)
            doc_count = sum(1 for _ in fts_dir.rglob("*.store"))

            # Get last modified time
            latest_file = max(
                (f for f in fts_dir.rglob("*") if f.is_file()),
                key=lambda f: f.stat().st_mtime,
                default=None,
            )

            if latest_file:
                last_updated = datetime.fromtimestamp(
                    latest_file.stat().st_mtime, tz=timezone.utc
                ).isoformat()
            else:
                last_updated = None

            return {
                "last_updated": last_updated,
                "document_count": doc_count,
                "index_health": "healthy",
                "status": "up_to_date",
            }
        except Exception:
            logger.warning(
                format_error_log(
                    "SVC-MIGRATE-006", "Failed to read FTS index status: {e}"
                ),
                extra=get_log_extra("SVC-MIGRATE-006"),
            )
            return {"status": "not_indexed"}

    def _get_temporal_status(self, repo_path: Path) -> Dict[str, Any]:
        """Get temporal index status."""
        from code_indexer.services.temporal.temporal_collection_naming import (
            is_temporal_collection as _is_temporal,
        )

        index_dir = repo_path / ".code-indexer" / "index"
        temporal_dir = next(
            (
                d
                for d in sorted(index_dir.iterdir() if index_dir.is_dir() else [])
                if d.is_dir() and _is_temporal(d.name)
            ),
            None,
        )

        if temporal_dir is None or not temporal_dir.exists():
            return {"status": "not_indexed"}

        metadata_file = temporal_dir / "metadata.json"
        if not metadata_file.exists():
            return {"status": "not_indexed"}

        try:
            with open(metadata_file) as f:
                metadata = json.load(f)

            last_indexed = metadata.get("last_indexed")
            commit_count = metadata.get("commit_count", 0)

            # Check if stale
            status = "up_to_date"
            if last_indexed:
                last_indexed_dt = datetime.fromisoformat(last_indexed)
                age_days = (datetime.now(timezone.utc) - last_indexed_dt).days
                if age_days > self.STALE_THRESHOLD_DAYS:
                    status = "stale"

            return {
                "last_indexed": last_indexed,
                "commit_count": commit_count,
                "date_range": metadata.get("date_range"),
                "status": status,
            }
        except Exception:
            logger.warning(
                format_error_log(
                    "SVC-MIGRATE-007", "Failed to read temporal index metadata: {e}"
                ),
                extra=get_log_extra("SVC-MIGRATE-007"),
            )
            return {"status": "not_indexed"}

    def _get_scip_status(self, repo_path: Path) -> Dict[str, Any]:
        """Get SCIP index status."""
        scip_dir = repo_path / ".code-indexer" / "scip"

        if not scip_dir.exists():
            return {"status": "not_indexed", "project_count": 0}

        # Check for .scip.db files
        scip_files = list(scip_dir.glob("*.scip.db"))

        if not scip_files:
            return {"status": "not_indexed", "project_count": 0}

        try:
            # Get last generated time from most recent file
            latest_file = max(scip_files, key=lambda f: f.stat().st_mtime)
            last_generated = datetime.fromtimestamp(
                latest_file.stat().st_mtime, tz=timezone.utc
            ).isoformat()

            # Count projects and extract names
            project_count = len(scip_files)
            projects = [f.stem.replace(".scip", "") for f in scip_files]

            return {
                "status": "SUCCESS",
                "project_count": project_count,
                "last_generated": last_generated,
                "projects": projects,
            }
        except Exception as e:
            logger.warning(
                format_error_log(
                    "SVC-MIGRATE-008", "Failed to read SCIP index status: {e}"
                ),
                extra=get_log_extra("SVC-MIGRATE-008"),
            )
            return {"status": "FAILED", "project_count": 0, "error": str(e)}
