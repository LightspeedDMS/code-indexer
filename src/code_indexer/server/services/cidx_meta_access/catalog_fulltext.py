"""Full-text doors refuse, before searching, a query form whose outcome on
the catalog could not be decided on the text a restricted caller sees.

Shared by REST ``/api/query`` (full-text and hybrid), ``/api/query/multi``
and MCP omni search; the single-repository search path refuses inside
``search_for_caller``. The query form is checked first, so an evaluable
query reads no store. The refusal depends only on the query and on whether
the request may serve the catalog (judged like the regex doors), never on
search results. Synchronous store reads: async callers run it on a thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .catalog_regex import catalog_regex_applies
from .catalog_search import FullTextQuery
from .fulltext_terms import (
    effective_edit_distance,
    parse_fulltext_terms,
    raise_fulltext_refusal,
)


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
