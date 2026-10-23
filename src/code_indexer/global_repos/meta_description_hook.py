"""
Meta description lifecycle hooks for golden repositories.

Provides hooks that automatically create/delete .md files in cidx-meta
when golden repos are added/removed, eliminating the need for special-case
meta directory management code.
"""

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from code_indexer.global_repos.catalog_staged_write import (
    SHARED_CATALOG_FILE_MODE,
    write_catalog_file,
)
from code_indexer.global_repos.cidx_meta_refresh_debouncer import (  # noqa: F401
    _DEFAULT_DEBOUNCE_SECONDS,
    _MAX_RETRY_INTERVAL_SECONDS,
    CidxMetaRefreshDebouncer,
    ran_in_process,
)
from code_indexer.global_repos.repo_analyzer import RepoAnalyzer
from code_indexer.utils.credential_redaction import mask_url_credentials
from code_indexer.global_repos.write_lock_manager import (
    describe_scheduler_lock_holder,
)
from code_indexer.server.services.claude_cli_manager import (
    ClaudeCliManager,
    get_claude_cli_manager,
)

logger = logging.getLogger(__name__)

# Bug #1842 AC3: small, FIXED retry bound for transient cidx-meta
# write-lock micro-contention -- never unbounded (Messi Rule #14). See
# _acquire_cidx_meta_lock_with_bounded_retry's docstring for the
# rationale and its explicit scope limits.
_LOCK_ACQUIRE_MAX_ATTEMPTS = 3
_LOCK_ACQUIRE_RETRY_DELAY_SECONDS = 0.05


def _describe_cidx_meta_holder(refresh_scheduler: Optional[Any]) -> str:
    """
    Best-effort description of who REALLY holds the 'cidx-meta' write
    lock, for use in contention diagnostics (Bug #1842 AC4).

    Before this helper existed, both atomic_write_description()'s
    LifecycleLockUnavailableError and on_repo_removed()'s lock-contention
    message hardcoded "(owner='lifecycle_writer')" -- the
    FAILED CALLER's own attempted identity, not the actual holder (e.g.
    'dependency_map_service' running a real multi-domain Claude CLI
    analysis). Delegates to the shared describe_scheduler_lock_holder()
    (write_lock_manager.py) for the real owner + hold duration -- never
    raises.
    """
    return describe_scheduler_lock_holder(refresh_scheduler, "cidx-meta")


def _acquire_cidx_meta_lock_with_bounded_retry(
    refresh_scheduler: Any, owner_name: str
) -> bool:
    """
    Attempt to acquire the 'cidx-meta' write lock, retrying a SMALL,
    FIXED number of times on failure (Bug #1842 AC3).

    Scope: this addresses transient MICRO-contention -- two callers
    racing to acquire the same lock within a fraction of a second (e.g.
    two on_repo_added() calls in rapid succession), where the real
    holder releases well within this bound. It does NOT and cannot help
    with the dominant AC3 scenario (a genuinely long hold, e.g. the
    lifecycle preflight's real Claude CLI calls, which can run for
    minutes) -- that case correctly exhausts this bound and returns
    False exactly as before, now with an honest diagnostic (AC4) naming
    the real holder. This is a fixed attempt count with a short fixed
    delay between attempts -- never an unbounded or time-based wait
    (Messi Rule #14). Deliberately does NOT narrow any lock's hold
    window elsewhere in the system (see dependency_map_service.py and
    the bug's final report for why that would reopen the
    RefreshScheduler/cidx-meta race Bug #1506 fixed).
    """
    for attempt in range(_LOCK_ACQUIRE_MAX_ATTEMPTS):
        if refresh_scheduler.acquire_write_lock("cidx-meta", owner_name=owner_name):
            return True
        if attempt < _LOCK_ACQUIRE_MAX_ATTEMPTS - 1:
            time.sleep(_LOCK_ACQUIRE_RETRY_DELAY_SECONDS)
    return False


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


def request_cidx_meta_refresh(
    refresh_scheduler: Any, debouncer: Optional[Any] = None
) -> None:
    """Trigger a cidx-meta refresh for a writer; never raises into the writer
    (callers run it in ``finally`` blocks). The submitted job is handed to
    the debouncer (*debouncer*, else the module's), which owes the write
    until the job covers it (the job may still skip, e.g. on another
    writer's lock). A refresh already queued or running may have listed the
    catalog before the write, so it too is handed to the debouncer, which
    owes the refresh that follows. A refresh deferred by the failure
    backoff was persisted as a pending trigger, which the refresh scheduler
    fires once the backoff ends. Any other failure (store or job tracker
    unavailable) is logged at ERROR and handed to the debouncer for a
    retry."""
    from code_indexer.global_repos.refresh_failure_recovery import (
        RefreshDeferredError,
    )
    from code_indexer.server.repositories.background_jobs import DuplicateJobError
    from code_indexer.server.services.job_tracker import (
        DuplicateJobError as TrackedDuplicateJobError,
    )

    from code_indexer.global_repos.catalog_pending_changes import (
        apply_pending_catalog_changes,
    )

    owed_to = debouncer if debouncer is not None else _debouncer
    try:
        parked_settled = apply_pending_catalog_changes(refresh_scheduler)
    except Exception as exc:
        logger.warning(f"Parked catalog changes not applied: {exc!r}")
        parked_settled = False
    if not parked_settled and owed_to is not None:
        owed_to.signal_dirty()
    seq = owed_to.note_direct_write() if owed_to is not None else 0
    try:
        job_id = refresh_scheduler.trigger_refresh_for_repo("cidx-meta-global")
        if owed_to is not None and job_id is not None:
            owed_to.track_submission(job_id, seq)
        elif owed_to is not None and not ran_in_process(refresh_scheduler):
            owed_to.signal_dirty()
    except RefreshDeferredError as exc:
        logger.info(
            f"cidx-meta refresh deferred until {exc.backoff_until:.0f}: "
            f"{exc.reason or 'persisted failure backoff'}; "
            f"the refresh scheduler fires it then"
        )
    except (DuplicateJobError, TrackedDuplicateJobError) as exc:
        if owed_to is None:
            logger.warning(f"cidx-meta refresh not retried (no debouncer): {exc}")
            return
        logger.debug(f"cidx-meta refresh debounced for retry: {exc}")
        owed_to.signal_dirty()
    except Exception as exc:
        logger.error(
            f"cidx-meta refresh request failed (the write itself succeeded; "
            f"{'retried by the debouncer' if owed_to else 'not retried'}): "
            f"{type(exc).__name__}: {exc}"
        )
        if owed_to is not None:
            owed_to.signal_dirty()


def owe_cidx_meta_refresh(debouncer: Optional[Any] = None) -> None:
    """Owe a cidx-meta refresh for a write made while the catalog write lock
    was still held (another writer's, the refresh's own publish lock, or ours
    after a failed release). A refresh requested now would skip on that lock
    and owe nothing, so the debouncer (*debouncer*, else the module's)
    carries it to a later refresh."""
    owed_to = debouncer if debouncer is not None else _debouncer
    if owed_to is None:
        logger.warning(
            "cidx-meta write made under a held catalog write lock is not "
            "retried (no debouncer); it is indexed only by a later cidx-meta "
            "refresh"
        )
        return
    owed_to.signal_dirty()


def refresh_after_catalog_write(
    refresh_scheduler: Any, lock_still_held: bool, debouncer: Optional[Any] = None
) -> None:
    """The refresh a completed catalog write needs, for every writer:
    requested now once the writer released its own lock, owed to the
    debouncer while a lock is still held. Never raises into the writer."""
    if lock_still_held:
        owe_cidx_meta_refresh(debouncer)
        return
    request_cidx_meta_refresh(refresh_scheduler, debouncer=debouncer)


def atomic_write_description(
    target_path: Path,
    content: str,
    refresh_scheduler: Optional[Any] = None,
) -> bool:
    """
    Write content to target_path atomically.

    ``catalog_staged_write.write_catalog_file`` stages the temp file in a
    hidden sibling of the real cidx-meta directory, fsyncs it, moves it in
    with ``os.replace``: neither a snapshot copy nor the backup sync's
    ``git add -A`` of cidx-meta can see an unfinished file. If
    refresh_scheduler is provided, acquires the cidx-meta write lock before
    the write and releases it afterwards. Lock acquisition failure propagates
    to the caller.

    Args:
        target_path: Destination Path object.
        content: Text content to write (UTF-8).
        refresh_scheduler: Optional scheduler exposing acquire_write_lock /
            release_write_lock.  Skipped when None.

    Returns:
        True when a refresh may be requested now; False when this call's own
        lock release failed (the lock may still be held, so a refresh
        requested now would skip) or the catalog directory could not be
        fsync'd after the description was put in place -- either way the
        caller owes the refresh to the debouncer.

    Raises:
        LifecycleLockUnavailableError: If refresh_scheduler is provided and
            acquire_write_lock returns False (another writer already holds
            the cidx-meta write lock).
        Any exception from file write or os.replace.
    """
    from code_indexer.global_repos.lifecycle_batch_runner import (
        LIFECYCLE_WRITER_LOCK_OWNER,
        LifecycleLockUnavailableError,
    )

    lock_acquired = False
    if refresh_scheduler is not None:
        lock_acquired = _acquire_cidx_meta_lock_with_bounded_retry(
            refresh_scheduler, LIFECYCLE_WRITER_LOCK_OWNER
        )
        if not lock_acquired:
            raise LifecycleLockUnavailableError(
                "Could not acquire write lock for 'cidx-meta' "
                f"(attempted as owner='{LIFECYCLE_WRITER_LOCK_OWNER}'); "
                f"{_describe_cidx_meta_holder(refresh_scheduler)} -- retry later"
            )

    released = True
    durable = True
    try:
        durable = write_catalog_file(
            target_path.parent, target_path, content, mode=SHARED_CATALOG_FILE_MODE
        )
        from code_indexer.global_repos.catalog_pending_changes import discard_pending

        # ^ invariant a direct write supersedes any parked change for the alias
        discard_pending(target_path.parent.parent, target_path.stem)
    finally:
        if lock_acquired:
            released = _release_cidx_meta_lock(refresh_scheduler)
    return released and durable


def _release_cidx_meta_lock(refresh_scheduler: Any) -> bool:
    """Release the lifecycle writer's cidx-meta lock; False (logged) when the
    release failed or raised, i.e. the lock may still be held."""
    from code_indexer.global_repos.lifecycle_batch_runner import (
        LIFECYCLE_WRITER_LOCK_OWNER,
    )

    try:
        released = bool(
            refresh_scheduler.release_write_lock(
                "cidx-meta", owner_name=LIFECYCLE_WRITER_LOCK_OWNER
            )
        )
    except Exception as exc:
        logger.warning(
            "cidx-meta write lock release raised (%s: %s); the refresh is "
            "owed to the debouncer",
            type(exc).__name__,
            exc,
        )
        return False
    if not released:
        logger.warning(
            "cidx-meta write lock release failed for lifecycle_writer; the "
            "refresh is owed to the debouncer"
        )
    return released


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

    # v10.4.13: Claude CLI is REQUIRED for description generation.
    # No README fallback — silent terse output masks misconfiguration.
    # If CLI is unavailable, raise so the caller logs ERROR and skips writing
    # any description. Admin retries when CLI is restored. Per Messi Rule #2
    # (anti-fallback): graceful failure over forced success.
    if cli_manager is None or not cli_manager.check_cli_available():
        reason = (
            "ClaudeCliManager not initialized at startup"
            if cli_manager is None
            else "Claude CLI not available on PATH"
        )
        raise RuntimeError(
            f"Cannot generate description for {repo_name}: {reason}. "
            f"NOT writing any stub or static fallback per v10.4.13 anti-fallback "
            f"contract. Description will be missing until the underlying issue is "
            f"fixed; admin can retry generation when ClaudeCliManager / Claude CLI "
            f"is restored."
        )

    # Generate .md file using Claude CLI (Phase 1 analysis only;
    # lifecycle detection handled separately by LifecycleBatchRunner).
    # cli_manager passed through so RepoAnalyzer invokes Claude (rich output)
    # instead of falling back to static regex extraction (terse output).
    md_content = _generate_repo_description(
        repo_name,
        repo_url,
        clone_path,
        cli_manager,
    )

    # Story #724 v2: optional post-generation verification pass BEFORE atomic write
    from code_indexer.server.services.config_service import (
        get_config_service,
    )

    ci_config = get_config_service().get_config().claude_integration_config
    verified_content = md_content  # default: unverified content when flag is off

    if ci_config is not None and ci_config.dep_map_fact_check_enabled:
        import tempfile

        from code_indexer.global_repos.dependency_map_analyzer import (
            DependencyMapAnalyzer,
        )

        from code_indexer.global_repos.catalog_staged_write import STAGING_DIR_NAME

        # The verification input lives in the staging dir beside the catalog,
        # never inside it: a backup stage-all must not see an unverified file.
        staging_dir = Path(golden_repos_dir) / STAGING_DIR_NAME
        staging_dir.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix=f".{repo_name}.verify.",
            suffix=".md",
            dir=str(staging_dir),
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(md_content)
            tmp_path = Path(tmp.name)

        try:
            _analyzer = DependencyMapAnalyzer(
                golden_repos_root=Path(golden_repos_dir),
                cidx_meta_path=cidx_meta_path,
                pass_timeout=ci_config.dependency_map_pass_timeout_seconds,
            )
            success = _analyzer.invoke_verification_pass(
                document_path=tmp_path,
                repo_list=[{"alias": repo_name, "clone_path": clone_path}],
                config=ci_config,
            )
            if success:
                verified_content = tmp_path.read_text(encoding="utf-8")
            else:
                logger.warning(
                    "Verification failed for meta description %s — using unverified content",
                    repo_name,
                )
        except Exception:
            logger.warning(
                "Verification pass raised for meta description %s",
                repo_name,
                exc_info=True,
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    # Single atomic write with (verified or original) content — always ONCE
    # (the content is generated once, above).
    put_repo_description(golden_repos_dir, repo_name, verified_content)


def put_repo_description(golden_repos_dir: str, repo_name: str, content: str) -> None:
    """Put *repo_name*'s description in cidx-meta through catalog admission
    (taking the cidx-meta write lock when free -- Bug #1506 7th-pass review:
    the module-level _refresh_scheduler is forwarded -- else under a
    cooperative holder, else parked until an exclusive holder releases),
    then request or owe the catalog refresh."""
    from code_indexer.global_repos.lifecycle_batch_runner import (
        LifecycleLockUnavailableError,
    )

    md_file = Path(golden_repos_dir) / "cidx-meta" / f"{repo_name}.md"
    # ^ invariant filename is the SHORT alias, never "-global" (v10.4.9 broke 10 read paths)
    verified_content = content
    try:
        refresh_now = atomic_write_description(
            md_file, verified_content, refresh_scheduler=_refresh_scheduler
        )
    except LifecycleLockUnavailableError as lock_busy:
        # The lock stayed busy past the bounded retry. The description is
        # never discarded: under a cooperative holder (a refresh, the
        # dependency map...) it is written atomically now; under an
        # exclusive one (write mode, the catalog backup) it is parked
        # outside the catalog and applied once released. Either way the
        # refresh is owed to the debouncer (one requested now would skip).
        from code_indexer.global_repos.catalog_lock_holder import (
            CatalogLockedExclusively,
            admitted_catalog_write,
        )
        from code_indexer.global_repos.catalog_pending_changes import (
            defer_description,
        )
        from code_indexer.global_repos.lifecycle_batch_runner import (
            LIFECYCLE_WRITER_LOCK_OWNER,
        )

        try:
            with admitted_catalog_write(
                _refresh_scheduler, LIFECYCLE_WRITER_LOCK_OWNER
            ):
                atomic_write_description(
                    md_file, verified_content, refresh_scheduler=None
                )
        except CatalogLockedExclusively as refused:
            defer_description(golden_repos_dir, repo_name, verified_content)
            logger.info(
                f"Parked the description of {repo_name}: {refused}; it is "
                f"applied once released"
            )
        else:
            logger.info(
                f"Created meta description file {md_file} while the cidx-meta "
                f"write lock was held ({lock_busy}); the refresh is owed to the "
                f"debouncer"
            )
        refresh_now = False
    else:
        logger.info(f"Created meta description file: {md_file}")

    # Trigger cidx-meta reindex to make the new description searchable, once
    # no lock of ours can still be held.
    if _refresh_scheduler is not None:
        refresh_after_catalog_write(_refresh_scheduler, lock_still_held=not refresh_now)


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
    # INVARIANT: cidx-meta descriptions use the SHORT repo alias as filename:
    #   {short_alias}.md  (e.g., JSqlParser.md)
    # The "-global" suffix belongs to the registry alias_name, NOT the filename.
    # DO NOT change this to use "-global" in the filename -- v10.4.9 did this
    # and broke 10 read paths, the UI, and access filtering. Fixed in v10.7.x.
    md_file = cidx_meta_path / f"{repo_name}.md"

    from code_indexer.global_repos.catalog_pending_changes import (
        discard_pending,
        pending_operation,
    )

    # Delete the .md file -- also when only a parked add exists for it: a
    # removal supersedes any pending change for the alias.
    if md_file.exists() or pending_operation(golden_repos_dir, repo_name):
        _lock_acquired = False
        # True while a refresh requested now would skip on a held lock: the
        # lock stayed busy past the bounded retry (any holder), or our own
        # release failed. The deletion is never skipped; the refresh is then
        # owed to the debouncer.
        _lock_still_held = False
        # True when an exclusive holder had the catalog: the removal is parked.
        _parked = False
        if _refresh_scheduler is not None:
            from code_indexer.global_repos.lifecycle_batch_runner import (
                LIFECYCLE_WRITER_LOCK_OWNER,
            )

            try:
                _lock_acquired = _acquire_cidx_meta_lock_with_bounded_retry(
                    _refresh_scheduler, LIFECYCLE_WRITER_LOCK_OWNER
                )
            except Exception as _lock_err:
                logger.warning(
                    "on_repo_removed: could not acquire write lock: %s", _lock_err
                )
            if not _lock_acquired:
                _lock_still_held = True
                from code_indexer.global_repos.catalog_lock_holder import (
                    CatalogLockedExclusively,
                    admit_catalog_write,
                )
                from code_indexer.global_repos.catalog_pending_changes import (
                    defer_removal,
                )

                try:
                    _lock_acquired = admit_catalog_write(
                        _refresh_scheduler, LIFECYCLE_WRITER_LOCK_OWNER
                    )
                except CatalogLockedExclusively as refused:
                    defer_removal(golden_repos_dir, repo_name)
                    logger.info(
                        f"Parked the removal of {md_file}: {refused}; it is "
                        f"applied once released"
                    )
                    refresh_after_catalog_write(_refresh_scheduler, True)
                    _parked = True
                else:
                    _lock_still_held = not _lock_acquired
                    if _lock_still_held:
                        logger.info(
                            "on_repo_removed: deleting %s while the cidx-meta "
                            "write lock is held -- %s; the refresh is owed to "
                            "the debouncer",
                            md_file,
                            _describe_cidx_meta_holder(_refresh_scheduler),
                        )
        _deleted = False
        try:
            if not _parked:
                md_file.unlink(missing_ok=True)
                discard_pending(golden_repos_dir, repo_name)
                _deleted = True
                logger.info(f"Deleted meta description file: {md_file}")
        except Exception as e:
            logger.error(
                f"Failed to delete meta description for {repo_name}: {e}", exc_info=True
            )
            # Don't crash the golden repo remove operation - log and continue
        finally:
            if _lock_acquired and not _release_cidx_meta_lock(_refresh_scheduler):
                _lock_still_held = True

        # Trigger cidx-meta reindex after the deletion, once the write lock is
        # released (a refresh starting under the lock skips and owes nothing).
        if _deleted and _refresh_scheduler is not None:
            refresh_after_catalog_write(_refresh_scheduler, _lock_still_held)
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


def _generate_repo_description(
    repo_name: str,
    repo_url: str,
    clone_path: str,
    cli_manager: ClaudeCliManager,
) -> str:
    """
    Generate .md file content for a repository using Phase 1 analysis only.

    Phase 1: RepoAnalyzer extracts technologies, purpose, summary, features,
             and use cases via Claude CLI.  Builds a frontmatter dict and
             markdown body.

    Lifecycle detection is handled separately by LifecycleBatchRunner
    (Story #876 unified pipeline).

    Args:
        repo_name: Repository name/alias
        repo_url: Repository URL
        clone_path: Path to cloned repository
        cli_manager: ClaudeCliManager instance (REQUIRED — v10.4.13).
            Passed to RepoAnalyzer so the rich Claude CLI extraction path
            runs instead of falling back to terse static regex extraction.
            Caller verified availability before calling.

    Raises:
        TypeError: if cli_manager is None or not a ClaudeCliManager instance.
            Anti-fallback contract: refuse to silently produce terse output.

    Returns:
        Content string (YAML frontmatter + markdown body).
    """
    if not isinstance(cli_manager, ClaudeCliManager):
        raise TypeError(
            f"_generate_repo_description requires a ClaudeCliManager instance "
            f"(v10.4.13 anti-fallback contract). Got {type(cli_manager).__name__!r}. "
            f"Caller must verify ClaudeCliManager is initialized AND CLI is "
            f"available before calling — see on_repo_added's guard at line 348."
        )

    now = datetime.now(timezone.utc).isoformat()

    # --- Phase 1: extract repo info ---
    # v10.4.13: pass cli_manager so RepoAnalyzer invokes Claude CLI for rich
    # output. Without this, RepoAnalyzer falls back to static regex extraction
    # capped at 10 features + 5 use cases — produces terse descriptions.
    analyzer = RepoAnalyzer(clone_path, claude_cli_manager=cli_manager)
    info = analyzer.extract_info()

    # The description is indexed in cidx-meta and globally queryable, so the
    # repository URL is written with its userinfo redacted.
    display_url = mask_url_credentials(repo_url)
    frontmatter_dict: dict = {
        "name": repo_name,
        "url": display_url,
        "technologies": list(info.technologies),
        "purpose": info.purpose,
        "last_analyzed": now,
    }

    # Build markdown body
    body = f"\n# {repo_name}\n\n{info.summary}\n\n**Repository URL**: {display_url}\n"
    if info.features:
        body += "\n## Features\n\n"
        for feat in info.features[:10]:
            body += f"- {feat}\n"
    if info.use_cases:
        body += "\n## Use Cases\n\n"
        for uc in info.use_cases[:5]:
            body += f"- {uc}\n"

    # --- Serialize ---
    fm_text = yaml.safe_dump(
        frontmatter_dict, default_flow_style=False, sort_keys=False
    )
    content = "---\n" + fm_text + "---\n" + body

    return content
