"""One record builder for search events, shared by every search door (#2104).

`record_search_event` and its siblings build SearchEventRecords and hand them
to the existing bounded SearchEventLogWriter queue. Recording is
fire-and-forget: it never blocks, never raises and never changes the search
it records -- a recording failure is logged and the search result is
returned as usual.

Recording rule (Story #2117, replacing Bug #2113's success-only rule): every
search a door ran is recorded with its `tool` and `door` (SearchOrigin) and
an `outcome`. A search that ran and succeeded is `success`; one that failed,
raised, or answered without having run is `failure` with a neutral
`error_class` (search_event_outcomes; never a message). Outcomes are decided
here, once per result shape, never per door. Not recorded: a job cancelled
by the user, a deferred temporal handoff (recorded when its result is
delivered), and requests rejected before the door resolved a repository
(input validation, admission refusal).

Bug #2111: a search over several repositories writes one row per repository
it searched (`note_repositories` on the request's SearchEventContext), each
with that repository's own result count; a repository that failed gets a
failure row (`repository_failed`). Rows of one call share its latency and
correlation id.

Asynchronous paths record when their job completes. A deferred temporal
handoff leaves a pending record in the cluster-shared PayloadCache
(`remember_deferred_search`); the result door that delivers the completed
result records it once per caller (`record_deferred_search`).
"""

import functools
import hashlib
import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Sized,
    Tuple,
    TypeVar,
    Union,
    cast,
)

from code_indexer.server.models.query import SEARCH_MODES
from code_indexer.server.services import search_event_outcomes as outcomes
from code_indexer.server.services.search_event_context import (
    ProviderCacheFields,
    SearchEventContext,
)
from code_indexer.server.services.search_event_log_writer import (
    SEARCH_EVENT_DOORS,
    TOOL_MAX_CHARS,
    SearchEventRecord,
)
from code_indexer.server.utils.runtime_node_id import resolve_runtime_node_id

logger = logging.getLogger(__name__)

# The search_type values a row can carry: the query modes (REST model and
# MCP search_code) plus the search kinds that are not a query mode. Writers
# and the Query Analytics Export page's Search Type filter use this one list.
# Story #2116 tool families (cache/provider columns always NULL):
# - "scip": SCIP code intelligence. query_text is the symbol ("from -> to"
#   for a call chain); result_count is the results returned (results,
#   affected symbols, call chains or context files).
# - "git_history": commit message / diff search. query_text is the query or
#   search term; result_count is the matching commits returned.
# - "ci_logs": CI run log search. query_text is the pattern; result_count is
#   the matching log lines returned.
SEARCH_TYPES = SEARCH_MODES + (
    "temporal",
    "regex",
    "xray",
    "scip",
    "git_history",
    "ci_logs",
)

# query_text is capped at this many characters before it is stored.
QUERY_TEXT_MAX_CHARS = 500

_SUCCESS = "success"
_FAILURE = "failure"


@dataclass(frozen=True)
class SearchOrigin:
    """Which tool or endpoint ran a search, and through which door.

    `tool` is the MCP tool name (e.g. "search_code"), the REST endpoint
    ("POST /api/query") or the Web page ("web:query"); `door` is one of
    SEARCH_EVENT_DOORS ("mcp", "rest", "web").
    """

    tool: str
    door: str


def writer_from_app(app: Any) -> Any:
    """The app's SearchEventLogWriter, or None when it is not wired."""
    return getattr(getattr(app, "state", None), "search_event_log_writer", None)


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
    origin: Optional[SearchOrigin],
    username: str,
    repo_alias: Optional[str],
    search_type: str,
    query_text: str,
    result_count: int,
    total_latency_ms: int,
    correlation_id: Optional[str],
    context: Optional[Union[SearchEventContext, ProviderCacheFields]],
    error_class: Optional[str] = None,
) -> None:
    """Build one SearchEventRecord and enqueue it. Raises on any error.

    `context` carries the row's embedding cache fields (None: NULL); a
    multi-repository row passes its repository's own lookup (Bug #2111).

    `origin` is None only for a deferred temporal search handed off by a node
    that predates Story #2117 (its pending record names no tool or door):
    the row is still written, with tool and door NULL like legacy rows.
    """
    if search_type not in SEARCH_TYPES:
        raise ValueError(f"unknown search_type {search_type!r}")
    if origin is not None and origin.door not in SEARCH_EVENT_DOORS:
        raise ValueError(f"unknown door {origin.door!r}")
    if origin is not None and (not origin.tool or len(origin.tool) > TOOL_MAX_CHARS):
        raise ValueError(f"tool must be 1..{TOOL_MAX_CHARS} characters")
    if error_class is not None and not outcomes.is_valid_error_class(error_class):
        raise ValueError("error_class must be a short neutral token")
    writer.enqueue(
        SearchEventRecord(
            timestamp=time.time(),
            username=username,
            repo_alias=repo_alias,
            search_type=search_type,
            # Bug #2114: the cap is applied here, and only here; doors pass
            # the full text so a cut text is marked.
            query_text=query_text[:QUERY_TEXT_MAX_CHARS],
            query_text_truncated=len(query_text) > QUERY_TEXT_MAX_CHARS,
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
            tool=origin.tool if origin is not None else None,
            door=origin.door if origin is not None else None,
            outcome=_SUCCESS if error_class is None else _FAILURE,
            error_class=error_class,
        )
    )


def _enqueue_context_rows(
    writer: Any,
    *,
    origin: SearchOrigin,
    context: SearchEventContext,
    search_type: str,
    result_count: int,
    repository_counts: Callable[[], Dict[str, int]],
    total_latency_ms: int,
    correlation_id: Optional[str],
    error_class: Optional[str] = None,
    username: Optional[str] = None,
    query_text: Optional[str] = None,
) -> None:
    """Rows of one search described by its request context.

    A search that noted no repositories is one row for `context.repo_alias`.
    A multi-repository search that noted the repositories it resolved
    (Bug #2111) is one row per resolved repository: a searched one with its
    own count (the counts the service tallied on the context, else
    `repository_counts()`), a failed one with `repository_failed`. When the
    whole call failed (`error_class`), every resolved repository gets a
    failure row with that class (Story #2117). `username` and `query_text`
    replace the context's when the door's context was opened only to collect
    the repositories (e.g. SCIP).
    """
    row_username = context.username if username is None else username
    row_query_text = context.query_text if query_text is None else query_text
    searched = context.searched_repositories
    if searched is None:
        _enqueue(
            writer,
            origin=origin,
            username=row_username,
            repo_alias=context.repo_alias,
            search_type=search_type,
            query_text=row_query_text,
            result_count=result_count,
            total_latency_ms=total_latency_ms,
            correlation_id=correlation_id,
            context=context,
            error_class=error_class,
        )
        return
    counts: Dict[str, int] = {}
    if error_class is None:
        tallied = context.repository_result_counts
        counts = tallied if tallied is not None else repository_counts()
    rows = [(alias, counts.get(alias, 0), error_class) for alias in searched] + [
        (alias, 0, error_class or outcomes.REPOSITORY_FAILED)
        for alias in context.failed_repositories or []
    ]
    for repo_alias, count, row_error in rows:
        _enqueue(
            writer,
            origin=origin,
            username=row_username,
            repo_alias=repo_alias,
            search_type=search_type,
            query_text=row_query_text,
            result_count=count,
            total_latency_ms=total_latency_ms,
            correlation_id=correlation_id,
            # Bug #2111: the repository's own cache lookup, never another
            # repository's.
            context=context.cache_fields_for(repo_alias),
            error_class=row_error,
        )


def record_search_event(
    writer: Any,
    *,
    origin: SearchOrigin,
    username: str,
    repo_alias: Optional[str],
    search_type: str,
    query_text: str,
    result_count: int,
    total_latency_ms: int,
    correlation_id: Optional[str],
    context: Optional[SearchEventContext] = None,
    error_class: Optional[str] = None,
) -> None:
    """Enqueue one search event (a failure when `error_class` is given).
    Never raises; a None writer records nothing.

    `context` carries the embedding cache fields collected during the search;
    without it those columns stay NULL (searches that never embed).
    """
    if writer is None:
        return
    with _never_raise(search_type):
        _enqueue(
            writer,
            origin=origin,
            username=username,
            repo_alias=repo_alias,
            search_type=search_type,
            query_text=query_text,
            result_count=result_count,
            total_latency_ms=total_latency_ms,
            correlation_id=correlation_id,
            context=context,
            error_class=error_class,
        )


def record_query_results(
    writer: Any,
    *,
    origin: SearchOrigin,
    context: SearchEventContext,
    results: Any,
    total_latency_ms: int,
    correlation_id: Optional[str],
) -> None:
    """Record a completed query whose delivered results are `results` (a list
    of result dicts carrying `repository_alias`). A multi-repository query
    (Bug #2111) is one row per repository it searched. Never raises."""
    if writer is None:
        return
    delivered = results if isinstance(results, list) else []
    with _never_raise(context.search_type):
        _enqueue_context_rows(
            writer,
            origin=origin,
            context=context,
            search_type=context.search_type,
            result_count=len(delivered),
            repository_counts=lambda: outcomes.results_repository_counts(
                delivered, "repository_alias"
            ),
            total_latency_ms=total_latency_ms,
            correlation_id=correlation_id,
        )


def record_query_failure(
    writer: Any,
    *,
    origin: SearchOrigin,
    context: SearchEventContext,
    error_class: Optional[str],
    total_latency_ms: int,
    correlation_id: Optional[str],
) -> None:
    """Record a query that failed (one failure row). `error_class` None is a
    request rejected before its repository resolved
    (outcomes.failure_error_class): nothing is recorded. Never raises."""
    if writer is None or error_class is None:
        return
    with _never_raise(context.search_type):
        _enqueue_context_rows(
            writer,
            origin=origin,
            context=context,
            search_type=context.search_type,
            result_count=0,
            repository_counts=dict,
            total_latency_ms=total_latency_ms,
            correlation_id=correlation_id,
            error_class=error_class,
        )


@contextmanager
def search_event_scope(
    *,
    username: str,
    repo_alias: Optional[str],
    search_type: str,
    query_text: str,
) -> Iterator[SearchEventContext]:
    """Install a search event context for one search (Story #2115).

    While it is installed the embedding call sites write their cache fields
    into it (a multi-repository fan-out shares it), so the row recorded
    afterwards carries them. Reset on exit.
    """
    from code_indexer.server.services.search_event_context import _search_event_ctx

    context = SearchEventContext(
        username=username,
        repo_alias=repo_alias,
        search_type=search_type,
        query_text=query_text,
    )
    token = _search_event_ctx.set(context)
    try:
        yield context
    finally:
        _search_event_ctx.reset(token)


_Door = TypeVar("_Door", bound=Callable[..., Any])


def in_scip_search_scope(door: _Door) -> _Door:
    """Run a SCIP search door inside a fresh search event context (Bug #2111).

    A SCIP search without an alias searches every SCIP-indexed repository;
    the SCIP service notes on this context the repositories it searched and
    each one's result count, and the door records from it
    (search_event_context.get_search_event_ctx). The context names no user
    or query: the door records those itself.
    """

    @functools.wraps(door)
    def _scoped(*args: Any, **kwargs: Any) -> Any:
        with search_event_scope(
            username="", repo_alias=None, search_type="scip", query_text=""
        ):
            return door(*args, **kwargs)

    return cast(_Door, _scoped)


def run_recorded_search(
    writer: Any,
    search: Callable[[], Dict[str, Any]],
    *,
    origin: SearchOrigin,
    username: str,
    repo_alias: Optional[str],
    search_type: str,
    query_text: str,
    correlation_id: Optional[str],
) -> Dict[str, Any]:
    """Run one synchronous query_user_repositories search and record it.

    `search` runs inside `search_event_scope`. When it returns, its rows are
    recorded with the results it returned and the embedding cache fields it
    collected; when it raises, a failure row (the exception's class) is
    recorded and the exception propagates unchanged. Recording never raises.
    """
    started = time.monotonic()
    with search_event_scope(
        username=username,
        repo_alias=repo_alias,
        search_type=search_type,
        query_text=query_text,
    ) as context:
        try:
            result = search()
        except Exception as exc:
            record_query_failure(
                writer,
                origin=origin,
                context=context,
                error_class=outcomes.failure_error_class(exc),
                total_latency_ms=elapsed_ms(started),
                correlation_id=correlation_id,
            )
            raise
    record_query_results(
        writer,
        origin=origin,
        context=context,
        results=result.get("results") or [],
        total_latency_ms=elapsed_ms(started),
        correlation_id=correlation_id,
    )
    return result


def elapsed_ms(started_monotonic: float) -> int:
    """Whole milliseconds since `started_monotonic` (a time.monotonic())."""
    return int((time.monotonic() - started_monotonic) * 1000)


@dataclass
class PendingSearchEvent:
    """A search whose result is not known yet (Bug #2113).

    Built at submission from the request's event context; the job installs
    `context` while it searches (so embedding cache fields are captured) and
    calls `record_result` with its result when it completes, or
    `record_failure` when it raised. The latency clock starts at submission.
    """

    writer: Any
    context: SearchEventContext
    correlation_id: Optional[str]
    origin: SearchOrigin
    started_monotonic: float = field(default_factory=time.monotonic)

    def record_result(
        self, result: Dict[str, Any], cancelled: Optional[Callable[[], bool]] = None
    ) -> None:
        """Record the job's query_user_repositories result. A job cancelled
        while it ran records nothing."""
        if cancelled is not None and cancelled():
            return
        record_query_results(
            self.writer,
            origin=self.origin,
            context=self.context,
            results=result.get("results") or [],
            total_latency_ms=elapsed_ms(self.started_monotonic),
            correlation_id=self.correlation_id,
        )

    def record_failure(
        self, exc: BaseException, cancelled: Optional[Callable[[], bool]] = None
    ) -> None:
        """Record a job that raised as a failure (its exception's class),
        unless it was cancelled or rejected the request."""
        if cancelled is not None and cancelled():
            return
        record_query_failure(
            self.writer,
            origin=self.origin,
            context=self.context,
            error_class=outcomes.failure_error_class(exc),
            total_latency_ms=elapsed_ms(self.started_monotonic),
            correlation_id=self.correlation_id,
        )


# A deferred temporal handoff's error code: the search is still running and
# is recorded when its result is delivered.
_DEFERRED_ERROR_CODE = "TEMPORAL_QUERY_DEFERRED"


def record_search_payload(
    writer: Any,
    payload: Dict[str, Any],
    *,
    origin: SearchOrigin,
    search_type: str,
    context: SearchEventContext,
    total_latency_ms: int,
    correlation_id: Optional[str],
) -> None:
    """Record one search_code search from its response payload.

    The outcome is `search_payload_outcome`'s; a deferred temporal handoff
    records nothing now, and a rejection (outcomes.is_rejected_payload)
    nothing at all. User, alias and query text come from `context`; an omni
    search that noted its repositories is one row per repository.
    """
    if (
        writer is None
        or payload.get("error_code") == _DEFERRED_ERROR_CODE
        or outcomes.is_rejected_payload(payload)
    ):
        return
    with _never_raise(search_type):
        error_class = outcomes.search_payload_outcome(payload)
        if (
            error_class == outcomes.NO_REPOSITORY_SEARCHED
            and context.searched_repositories is not None
        ):
            # The repositories that resolved are known: each failed one gets
            # its own repository_failed row (an unresolved alias none).
            error_class = None
        _enqueue_context_rows(
            writer,
            origin=origin,
            context=context,
            search_type=search_type,
            result_count=(
                0
                if error_class is not None
                else outcomes.search_payload_result_count(payload)
            ),
            repository_counts=lambda: outcomes.payload_repository_counts(payload),
            total_latency_ms=total_latency_ms,
            correlation_id=correlation_id,
            error_class=error_class,
        )


# A deferred temporal handoff's pending record lives this long in the shared
# PayloadCache: a result delivered later than this is not recorded, and a
# record whose job failed, was cancelled or was never polled expires.
DEFERRED_SEARCH_TTL_SECONDS = 24 * 60 * 60
_DEFERRED_KEY_PREFIX = "search-event-deferred:"
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
    origin: SearchOrigin,
    context: SearchEventContext,
    correlation_id: Optional[str],
    started_at: float,
) -> None:
    """At a deferred temporal handoff (Bug #2109), leave a pending record.

    `payload` is the handoff response body; anything but a
    TEMPORAL_QUERY_DEFERRED handoff with a job_id is ignored. The record is
    stored in the cluster-shared PayloadCache (never process memory), so the
    result door on any node can record the search with the `origin` that
    submitted it. `started_at` is the search's start (time.time()). Never
    raises.
    """
    if payload.get("error_code") != _DEFERRED_ERROR_CODE or not payload.get("job_id"):
        return
    with _never_raise("temporal"):
        if payload_cache is None:
            raise RuntimeError("no payload cache for the deferred search record")
        pending = {name: getattr(context, name) for name in _DEFERRED_CONTEXT_FIELDS}
        # Bounded in the shared store, one character past the cap so the
        # recorded row is still marked truncated (Bug #2114).
        pending["query_text"] = context.query_text[: QUERY_TEXT_MAX_CHARS + 1]
        pending.update(
            started_at=started_at,
            correlation_id=correlation_id,
            tool=origin.tool,
            door=origin.door,
        )
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
    the search's start; tool and door are the submitting door's. Never
    raises.
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
        # A record from a node predating Story #2117 names no tool or door:
        # it is still recorded, with tool and door NULL.
        tool, door = pending.get("tool"), pending.get("door")
        origin = (
            SearchOrigin(tool=tool, door=door)
            if tool is not None and door is not None
            else None
        )
        _enqueue(
            writer,
            origin=origin,
            username=context.username,
            repo_alias=context.repo_alias,
            search_type="temporal",
            query_text=context.query_text,
            result_count=result_count,
            total_latency_ms=max(0, int((time.time() - pending["started_at"]) * 1000)),
            correlation_id=pending["correlation_id"],
            context=context,
        )


def record_xray_search(
    writer: Any,
    *,
    origin: SearchOrigin,
    username: str,
    repo_alias: Optional[str],
    pattern: str,
    result: Optional[Dict[str, Any]],
    started_monotonic: float,
    correlation_id: Optional[str],
    cancelled: Optional[Callable[[], bool]] = None,
    error: Optional[BaseException] = None,
    batch_repositories: Optional[Sequence[str]] = None,
) -> None:
    """Record one X-Ray search (Story #2107).

    Called on the worker thread with the engine's result once the search has
    run, or with `error` when the job raised before producing a result
    (Story #2117: a failure row with the exception's class; a rejection
    records nothing). A cancelled search (`result["cancelled"]`, or
    `cancelled()` true -- its killed children leave a partial result)
    records nothing. A single search is one row (`xray_result_outcome`); a
    batch over `batch_repositories` is one row per repository (Bug #2111,
    `xray_batch_repository_rows`). query_text is the driver pattern -- never
    evaluator source; result_count is the number of matches.
    """
    if writer is None:
        return
    with _never_raise("xray"):
        if (cancelled is not None and cancelled()) or (
            result is not None and result.get("cancelled")
        ):
            return
        repositories: List[Optional[str]] = (
            list(batch_repositories) if batch_repositories else [repo_alias]
        )
        rows: List[Tuple[Optional[str], int, Optional[str]]]
        if error is not None:
            error_class = outcomes.failure_error_class(error)
            if error_class is None:
                return
            rows = [(alias, 0, error_class) for alias in repositories]
        elif result is None:
            raise ValueError("record_xray_search needs a result or an error")
        elif batch_repositories:
            rows = list(outcomes.xray_batch_repository_rows(result, batch_repositories))
        else:
            rows = [
                (
                    repo_alias,
                    len(result.get("matches") or []),
                    outcomes.xray_result_outcome(result),
                )
            ]
        for row_alias, count, error_class in rows:
            _enqueue(
                writer,
                origin=origin,
                username=username,
                repo_alias=row_alias,
                search_type="xray",
                query_text=pattern,
                result_count=count,
                total_latency_ms=elapsed_ms(started_monotonic),
                correlation_id=correlation_id,
                context=None,
                error_class=error_class,
            )


def record_regex_search(
    writer: Any,
    *,
    origin: SearchOrigin,
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
    record_completed_search(
        writer,
        origin=origin,
        search_type="regex",
        username=username,
        repo_alias=repo_alias,
        query_text=pattern,
        result_count=result_count,
        started_monotonic=started_monotonic,
        correlation_id=correlation_id,
    )


def record_completed_search(
    writer: Any,
    *,
    origin: SearchOrigin,
    search_type: str,
    username: str,
    repo_alias: Optional[str],
    query_text: str,
    result_count: int,
    started_monotonic: float,
    correlation_id: Optional[str],
) -> None:
    """Record one search the caller knows completed successfully.

    For searches that never embed (cache and provider columns stay NULL):
    regex, git history, CI logs, a Web SCIP query. Latency runs from
    `started_monotonic` (a time.monotonic()). Never raises.
    """
    record_search_event(
        writer,
        origin=origin,
        username=username,
        repo_alias=repo_alias,
        search_type=search_type,
        query_text=query_text,
        result_count=result_count,
        total_latency_ms=elapsed_ms(started_monotonic),
        correlation_id=correlation_id,
    )


@contextmanager
def recording_failure(
    writer: Any,
    *,
    origin: SearchOrigin,
    search_type: str,
    username: str,
    repo_alias: Optional[str],
    query_text: str,
    started_monotonic: float,
    correlation_id: Optional[str],
) -> Iterator[None]:
    """Wrap the part of a door that runs one search (Story #2117).

    When the block raises, one failure row is recorded -- the exception's
    class (outcomes.failure_error_class; a rejection records nothing) -- and
    the exception propagates unchanged. Recording never raises. A block that
    completes records nothing here: the door records its result.
    """
    try:
        yield
    except Exception as exc:
        error_class = outcomes.failure_error_class(exc)
        if writer is not None and error_class is not None:
            with _never_raise(search_type):
                _enqueue(
                    writer,
                    origin=origin,
                    username=username,
                    repo_alias=repo_alias,
                    search_type=search_type,
                    query_text=query_text,
                    result_count=0,
                    total_latency_ms=elapsed_ms(started_monotonic),
                    correlation_id=correlation_id,
                    context=None,
                    error_class=error_class,
                )
        raise


def record_multi_repository_search(
    writer: Any,
    grouped: Mapping[str, Sized],
    *,
    origin: SearchOrigin,
    context: SearchEventContext,
    started_monotonic: float,
    correlation_id: Optional[str],
) -> None:
    """Record a completed multi-repository search (Bug #2111, Story #2117).

    The rows are the repositories the service noted on `context` (the
    request's search event context): each searched one with its own count
    (`grouped` maps it to the results it returned), each that resolved and
    failed with `repository_failed`. An alias that never resolved was not
    noted and gets no row. Never raises.
    """
    if writer is None:
        return
    with _never_raise(context.search_type):
        _enqueue_context_rows(
            writer,
            origin=origin,
            context=context,
            search_type=context.search_type,
            result_count=sum(len(results) for results in grouped.values()),
            repository_counts=lambda: {
                alias: len(results) for alias, results in grouped.items()
            },
            total_latency_ms=elapsed_ms(started_monotonic),
            correlation_id=correlation_id,
        )


@contextmanager
def recording_multi_repository_failure(
    writer: Any,
    *,
    origin: SearchOrigin,
    context: SearchEventContext,
    started_monotonic: float,
    correlation_id: Optional[str],
) -> Iterator[None]:
    """Wrap a multi-repository door's whole call (Story #2117).

    When it raises after the service noted the repositories it resolved, one
    failure row per resolved repository (the exception's class); a failure
    before any repository resolved, or a rejection, records nothing. The
    exception propagates unchanged.
    """
    try:
        yield
    except Exception as exc:
        if context.searched_repositories is not None:
            record_query_failure(
                writer,
                origin=origin,
                context=context,
                error_class=outcomes.failure_error_class(exc),
                total_latency_ms=elapsed_ms(started_monotonic),
                correlation_id=correlation_id,
            )
        raise


def scip_callchain_query_text(from_symbol: Any, to_symbol: Any) -> str:
    """query_text of a call chain search (Story #2116)."""
    return f"{from_symbol} -> {to_symbol}"


def record_scip_search(
    writer: Any,
    payload: Dict[str, Any],
    *,
    origin: SearchOrigin,
    username: str,
    repo_alias: Optional[str],
    query_text: str,
    started_monotonic: float,
    correlation_id: Optional[str],
    error: Optional[BaseException] = None,
    context: Optional[SearchEventContext] = None,
) -> None:
    """Record one SCIP search from its response payload (Story #2116).

    A search without an alias searches every SCIP-indexed repository the
    caller may access; when the service noted them on `context` (the door's
    search event context) it is one row per repository searched, each with
    the count the service tallied (Bug #2111).

    The outcome is `scip_payload_outcome`'s, or -- when the search raised
    (`error`, Story #2117) -- a failure with the exception's class
    (outcomes.failure_error_class; a rejection records nothing).
    result_count is the number of results the payload returns. query_text
    is the symbol (`scip_callchain_query_text` for a call chain). A rejected
    payload (outcomes.is_rejected_payload) records nothing. Never raises.

    Story #2117: the SCIP service reports on `context` the repositories the
    search resolved (`resolved_repositories`). None resolved -- an unknown
    or inaccessible alias, or no SCIP index anywhere -- means it searched
    nothing: no row. A search for one alias records the repository the
    service resolved it to ("<name>-global"), not the caller's spelling.
    """
    if writer is None:
        return
    with _never_raise("scip"):
        if outcomes.is_rejected_payload(payload):
            return
        resolved = context.resolved_repositories if context is not None else None
        if resolved is not None and not resolved:
            return
        if resolved is not None and repo_alias is not None and len(resolved) == 1:
            repo_alias = resolved[0]
        if error is not None:
            error_class = outcomes.failure_error_class(error)
            if error_class is None:
                return
        else:
            error_class = outcomes.scip_payload_outcome(payload)
        if context is not None and context.searched_repositories is not None:
            _enqueue_context_rows(
                writer,
                origin=origin,
                context=context,
                search_type="scip",
                result_count=outcomes.scip_payload_result_count(payload),
                repository_counts=dict,
                total_latency_ms=elapsed_ms(started_monotonic),
                correlation_id=correlation_id,
                error_class=error_class,
                username=username,
                query_text=query_text,
            )
            return
        _enqueue(
            writer,
            origin=origin,
            username=username,
            repo_alias=repo_alias,
            search_type="scip",
            query_text=query_text,
            result_count=(
                0 if error is not None else outcomes.scip_payload_result_count(payload)
            ),
            total_latency_ms=elapsed_ms(started_monotonic),
            correlation_id=correlation_id,
            context=None,
            error_class=error_class,
        )


def bind_xray_recorder(
    writer: Any,
    *,
    origin: SearchOrigin,
    username: str,
    repo_alias: Optional[str],
    pattern: str,
    correlation_id: Optional[str],
    batch_repositories: Optional[Sequence[str]] = None,
) -> "XRayRecorder":
    """Bind an X-Ray search's request-time fields; call the result with the
    job's result (and its cancellation check) when it completes. The latency
    clock starts now. A batch passes the repositories it resolved
    (`batch_repositories`): one row per repository."""
    started = time.monotonic()

    def _record(
        result: Optional[Dict[str, Any]],
        cancelled: Optional[Callable[[], bool]] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        record_xray_search(
            writer,
            origin=origin,
            username=username,
            repo_alias=repo_alias,
            pattern=pattern,
            result=result,
            started_monotonic=started,
            correlation_id=correlation_id,
            cancelled=cancelled,
            error=error,
            batch_repositories=batch_repositories,
        )

    return _record


# What bind_xray_recorder returns: call with (result, cancelled check), or
# with (None, cancelled check, error=exc) when the job raised (Story #2117).
XRayRecorder = Callable[..., None]


def run_recorded_xray(
    record_event: XRayRecorder,
    run: Callable[[], Dict[str, Any]],
    cancelled: Optional[Callable[[], bool]],
) -> Dict[str, Any]:
    """Run one X-Ray search (`run`, the engine or batch call only) and record
    it: its result when it returns, a failure row when it raises -- the
    exception propagates unchanged. Work before the search starts (status
    updates, slot waits) stays outside `run`, so it never records."""
    try:
        result = run()
    except Exception as exc:
        record_event(None, cancelled, error=exc)
        raise
    record_event(result, cancelled)
    return result


# A cancelled job's JobTracker status, and the error BackgroundJobManager
# .cancel_job fails a local X-Ray job with.
_CANCELLED = "cancelled"


def tracked_job_cancelled(tracker: Any, job_id: str) -> bool:
    """Whether a JobTracker-tracked job was cancelled while it ran.

    MCP X-Ray jobs bypass BackgroundJobManager.submit_job: a cancel fails
    them in the JobTracker with the error "cancelled"
    (BackgroundJobManager.cancel_job) and kills their child processes, so the
    engine returns a partial result as if normal. A job failed for any other
    reason is not a cancellation.
    """
    job = tracker.get_job(job_id) if tracker is not None else None
    if job is None:
        return False
    return bool(
        job.status == _CANCELLED
        or (job.status == "failed" and getattr(job, "error", None) == _CANCELLED)
    )
