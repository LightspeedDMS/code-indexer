"""SearchEventContext — per-request context for search event telemetry (Issue #1159).

Propagates embedding cache metadata through the async call stack without
threading data through every intermediate function signature.

Usage:
    # In the search handler, before calling the search service:
    ctx = SearchEventContext(username="alice", repo_alias="repo1",
                             search_type="semantic", query_text="hello")
    token = _search_event_ctx.set(ctx)
    try:
        result = await search_service.search(...)
        ctx.total_latency_ms = ...
        ctx.result_count = len(result)
    finally:
        _search_event_ctx.reset(token)
        # Enqueue the completed ctx to SearchEventLogWriter
"""

import threading
<<<<<<< HEAD
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional
=======
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Union


@dataclass
class ProviderCacheFields:
    """One repository's embedding cache lookup in a multi-repository search
    (Bug #2111): the same six fields SearchEventContext carries for the
    whole call."""

    voyage_cache_hit: Optional[bool] = None
    voyage_cache_mode: Optional[str] = None
    voyage_latency_ms: Optional[int] = None
    cohere_cache_hit: Optional[bool] = None
    cohere_cache_mode: Optional[str] = None
    cohere_latency_ms: Optional[int] = None
>>>>>>> upstream/master


@dataclass
class SearchEventContext:
    """Mutable per-request container for search event data.

    Fields are populated progressively as the request moves through the
    pipeline:
      - username, repo_alias, search_type, query_text: set by the handler
        before search begins.
      - voyage_cache_hit, voyage_cache_mode: set after
        coalesced_query_embedding() returns EmbeddingCacheMetadata.
      - cohere_cache_hit, cohere_cache_mode: same for Cohere provider.
      - total_latency_ms, result_count: set after the search completes.

    Bug #1813 (DEFECT 2): the omni/multi-repo fan-out
    (multi/multi_search_service.py) submits one search task per repo to a
    shared ThreadPoolExecutor via contextvars.copy_context().run(...) -- a
    documented, correct pattern for propagating correlation_id across the
    executor boundary. But a context copy is SHALLOW: every fan-out
    worker's copy still resolves to the IDENTICAL SearchEventContext
<<<<<<< HEAD
    instance. Two repos using the same provider (e.g. both Voyage) whose
    embed calls complete concurrently can therefore race, unsynchronized,
    on this SAME provider's (cache_hit, cache_mode, latency_ms) triple --
    record_provider_cache_fields() below is the single, lock-protected
    write path both call sites (filesystem_vector_store.py,
    search_service.py) MUST use instead of assigning the three fields
    directly, so a concurrent writer can never observe (or leave behind) a
    torn combination mixing two different calls' outcomes.
=======
    instance. record_provider_cache_fields() below is the single,
    lock-protected write path both call sites (filesystem_vector_store.py,
    search_service.py) MUST use instead of assigning the fields directly, so
    a concurrent writer can never leave behind a torn combination.

    Bug #2111: a fan-out names the repository each worker searches
    (`searching_repository`); a lookup made while one is named is kept for
    that repository (`repository_cache_fields`), so each repository's row
    carries its own lookup. A lookup made with no repository named (a
    single-repository search, or a query embedded once before the fan-out)
    is the call-level fields on the context itself.
>>>>>>> upstream/master
    """

    username: str
    repo_alias: Optional[str]
    search_type: str
    query_text: str

    # Voyage embedding cache telemetry (None if Voyage not used)
    voyage_cache_hit: Optional[bool] = None
    voyage_cache_mode: Optional[str] = None
    voyage_latency_ms: Optional[int] = None

    # Cohere embedding cache telemetry (None if Cohere not used)
    cohere_cache_hit: Optional[bool] = None
    cohere_cache_mode: Optional[str] = None
    cohere_latency_ms: Optional[int] = None

    # End-of-request totals
    total_latency_ms: int = 0
    result_count: int = 0

<<<<<<< HEAD
=======
    # Bug #2111: a search over several repositories reports here which it
    # searched successfully and which failed (note_repositories). None for a
    # single-repository search: its row carries `repo_alias`.
    searched_repositories: Optional[List[str]] = None
    failed_repositories: Optional[List[str]] = None
    # Each searched repository's result count, when the service tallied it
    # (its results carry no repository, e.g. SCIP); None: counted from the
    # delivered results' repository field.
    repository_result_counts: Optional[Dict[str, int]] = None
    # Bug #2111: each fan-out repository's own embedding cache lookup.
    repository_cache_fields: Dict[str, ProviderCacheFields] = field(
        default_factory=dict
    )
    # Story #2117: the repositories a search resolved before it ran, as the
    # aliases its rows carry (SCIP: the accessible repositories holding a
    # SCIP index, "<name>-global"); [] = resolved nothing, so it searched
    # nothing; None = the search never reported its resolution.
    resolved_repositories: Optional[List[str]] = None

>>>>>>> upstream/master
    # Bug #1813 (DEFECT 2): guards record_provider_cache_fields() so a
    # provider's (hit, mode, latency) triple is always written atomically.
    # Excluded from repr/eq (a lock has no meaningful value semantics).
    _provider_fields_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def record_provider_cache_fields(
        self,
        provider_name: str,
        *,
        cache_hit: Optional[bool],
        cache_mode: Optional[str],
        latency_ms: Optional[int],
    ) -> None:
        """Atomically record one provider's cache-hit/-mode/-latency triple.

        Bug #1813 (DEFECT 2): the single, lock-protected write path for the
        (voyage_cache_hit, voyage_cache_mode, voyage_latency_ms) /
<<<<<<< HEAD
        (cohere_cache_hit, cohere_cache_mode, cohere_latency_ms) triples.
        Concurrent callers (e.g. two omni fan-out repos sharing this SAME
        context via contextvars.copy_context()'s shallow-copy semantics)
        serialize here -- each call's three fields are written as one
        atomic group, so the final state always reflects exactly one
        caller's outcome (the last to finish), never a scrambled mix of
        two different calls' fields.
        """
        with self._provider_fields_lock:
            if "cohere" in provider_name.lower():
                self.cohere_cache_hit = cache_hit
                self.cohere_cache_mode = cache_mode
                self.cohere_latency_ms = latency_ms
            else:
                self.voyage_cache_hit = cache_hit
                self.voyage_cache_mode = cache_mode
                self.voyage_latency_ms = latency_ms
=======
        (cohere_cache_hit, cohere_cache_mode, cohere_latency_ms) triples;
        each call's three fields are written as one atomic group.

        Bug #2111: while a fan-out names the repository being searched
        (`searching_repository`), the triple is that repository's own;
        otherwise it is the call-level triple on this context.
        """
        repository = _search_event_repository.get()
        with self._provider_fields_lock:
            target: Union["SearchEventContext", ProviderCacheFields] = self
            if repository is not None:
                target = self.repository_cache_fields.setdefault(
                    repository, ProviderCacheFields()
                )
            if "cohere" in provider_name.lower():
                target.cohere_cache_hit = cache_hit
                target.cohere_cache_mode = cache_mode
                target.cohere_latency_ms = latency_ms
            else:
                target.voyage_cache_hit = cache_hit
                target.voyage_cache_mode = cache_mode
                target.voyage_latency_ms = latency_ms

    def cache_fields_for(
        self, repo_alias: Optional[str]
    ) -> Union["SearchEventContext", ProviderCacheFields]:
        """Bug #2111: the cache lookup a multi-repository row carries -- the
        repository's own lookup, else the call-level lookup (a query
        embedded once before the fan-out served every repository)."""
        with self._provider_fields_lock:
            own = (
                self.repository_cache_fields.get(repo_alias)
                if repo_alias is not None
                else None
            )
        return own if own is not None else self

    def note_repositories(
        self,
        *,
        searched: Iterable[str],
        failed: Iterable[str],
        counts: Optional[Mapping[str, int]] = None,
    ) -> None:
        """Bug #2111: record which repositories a multi-repository search
        searched successfully and which failed (canonical aliases), and --
        when the service tallied them -- each searched repository's result
        count. The recorded rows are then one per repository."""
        with self._provider_fields_lock:
            self.searched_repositories = list(searched)
            self.failed_repositories = list(failed)
            self.repository_result_counts = dict(counts) if counts is not None else None
>>>>>>> upstream/master


# Process-level ContextVar — one slot per concurrent asyncio task / thread.
# Default is None so reads outside a search request never raise LookupError.
_search_event_ctx: ContextVar[Optional[SearchEventContext]] = ContextVar(
    "_search_event_ctx", default=None
)

# Bug #2111: the repository a fan-out worker is searching (None outside a
# fan-out). Each worker runs in its own copied context, so workers never see
# each other's value.
_search_event_repository: ContextVar[Optional[str]] = ContextVar(
    "_search_event_repository", default=None
)


@contextmanager
def searching_repository(repo_alias: str) -> Iterator[None]:
    """Bug #2111: name the repository a fan-out searches inside the block,
    so the embedding cache lookups made there are kept for its row. An
    outer fan-out's repository is kept: the outer fan-out notes the rows,
    under its own aliases."""
    if _search_event_repository.get() is not None:
        yield
        return
    token = _search_event_repository.set(repo_alias)
    try:
        yield
    finally:
        _search_event_repository.reset(token)


def get_search_event_ctx() -> Optional[SearchEventContext]:
    """Return the current request's SearchEventContext, or None outside a search."""
    return _search_event_ctx.get()


def note_current_repositories(
    searched: Iterable[str],
    failed: Iterable[str],
    counts: Optional[Mapping[str, int]] = None,
) -> None:
    """Bug #2111: a multi-repository search reports the repositories it
    searched and those that failed on the current request's context (and,
    when its results carry no repository, each searched one's result
    count). Outside a recorded search (CLI, no context installed) this does
    nothing."""
    context = _search_event_ctx.get()
    if context is not None:
        context.note_repositories(searched=searched, failed=failed, counts=counts)


def begin_repository_rows() -> None:
    """Bug #2111 / Story #2117: the current request's search is over several
    repositories and notes each one as it resolves
    (`searching_resolved_repository`), so a whole-call failure later still
    writes one failure row per repository that resolved. Outside a recorded
    search this does nothing."""
    note_current_repositories(searched=[], failed=[])


def note_current_resolution(repositories: Iterable[str]) -> None:
    """Story #2117: the current request's search resolved `repositories` (the
    aliases its rows carry; empty: it resolved nothing and searched nothing,
    so nothing is recorded). Outside a recorded search this does nothing."""
    context = _search_event_ctx.get()
    if context is not None:
        with context._provider_fields_lock:
            context.resolved_repositories = list(repositories)


@contextmanager
def searching_resolved_repository(repo_alias: str) -> Iterator[None]:
    """Bug #2111: a sequential fan-out resolved `repo_alias` and searches it
    in the block. When the current request collects one row per repository
    (`begin_repository_rows`), the repository is noted as resolved now and
    its embedding cache lookups are kept for its row; otherwise (a
    single-repository search) this does nothing."""
    context = _search_event_ctx.get()
    if context is None:
        yield
        return
    with context._provider_fields_lock:
        searched = context.searched_repositories
        if searched is not None and repo_alias not in searched:
            searched.append(repo_alias)
    if searched is None:
        yield
        return
    with searching_repository(repo_alias):
        yield


def finish_repository_rows(failed: Iterable[str]) -> None:
    """Story #2117: a multi-repository search that collects repository rows
    (`begin_repository_rows`) finished. Its rows are exactly the
    repositories noted as resolved and searched
    (`searching_resolved_repository`): those in `failed` fail
    (`repository_failed`), the others succeed. A repository never noted --
    an alias that never resolved, or one the time budget never reached --
    gets no row. Outside a recorded search, or when the request collects no
    repository rows, this does nothing."""
    context = _search_event_ctx.get()
    if context is None:
        return
    failed_set = set(failed)
    with context._provider_fields_lock:
        resolved = context.searched_repositories
        if resolved is None:
            return
        context.searched_repositories = [a for a in resolved if a not in failed_set]
        context.failed_repositories = [a for a in resolved if a in failed_set]
        context.repository_result_counts = None
