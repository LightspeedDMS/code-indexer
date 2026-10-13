"""
SCIP Resolution Queue.

Manages serialized FIFO queue for processing SCIP project resolution attempts
one at a time to prevent conflicts. Part of Story #645 AC4.
"""

import asyncio
import logging
import queue as _queue_mod
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from code_indexer.scip.indexers.base import DELEGATED_BACKPRESSURE_MARKER
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.middleware.correlation import get_correlation_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from code_indexer.scip.generator import GenerationResult

logger = logging.getLogger(__name__)

# Queue worker poll timeout (seconds)
WORKER_POLL_TIMEOUT = 1.0

# Base directory for SCIP resolution workspaces
SCIP_WORKSPACE_BASE = "/tmp/cidx-scip"


@dataclass
class QueuedProject:
    """Represents a queued SCIP project for resolution."""

    job_id: str
    project_path: str
    language: str
    build_system: str
    stderr: str


class SCIPResolutionQueue:
    """
    Serialized FIFO queue for SCIP project resolution.

    Ensures only one Claude Code invocation runs at any time,
    preventing conflicts and resource contention.
    """

    def __init__(self, self_healing_service, job_tracker=None):
        """
        Initialize SCIP Resolution Queue.

        Args:
            self_healing_service: SCIPSelfHealingService instance for
                invoking Claude Code and handling responses
            job_tracker: Optional JobTracker for dashboard visibility (Story #314)
        """
        self.__queue: Optional[asyncio.Queue] = None  # lazy — needs event loop
        self.self_healing_service = self_healing_service
        self.worker_task: Optional[asyncio.Task] = None
        self.current_project: Optional[QueuedProject] = None
        self.is_running: bool = False
        self.__lock: Optional[asyncio.Lock] = None
        self._job_tracker = job_tracker  # Story #314: dashboard visibility

    @property
    def _queue(self) -> "asyncio.Queue[QueuedProject]":
        if self.__queue is None:
            self.__queue = asyncio.Queue()
        return self.__queue

    @property
    def _lock(self) -> asyncio.Lock:
        if self.__lock is None:
            self.__lock = asyncio.Lock()
        return self.__lock

    async def enqueue_project(
        self,
        job_id: str,
        project_path: str,
        language: str,
        build_system: str,
        stderr: str,
    ) -> None:
        """
        Add a failed SCIP project to the resolution queue.

        Args:
            job_id: Background job ID
            project_path: Project relative path (e.g., "backend/")
            language: Project language (e.g., "python")
            build_system: Build system (e.g., "poetry")
            stderr: SCIP indexer error output for this project
        """
        project = QueuedProject(
            job_id=job_id,
            project_path=project_path,
            language=language,
            build_system=build_system,
            stderr=stderr,
        )

        await self._queue.put(project)

        logger.info(
            f"Enqueued {language} project at {project_path} for job {job_id} "
            f"(queue size: {self._queue.qsize()})",
            extra={"correlation_id": get_correlation_id()},
        )

    async def process_next_project(self) -> None:
        """
        Process the next project in the queue (FIFO).

        Gets project from queue, sets as current_project, invokes Claude Code,
        and handles the response.
        Registers a scip_resolution job in JobTracker for dashboard visibility (Story #314).
        """
        project: Optional[QueuedProject] = None
        item_retrieved = False

        # Story #314: Register scip_resolution job for dashboard visibility
        tracked_job_id = None
        if self._job_tracker is not None:
            try:
                tracked_job_id = f"scip-resolution-{uuid.uuid4().hex[:8]}"
                self._job_tracker.register_job(
                    tracked_job_id,
                    "scip_resolution",
                    username="system",
                    repo_alias="server",
                )
                self._job_tracker.update_status(tracked_job_id, status="running")
            except Exception as e:
                logger.debug(f"Failed to register scip_resolution job: {e}")
                tracked_job_id = None

        try:
            # Get next project from queue (FIFO order)
            project = await self._queue.get()
            item_retrieved = True

            async with self._lock:
                self.current_project = project

            logger.info(
                f"Processing {project.language} project at {project.project_path} "
                f"for job {project.job_id}",
                extra={"correlation_id": get_correlation_id()},
            )

            # Create workspace for this project
            workspace_path = Path(
                f"{SCIP_WORKSPACE_BASE}-{project.job_id}/{project.project_path}"
            )
            workspace_path.mkdir(parents=True, exist_ok=True)

            # Get current attempt count for --resume flag
            # (stub - will be integrated with job manager in AC5)
            attempt = 1

            # Invoke Claude Code for this specific project
            response = await self.self_healing_service.invoke_claude_code(
                job_id=project.job_id,
                project_path=project.project_path,
                language=project.language,
                build_system=project.build_system,
                stderr=project.stderr,
                workspace=workspace_path,
                attempt=attempt,
            )

            # Handle the response (update job status, retry SCIP if needed, etc.)
            await self.self_healing_service.handle_project_response(
                job_id=project.job_id,
                project_path=project.project_path,
                response=response,
            )

            logger.info(
                f"Completed processing {project.project_path} for job {project.job_id}, "
                f"status: {response.status}",
                extra={"correlation_id": get_correlation_id()},
            )

            if tracked_job_id and self._job_tracker is not None:
                try:
                    self._job_tracker.complete_job(tracked_job_id)
                except Exception as e:
                    logger.debug(
                        f"Failed to complete scip_resolution job {tracked_job_id}: {e}"
                    )

        except asyncio.CancelledError:
            # Worker is being stopped, re-queue current project if retrieved
            if item_retrieved and project:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-143",
                        f"Worker cancelled while processing {project.project_path}, "
                        "re-queuing project",
                    )
                )
                await self._queue.put(project)
            if tracked_job_id and self._job_tracker is not None:
                try:
                    self._job_tracker.fail_job(tracked_job_id, error="Worker cancelled")
                except Exception as e:
                    logger.debug(
                        f"Failed to fail scip_resolution job {tracked_job_id}: {e}"
                    )
            raise
        except Exception as e:
            # Log error with project info if available
            if project:
                logger.error(
                    format_error_log(
                        "MCP-GENERAL-144",
                        f"Error processing project {project.project_path}: {e}",
                    ),
                    exc_info=True,
                )
            else:
                logger.error(
                    format_error_log(
                        "MCP-GENERAL-145",
                        f"Error before project retrieval: {e}",
                    ),
                    exc_info=True,
                )
            if tracked_job_id and self._job_tracker is not None:
                try:
                    self._job_tracker.fail_job(tracked_job_id, error=str(e))
                except Exception as e2:
                    logger.debug(
                        f"Failed to mark scip_resolution job {tracked_job_id} as failed: {e2}"
                    )
        finally:
            async with self._lock:
                self.current_project = None
            # Only call task_done if item was successfully retrieved
            if item_retrieved:
                self._queue.task_done()

    def get_status(self) -> Dict[str, Any]:
        """
        Get current queue status.

        Returns:
            Dictionary with:
            - pending_count: Number of projects waiting in queue
            - current_project: Currently processing project (or None)
            - is_running: Whether worker is running
        """
        current = None
        if self.current_project:
            current = asdict(self.current_project)

        return {
            "pending_count": self._queue.qsize(),
            "current_project": current,
            "is_running": self.is_running,
        }

    def start_worker(self) -> None:
        """
        Start the queue worker task.

        Worker continuously processes projects from queue until stopped.
        Only one worker runs at a time.
        """
        if self.is_running:
            logger.debug(
                "Worker already running, ignoring start request",
                extra={"correlation_id": get_correlation_id()},
            )
            return

        self.is_running = True
        self.worker_task = asyncio.create_task(self._worker_loop())

        logger.info(
            "SCIP resolution queue worker started",
            extra={"correlation_id": get_correlation_id()},
        )

    async def stop_worker(self) -> None:
        """
        Stop the queue worker gracefully.

        Cancels worker task and waits for it to complete.
        Currently processing project will be re-queued.
        """
        if not self.is_running:
            logger.debug(
                "Worker not running, ignoring stop request",
                extra={"correlation_id": get_correlation_id()},
            )
            return

        self.is_running = False

        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
            self.worker_task = None

        logger.info(
            "SCIP resolution queue worker stopped",
            extra={"correlation_id": get_correlation_id()},
        )

    async def _worker_loop(self) -> None:
        """
        Worker loop that continuously processes projects from queue.

        Runs until is_running becomes False or task is cancelled.
        """
        logger.debug(
            "Worker loop started", extra={"correlation_id": get_correlation_id()}
        )

        try:
            while self.is_running:
                # Wait for next project (blocks if queue empty)
                try:
                    await asyncio.wait_for(
                        self.process_next_project(),
                        timeout=WORKER_POLL_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    # No projects in queue, continue loop
                    if self._queue.empty() and self.is_running:
                        # Stop worker if queue is empty
                        self.is_running = False
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(
                        format_error_log(
                            "MCP-GENERAL-146",
                            f"Error in worker loop: {e}",
                        ),
                        exc_info=True,
                    )
                    # Continue processing other projects

        except asyncio.CancelledError:
            logger.debug(
                "Worker loop cancelled", extra={"correlation_id": get_correlation_id()}
            )
            raise
        finally:
            logger.debug(
                "Worker loop exited", extra={"correlation_id": get_correlation_id()}
            )


# =============================================================================
# EVO-65021 — Delegated SCIP build queue + fail-soft metrics
# =============================================================================
#
# The delegated SCIP backend (stream-01) hands each golden-repo compile step to
# sandbox-mcp. Because a single build can take up to 1800s, it must NOT run
# inline in the golden-repo index job (a 30s query vs a 1800s build). This
# section adds a *serialized* build drain that reuses the same one-at-a-time
# FIFO shape as ``SCIPResolutionQueue`` above (serialization is itself the
# backpressure on sandbox-mcp), plus explicit 429/backpressure re-enqueue.
#
# It is thread-backed rather than asyncio-backed on purpose: the two funnel
# entrypoints (``refresh_scheduler`` and ``activated_repo_index_manager``) are
# synchronous background-job threads, not coroutines, so a ``queue.Queue`` +
# daemon worker is the honest fit and keeps enqueue callable straight from those
# sync paths without bridging an event loop.

# sandbox-mcp returns HTTP 429 for backpressure; ``DelegatedIndexer`` maps that
# to a FAILED IndexerResult whose stderr carries the ``HTTP 429`` token
# (delegated.py ``_failure`` on ``status_code >= 300``). Matched to distinguish
# "retry later" from a genuine build failure (e.g. exit-69 unsupported-language,
# which is a plain FAILED with no 429 token → fail-soft dropped, not retried).
# The plugin-backed indexer (``scip_backend='delegated-plugin'``) reports a full
# sandbox (no capacity, HTTP 429) with ``DELEGATED_BACKPRESSURE_MARKER``.
_BACKPRESSURE_MARKERS = (
    "http 429",
    "too many requests",
    DELEGATED_BACKPRESSURE_MARKER,
)

# Backoff between backpressure re-enqueues, and the retry ceiling.
DELEGATED_BACKPRESSURE_RETRY_SECONDS = 30.0
DELEGATED_MAX_BACKPRESSURE_RETRIES = 5


# --- fail-soft metrics -------------------------------------------------------
# No project-wide metrics facility exists (grep: only per-domain counters), so
# EVO-65021 records skip/outcome counts in a process-local counter, exposed for
# observability and asserted in tests. Every fail-soft branch increments here.
_scip_metrics: Dict[str, int] = defaultdict(int)
_scip_metrics_lock = threading.Lock()


def record_scip_metric(name: str, n: int = 1) -> None:
    """Increment the EVO-65021 delegated-SCIP counter *name* by *n*."""
    with _scip_metrics_lock:
        _scip_metrics[name] += n


def get_scip_metrics() -> Dict[str, int]:
    """Return a snapshot copy of the delegated-SCIP counters."""
    with _scip_metrics_lock:
        return dict(_scip_metrics)


def reset_scip_metrics() -> None:
    """Clear the delegated-SCIP counters (test helper)."""
    with _scip_metrics_lock:
        _scip_metrics.clear()


def _result_is_success(result: Optional["GenerationResult"]) -> bool:
    """True iff the build reached its expected terminal outcome — safe to mark fresh.

    EVO-65021 freshness design (supersedes the plain ``is_complete_success``):
    freshness is "no UNEXPECTED failures", not "every project succeeded".

    * MAJOR-3 (kept intact): a PARTIAL success with a REAL failure must NOT be
      treated as terminal success — it would mask the failed language, poison
      the ``.build_commit`` marker, and (because backpressure is only consulted
      when this returns False) defeat 429 retry.
    * Freshness-thrash fix: on the Phase-1 TS/JS-only sandbox image every non-TS
      project exits 69 ("no SCIP indexer for language"), an EXPECTED, benign
      outcome. If that blocked the marker, a mixed-language repo could NEVER
      write ``.build_commit`` and would re-enqueue a doomed rebuild forever.

    So use ``GenerationResult.is_fresh_eligible()`` — ``total_projects > 0`` and
    NO real (non-unsupported-language) failures. A repo whose only failures are
    unsupported languages is fresh-eligible (``on_success`` fires, marker
    written); a repo with a genuine build failure or HTTP 429 backpressure is
    not, and falls through to ``_detect_backpressure`` / fail-soft.
    """
    if result is None:
        return False
    return bool(result.is_fresh_eligible())


def _detect_backpressure(result: Optional["GenerationResult"]) -> bool:
    """True iff a FAILED build looks like sandbox-mcp backpressure (HTTP 429).

    Scans every per-project ``stderr`` for a backpressure marker. A genuine
    build failure (unsupported-language exit 69, missing deps, etc.) carries no
    such marker and is therefore NOT retried — it is fail-soft dropped.
    """
    if result is None:
        return False
    for project_result in getattr(result, "project_results", []) or []:
        indexer_result = getattr(project_result, "indexer_result", None)
        stderr = (getattr(indexer_result, "stderr", "") or "").lower()
        if any(marker in stderr for marker in _BACKPRESSURE_MARKERS):
            return True
    return False


def _count_degenerate_projects(result: Optional["GenerationResult"]) -> int:
    """Count per-project results flagged DEGENERATE (EVO-65365).

    A degenerate (locals-only) index is surfaced on ``IndexerResult.degenerate``
    by the DelegatedIndexer. ``getattr`` defaults keep this safe against older /
    lightweight result stubs that carry no ``degenerate`` attribute.
    """
    if result is None:
        return 0
    count = 0
    for project_result in getattr(result, "project_results", []) or []:
        indexer_result = getattr(project_result, "indexer_result", None)
        if getattr(indexer_result, "degenerate", False):
            count += 1
    return count


@dataclass
class DelegatedScipBuild:
    """A queued delegated SCIP build (EVO-65021).

    Carries an opaque ``run_build`` callable (a closure over the config-selected
    ``SCIPGenerator``) so the queue stays decoupled from generator construction
    and from ``workspace_id`` resolution. ``dedupe_key`` (``alias@commit``)
    prevents the two golden-repo entrypoints from building the same repo+commit
    twice.
    """

    alias: str
    dedupe_key: str
    run_build: Callable[[], "GenerationResult"]
    on_success: Optional[Callable[["GenerationResult"], None]] = None
    backpressure_attempts: int = field(default=0)


class DelegatedScipBuildQueue:
    """Serialized FIFO drain for delegated SCIP builds (EVO-65021).

    One build runs at a time (natural backpressure on sandbox-mcp). Terminal
    outcomes:

    * success (≥1 project index produced) → ``on_success`` fires (records the
      freshness commit), dedupe key released;
    * backpressure (HTTP 429) → sleep + re-enqueue, up to
      ``max_backpressure_retries`` times, dedupe key retained;
    * any other FAILED result (incl. exit-69 unsupported-language) or a raised
      build → **fail-soft**: logged + metered and dropped. The repo stays at the
      existing ``"skipped: No SCIP index available"`` state. NEVER re-raised.
    """

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        backpressure_retry_seconds: float = DELEGATED_BACKPRESSURE_RETRY_SECONDS,
        max_backpressure_retries: int = DELEGATED_MAX_BACKPRESSURE_RETRIES,
    ) -> None:
        self._queue: "_queue_mod.Queue[Optional[DelegatedScipBuild]]" = (
            _queue_mod.Queue()
        )
        self._inflight_keys: set[str] = set()
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._current_alias: Optional[str] = None
        self._sleep = sleep
        self._backpressure_retry_seconds = backpressure_retry_seconds
        self._max_backpressure_retries = max_backpressure_retries

    def enqueue(self, build: DelegatedScipBuild, *, start_worker: bool = True) -> bool:
        """Queue *build*; return False (deduped) if its key is already in flight.

        ``start_worker=False`` is used by tests to enqueue without spawning the
        background thread, then drain deterministically via
        :meth:`drain_pending`.
        """
        with self._lock:
            if build.dedupe_key in self._inflight_keys:
                logger.info(
                    "EVO-65021: dedupe — delegated SCIP build '%s' already "
                    "queued/in-flight; skipping",
                    build.dedupe_key,
                )
                record_scip_metric("delegated_build_deduped")
                return False
            self._inflight_keys.add(build.dedupe_key)
        self._queue.put(build)
        if start_worker:
            self._ensure_worker()
        return True

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="delegated-scip-build-queue",
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            build = self._queue.get()
            try:
                if build is None:  # stop sentinel
                    return
                self._run_and_maybe_requeue(build)
            except Exception:
                # EVO-65021 MAJOR-4: this is the process-wide singleton's ONLY
                # daemon worker, with no supervisor and no restart. An unhandled
                # exception escaping here would end SCIP delegation for the rest
                # of the process — silently, since the thread just dies with no
                # metric or log, while ``funnel_scip_generation`` keeps returning
                # ENQUEUED and nothing ever drains the queue. Log + meter and
                # keep the loop alive so the queue keeps draining. The dedupe key
                # is released by ``_run_and_maybe_requeue``'s ``finally`` on every
                # non-requeue exit path, so a raise here does not leak it either.
                logger.error(
                    format_error_log(
                        "MCP-GENERAL-170",
                        "EVO-65021: delegated SCIP worker loop caught an "
                        f"unexpected error for "
                        f"'{getattr(build, 'alias', '?')}'; worker survives and "
                        "continues draining",
                        exc_info=True,
                    ),
                    exc_info=True,
                )
                record_scip_metric("delegated_worker_loop_error")
            finally:
                self._queue.task_done()

    def _run_and_maybe_requeue(self, build: DelegatedScipBuild) -> None:
        """Process one build; on backpressure sleep + re-enqueue (key retained).

        The dedupe key is released in a ``finally`` on every exit path EXCEPT a
        successful backpressure re-enqueue (EVO-65021 MAJOR-4): a raise from
        ``_process`` (belt & braces — it is itself fail-soft), from ``_sleep``,
        or from the re-enqueue ``put`` must not leave the ``alias@commit`` key
        stuck in flight, which would make ``funnel_scip_generation`` return
        DEDUPED forever and permanently block that repo from retrying.
        """
        requeued = False
        try:
            terminal = self._process(build)
            if not terminal:
                # Backpressure: the dedupe key stays in flight so a concurrent
                # funnel call for the same repo+commit is still deduped while we
                # retry. Only mark ``requeued`` once the re-enqueue has actually
                # succeeded, so a failure before then releases the key.
                self._sleep(self._backpressure_retry_seconds)
                self._queue.put(build)
                requeued = True
        finally:
            if not requeued:
                with self._lock:
                    self._inflight_keys.discard(build.dedupe_key)

    def drain_pending(self, max_items: Optional[int] = None) -> int:
        """Synchronously process queued builds in the calling thread (tests).

        Backpressure re-enqueues are honored inline (no thread, no real sleep
        beyond the injected ``sleep``). Returns the number of items processed.
        Stops when the queue is empty or *max_items* is reached.
        """
        processed = 0
        while max_items is None or processed < max_items:
            try:
                build = self._queue.get_nowait()
            except _queue_mod.Empty:
                break
            try:
                if build is None:
                    continue
                self._run_and_maybe_requeue(build)
            finally:
                self._queue.task_done()
            processed += 1
        return processed

    def _process(self, build: DelegatedScipBuild) -> bool:
        """Run one build. Return True when terminal, False to re-enqueue.

        The caller (:meth:`_run_and_maybe_requeue`) releases the dedupe key on a
        terminal (True) outcome and retains it across a backpressure re-enqueue.
        Fail-soft is enforced here: neither a raised build nor a FAILED result
        propagates out of this method.
        """
        with self._lock:
            self._current_alias = build.alias
        try:
            try:
                result = build.run_build()
            except Exception as exc:  # DelegatedIndexer fail-softs; belt & braces
                logger.error(
                    format_error_log(
                        "MCP-GENERAL-169",
                        f"EVO-65021 fail-soft: delegated SCIP build for "
                        f"'{build.alias}' raised and was dropped: {exc}",
                        exc_info=True,
                    )
                )
                record_scip_metric("delegated_build_error")
                return True

            if _result_is_success(result):
                record_scip_metric("delegated_build_success")
                # EVO-65365: a DEGENERATE (locals-only) index is a success but must
                # be OBSERVABLE and distinct from a full one. The DelegatedIndexer
                # surfaces it per project on ``IndexerResult.degenerate``; count
                # those here and meter them. (The metric lives in this server layer,
                # so the pure scip/indexers layer carries no server-metrics import.)
                degenerate_count = _count_degenerate_projects(result)
                if degenerate_count:
                    record_scip_metric("delegated_degenerate", degenerate_count)
                    logger.info(
                        "EVO-65365: delegated SCIP build for '%s' produced %d "
                        "degenerate (locals-only) index(es)",
                        build.alias,
                        degenerate_count,
                    )
                if build.on_success is not None:
                    try:
                        build.on_success(result)
                    except Exception as exc:  # freshness bookkeeping is best-effort
                        logger.warning(
                            "EVO-65021: on_success hook failed for '%s' "
                            "(non-fatal): %s",
                            build.alias,
                            exc,
                        )
                return True

            if (
                _detect_backpressure(result)
                and build.backpressure_attempts < self._max_backpressure_retries
            ):
                build.backpressure_attempts += 1
                record_scip_metric("delegated_build_backpressure_retry")
                logger.warning(
                    "EVO-65021: sandbox-mcp backpressure (HTTP 429) for '%s'; "
                    "re-enqueuing (attempt %d/%d)",
                    build.alias,
                    build.backpressure_attempts,
                    self._max_backpressure_retries,
                )
                return False

            # Genuine failure (incl. exit-69 unsupported-language) → fail-soft.
            record_scip_metric("delegated_build_skipped")
            logger.warning(
                "EVO-65021 fail-soft: delegated SCIP build for '%s' produced no "
                "index (dropped); repo stays 'skipped: No SCIP index available'",
                build.alias,
            )
            return True
        finally:
            with self._lock:
                self._current_alias = None

    def get_status(self) -> Dict[str, Any]:
        """Return queue depth, current build alias, and in-flight dedupe keys."""
        with self._lock:
            return {
                "pending_count": self._queue.qsize(),
                "current_alias": self._current_alias,
                "inflight_keys": sorted(self._inflight_keys),
            }

    def stop(self) -> None:
        """Signal the worker to exit after draining the sentinel."""
        self._queue.put(None)
        with self._lock:
            worker = self._worker
        if worker is not None:
            worker.join(timeout=5.0)


# Process-wide singleton so both funnel entrypoints share one serialized drain
# (cross-entrypoint dedupe + one-build-at-a-time backpressure).
_delegated_queue_singleton: Optional[DelegatedScipBuildQueue] = None
_delegated_queue_singleton_lock = threading.Lock()


def get_delegated_scip_build_queue() -> DelegatedScipBuildQueue:
    """Return the process-wide :class:`DelegatedScipBuildQueue` singleton."""
    global _delegated_queue_singleton
    with _delegated_queue_singleton_lock:
        if _delegated_queue_singleton is None:
            _delegated_queue_singleton = DelegatedScipBuildQueue()
        return _delegated_queue_singleton
