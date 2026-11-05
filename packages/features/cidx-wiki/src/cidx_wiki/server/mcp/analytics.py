"""MCP tool ``wiki_article_analytics`` (Story #293): article view analytics.

Contributed through the ``wiki`` feature's ``tools()``; the core authorizes
it exactly like a core tool (the doc's ``required_permission``, group grants,
and the repository pre-check on ``repo_alias``). Moved from the core's
``mcp/handlers/guides.py``.

For the internal catalog the rows a restricted caller sees go through the
core's ``access_projection`` capability.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Set

from cidx_sdk.tool_args import coerce_int
from cidx_wiki._platform import server_platform
from cidx_wiki.server import runtime
from cidx_wiki.server.wiki_cache import WikiCache

logger = logging.getLogger(__name__)

TOOL_NAME = "wiki_article_analytics"

# Minimum search query length to trigger CIDX search filter
_WIKI_ANALYTICS_MIN_QUERY_LENGTH = 2
# Max CIDX results to use as path filter (100 is sufficient for wiki article lists)
_WIKI_ANALYTICS_MAX_SEARCH_RESULTS = 100
_GLOBAL_SUFFIX = "-global"


def _mcp_response(data: Dict[str, Any]) -> Dict[str, Any]:
    return dict(server_platform().mcp_response(data))


def _get_wiki_enabled_repos() -> Set[str]:
    """Golden repository aliases whose wiki is enabled (Story #292 AC2);
    empty when they cannot be read."""
    return set(server_platform().wiki_enabled_golden_aliases())


def _get_wiki_cache_for_handler() -> Optional[WikiCache]:
    """A WikiCache over the server database (lightweight: no I/O until a
    method is called); None when golden_repo_manager is unavailable."""
    grm = server_platform().app_attr("golden_repo_manager")
    if grm is None:
        return None
    db_path = getattr(grm, "db_path", None)
    if not db_path:
        return None
    # Bug #1665: the shared PostgreSQL wiki_cache backend in cluster mode, so
    # this handler sees the same store regardless of which node handles it.
    backend = server_platform().backend_registry_attr(
        "wiki_cache", "guides._get_wiki_cache_for_handler"
    )
    return WikiCache(db_path, storage_backend=backend)


def _wiki_analytics_filter_by_search(
    wiki_alias: str, search_query: str, search_mode: str, username: str
) -> Optional[set]:
    """Filter wiki article paths via CIDX search (AC4).

    Searches the wiki's GOLDEN repository (``{wiki_alias}-global``), never
    a user activation that happens to carry the wiki's bare name.

    Returns a set of matching file_paths, or None if search was not performed.
    Returns an empty set when search runs but finds no matches.
    Raises RuntimeError if semantic_query_manager is unavailable.
    """
    if not search_query or len(search_query) < _WIKI_ANALYTICS_MIN_QUERY_LENGTH:
        return None
    sqm = server_platform().app_attr("semantic_query_manager")
    if sqm is None:
        raise RuntimeError("semantic_query_manager not available for search filter")
    result = sqm.query_user_repositories(
        username=username,
        query_text=search_query,
        repository_alias=f"{wiki_alias}{_GLOBAL_SUFFIX}",
        search_mode=search_mode,
        limit=_WIKI_ANALYTICS_MAX_SEARCH_RESULTS,
        file_extensions=[".md"],
    )
    search_results = result.get("results", [])
    return {r.get("file_path", "") for r in search_results}


def _wiki_analytics_build_articles(all_views: list, wiki_alias: str) -> list:
    """Transform view count records into article response dicts (AC2).

    Derives human-readable title from filename: hyphens and underscores become
    spaces, result is title-cased. Strips .md extension from wiki_url path.
    """
    articles = []
    for v in all_views:
        path = v["article_path"]
        path_no_ext = path[:-3] if path.endswith(".md") else path
        last_segment = path_no_ext.rsplit("/", 1)[-1]
        title = last_segment.replace("-", " ").replace("_", " ").title()
        articles.append(
            {
                "title": title,
                "path": path,
                "real_views": v["real_views"],
                "first_viewed_at": v["first_viewed_at"],
                "last_viewed_at": v["last_viewed_at"],
                "wiki_url": f"/wiki/{wiki_alias}/{path_no_ext}",
            }
        )
    return articles


def _visible_views(username: str, wiki_alias: str, rows: List[Dict]) -> List[Dict]:
    """The rows *username* may see (the catalog access projection)."""
    projection = runtime.access_projection()
    if projection is None:
        return rows
    return [
        dict(row)
        for row in projection.visible_article_views(username, wiki_alias, rows)
    ]


def handle_wiki_article_analytics(params: Dict[str, Any], user: Any) -> Dict[str, Any]:
    """Query wiki article view analytics (Story #293).

    AC2: Returns title, path, real_views, first_viewed_at, last_viewed_at, wiki_url.
    AC3: sort_by most_viewed=DESC, least_viewed=ASC; tie-break alphabetical by path.
    AC4: Optional search_query filters via CIDX; results still sorted by views.
    AC5: Returns explicit error for non-wiki-enabled repos.
    AC6: Permission enforced by MCP middleware via tool doc required_permission.
    """
    try:
        repo_alias = params.get("repo_alias", "")
        sort_by = params.get("sort_by", "most_viewed")
        if sort_by not in ("most_viewed", "least_viewed"):
            sort_by = "most_viewed"
        limit = coerce_int(params.get("limit"), 20)
        limit = max(1, min(limit, 500))
        search_query = params.get("search_query")
        search_mode = params.get("search_mode", "semantic")

        # Strip -global suffix to get base alias for wiki_enabled check
        wiki_alias = (
            repo_alias[: -len(_GLOBAL_SUFFIX)]
            if repo_alias.endswith(_GLOBAL_SUFFIX)
            else repo_alias
        )

        # AC5: Reject non-wiki-enabled repos with explicit error
        if wiki_alias not in _get_wiki_enabled_repos():
            return _mcp_response(
                {
                    "success": False,
                    "error": "Wiki is not enabled for this repository",
                }
            )

        # AC4: Optional search filter - raises on sqm unavailability
        article_paths_filter = _wiki_analytics_filter_by_search(
            wiki_alias, search_query or "", search_mode, user.username
        )
        if article_paths_filter is not None and not article_paths_filter:
            return _mcp_response(
                {
                    "success": True,
                    "articles": [],
                    "total_count": 0,
                    "repo_alias": repo_alias,
                    "sort_by": sort_by,
                    "wiki_enabled": True,
                }
            )

        wiki_cache = _get_wiki_cache_for_handler()
        if wiki_cache is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Wiki cache not available",
                }
            )

        all_views = wiki_cache.get_all_view_counts(wiki_alias)

        if article_paths_filter is not None:
            all_views = [
                v for v in all_views if v["article_path"] in article_paths_filter
            ]
        all_views = _visible_views(user.username, wiki_alias, all_views)

        # AC3: Sort with alphabetical tie-breaking
        if sort_by == "least_viewed":
            all_views.sort(key=lambda x: (x["real_views"], x["article_path"]))
        else:
            all_views.sort(key=lambda x: (-x["real_views"], x["article_path"]))

        all_views = all_views[:limit]
        articles = _wiki_analytics_build_articles(all_views, wiki_alias)

        return _mcp_response(
            {
                "success": True,
                "articles": articles,
                "total_count": len(articles),
                "repo_alias": repo_alias,
                "sort_by": sort_by,
                "wiki_enabled": True,
            }
        )

    except Exception as e:
        logger.warning(
            "[WIKI-ANALYTICS-001] Error in handle_wiki_article_analytics: %s", e
        )
        return _mcp_response({"success": False, "error": str(e)})


def _register(registry: Dict[str, Callable[..., object]]) -> None:
    """The feature's MCP handlers, by tool name."""
    registry[TOOL_NAME] = handle_wiki_article_analytics
