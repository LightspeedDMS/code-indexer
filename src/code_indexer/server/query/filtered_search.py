"""The single place where a search request's filters become one repository's
search (#2047).

Every server door builds its per-repository search here:

* single-repository search -- SemanticQueryManager (MCP ``search_code`` and
  REST ``/api/query``) and the REST ``/api/query`` FTS branch;
* multi-repository search -- MultiSearchService (MCP omni ``search_code`` and
  REST ``/api/query/multi``).

So ``language``, ``path_filter``, ``exclude_language``, ``exclude_path`` and
the ``file_extensions`` rule (services/extension_filter.py) mean the same
thing at every door.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

# Share of the handler timeout an extension-filtered request may spend on FTS
# fill rounds and on starting further repositories. The handler kills a
# request at its full timeout, so a budget equal to it would turn a
# short-but-correct answer into a client-visible timeout; the other 20% is
# left for the search already in flight, the merge, the payload-truncation
# pass and serialization, after which the client receives the (INFO-logged)
# short answer.
OVERFETCH_BUDGET_FRACTION = 0.8


def extension_overfetch_deadline(
    file_extensions: Optional[List[str]],
) -> Optional[float]:
    """#2047: the ONE time budget of an extension-filtered request (None
    without an extension filter): OVERFETCH_BUDGET_FRACTION of the query's
    own handler budget from SearchTimeoutsConfig -- the smaller of the MCP
    search_code and REST /api/query handler timeouts -- counted from now."""
    if not file_extensions:
        return None
    from ..services.config_service import get_config_service

    timeouts = get_config_service().get_config().search_timeouts_config
    assert timeouts is not None, "ServerConfig always sets search_timeouts_config"
    handler_timeout = min(
        timeouts.search_code_handler_timeout_seconds,
        timeouts.rest_query_handler_timeout_seconds,
    )
    return time.monotonic() + OVERFETCH_BUDGET_FRACTION * handler_timeout


class SearchBudgetExhausted(Exception):
    """A repository's search was not started: the request's #2047 time
    budget ran out first (reported per repository, never silently)."""


def extension_budget_spent(deadline: Optional[float]) -> bool:
    """True once *deadline* (an extension_overfetch_deadline value) has
    passed. Checked before STARTING each repository's search, so no new
    repository search starts after the request's budget; a search already
    running finishes. None (no extension filter) never expires."""
    return deadline is not None and time.monotonic() >= deadline


def fts_filter_kwargs(
    *,
    language: Optional[str],
    path_filter: Optional[str],
    exclude_language: Optional[str],
    exclude_path: Optional[str],
    file_extensions: Optional[List[str]],
    deadline: Optional[float],
) -> Dict[str, Any]:
    """The request filters as TantivyIndexManager.search keyword arguments.

    ``language`` goes in as ``languages`` so it applies together with
    ``exclude_language``; ``exclude_path`` is split on commas (Bug #1095);
    ``file_extensions`` is pushed down with the #2047 bounded refetch under
    the request-wide ``deadline``.
    """
    from ...services.path_pattern_matcher import parse_exclude_patterns

    return {
        "languages": [language] if language else None,
        "path_filters": [path_filter] if path_filter else None,
        "exclude_languages": [exclude_language] if exclude_language else None,
        "exclude_paths": parse_exclude_patterns(exclude_path) or None,
        "file_extensions": file_extensions,
        "deadline": deadline,
    }


def filtered_semantic_search(
    search: Callable[..., Any],
    *,
    query: str,
    limit: int,
    language: Optional[str],
    path_filter: Optional[str],
    exclude_language: Optional[str],
    exclude_path: Optional[str],
    accuracy: Optional[str],
    no_embedding_cache_shortcut: bool,
    file_extensions: Optional[List[str]],
    **search_kwargs: Any,
) -> List[Any]:
    """One repository's semantic results (SearchResultItem) for *limit*.

    *search* is a SemanticSearchService search method
    (``search_repository_path`` or ``search_repository_path_with_provider``);
    *search_kwargs* are that method's own arguments (repo_path, activation_id,
    provider_name, hnsw_cache, precomputed_query_vector).

    ONE store query either way. ``file_extensions``, when given, is pushed
    into the vector store as the shared ``any_ext`` condition, intersected
    with the other filters, over a widened candidate window (RECALL RULE:
    services/filtered_window.filtered_window_kwargs); without it the call is exactly
    the unfiltered one.
    """
    from ..models.api_models import InternalSemanticSearchRequest

    # Internal type: `limit` may carry rerank/access-filter over-fetch above
    # the public 100 cap (up to MAX_CANDIDATE_LIMIT).
    search_request = InternalSemanticSearchRequest(
        query=query,
        limit=limit,
        include_source=True,
        path_filter=path_filter,
        language=language,
        exclude_language=exclude_language,
        exclude_path=exclude_path,
        accuracy=accuracy,
        # Story #1108 (S4): per-request cache bypass
        no_embedding_cache_shortcut=no_embedding_cache_shortcut,
    )
    extension_kwargs: Dict[str, Any] = (
        {"file_extensions": file_extensions} if file_extensions else {}
    )
    return list(
        search(
            search_request=search_request, **extension_kwargs, **search_kwargs
        ).results
    )
