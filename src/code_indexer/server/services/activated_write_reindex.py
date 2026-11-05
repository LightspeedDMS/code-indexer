"""Reindex an activated copy after cidx itself changes it (mount-less pods).

With ``content_backend: local`` an activated copy is on this pod's disk and the
file tools start auto-watch (``auto_watch_manager.start_watch``) before they
write: the watcher sees the change and re-indexes it. Nothing here runs then.

With ``content_backend: delegated`` the copy lives on workspace-mcp's storage,
there is nothing on this pod to watch (``start_watch`` answers "disabled"), and
workspace-mcp has no change feed. So after cidx's OWN successful write -- the
file tools (create/edit/delete) and the git tools that move the working tree
(merge, merge abort, branch switch, checkout file, reset, clean, pull, stash
push/pop/apply) -- this module enqueues an incremental reindex of the copy through
``ActivatedRepoIndexManager.trigger_reindex`` (``clear=False``): the indexer's
own change detection (the content backend's manifest under delegated) picks up
the touched paths. ``trigger_reindex`` is then not needed after cidx's edits.
A merge or pull that stops on conflicts has rewritten the tree too (cleanly
merged files updated, conflicted ones holding markers), and a stash pop/apply
that fails may have; those are reindexed as well (see ``tree_changed``).

Per copy, writes coalesce:

* a reindex job for the copy is PENDING -> it has not read the copy yet, so it
  will see this write: ``coalesced`` (its job id);
* a reindex job for the copy (or, as ``trigger_reindex`` allows one per user,
  any of the user's copies) is RUNNING -> it may already have read past the
  written paths: a follow-up is scheduled for when it is done: ``deferred``;
* otherwise a new job: ``queued`` (its job id).

Fail loud: the outcome is returned to the tool, which adds it to its response
as ``reindex``; ``failed`` carries the error and is logged at ERROR
(SVC-REINDEX-001). A follow-up that never gets a slot is logged at ERROR too
(SVC-REINDEX-002) -- never dropped silently. Every enqueue is gated on the
registry: the copy must be one of THIS user's activated repositories.

Edits made OUTSIDE cidx (a git push to the copy's remote does not touch the
copy; a process writing workspace-mcp's storage directly does) are NOT seen:
they reach the index only through ``trigger_reindex``, ``sync_repository`` or
the next reindex this module enqueues for a later cidx write (whose
incremental pass picks up every change since the last one).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

#: Index types rebuilt after a cidx write. Temporal is refused for activated
#: copies by trigger_reindex; SCIP is a whole-project build left to an explicit
#: trigger_reindex.
REINDEX_TYPES_AFTER_WRITE: Tuple[str, ...] = ("semantic", "fts")

#: Seconds between attempts to place a deferred follow-up, and how many.
RETRY_DELAY_SECONDS = 15.0
MAX_RETRY_ATTEMPTS = 240

#: trigger_reindex's refusal when the user already has a reindex job.
_BUSY_MARKER = "already running/pending"

_REINDEX_OPERATION = "reindex"
_JOB_LIST_LIMIT = 100

Outcome = Dict[str, Any]
Scheduler = Callable[[float, Callable[[], None]], None]


def _daemon_timer(delay: float, func: Callable[[], None]) -> None:
    timer = threading.Timer(delay, func)
    timer.daemon = True
    timer.start()


def _default_delegated() -> bool:
    from code_indexer.server.startup.content_backend_wiring import (
        is_content_seam_delegated,
    )

    return is_content_seam_delegated()


def _default_activated_repo_manager() -> Any:
    from code_indexer.server.mcp.handlers import _utils

    manager = _utils._get_activated_repo_manager()
    if manager is None:
        raise RuntimeError("activated_repo_manager not initialized on app.state")
    return manager


def _default_index_manager() -> Any:
    from code_indexer.server.mcp.handlers import _utils
    from code_indexer.server.services.activated_repo_index_manager import (
        ActivatedRepoIndexManager,
    )

    background_job_manager = _utils.app_module.background_job_manager
    if background_job_manager is None:
        raise RuntimeError(
            "Server not fully initialized: background_job_manager unavailable"
        )
    return ActivatedRepoIndexManager(
        background_job_manager=background_job_manager,
        activated_repo_manager=_default_activated_repo_manager(),
    )


class ActivatedWriteReindexer:
    """Enqueues the reindex an activated copy needs after a cidx write."""

    def __init__(
        self,
        *,
        delegated: Callable[[], bool] = _default_delegated,
        activated_repo_manager: Callable[[], Any] = _default_activated_repo_manager,
        index_manager: Callable[[], Any] = _default_index_manager,
        scheduler: Scheduler = _daemon_timer,
        retry_delay: float = RETRY_DELAY_SECONDS,
        max_attempts: int = MAX_RETRY_ATTEMPTS,
    ) -> None:
        self._delegated = delegated
        self._activated_repo_manager = activated_repo_manager
        self._index_manager = index_manager
        self._scheduler = scheduler
        self._retry_delay = retry_delay
        self._max_attempts = max_attempts
        self._lock = threading.Lock()
        # (username, alias) -> paths written since the last enqueue that will
        # see them; present while a follow-up is scheduled.
        self._deferred: Dict[Tuple[str, str], Set[str]] = {}

    def after_write(
        self,
        username: str,
        repo_alias: str,
        paths: Optional[Iterable[str]] = None,
    ) -> Optional[Outcome]:
        """Enqueue (or coalesce) the copy's reindex after a successful write.

        Returns None when there is nothing to do here: the local content
        backend (auto-watch re-indexes), or a golden repository in write mode
        (its refresh on exit_write_mode re-indexes). Otherwise the outcome.
        ``paths`` are the repository-relative paths written, None for a git
        operation that may have moved any of them.
        """
        if not self._delegated():
            return None
        if repo_alias.endswith("-global") or self._is_write_exception(repo_alias):
            return None
        touched = sorted(paths) if paths is not None else ["*"]
        try:
            if not self._activated_repo_manager().user_has_activated_repo(
                username, repo_alias
            ):
                raise PermissionError(
                    f"'{repo_alias}' is not an activated repository of "
                    f"user '{username}'"
                )
            return self._enqueue(username, repo_alias, touched)
        except Exception as e:
            return self._failed(username, repo_alias, touched, e)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _is_write_exception(repo_alias: str) -> bool:
        from code_indexer.server.services.file_crud_service import file_crud_service

        return bool(file_crud_service.is_write_exception(repo_alias))

    def _enqueue(self, username: str, repo_alias: str, touched: List[str]) -> Outcome:
        manager = self._index_manager()
        active = self._active_reindex_jobs(manager, username)
        for job in active:
            if job.get("repo_alias") == repo_alias and job.get("status") == "pending":
                return self._outcome("coalesced", touched, job_id=job.get("job_id"))
        if any(job.get("status") == "running" for job in active):
            return self._defer(username, repo_alias, touched)
        try:
            job_id = manager.trigger_reindex(
                repo_alias=repo_alias,
                index_types=list(REINDEX_TYPES_AFTER_WRITE),
                clear=False,
                username=username,
            )
        except ValueError as e:
            if _BUSY_MARKER not in str(e):
                raise
            return self._defer(username, repo_alias, touched)
        logger.info(
            "Reindex job %s queued for %s/%s after a write (%d path(s))",
            job_id,
            username,
            repo_alias,
            len(touched),
        )
        return self._outcome("queued", touched, job_id=job_id)

    @staticmethod
    def _active_reindex_jobs(manager: Any, username: str) -> List[Dict[str, Any]]:
        jobs: List[Dict[str, Any]] = []
        for status in ("pending", "running"):
            listing = manager.background_job_manager.list_jobs(
                username=username, status_filter=status, limit=_JOB_LIST_LIMIT
            )
            jobs.extend(
                job
                for job in listing.get("jobs", [])
                if job.get("operation_type") == _REINDEX_OPERATION
            )
        return jobs

    def _defer(self, username: str, repo_alias: str, touched: List[str]) -> Outcome:
        key = (username, repo_alias)
        with self._lock:
            already = key in self._deferred
            self._deferred.setdefault(key, set()).update(touched)
        if not already:
            self._schedule_retry(username, repo_alias, attempt=1)
        return self._outcome("deferred", touched)

    def _schedule_retry(self, username: str, repo_alias: str, attempt: int) -> None:
        self._scheduler(
            self._retry_delay,
            lambda: self._retry(username, repo_alias, attempt),
        )

    def _retry(self, username: str, repo_alias: str, attempt: int) -> None:
        key = (username, repo_alias)
        with self._lock:
            touched = sorted(self._deferred.get(key, set()))
        try:
            manager = self._index_manager()
            active = self._active_reindex_jobs(manager, username)
            pending = [
                job
                for job in active
                if job.get("repo_alias") == repo_alias
                and job.get("status") == "pending"
            ]
            if pending:
                self._settle(key)
                return
            busy = any(job.get("status") == "running" for job in active)
            if not busy:
                try:
                    manager.trigger_reindex(
                        repo_alias=repo_alias,
                        index_types=list(REINDEX_TYPES_AFTER_WRITE),
                        clear=False,
                        username=username,
                    )
                    self._settle(key)
                    return
                except ValueError as e:
                    if _BUSY_MARKER not in str(e):
                        raise
        except Exception as e:
            self._settle(key)
            self._failed(username, repo_alias, touched, e)
            return
        if attempt >= self._max_attempts:
            self._settle(key)
            logger.error(
                format_error_log(
                    "SVC-REINDEX-002",
                    f"Gave up re-indexing {username}/{repo_alias} after a write: "
                    f"another reindex stayed busy for {attempt} attempts; "
                    f"{len(touched)} written path(s) are not indexed until the "
                    f"next reindex (trigger_reindex)",
                )
            )
            return
        self._schedule_retry(username, repo_alias, attempt + 1)

    def _settle(self, key: Tuple[str, str]) -> None:
        with self._lock:
            self._deferred.pop(key, None)

    def _failed(
        self, username: str, repo_alias: str, touched: List[str], error: Exception
    ) -> Outcome:
        message = f"{type(error).__name__}: {error}"
        logger.error(
            format_error_log(
                "SVC-REINDEX-001",
                f"Could not enqueue the reindex of {username}/{repo_alias} after "
                f"a write ({len(touched)} path(s)): {message}",
            )
        )
        return self._outcome("failed", touched, error=message)

    @staticmethod
    def _outcome(status: str, touched: List[str], **extra: Any) -> Outcome:
        outcome: Outcome = {"status": status, "paths": touched}
        outcome.update({k: v for k, v in extra.items() if v is not None})
        return outcome


_REINDEXER: Optional[ActivatedWriteReindexer] = None
_REINDEXER_LOCK = threading.Lock()


def activated_write_reindexer() -> ActivatedWriteReindexer:
    """The process-wide reindexer (created on first use, no import side effect)."""
    global _REINDEXER
    with _REINDEXER_LOCK:
        if _REINDEXER is None:
            _REINDEXER = ActivatedWriteReindexer()
        return _REINDEXER


def tree_changed(result: Any) -> bool:
    """Whether the operation behind ``result`` changed the working tree.

    True after a successful operation (``success`` is True) and after a merge
    or pull that stopped on conflicts (``success`` False with a non-empty
    ``conflicts`` list): git has already rewritten the tree then.
    """
    if not isinstance(result, dict):
        return False
    return result.get("success") is True or bool(result.get("conflicts"))


def attach_reindex_outcome(
    result: Any,
    username: str,
    repo_alias: str,
    paths: Optional[Iterable[str]] = None,
    *,
    changed: Optional[bool] = None,
) -> Any:
    """``result`` with the after-write reindex outcome under ``reindex``.

    Only when the operation changed the working tree: ``changed`` when given,
    else ``tree_changed(result)``. ``result`` must be a dict to carry the
    outcome. Unchanged (the same object, no key added) when nothing is to be
    done -- always the case under the local content backend. A REST response
    model drops the key; a failure is logged at ERROR either way.
    """
    if not isinstance(result, dict):
        return result
    if not (tree_changed(result) if changed is None else changed):
        return result
    outcome = activated_write_reindexer().after_write(username, repo_alias, paths)
    if outcome is not None:
        result["reindex"] = outcome
    return result
