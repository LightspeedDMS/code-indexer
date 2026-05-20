"""REST route POST /api/regex/search (Story #1011).

Mirrors the MCP regex_search tool functionality via a REST endpoint.
Supports single-repo and omni (multi-repo) searches with PCRE2, context
lines, include/exclude patterns, and structured error responses.

This endpoint is purely additive — the existing Tantivy-based FTS regex
in /api/query remains unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union, cast

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.api_metrics_service import api_metrics_service
from code_indexer.server.services.config_service import get_config_service

# Module-level import so test patches against
# code_indexer.server.routes.regex_routes.RegexSearchService work correctly.
from code_indexer.global_repos.regex_search import RegexSearchService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/regex", tags=["regex"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class RegexSearchRequest(BaseModel):
    """Pydantic request body for POST /api/regex/search."""

    pattern: str
    repository_alias: Union[str, List[str]]
    path: Optional[str] = None
    include_patterns: Optional[List[str]] = None
    exclude_patterns: Optional[List[str]] = None
    case_sensitive: bool = True
    context_lines: int = Field(default=0, ge=0, le=10)
    max_results: int = Field(default=100, ge=1, le=1000)
    multiline: bool = False
    pcre2: bool = False


# ---------------------------------------------------------------------------
# Helpers — extracted for easy mocking in tests
# ---------------------------------------------------------------------------


def _resolve_repo_path(alias: str) -> Optional[str]:
    """Resolve a global repo alias to its versioned snapshot path.

    Returns None when the alias is unknown.
    """
    from code_indexer.server.mcp.handlers.repos import _resolve_golden_repo_path

    return cast(Optional[str], _resolve_golden_repo_path(alias))


# ---------------------------------------------------------------------------
# Internal: single-repo search execution
# ---------------------------------------------------------------------------


async def _execute_single_search(
    body: RegexSearchRequest,
    repo_path_str: str,
    timeout_seconds: Optional[int] = None,
    user: Optional[User] = None,
) -> Dict[str, Any]:
    """Execute a single-repo regex search.

    Returns a dict with matches + metadata.
    Raises ValueError, TimeoutError, or RipgrepExecutionError on failure.
    """
    from pathlib import Path

    if user is not None:
        api_metrics_service.increment_regex_search(username=user.username)

    repo_path = Path(repo_path_str)
    service = RegexSearchService(repo_path)
    result = await service.search(
        pattern=body.pattern,
        path=body.path,
        include_patterns=body.include_patterns,
        exclude_patterns=body.exclude_patterns,
        case_sensitive=body.case_sensitive,
        context_lines=body.context_lines,
        max_results=body.max_results,
        multiline=body.multiline,
        pcre2=body.pcre2,
        timeout_seconds=timeout_seconds,
    )

    matches = [
        {
            "file_path": m.file_path,
            "line_number": m.line_number,
            "column": m.column,
            "line_content": m.line_content,
            "context_before": m.context_before,
            "context_after": m.context_after,
        }
        for m in result.matches
    ]

    return {
        "matches": matches,
        "total_matches": result.total_matches,
        "truncated": result.truncated,
        "search_engine": result.search_engine,
        "search_time_ms": result.search_time_ms,
    }


# ---------------------------------------------------------------------------
# Internal: omni (multi-repo) fan-out
# ---------------------------------------------------------------------------


async def _execute_omni_search(
    body: RegexSearchRequest,
    aliases: List[str],
    user: User,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Fan out search across multiple repos, collecting results and errors."""
    import time

    start_time = time.time()
    all_matches: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    repos_searched = 0
    truncated = False
    search_engine = "ripgrep"

    for alias in aliases:
        repo_path_str = _resolve_repo_path(alias)
        if repo_path_str is None:
            errors[alias] = f"Repository alias {alias!r} not found"
            continue

        try:
            result = await _execute_single_search(
                body, repo_path_str, timeout_seconds=timeout_seconds, user=user
            )
            repos_searched += 1
            search_engine = result.get("search_engine", search_engine)
            for match in result["matches"]:
                match["source_repo"] = alias
            all_matches.extend(result["matches"])
            if result.get("truncated"):
                truncated = True
        except Exception as e:
            errors[alias] = str(e)
            logger.warning(
                "Omni regex search failed for %r: %s",
                alias,
                e,
                exc_info=False,
            )

    elapsed_ms = (time.time() - start_time) * 1000
    return {
        "matches": all_matches,
        "total_matches": len(all_matches),
        "truncated": truncated,
        "search_engine": search_engine,
        "search_time_ms": elapsed_ms,
        "repos_searched": repos_searched,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.post(
    "/search",
    status_code=status.HTTP_200_OK,
)
async def regex_search(
    body: RegexSearchRequest,
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Execute a ripgrep-powered regex search against one or more golden repos.

    Supports single-repo and omni (multi-repo) searches. Mirrors the MCP
    regex_search tool but returns results synchronously (no background job).

    Error codes:
        auth_required           — missing query_repos permission (403)
        repository_not_found    — alias cannot be resolved (404)
        pcre2_unavailable       — PCRE2 not built into ripgrep (422)
        search_timeout          — search exceeded timeout (408)
        search_engine_error     — ripgrep execution failure (500)
    """
    from code_indexer.global_repos.regex_search import RipgrepExecutionError

    # ------------------------------------------------------------------
    # 1. Permission check
    # ------------------------------------------------------------------
    if not user.has_permission("query_repos"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "auth_required",
                "detail": "query_repos permission required",
            },
        )

    # ------------------------------------------------------------------
    # 2. Load configured timeout
    # ------------------------------------------------------------------
    config = get_config_service().get_config()
    effective_timeout = config.search_limits_config.timeout_seconds

    # ------------------------------------------------------------------
    # 3. Omni (multi-repo) path
    # ------------------------------------------------------------------
    if isinstance(body.repository_alias, list):
        aliases = body.repository_alias
        # Enforce omni fan-out cap (server invariant: omni_max_repos_per_search=50)
        max_repos = 50
        if len(aliases) > max_repos:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "too_many_repositories",
                    "detail": f"Maximum {max_repos} repositories per search, got {len(aliases)}",
                },
            )
        result = await _execute_omni_search(
            body, aliases, user, timeout_seconds=effective_timeout
        )
        return result

    # ------------------------------------------------------------------
    # 4. Single-repo path
    # ------------------------------------------------------------------
    repo_path_str = _resolve_repo_path(body.repository_alias)
    if repo_path_str is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "repository_not_found",
                "detail": f"Repository alias {body.repository_alias!r} not found",
            },
        )

    try:
        result = await _execute_single_search(
            body, repo_path_str, timeout_seconds=effective_timeout, user=user
        )
        return result
    except ValueError as e:
        error_msg = str(e)
        if "PCRE2" in error_msg or "pcre2" in error_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "pcre2_unavailable",
                    "detail": error_msg,
                },
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error_code": "invalid_request",
                "detail": error_msg,
            },
        )
    except TimeoutError as e:
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail={
                "error_code": "search_timeout",
                "detail": str(e),
            },
        )
    except RipgrepExecutionError as e:
        logger.error("Ripgrep execution error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_code": "search_engine_error",
                "detail": str(e),
            },
        )
