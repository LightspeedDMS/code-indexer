"""One record builder for search events, shared by every search door (#2104).

`record_search_event` builds a SearchEventRecord and hands it to the existing
bounded SearchEventLogWriter queue. It is fire-and-forget: it never blocks,
never raises and never changes the search it records -- a recording failure
is logged and the search result is returned as usual.

Recording rule (Bug #2113): a row is written for a search whose result is
known and successful. Searches that fail, are rejected, answer success
without having run, or only submit a job whose result is not yet known write
no row. Whether a result is known is decided here, once per result shape
(`search_payload_is_known`, `xray_result_is_known`). Asynchronous paths
record when their job completes, with the real result count, unless the job
was cancelled. A deferred temporal handoff leaves a pending record in the
cluster-shared PayloadCache (`remember_deferred_search`); the result door
that delivers the completed result records it once per caller
(`record_deferred_search`).
"""

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Optional

from code_indexer.server.models.query import SEARCH_MODES
from code_indexer.server.services.search_event_context import SearchEventContext
from code_indexer.server.services.search_event_log_writer import SearchEventRecord
from code_indexer.server.utils.runtime_node_id import resolve_runtime_node_id

logger = logging.getLogger(__name__)

# The search_type values a row can carry: the query modes (REST model and
# MCP search_code) plus the search kinds that are not a query mode. Writers
# and the Query Analytics Export page's Search Type filter use this one list.
SEARCH_TYPES = SEARCH_MODES + ("temporal", "regex", "xray")

# query_text is capped at this many characters before it is stored.
QUERY_TEXT_MAX_CHARS = 500

# The file_path the X-Ray engine gives an evaluation error about the whole
# run rather than one file (compile error, xray-cli failure, the synthetic
# EvaluatorTimeout of a run that timed out with no matches).
_WHOLE_RUN_FILE_PATH = ""

# Bug #1804's completeness marker: every embedding provider was unavailable,
# so the search answered success with no results without ever running.
_PROVIDERS_UNAVAILABLE = "providers_unavailable"


def writer_from_app(app: Any) -> Any:
    """The app's SearchEventLogWriter, or None when it is not wired."""
    return getattr(getattr(app, "state", None), "search_event_log_writer", None)


def record_search_event(
    writer: Any,
    *,
    username: str,
    repo_alias: Optional[str],
    search_type: str,
    query_text: str,
    result_count: int,
    total_latency_ms: int,
    correlation_id: Optional[str],
    context: Optional[SearchEventContext] = None,
) -> None:
    """Enqueue one search event. Never raises; a None writer records nothing.

    `context` carries the embedding cache fields collected during the search;
    without it those columns stay NULL (searches that never embed).
    """
    if writer is None:
        return
    with _never_raise(search_type):
        _enqueue(
            writer,
            username=username,
            repo_alias=repo_alias,
            search_type=search_type,
            query_text=query_text,
            result_count=result_count,
            total_latency_ms=total_latency_ms,
            correlation_id=correlation_id,
            context=context,
        )


@contextmanager
def _never_raise(search_type: str) -> Iterator[None]:
    """Recording never fails a search: any error is logged and swallowed."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001 -- recording never fails a search
        logger.warning(
            "search event not recorded (%s): %s: %s",
            search_type,
            type(exc).__name__,
            exc,
        )


def _enqueue(
    writer: Any,
    *,
    username: str,
    repo_alias: Optional[str],
    search_type: str,
    query_text: str,
    result_count: int,
    total_latency_ms: int,
    correlation_id: Optional[str],
    context: Optional[SearchEventContext],
) -> None:
    """Build one SearchEventRecord and enqueue it. Raises on any error."""
    if search_type not in SEARCH_TYPES:
        raise ValueError(f"unknown search_type {search_type!r}")
    writer.enqueue(
        SearchEventRecord(
            timestamp=time.time(),
            username=username,
            repo_alias=repo_alias,
            search_type=search_type,
            query_text=query_text[:QUERY_TEXT_MAX_CHARS],
            voyage_cache_hit=context.voyage_cache_hit if context else None,
            voyage_cache_mode=context.voyage_cache_mode if context else None,
            voyage_latency_ms=context.voyage_latency_ms if context else None,
            cohere_cache_hit=context.cohere_cache_hit if context else None,
            cohere_cache_mode=context.cohere_cache_mode if context else None,
            cohere_latency_ms=context.cohere_latency_ms if context else None,
            total_latency_ms=total_latency_ms,
            result_count=result_count,
            node_id=resolve_runtime_node_id(),
            correlation_id=correlation_id,
        )
    )


def elapsed_ms(started_monotonic: float) -> int:
    """Whole milliseconds since `started_monotonic` (a time.monotonic())."""
    return int((time.monotonic() - started_monotonic) * 1000)


@dataclass
class PendingSearchEvent:
    """A search whose result is not known yet (Bug #2113).

    Built at submission from the request's event context; the job installs
    `context` while it searches (so embedding cache fields are captured) and
    calls `record_result` with its result when it completes. The latency
    clock starts at submission.
    """

    writer: Any
    context: SearchEventContext
    correlation_id: Optional[str]
    started_monotonic: float = field(default_factory=time.monotonic)

    def record_result(
        self, result: Dict[str, Any], cancelled: Optional[Callable[[], bool]] = None
    ) -> None:
        """Record the job's query_user_repositories result (its `results`
        list is counted). A job cancelled while it ran records nothing; a job
        that raised never calls this."""
        if self.writer is None:
            return
        with _never_raise(self.context.search_type):
            if cancelled is not None and cancelled():
                return
            _enqueue(
                self.writer,
                username=self.context.username,
                repo_alias=self.context.repo_alias,
                search_type=self.context.search_type,
                query_text=self.context.query_text,
                result_count=len(result.get("results") or []),
                total_latency_ms=elapsed_ms(self.started_monotonic),
                correlation_id=self.correlation_id,
                context=self.context,
            )


def search_payload_is_known(payload: Dict[str, Any]) -> bool:
    """Whether a search_code response payload is a search that really ran.

    Not known: `success` is not true (a handled error or a deferred
    handoff), every embedding provider was unavailable (Bug #1804:
    success true with completeness "providers_unavailable", the search never
    ran), and an omni search that searched no repository successfully
    (success true, total_repos_searched 0, only an errors map).
    """
    if payload.get("success") is not True:
        return False
    results = payload.get("results")
    if not isinstance(results, dict):
        return True
    metadata = results.get("query_metadata")
    if isinstance(metadata, dict) and (
        metadata.get("completeness") == _PROVIDERS_UNAVAILABLE
    ):
        return False
    return "total_repos_searched" not in results or bool(
        results["total_repos_searched"]
    )


def search_payload_result_count(payload: Dict[str, Any]) -> int:
    """Number of results in a search_code response payload.

    `results` is a list (global path), a dict wrapping a `results` list
    (activated-repo and temporal paths, flat omni), or a grouped omni dict
    carrying `total_results`.
    """
    results = payload.get("results")
    if isinstance(results, dict):
        if "results_by_repo" in results:
            return int(results.get("total_results") or 0)
        results = results.get("results")
    return len(results) if isinstance(results, list) else 0


def record_search_payload(
    writer: Any,
    payload: Dict[str, Any],
    *,
    search_type: str,
    context: SearchEventContext,
    total_latency_ms: int,
    correlation_id: Optional[str],
) -> None:
    """Record one search_code search from its response payload (Bug #2113).

    Records only when `search_payload_is_known`; the result count is the
    payload's. User, alias and query text come from `context`.
    """
    if writer is None:
        return
    with _never_raise(search_type):
        if not search_payload_is_known(payload):
            return
        _enqueue(
            writer,
            username=context.username,
            repo_alias=context.repo_alias,
            search_type=search_type,
            query_text=context.query_text,
            result_count=search_payload_result_count(payload),
            total_latency_ms=total_latency_ms,
            correlation_id=correlation_id,
            context=context,
        )


# A deferred temporal handoff's pending record lives this long in the shared
# PayloadCache: a result delivered later than this is not recorded, and a
# record whose job failed, was cancelled or was never polled expires.
DEFERRED_SEARCH_TTL_SECONDS = 24 * 60 * 60
_DEFERRED_KEY_PREFIX = "search-event-deferred:"
_DEFERRED_ERROR_CODE = "TEMPORAL_QUERY_DEFERRED"
_DEFERRED_CONTEXT_FIELDS = (
    "username",
    "repo_alias",
    "query_text",
    "voyage_cache_hit",
    "voyage_cache_mode",
    "voyage_latency_ms",
    "cohere_cache_hit",
    "cohere_cache_mode",
    "cohere_latency_ms",
)


def _deferred_key(job_id: str, username: str) -> str:
    """One pending record per (job, user). The temporal dedup signature
    includes the username, so a job is never shared across users; a
    same-user repeat of an identical deferred search reuses the job and
    overwrites this one record, so the repeats are recorded as one row."""
    material = json.dumps([job_id, username], separators=(",", ":"))
    return _DEFERRED_KEY_PREFIX + hashlib.sha256(material.encode()).hexdigest()


def remember_deferred_search(
    payload_cache: Any,
    payload: Dict[str, Any],
    *,
    context: SearchEventContext,
    correlation_id: Optional[str],
    started_at: float,
) -> None:
    """At a deferred temporal handoff (Bug #2109), leave a pending record.

    `payload` is the handoff response body; anything but a
    TEMPORAL_QUERY_DEFERRED handoff with a job_id is ignored. The record is
    stored in the cluster-shared PayloadCache (never process memory), so the
    result door on any node can record the search. `started_at` is the
    search's start (time.time()). Never raises.
    """
    if payload.get("error_code") != _DEFERRED_ERROR_CODE or not payload.get("job_id"):
        return
    with _never_raise("temporal"):
        if payload_cache is None:
            raise RuntimeError("no payload cache for the deferred search record")
        pending = {name: getattr(context, name) for name in _DEFERRED_CONTEXT_FIELDS}
        pending.update(started_at=started_at, correlation_id=correlation_id)
        payload_cache.store_expiring_key(
            _deferred_key(str(payload["job_id"]), context.username),
            json.dumps(pending),
            DEFERRED_SEARCH_TTL_SECONDS,
        )


def record_deferred_search(
    payload_cache: Any,
    writer: Any,
    *,
    job_id: str,
    username: str,
    result_count: int,
) -> None:
    """At a result door delivering a COMPLETED result to `username`, record
    the deferred temporal search that caller handed off (Bug #2109).

    The pending record is read, then claimed with one atomic consume on the
    shared store: only the claimer records, so repeated or concurrent polls
    (any node) write one row. No pending record (not a deferred temporal
    search, already recorded, or expired) records nothing. Latency runs from
    the search's start. Never raises.
    """
    if payload_cache is None or writer is None:
        return
    from code_indexer.server.cache.payload_cache import CacheNotFoundError

    with _never_raise("temporal"):
        key = _deferred_key(job_id, username)
        try:
            content = payload_cache.retrieve_full(key)
        except CacheNotFoundError:
            return
        if not payload_cache.consume_key(key):
            return
        pending = json.loads(content)
        context = SearchEventContext(
            search_type="temporal",
            **{name: pending[name] for name in _DEFERRED_CONTEXT_FIELDS},
        )
        _enqueue(
            writer,
            username=context.username,
            repo_alias=context.repo_alias,
            search_type="temporal",
            query_text=context.query_text,
            result_count=result_count,
            total_latency_ms=max(0, int((time.time() - pending["started_at"]) * 1000)),
            correlation_id=pending["correlation_id"],
            context=context,
        )


def _whole_run_failed(error: Dict[str, Any]) -> bool:
    """An evaluation error that names no file is about the whole run: the
    engine's marker for a compile error, an xray-cli or output failure, and
    its synthetic EvaluatorTimeout (search_engine.py, Bug #1827). A per-file
    error (e.g. one file's 5 s sandbox timeout) names its file."""
    return error.get("file_path") == _WHOLE_RUN_FILE_PATH


def xray_result_is_known(result: Dict[str, Any]) -> bool:
    """Whether an X-Ray engine or batch result is a search that really ran.

    Not known: a Phase 1 (ripgrep) failure, a timeout with no matches, a
    cancelled batch, a single run with no matches whose evaluator never ran
    (a whole-run evaluation error: compile, xray-cli, timeout), and a batch
    in which no cell ran (each cell has a cell-level error or a whole-run
    evaluation error). A partial result with matches (timeout, file cap,
    some failed cells, per-file errors) is known.
    """
    if result.get("phase1_failed") or result.get("cancelled"):
        return False
    if result.get("matches"):
        return True
    if result.get("timeout"):
        return False
    evaluation_errors = result.get("evaluation_errors") or []
    if "total_cells" not in result:
        return not any(_whole_run_failed(e) for e in evaluation_errors)
    failed_cells = {
        (e.get("repository_alias"), e.get("scan_index"))
        for e in result.get("errors") or []
        if e.get("error_level") == "cell"
    } | {
        (e.get("repository_alias"), e.get("scan_index"))
        for e in evaluation_errors
        if _whole_run_failed(e)
    }
    return len(failed_cells) < int(result["total_cells"])


def record_xray_search(
    writer: Any,
    *,
    username: str,
    repo_alias: Optional[str],
    pattern: str,
    result: Dict[str, Any],
    started_monotonic: float,
    correlation_id: Optional[str],
    cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    """Record one completed X-Ray search (Story #2107).

    Called on the worker thread with the engine's result once the search has
    run; a job that fails or never gets a worker slot does not call it. A
    result that is not known (`xray_result_is_known`) and a job that was
    cancelled (`cancelled()` true -- its killed children leave a partial
    result) record nothing. query_text is the driver pattern -- never
    evaluator source; result_count is the number of matches.
    """
    if writer is None:
        return
    with _never_raise("xray"):
        if not xray_result_is_known(result):
            return
        if cancelled is not None and cancelled():
            return
        _enqueue(
            writer,
            username=username,
            repo_alias=repo_alias,
            search_type="xray",
            query_text=pattern,
            result_count=len(result.get("matches") or []),
            total_latency_ms=elapsed_ms(started_monotonic),
            correlation_id=correlation_id,
            context=None,
        )


def record_regex_search(
    writer: Any,
    *,
    username: str,
    repo_alias: Optional[str],
    pattern: str,
    result_count: int,
    started_monotonic: float,
    correlation_id: Optional[str],
) -> None:
    """Record one completed single-repository regex search (Story #2108).

    MCP and REST both call this once per searched repository (an omni search
    fans out one search per repository). query_text is the pattern;
    result_count is the number of matches returned.
    """
    record_search_event(
        writer,
        username=username,
        repo_alias=repo_alias,
        search_type="regex",
        query_text=pattern,
        result_count=result_count,
        total_latency_ms=elapsed_ms(started_monotonic),
        correlation_id=correlation_id,
    )


def bind_xray_recorder(
    writer: Any,
    *,
    username: str,
    repo_alias: Optional[str],
    pattern: str,
    correlation_id: Optional[str],
) -> "XRayRecorder":
    """Bind an X-Ray search's request-time fields; call the result with the
    job's result (and its cancellation check) when it completes. The latency
    clock starts now."""
    started = time.monotonic()

    def _record(
        result: Dict[str, Any], cancelled: Optional[Callable[[], bool]] = None
    ) -> None:
        record_xray_search(
            writer,
            username=username,
            repo_alias=repo_alias,
            pattern=pattern,
            result=result,
            started_monotonic=started,
            correlation_id=correlation_id,
            cancelled=cancelled,
        )

    return _record


# What bind_xray_recorder returns: call with (result, cancelled check).
XRayRecorder = Callable[[Dict[str, Any], Optional[Callable[[], bool]]], None]

# JobTracker statuses of a job that will never report a result.
_ENDED_WITHOUT_RESULT = ("failed", "cancelled")


def tracked_job_cancelled(tracker: Any, job_id: str) -> bool:
    """Whether a JobTracker-tracked job was cancelled while it ran.

    MCP X-Ray jobs bypass BackgroundJobManager.submit_job: a cancel fails
    them in the JobTracker (BackgroundJobManager.cancel_job) and kills their
    child processes, so the engine returns a partial result as if normal.
    """
    job = tracker.get_job(job_id) if tracker is not None else None
    return job is not None and job.status in _ENDED_WITHOUT_RESULT
