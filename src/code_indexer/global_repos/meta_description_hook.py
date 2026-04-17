"""
Meta description lifecycle hooks for golden repositories.

Provides hooks that automatically create/delete .md files in cidx-meta
when golden repos are added/removed, eliminating the need for special-case
meta directory management code.
"""

import difflib
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from code_indexer.global_repos.lifecycle_schema import LIFECYCLE_SCHEMA_VERSION
from code_indexer.global_repos.repo_analyzer import (
    RepoAnalyzer,
    invoke_lifecycle_detection,
)
from code_indexer.server.services.claude_cli_manager import (
    get_claude_cli_manager,
)

logger = logging.getLogger(__name__)

# Maximum number of unified-diff lines emitted in the AC9 structured log (Story #724 B1).
_AC9_DIFF_MAX_LINES = 200

# README file detection order
README_NAMES = [
    "README.md",
    "README.rst",
    "README.txt",
    "README",
    "readme.md",
    "Readme.md",
]

# Module-level tracking backend (initialized by set_tracking_backend)
_tracking_backend: Optional["DescriptionRefreshTrackingBackend"] = None  # type: ignore[name-defined]  # noqa: F821
_scheduler: Optional["DescriptionRefreshScheduler"] = None  # type: ignore[name-defined]  # noqa: F821
_refresh_scheduler: Optional[Any] = None  # type: ignore
_debouncer: Optional["CidxMetaRefreshDebouncer"] = None

_DEFAULT_DEBOUNCE_SECONDS = 30


class CidxMetaRefreshDebouncer:
    """
    Debounces cidx-meta refresh triggers to coalesce rapid batch registrations.

    When multiple repositories are registered in rapid succession, each
    on_repo_added() call attempts trigger_refresh_for_repo("cidx-meta-global").
    The first succeeds, but subsequent calls raise DuplicateJobError. This
    debouncer coalesces those failures into a single deferred refresh that fires
    after the debounce interval with no further activity.

    Usage:
        debouncer = CidxMetaRefreshDebouncer(refresh_scheduler, debounce_seconds=30)
        debouncer.signal_dirty()   # Called when DuplicateJobError is caught
        debouncer.shutdown()       # Called on server shutdown
    """

    def __init__(
        self,
        refresh_scheduler: Any,
        debounce_seconds: float = _DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        self._refresh_scheduler = refresh_scheduler
        self._debounce_seconds = debounce_seconds
        self._dirty: bool = False
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._shutdown: bool = False

    def signal_dirty(self) -> None:
        """
        Mark cidx-meta as needing a refresh and (re)start the debounce timer.

        If a timer is already running it is cancelled and a new one is started
        with the full debounce interval (coalescing behavior).  After shutdown
        the call is silently ignored.
        """
        with self._lock:
            self._dirty = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._shutdown:
                logger.debug(
                    "cidx-meta debouncer: shutdown in progress, ignoring signal_dirty"
                )
                return
            self._timer = threading.Timer(
                self._debounce_seconds, self._on_timer_expired
            )
            self._timer.daemon = True
            self._timer.start()
        logger.debug(
            "cidx-meta marked dirty, debounce timer (re)started "
            f"(interval={self._debounce_seconds}s)"
        )

    def _on_timer_expired(self) -> None:
        """
        Called by the timer thread when the debounce interval elapses.

        Clears dirty state after successful refresh.  If trigger raises
        DuplicateJobError the job is still running; re-mark dirty and retry
        after another debounce interval.  Any other exception is logged and
        swallowed (non-blocking).
        """
        from code_indexer.server.repositories.background_jobs import DuplicateJobError

        with self._lock:
            if not self._dirty or self._shutdown:
                return
            # Don't clear _dirty yet — clear after successful refresh
            self._timer = None

        # Trigger outside lock to avoid holding lock during I/O
        try:
            self._refresh_scheduler.trigger_refresh_for_repo("cidx-meta-global")
            logger.info("Debounced cidx-meta refresh triggered successfully")
            # Success — NOW clear dirty
            with self._lock:
                self._dirty = False
        except DuplicateJobError:
            logger.info("cidx-meta refresh still running, will retry after debounce")
            with self._lock:
                # _dirty stays True (was never cleared)
                if not self._shutdown:
                    self._timer = threading.Timer(
                        self._debounce_seconds, self._on_timer_expired
                    )
                    self._timer.daemon = True
                    self._timer.start()
        except Exception as exc:
            logger.warning("Debounced cidx-meta refresh failed: %s", exc)
            # On generic failure, clear dirty to avoid infinite retry
            with self._lock:
                self._dirty = False

    def shutdown(self) -> None:
        """
        Cancel any pending timer and prevent future timers from being started.

        Safe to call multiple times (idempotent).
        """
        with self._lock:
            self._shutdown = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        logger.debug("CidxMetaRefreshDebouncer shut down")


def set_tracking_backend(backend) -> None:
    """
    Set module-level tracking backend.

    Args:
        backend: DescriptionRefreshTrackingBackend instance

    Note:
        Called during server startup to inject the tracking backend.
    """
    global _tracking_backend
    _tracking_backend = backend


def set_scheduler(scheduler) -> None:
    """
    Set module-level scheduler.

    Args:
        scheduler: DescriptionRefreshScheduler instance

    Note:
        Called during server startup to inject the scheduler.
    """
    global _scheduler
    _scheduler = scheduler


def set_refresh_scheduler(scheduler) -> None:
    """
    Set module-level RefreshScheduler for triggering cidx-meta reindex.

    Args:
        scheduler: RefreshScheduler instance (from GlobalReposLifecycleManager)

    Note:
        Called during server startup after global_lifecycle_manager is created.
        Used by on_repo_added() and on_repo_removed() to trigger versioned
        CoW reindex of cidx-meta via trigger_refresh_for_repo().
    """
    global _refresh_scheduler
    _refresh_scheduler = scheduler


def set_debouncer(debouncer: Optional["CidxMetaRefreshDebouncer"]) -> None:
    """
    Set module-level CidxMetaRefreshDebouncer for deferred cidx-meta refresh.

    Args:
        debouncer: CidxMetaRefreshDebouncer instance, or None to clear.

    Note:
        Called during server startup after the debouncer is created.
        Used by on_repo_added() and on_repo_removed() when DuplicateJobError
        is raised by trigger_refresh_for_repo().
    """
    global _debouncer
    _debouncer = debouncer


def atomic_write_description(
    target_path: Path,
    content: str,
    refresh_scheduler: Optional[Any] = None,
) -> None:
    """
    Write content to target_path atomically via tempfile + os.replace.

    Creates a temporary file in the same directory as target_path (same
    filesystem), writes all content, then swaps atomically.  If
    refresh_scheduler is provided, acquires the cidx-meta write lock
    before the write and releases it in a finally block.  Lock acquisition
    failure propagates to the caller.

    Args:
        target_path: Destination Path object.
        content: Text content to write (UTF-8).
        refresh_scheduler: Optional scheduler exposing acquire_write_lock /
            release_write_lock.  Skipped when None.

    Raises:
        Any exception from acquire_write_lock, file write, or os.replace.
    """
    lock_acquired = False
    if refresh_scheduler is not None:
        refresh_scheduler.acquire_write_lock("cidx-meta", owner_name="lifecycle_writer")
        lock_acquired = True

    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(target_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp_path, str(target_path))
        except Exception:
            # Best-effort cleanup: remove the temp file so it does not linger.
            # A failure here is non-fatal; the original exception is the real error.
            try:
                os.unlink(tmp_path)
            except OSError as cleanup_exc:
                logger.debug(
                    "atomic_write_description: temp file cleanup failed "
                    "(non-fatal, original exception will propagate): %s",
                    cleanup_exc,
                )
            raise
    finally:
        if lock_acquired and refresh_scheduler is not None:
            refresh_scheduler.release_write_lock(
                "cidx-meta", owner_name="lifecycle_writer"
            )


def on_repo_added(
    repo_name: str,
    repo_url: str,
    clone_path: str,
    golden_repos_dir: str,
    mcp_registration_service: Optional[Any] = None,
) -> None:
    """
    Hook called after a golden repository is successfully added.

    Creates a .md description file in cidx-meta and triggers reindex via RefreshScheduler.
    Also creates a tracking record for periodic description refresh (Story #190).

    Args:
        repo_name: Name/alias of the repository
        repo_url: Repository URL
        clone_path: Path to cloned repository
        golden_repos_dir: Path to golden-repos directory

    Note:
        - Skips cidx-meta itself (no self-referential .md file)
        - Handles missing clone paths gracefully (logs warning, no crash)
        - Triggers cidx-meta reindex via RefreshScheduler after creating .md file
        - Falls back to README copy when Claude CLI unavailable or fails
        - Creates tracking record for scheduled refresh (if tracking backend available)
    """
    # Skip cidx-meta itself
    if repo_name == "cidx-meta":
        logger.info("Skipping meta description generation for cidx-meta itself")
        return

    # Create tracking record for scheduled refresh (Story #190)
    if _tracking_backend is not None:
        try:
            now_iso = datetime.now(timezone.utc).isoformat()

            # Calculate next run using scheduler if available, else use now
            if _scheduler is not None:
                next_run = _scheduler.calculate_next_run(repo_name)
            else:
                next_run = now_iso

            _tracking_backend.upsert_tracking(
                repo_alias=repo_name,
                status="pending",
                next_run=next_run,
                created_at=now_iso,
                updated_at=now_iso,
            )
            logger.info(
                f"Created tracking record for {repo_name} (next_run: {next_run})"
            )
        except Exception as e:
            # Don't block repo add if tracking fails
            logger.warning(
                f"Failed to create tracking record for {repo_name}: {e}", exc_info=True
            )
    else:
        logger.debug(
            f"Tracking backend not available, skipping tracking record for {repo_name}"
        )

    cidx_meta_path = Path(golden_repos_dir) / "cidx-meta"

    # Ensure cidx-meta directory exists
    if not cidx_meta_path.exists():
        logger.warning(
            f"cidx-meta directory does not exist at {cidx_meta_path}, cannot create .md file"
        )
        return

    # Use global ClaudeCliManager singleton (Story #23, AC4)
    # This ensures consistent API key handling and avoids creating multiple instances
    cli_manager = get_claude_cli_manager()

    # Determine whether to use Claude CLI or README fallback
    if cli_manager is None:
        logger.info(
            f"ClaudeCliManager not initialized, using README fallback for {repo_name}"
        )
        _create_readme_fallback(Path(clone_path), repo_name, cidx_meta_path)

    elif not cli_manager.check_cli_available():
        logger.info(f"Claude CLI unavailable, using README fallback for {repo_name}")
        _create_readme_fallback(Path(clone_path), repo_name, cidx_meta_path)

    else:
        # Generate .md file using Claude CLI (two-phase: Phase 1 + Phase 2 lifecycle)
        try:
            md_content, phase2_outcome = _generate_repo_description(
                repo_name,
                repo_url,
                clone_path,
                mcp_registration_service=mcp_registration_service,
            )

            # --- Story #724 Phase B1: optional post-generation verification pass ---
            # Load config lazily to avoid circular imports at module level.
            write_content = md_content
            verification_result = None
            try:
                from code_indexer.server.services.config_service import (
                    get_config_service,
                )
                from code_indexer.global_repos.dependency_map_analyzer import (
                    DependencyMapAnalyzer,
                )

                ci_config = get_config_service().get_config().claude_integration_config
                if ci_config is not None and ci_config.dep_map_fact_check_enabled:
                    _analyzer = DependencyMapAnalyzer(
                        golden_repos_root=Path(golden_repos_dir),
                        cidx_meta_path=cidx_meta_path,
                        pass_timeout=ci_config.dependency_map_pass_timeout_seconds,
                    )
                    _verif_started = time.monotonic()
                    verification_result = _analyzer.invoke_verification_pass(
                        document_content=md_content,
                        repo_list=[{"alias": repo_name, "clone_path": clone_path}],
                        discovery_mode=False,
                        claude_integration_config=ci_config,
                    )
                    _verif_duration_ms = int(
                        round((time.monotonic() - _verif_started) * 1000)
                    )
                    write_content = verification_result.verified_document
            except Exception as _verif_exc:
                logger.warning(
                    "Verification pass failed for %s (using original content): %s",
                    repo_name,
                    _verif_exc,
                )
                write_content = md_content
            # --- End verification pass ---

            md_file = cidx_meta_path / f"{repo_name}.md"
            atomic_write_description(md_file, write_content)

            # AC9: emit structured log after the single atomic write
            if verification_result is not None:
                diff_lines = list(
                    difflib.unified_diff(
                        md_content.splitlines(keepends=True),
                        write_content.splitlines(keepends=True),
                        fromfile="original",
                        tofile="verified",
                    )
                )
                truncated = len(diff_lines) > _AC9_DIFF_MAX_LINES
                diff_summary = "".join(diff_lines[:_AC9_DIFF_MAX_LINES])
                if truncated:
                    diff_summary += (
                        "\n... diff truncated (exceeded _AC9_DIFF_MAX_LINES)\n"
                    )
                logger.info(
                    "verification_pass",
                    extra={
                        "domain_or_repo": repo_name,
                        "counts": verification_result.counts,
                        "evidence": verification_result.evidence,
                        "diff_summary": diff_summary,
                        "duration_ms": _verif_duration_ms,
                        "fallback_reason": verification_result.fallback_reason,
                    },
                )

            logger.info(
                f"Created meta description file: {md_file} "
                f"(phase2_outcome={phase2_outcome})"
            )

        except Exception as e:
            logger.error(
                f"Failed to create meta description for {repo_name}: {e}", exc_info=True
            )
            # Fall back to README copy
            logger.info(f"Falling back to README copy for {repo_name}")
            _create_readme_fallback(Path(clone_path), repo_name, cidx_meta_path)

    # Trigger cidx-meta reindex to make the new description searchable
    if _refresh_scheduler is not None:
        try:
            _refresh_scheduler.trigger_refresh_for_repo("cidx-meta-global")
            logger.info(f"Triggered cidx-meta refresh after adding {repo_name}")
        except Exception as e:
            from code_indexer.server.repositories.background_jobs import (
                DuplicateJobError,
            )

            if isinstance(e, DuplicateJobError):
                if _debouncer is not None:
                    _debouncer.signal_dirty()
                    logger.info(
                        f"cidx-meta refresh deferred (debounced) for {repo_name}"
                    )
                else:
                    logger.warning(
                        f"cidx-meta refresh skipped for {repo_name}: no debouncer"
                    )
            else:
                logger.warning(
                    f"Failed to trigger cidx-meta refresh for {repo_name}: {e}"
                )


def on_repo_removed(repo_name: str, golden_repos_dir: str) -> None:
    """
    Hook called after a golden repository is successfully removed.

    Deletes the .md description file from cidx-meta and triggers reindex via RefreshScheduler.
    Also deletes the tracking record for description refresh (Story #190).

    Args:
        repo_name: Name/alias of the repository being removed
        golden_repos_dir: Path to golden-repos directory

    Note:
        - Handles nonexistent .md files gracefully (no crash)
        - Triggers cidx-meta reindex via RefreshScheduler if file was actually deleted
        - Deletes tracking record (if tracking backend available)
    """
    cidx_meta_path = Path(golden_repos_dir) / "cidx-meta"
    md_file = cidx_meta_path / f"{repo_name}.md"

    # Delete .md file if it exists
    if md_file.exists():
        try:
            md_file.unlink()
            logger.info(f"Deleted meta description file: {md_file}")

            # Trigger cidx-meta reindex after successful deletion
            if _refresh_scheduler is not None:
                try:
                    _refresh_scheduler.trigger_refresh_for_repo("cidx-meta-global")
                    logger.info(
                        f"Triggered cidx-meta refresh after removing {repo_name}"
                    )
                except Exception as e:
                    from code_indexer.server.repositories.background_jobs import (
                        DuplicateJobError,
                    )

                    if isinstance(e, DuplicateJobError):
                        if _debouncer is not None:
                            _debouncer.signal_dirty()
                            logger.info(
                                f"cidx-meta refresh deferred (debounced) for {repo_name}"
                            )
                        else:
                            logger.warning(
                                f"cidx-meta refresh skipped for {repo_name}: no debouncer"
                            )
                    else:
                        logger.warning(
                            f"Failed to trigger cidx-meta refresh for {repo_name}: {e}"
                        )

        except Exception as e:
            logger.error(
                f"Failed to delete meta description for {repo_name}: {e}", exc_info=True
            )
            # Don't crash the golden repo remove operation - log and continue
    else:
        logger.debug(f"No meta description file to delete for {repo_name}")

    # Delete tracking record (Story #190)
    if _tracking_backend is not None:
        try:
            _tracking_backend.delete_tracking(repo_name)
            logger.info(f"Deleted tracking record for {repo_name}")
        except Exception as e:
            # Don't block repo removal if tracking deletion fails
            logger.warning(
                f"Failed to delete tracking record for {repo_name}: {e}", exc_info=True
            )
    else:
        logger.debug(
            f"Tracking backend not available, skipping tracking record deletion for {repo_name}"
        )


def _find_readme(repo_path: Path) -> Optional[Path]:
    """
    Find README file in repository.

    Args:
        repo_path: Path to repository

    Returns:
        Path to README file if found, None otherwise

    Note:
        Checks README files in priority order defined by README_NAMES.
    """
    for readme_name in README_NAMES:
        readme_path = repo_path / readme_name
        if readme_path.exists():
            return readme_path
    return None


def _create_readme_fallback(
    repo_path: Path, alias: str, meta_dir: Path
) -> Optional[Path]:
    """
    Create README fallback file in meta directory.

    Args:
        repo_path: Path to repository
        alias: Repository alias/name
        meta_dir: Path to cidx-meta directory

    Returns:
        Path to created fallback file if README found, None otherwise

    Note:
        - Creates file named <alias>_README.md
        - Preserves original README content exactly
        - Called from on_repo_added() which triggers cidx-meta reindex
    """
    readme_path = _find_readme(repo_path)
    if readme_path is None:
        logger.warning(f"No README found in {repo_path} for fallback")
        return None

    # Create fallback file with <alias>_README.md naming
    fallback_path = meta_dir / f"{alias}_README.md"

    try:
        # Copy README content exactly
        content = readme_path.read_text(encoding="utf-8")
        fallback_path.write_text(content, encoding="utf-8")
        logger.info(f"Created README fallback: {fallback_path}")

        return fallback_path

    except Exception as e:
        logger.error(
            f"Failed to create README fallback for {alias}: {e}", exc_info=True
        )
        return None


def _generate_repo_description(
    repo_name: str,
    repo_url: str,
    clone_path: str,
    mcp_registration_service: Optional[Any] = None,
) -> tuple:
    """
    Generate .md file content for a repository using two-phase analysis.

    Phase 1: RepoAnalyzer extracts technologies, purpose, summary, features,
             and use cases.  Builds a frontmatter dict and markdown body.

    Phase 2: invoke_lifecycle_detection runs lifecycle detection via Claude
             CLI and returns a parsed dict (or None on failure).  The result
             is merged into the frontmatter dict before serialisation.

    Args:
        repo_name: Repository name/alias
        repo_url: Repository URL
        clone_path: Path to cloned repository
        mcp_registration_service: Optional MCPSelfRegistrationService; when
            not None, ensure_registered() is called before Phase 2.

    Returns:
        Tuple of (content_str, phase2_outcome) where phase2_outcome is one
        of "success" | "failed_degraded_to_unknown".
    """
    now = datetime.now(timezone.utc).isoformat()

    # --- MCP registration (before Phase 2) ---
    if mcp_registration_service is not None:
        mcp_registration_service.ensure_registered()
    else:
        logger.warning(
            "MCPSelfRegistrationService not wired; skipping ensure_registered(). "
            "Phase 2 MCP access not guaranteed."
        )

    # --- Phase 1: extract repo info ---
    analyzer = RepoAnalyzer(clone_path)
    info = analyzer.extract_info()

    frontmatter_dict: dict = {
        "name": repo_name,
        "url": repo_url,
        "technologies": list(info.technologies),
        "purpose": info.purpose,
        "last_analyzed": now,
    }

    # Build markdown body
    body = f"\n# {repo_name}\n\n{info.summary}\n\n**Repository URL**: {repo_url}\n"
    if info.features:
        body += "\n## Features\n\n"
        for feat in info.features[:10]:
            body += f"- {feat}\n"
    if info.use_cases:
        body += "\n## Use Cases\n\n"
        for uc in info.use_cases[:5]:
            body += f"- {uc}\n"

    # --- Phase 2: lifecycle detection ---
    lifecycle_result = invoke_lifecycle_detection(clone_path)

    frontmatter_dict["lifecycle_schema_version"] = LIFECYCLE_SCHEMA_VERSION
    if lifecycle_result is not None:
        frontmatter_dict["lifecycle"] = lifecycle_result["lifecycle"]
        phase2_outcome = "success"
    else:
        frontmatter_dict["lifecycle"] = {
            "branches_to_env": {},
            "detected_sources": [],
            "confidence": "unknown",
            "claude_notes": "Phase 2 lifecycle detection did not return a result.",
        }
        phase2_outcome = "failed_degraded_to_unknown"

    # --- Serialize ---
    fm_text = yaml.safe_dump(
        frontmatter_dict, default_flow_style=False, sort_keys=False
    )
    content = "---\n" + fm_text + "---\n" + body

    return content, phase2_outcome
