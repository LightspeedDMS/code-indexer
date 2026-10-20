"""Full-text doors refuse, before searching, a query form whose outcome on
the catalog could not be decided on the text a restricted caller sees.

Shared by REST ``/api/query`` (full-text and hybrid), ``/api/query/multi``
and MCP omni search; the single-repository search path refuses inside
``search_for_caller``. The query form is checked first, so an evaluable
query reads no store. The refusal depends only on the query and on whether
the request may serve the catalog (judged like the regex doors), never on
search results. Synchronous store reads: async callers run it on a thread.

``rest_fulltext_rows`` runs REST ``/api/query``'s full-text search (and
the full-text half of hybrid): a restricted caller's literal query that may
serve the catalog is answered per shown document (``fulltext_page``);
every other search runs exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .catalog_regex import catalog_regex_applies
from .catalog_search import (
    CatalogProjectionUnavailable,
    FullTextQuery,
    LazyCatalogScope,
    rows_include_catalog,
    server_app_state,
)
from .fulltext_pages import fulltext_page
from .fulltext_terms import (
    effective_edit_distance,
    parse_fulltext_terms,
    raise_fulltext_refusal,
)
from .hit_projection import open_catalog_scope
from .universe_provider import registry_of
from .visibility import unrestricted_caller


def refuse_unevaluable_catalog_fulltext(
    query: str,
    edit_distance: int,
    aliases: Any,
    repo_path: Optional[Path],
    username: str,
    access_service: Optional[Any],
    activated_repo_manager: Optional[Any],
) -> None:
    """Return when the request may run.

    Raises:
        SearchParameterError: the query's form cannot be judged on shown
            text and the request may serve the catalog to a restricted
            caller (one alias, a list, or the served directory).
    """
    if parse_fulltext_terms(query, edit_distance) is not None:
        return
    if catalog_regex_applies(
        aliases, repo_path, username, access_service, activated_repo_manager
    ):
        raise_fulltext_refusal()


def refuse_unevaluable_rest_fulltext(
    request: "FullTextQuery",
    alias: Optional[str],
    repo_path: Optional[Path],
    username: str,
    access_service: Optional[Any],
    activated_repo_manager: Optional[Any],
) -> None:
    """``refuse_unevaluable_catalog_fulltext`` for a REST query request
    (full-text or hybrid) served from *repo_path* under *alias*; regex mode
    keeps its own rules."""
    if request.regex:
        return
    refuse_unevaluable_catalog_fulltext(
        request.query_text,
        effective_edit_distance(request.fuzzy, request.edit_distance),
        alias,
        repo_path,
        username,
        access_service,
        activated_repo_manager,
    )


def rest_fulltext_rows(
    index: Any,
    request: "FullTextQuery",
    filters: Dict[str, Any],
    alias: Optional[str],
    repo_path: Optional[Path],
    username: str,
) -> List[Dict[str, Any]]:
    """The engine rows of a REST full-text search of *index* (the full-text
    index of *repo_path*, served under *alias*) with the request's options
    and *filters*: the whole query, unless the caller is restricted, the
    query literal and *alias* serves the catalog (judged like the REST rows,
    ``rows_include_catalog``); then one row per shown document from
    single-word searches.

    Raises:
        SearchParameterError: the query form cannot be judged on shown text.
        CatalogProjectionUnavailable: no served directory or alias registry.
    """
    distance = effective_edit_distance(request.fuzzy, request.edit_distance)

    def search(text: str, k: int) -> List[Dict[str, Any]]:
        return list(
            index.search(
                query_text=text,
                case_sensitive=request.case_sensitive,
                edit_distance=distance,
                snippet_lines=request.snippet_lines,
                limit=k,
                use_regex=request.regex,
                **filters,
            )
        )

    state = server_app_state()
    access_service = getattr(state, "access_filtering_service", None)
    if (
        request.regex
        or access_service is None
        or unrestricted_caller(username, access_service)
        or not rows_include_catalog([{"repository_alias": alias}], username)
    ):
        return search(request.query_text, request.limit)
    terms = parse_fulltext_terms(request.query_text, distance)
    if terms is None:
        raise_fulltext_refusal()
    registry = None if state is None else registry_of(state)
    if repo_path is None or registry is None:
        raise CatalogProjectionUnavailable("catalog full-text needs the registry")
    root = Path(repo_path)
    scope = LazyCatalogScope(
        lambda: open_catalog_scope(username, access_service, root, registry)
    )
    return fulltext_page(search, scope.get, terms, request.limit, lambda: False)
