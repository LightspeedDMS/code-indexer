"""
LifecycleBatchRunner — Story #876.

Implements the one-call-per-repo lifecycle detection rebuild:
  - write_meta_md: cooperative-lock writer for cidx-meta/<alias>.md
  - LifecycleLockUnavailableError: raised when write lock cannot be acquired
  - _validate_alias: path-traversal guard
  - _do_write: atomic write via tempfile.mkstemp + os.rename

The LifecycleBatchRunner class and LifecycleFleetScanner will be added in
subsequent increments.
"""

import logging as _logging
import math
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import yaml

from code_indexer.global_repos.unified_response_parser import (
    CURRENT_LIFECYCLE_SCHEMA_VERSION,
)

# Alias character whitelist — prevents path traversal via
# directory separators, null bytes, or shell metacharacters.
_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class LifecycleLockUnavailableError(Exception):
    """
    Raised when write_meta_md cannot acquire the cidx-meta write lock
    because another writer already holds it.

    Callers must treat this as a transient condition and retry later.
    """


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_alias(alias: str) -> None:
    """
    Reject alias values that could escape the cidx-meta directory.

    Raises:
        ValueError: if alias is empty, contains path separators, or contains
                    characters outside [A-Za-z0-9._-].
    """
    if not alias:
        raise ValueError("alias must be a non-empty string")
    if not _ALIAS_PATTERN.match(alias):
        raise ValueError(
            f"alias contains invalid characters (only [A-Za-z0-9._-] allowed): {alias!r}"
        )


def _do_write(
    meta_md_path: Path,
    cidx_meta_path: Path,
    description_body: str,
    lifecycle_frontmatter: Dict[str, Any],
) -> None:
    """
    Atomically write a YAML-frontmatter Markdown file to *meta_md_path*.

    Algorithm:
      1. Render YAML frontmatter from *lifecycle_frontmatter*.
      2. Write to a temporary file in *cidx_meta_path/* (same filesystem as
         destination — required for atomic rename to work on Linux).
      3. Rename temp → *meta_md_path* (atomic on POSIX).

    Raises:
        yaml.YAMLError: if lifecycle_frontmatter cannot be serialised to YAML.
        OSError: on I/O failures during write or rename.
    """
    # Fail-closed: re-raise yaml serialisation errors rather than silently
    # writing a corrupt file (Messi Rule #13 — Anti-Silent-Failure).
    frontmatter_yaml = yaml.dump(
        lifecycle_frontmatter, default_flow_style=False, allow_unicode=True
    )

    content = f"---\n{frontmatter_yaml}---\n\n{description_body}\n"

    fd, tmp_path = tempfile.mkstemp(dir=str(cidx_meta_path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception:
        # Best-effort cleanup: the original write exception is the real failure.
        # If unlink also fails we discard that secondary OSError intentionally —
        # the orphaned temp file is harmless and the caller sees the root cause.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass  # Discard: cleanup is best-effort; original exception propagates
        raise

    os.rename(tmp_path, str(meta_md_path))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_meta_md(
    *,
    alias: str,
    description_body: str,
    lifecycle_frontmatter: Dict[str, Any],
    already_locked: bool,
    refresh_scheduler: Any,
    golden_repos_dir: Union[str, os.PathLike],
) -> None:
    """
    Write *cidx-meta/<alias>.md* with YAML frontmatter.

    Cooperative-lock contract:
      - already_locked=False: acquires the cidx-meta write lock before
        writing; releases it after. Raises LifecycleLockUnavailableError
        if acquire_write_lock returns False.
      - already_locked=True: assumes the caller already holds the lock;
        neither acquires nor releases it.

    Atomic write: uses tempfile.mkstemp(dir=cidx-meta/) + os.rename so
    readers never see a partial file.

    Does NOT call cidx_meta_refresh_debouncer.signal_dirty — that is the
    caller's responsibility (Messi Rule #12 — Anti-Orphan-Code: the signal
    belongs to the orchestrator, not the leaf writer).

    Args:
        alias: Repository alias. Must match [A-Za-z0-9._-]+.
        description_body: Markdown body text (description paragraph).
        lifecycle_frontmatter: Dict serialised as YAML frontmatter. Must
            contain at minimum the 'lifecycle' and 'lifecycle_schema_version'
            keys required by the cidx-meta contract.
        already_locked: When True, skip lock acquisition/release.
        refresh_scheduler: Object with acquire_write_lock / release_write_lock
            methods (protocol: RefreshScheduler).
        golden_repos_dir: Path to the golden_repos root directory. The
            cidx-meta subdirectory is resolved as golden_repos_dir/cidx-meta/.

    Raises:
        ValueError: on invalid alias, golden_repos_dir type, description_body
            type, lifecycle_frontmatter type, or refresh_scheduler missing
            required methods.
        LifecycleLockUnavailableError: when already_locked=False and the
            write lock cannot be acquired.
        yaml.YAMLError: if lifecycle_frontmatter cannot be serialised.
        OSError: on I/O failure.
    """
    # -- Input validation ---------------------------------------------------
    _validate_alias(alias)

    if not isinstance(description_body, str):
        raise ValueError(
            f"description_body must be a str, got {type(description_body).__name__}"
        )
    if not isinstance(lifecycle_frontmatter, dict):
        raise ValueError(
            f"lifecycle_frontmatter must be a dict, got {type(lifecycle_frontmatter).__name__}"
        )
    if not isinstance(golden_repos_dir, (str, os.PathLike)):
        raise ValueError(
            f"golden_repos_dir must be a str or path-like object, got {type(golden_repos_dir).__name__}"
        )
    if not hasattr(refresh_scheduler, "acquire_write_lock") or not hasattr(
        refresh_scheduler, "release_write_lock"
    ):
        raise ValueError(
            "refresh_scheduler must have acquire_write_lock and release_write_lock methods"
        )

    cidx_meta_path = Path(golden_repos_dir) / "cidx-meta"
    meta_md_path = cidx_meta_path / f"{alias}.md"

    # -- Cooperative lock ---------------------------------------------------
    _LOCK_OWNER = "lifecycle_writer"
    _LOCK_KEY = "cidx-meta"

    if not already_locked:
        acquired = refresh_scheduler.acquire_write_lock(
            _LOCK_KEY, owner_name=_LOCK_OWNER
        )
        if not acquired:
            raise LifecycleLockUnavailableError(
                f"Could not acquire write lock for {_LOCK_KEY!r} (owner={_LOCK_OWNER!r}); "
                "another writer holds it — retry later"
            )

    try:
        _do_write(meta_md_path, cidx_meta_path, description_body, lifecycle_frontmatter)
    finally:
        if not already_locked:
            refresh_scheduler.release_write_lock(_LOCK_KEY, owner_name=_LOCK_OWNER)


# ---------------------------------------------------------------------------
# Self-alias guard
# ---------------------------------------------------------------------------

# The cidx-meta directory alias must never be scanned for lifecycle health —
# it is the metadata store itself, not a golden repo clone.
_CIDX_META_SELF_ALIAS: str = "cidx-meta"


# ---------------------------------------------------------------------------
# LifecycleFleetScanner
# ---------------------------------------------------------------------------


class LifecycleFleetScanner:
    """
    Scans the cidx-meta store and returns aliases whose lifecycle metadata is
    missing, incomplete, outdated, or poisoned.

    Flags an alias when any of the following is true:
      - cidx-meta/<alias>.md does not exist
      - frontmatter is absent or malformed (split_frontmatter_and_body returns {})
      - 'lifecycle' key is absent from frontmatter
      - lifecycle_schema_version is missing, non-integer, or < CURRENT_LIFECYCLE_SCHEMA_VERSION
      - lifecycle.confidence == 'unknown' (poison row from old fallback path)

    Never flags the 'cidx-meta' self-alias (the metadata store directory).

    Args:
        golden_repos_dir: Path to the golden_repos root (contains cidx-meta/).
        repo_aliases: List of repository alias strings to inspect.

    Raises:
        ValueError: if golden_repos_dir is not a path-like, or if repo_aliases
                    is None or contains non-string elements.
    """

    def __init__(
        self,
        golden_repos_dir: Union[str, os.PathLike],
        repo_aliases: List[str],
    ) -> None:
        if not isinstance(golden_repos_dir, (str, os.PathLike)):
            raise ValueError(
                f"golden_repos_dir must be str or path-like, got {type(golden_repos_dir).__name__}"
            )
        if repo_aliases is None:
            raise ValueError("repo_aliases must not be None")
        if not all(isinstance(a, str) for a in repo_aliases):
            raise ValueError("all elements of repo_aliases must be strings")

        self._cidx_meta_dir: Path = Path(golden_repos_dir) / "cidx-meta"
        self._repo_aliases: List[str] = list(repo_aliases)

    def find_broken_or_missing(self) -> List[str]:
        """
        Return aliases whose lifecycle metadata is missing or broken.

        Each alias is validated with _validate_alias() before path construction
        to prevent directory traversal. lifecycle_schema_version is coerced to
        int safely — a non-integer value is treated as broken rather than crashing.

        Returns:
            List of alias strings that need lifecycle (re-)detection.
            Order matches the order of repo_aliases provided at construction.
        """
        from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body

        broken: List[str] = []
        for alias in self._repo_aliases:
            if alias == _CIDX_META_SELF_ALIAS:
                continue  # Never scan the metadata store directory itself

            # Validate alias characters before constructing any path.
            try:
                _validate_alias(alias)
            except ValueError:
                broken.append(alias)
                continue

            meta_path = self._cidx_meta_dir / f"{alias}.md"

            if not meta_path.exists():
                broken.append(alias)
                continue

            content = meta_path.read_text(encoding="utf-8")
            frontmatter, _ = split_frontmatter_and_body(content)

            if not frontmatter:
                # Malformed or absent frontmatter
                broken.append(alias)
                continue

            lifecycle = frontmatter.get("lifecycle")
            if lifecycle is None:
                broken.append(alias)
                continue

            # Coerce schema_version to int — external file data may be a string.
            raw_version = frontmatter.get("lifecycle_schema_version")
            try:
                schema_version = int(raw_version) if raw_version is not None else 0
            except (TypeError, ValueError):
                broken.append(alias)
                continue

            if schema_version < CURRENT_LIFECYCLE_SCHEMA_VERSION:
                broken.append(alias)
                continue

            confidence = (
                lifecycle.get("confidence") if isinstance(lifecycle, dict) else None
            )
            if confidence == "unknown":
                broken.append(alias)

        return broken


# ---------------------------------------------------------------------------
# LifecycleBatchRunner
# ---------------------------------------------------------------------------

_logger = _logging.getLogger(__name__)

# Default write-lock TTL matching WriteLockManager.acquire's default (seconds).
_DEFAULT_TTL_SECONDS: int = 3600

# Amortized wall-clock estimate per Claude CLI call (seconds).
_DEFAULT_ESTIMATED_SECONDS_PER_REPO: int = 30

# Lock owner name used by the batch runner when acquiring cidx-meta write lock.
_BATCH_RUNNER_LOCK_OWNER: str = "lifecycle_batch_runner"


class LifecycleBatchRunner:
    """
    Runs one unified Claude CLI call per repo to populate cidx-meta/<alias>.md
    with lifecycle metadata and description.

    Sub-batch pattern:
      - Computes sub_batch_size = max(1, floor(0.5 * ttl * concurrency / est_secs))
        so each sub-batch's worst-case wall time stays within the lock TTL.
      - Acquires the cidx-meta write lock once per sub-batch; releases it in
        a try/finally so other cidx-meta consumers can interleave between batches.
      - Runs repos within a sub-batch in a thread pool (concurrency workers).
      - Per-repo failures are logged at ERROR level and do not abort the sub-batch
        (other repos in the same sub-batch continue). The job completes after all
        sub-batches regardless of per-repo failures.
      - Signals the debouncer exactly once after all sub-batches complete.
      - Calls job_tracker.complete_job exactly once after all sub-batches join.

    Lock acquisition failure is fatal for the entire batch:
      - If acquire_write_lock returns False for any sub-batch, run() raises
        LifecycleLockUnavailableError immediately. complete_job and signal_dirty
        are NOT called.

    All external dependencies are injected so the class is fully testable
    without a running server.

    Args:
        golden_repos_dir: Root directory containing cidx-meta/.
        job_tracker: Object with update_status / complete_job / fail_job methods.
        refresh_scheduler: Object with acquire_write_lock / release_write_lock.
        debouncer: Object with signal_dirty().
        claude_cli_invoker: Callable(alias, repo_path) -> UnifiedResult.
        concurrency: Thread pool size (reuse max_concurrent_claude_cli).
        ttl_seconds: Write-lock TTL (default 3600 = WriteLockManager default).
        estimated_seconds_per_repo: Amortized Claude CLI time per repo (default 30).
        sub_batch_size_override: If set, bypasses the formula — used in tests.

    Raises:
        ValueError: on invalid constructor arguments.
    """

    def __init__(
        self,
        golden_repos_dir: Union[str, os.PathLike],
        job_tracker: Any,
        refresh_scheduler: Any,
        debouncer: Any,
        claude_cli_invoker: Callable,
        concurrency: int = 2,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        estimated_seconds_per_repo: int = _DEFAULT_ESTIMATED_SECONDS_PER_REPO,
        sub_batch_size_override: Optional[int] = None,
        tracking_backend: Optional[Any] = None,
    ) -> None:
        if not isinstance(golden_repos_dir, (str, os.PathLike)):
            raise ValueError(
                f"golden_repos_dir must be str or path-like, got {type(golden_repos_dir).__name__}"
            )
        if concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {concurrency}")
        if ttl_seconds < 1:
            raise ValueError(f"ttl_seconds must be >= 1, got {ttl_seconds}")
        if estimated_seconds_per_repo < 1:
            raise ValueError(
                f"estimated_seconds_per_repo must be >= 1, got {estimated_seconds_per_repo}"
            )
        if sub_batch_size_override is not None and sub_batch_size_override < 1:
            raise ValueError(
                f"sub_batch_size_override must be >= 1, got {sub_batch_size_override}"
            )
        if not callable(claude_cli_invoker):
            raise ValueError("claude_cli_invoker must be callable")
        for attr_name, obj in (
            ("job_tracker", job_tracker),
            ("refresh_scheduler", refresh_scheduler),
            ("debouncer", debouncer),
        ):
            if obj is None:
                raise ValueError(f"{attr_name} must not be None")

        self._golden_repos_dir: Path = Path(golden_repos_dir)
        self._job_tracker = job_tracker
        self._refresh_scheduler = refresh_scheduler
        self._debouncer = debouncer
        self._claude_cli_invoker = claude_cli_invoker
        self._concurrency: int = concurrency
        self._ttl_seconds: int = ttl_seconds
        self._estimated_seconds_per_repo: int = estimated_seconds_per_repo
        self._sub_batch_size_override: Optional[int] = sub_batch_size_override
        self._tracking_backend: Optional[Any] = tracking_backend

    @staticmethod
    def compute_sub_batch_size(
        ttl_seconds: int,
        concurrency: int,
        estimated_seconds_per_repo: int,
    ) -> int:
        """
        Compute the sub-batch size that keeps each sub-batch's worst-case
        wall time within 0.5 * ttl_seconds.

        Formula: max(1, floor(0.5 * ttl_seconds * concurrency / estimated_seconds_per_repo))

        Returns at least 1 so the runner always makes forward progress.
        """
        return max(
            1,
            math.floor(0.5 * ttl_seconds * concurrency / estimated_seconds_per_repo),
        )

    def run(
        self,
        repo_aliases: List[str],
        parent_job_id: str,
    ) -> None:
        """
        Run lifecycle detection for all aliases, sub-batching to stay within
        the write-lock TTL.

        Per-repo failures are logged at ERROR level and do not abort the batch.
        Lock acquisition failure aborts immediately with LifecycleLockUnavailableError.

        Args:
            repo_aliases: Aliases to process.
            parent_job_id: Job identifier for progress/completion reporting.

        Raises:
            LifecycleLockUnavailableError: if cidx-meta write lock cannot be
                acquired for any sub-batch. complete_job and signal_dirty are
                NOT called in this case.
        """
        total = len(repo_aliases)

        self._job_tracker.update_status(
            job_id=parent_job_id,
            status="running",
            progress=0,
        )

        sub_batch_size = (
            self._sub_batch_size_override
            if self._sub_batch_size_override is not None
            else self.compute_sub_batch_size(
                self._ttl_seconds, self._concurrency, self._estimated_seconds_per_repo
            )
        )

        sub_batches: List[List[str]] = (
            [
                repo_aliases[i : i + sub_batch_size]
                for i in range(0, total, sub_batch_size)
            ]
            if total > 0
            else []
        )

        for sub_batch in sub_batches:
            self._run_sub_batch(sub_batch, parent_job_id)

        # Signal debouncer ONCE after all sub-batches have been released.
        self._debouncer.signal_dirty()

        # Terminal transition — complete_job, not update_status("completed").
        self._job_tracker.complete_job(
            job_id=parent_job_id,
            result={"phase": "lifecycle", "done": total, "total": total},
        )

    def _run_sub_batch(self, sub_batch: List[str], parent_job_id: str) -> None:
        """
        Process all aliases in sub_batch concurrently under an already-held lock.

        Per-repo exceptions are logged at ERROR level and do not abort the
        sub-batch — all repos in the batch are attempted. Only BaseException
        subclasses that are not Exception (e.g. KeyboardInterrupt) propagate.
        """
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            futures = {
                pool.submit(self._process_one_repo, alias, parent_job_id): alias
                for alias in sub_batch
            }
            for future in as_completed(futures):
                alias = futures[future]
                exc = future.exception()
                if exc is not None:
                    if isinstance(exc, Exception):
                        # Log the failure; the sub-batch continues for other repos.
                        _logger.error(
                            "lifecycle-runner: failed to process alias %r: %s: %s",
                            alias,
                            type(exc).__name__,
                            exc,
                        )
                    else:
                        # BaseException (e.g. KeyboardInterrupt) — re-raise immediately.
                        raise exc

    def _process_one_repo(self, alias: str, parent_job_id: str) -> None:
        """
        Run one unified Claude CLI call for alias and write cidx-meta/<alias>.md.

        Lock contract (Bug #876 fix):
          Acquires a per-alias lock key f"lifecycle:{alias}" before the file
          write, so concurrent jobs for DIFFERENT aliases never contend.
          Raises LifecycleLockUnavailableError if acquire returns False (same
          alias already in-flight on another thread or node).

        On success:
          - Writes metadata atomically under the per-alias lock.
          - Releases the per-alias lock.
          - Updates tracking_backend with lifecycle_schema_version and status
            'completed' if tracking_backend was injected.

        On failure: does NOT write a partial file; exception propagates to
        _run_sub_batch which logs it at ERROR level (fail-closed per file,
        Messi Rule #13).
        """
        repo_path = self._golden_repos_dir / alias
        result = self._claude_cli_invoker(alias, repo_path)

        per_alias_lock_key = f"lifecycle:{alias}"
        acquired = self._refresh_scheduler.acquire_write_lock(
            per_alias_lock_key, owner_name=_BATCH_RUNNER_LOCK_OWNER
        )
        if not acquired:
            raise LifecycleLockUnavailableError(
                f"per-alias write lock for {alias!r} held by another worker; "
                "lifecycle write aborted"
            )
        try:
            write_meta_md(
                alias=alias,
                description_body=result.description,
                lifecycle_frontmatter={
                    "lifecycle": result.lifecycle,
                    "lifecycle_schema_version": CURRENT_LIFECYCLE_SCHEMA_VERSION,
                },
                already_locked=True,
                refresh_scheduler=self._refresh_scheduler,
                golden_repos_dir=self._golden_repos_dir,
            )
        finally:
            self._refresh_scheduler.release_write_lock(
                per_alias_lock_key, owner_name=_BATCH_RUNNER_LOCK_OWNER
            )

        # Update tracking DB with lifecycle schema version after successful write.
        # Only when tracking_backend was injected (optional collaborator).
        if self._tracking_backend is not None:
            now_iso = datetime.now(timezone.utc).isoformat()
            self._tracking_backend.upsert_tracking(
                repo_alias=alias,
                lifecycle_schema_version=CURRENT_LIFECYCLE_SCHEMA_VERSION,
                status="completed",
                updated_at=now_iso,
            )
