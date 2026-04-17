"""Read-only git handler functions for CIDX MCP server.

Covers: git_file_history, git_log, git_show_commit, git_file_at_revision,
git_diff (both older handle_git_diff and newer git_diff), git_blame,
git_search_commits, git_search_diffs, git_status, git_fetch,
git_branch_list, git_conflict_status.

The older handle_git_* variants are still referenced by omni handlers
(_omni_git_log, _omni_git_search_commits) and must be included here.
The newer git_diff / git_log variants overwrite the HANDLER_REGISTRY entries
for those keys (matching the order in the original _legacy.py).

Shared helpers that remain in _legacy.py and are accessed via _get_legacy():
  _resolve_git_repo_path, _resolve_repo_path, _is_git_repo,
  _find_latest_versioned_repo.

Stories: #496 (modularise MCP handlers), #34, #35, #555, #556, #558,
         #639, #653, #654, #658, #660, #686.
"""

from __future__ import annotations

import json as json_module
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from code_indexer.server.auth.user_manager import User
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.git_operations_service import (
    GitCommandError,
    git_operations_service,
)
from code_indexer.global_repos.git_operations import GitOperationsService
from code_indexer.server.mcp import reranking as _mcp_reranking

from ._utils import (
    _coerce_int,
    _expand_wildcard_patterns,
    _format_omni_response,
    _get_access_filtering_service,
    _mcp_response,
    _parse_json_string_array,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Forward references to helpers that remain in _legacy.py (shared with other
# domain modules — git_write, files, repos, etc.).  We import lazily inside
# each handler to avoid circular imports at module load time.
# ---------------------------------------------------------------------------


def _get_legacy():
    """Return the _legacy module for access to shared private helpers."""
    import code_indexer.server.mcp.handlers._legacy as _leg

    return _leg


# ---------------------------------------------------------------------------
# Story #653 AC3: Constants used by reranking helpers
# ---------------------------------------------------------------------------
_DEFAULT_OVERFETCH_MULTIPLIER = 5
_MAX_RERANK_FETCH_LIMIT = 200


# ---------------------------------------------------------------------------
# Private helpers exclusive to git_read
# ---------------------------------------------------------------------------


def _serialize_file_history_commits(commits: list) -> List[dict]:
    """Convert a list of FileHistoryCommit dataclass instances to JSON-serializable dicts."""
    return [
        {
            "hash": c.hash,
            "short_hash": c.short_hash,
            "author_name": c.author_name,
            "author_date": c.author_date,
            "subject": c.subject,
            "insertions": c.insertions,
            "deletions": c.deletions,
            "old_path": c.old_path,
        }
        for c in commits
    ]


def _compute_file_history_fetch_limit(
    requested_limit: int, rerank_query: Optional[str]
) -> int:
    """Return the effective fetch limit for get_file_history.

    When rerank_query is set, overfetch so the reranker has more candidates.
    The result is capped at _MAX_RERANK_FETCH_LIMIT.
    """
    if not rerank_query:
        return requested_limit
    _rc = get_config_service().get_config().rerank_config
    _m = getattr(_rc, "overfetch_multiplier", None) if _rc is not None else None
    multiplier = _m if isinstance(_m, int) and _m > 0 else _DEFAULT_OVERFETCH_MULTIPLIER
    return min(requested_limit * multiplier, _MAX_RERANK_FETCH_LIMIT)


# ---------------------------------------------------------------------------
# Public handlers — older handle_git_* variants (still called by omni helpers)
# ---------------------------------------------------------------------------


def handle_git_file_history(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_file_history tool - get commit history for a file."""
    leg = _get_legacy()
    _resolve_git_repo_path = leg._resolve_git_repo_path

    repository_alias = args.get("repository_alias")
    path = args.get("path")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not path:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: path"}
        )
    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None
        requested_limit = max(1, _coerce_int(args.get("limit"), 50))
        rerank_query = args.get("rerank_query") or None
        fetch_limit = _compute_file_history_fetch_limit(requested_limit, rerank_query)
        service = GitOperationsService(Path(repo_path))
        result = service.get_file_history(
            path=path,
            limit=fetch_limit,
            follow_renames=args.get("follow_renames", True),
        )
        commits = _serialize_file_history_commits(result.commits)
        # Story #658: Apply cross-encoder reranking after retrieval, before return.
        if rerank_query:
            commits, _rerank_meta = _mcp_reranking._apply_reranking_sync(
                results=commits,
                rerank_query=rerank_query,
                rerank_instruction=args.get("rerank_instruction"),
                content_extractor=lambda r: r.get("subject") or "",
                requested_limit=requested_limit,
                config_service=get_config_service(),
            )
        else:
            _rerank_meta = {
                "reranker_used": False,
                "reranker_provider": None,
                "rerank_time_ms": 0,
            }
        return _mcp_response(
            {
                "success": True,
                "path": result.path,
                "commits": commits,
                "total_count": result.total_count,
                "truncated": result.truncated,
                "renamed_from": result.renamed_from,
                "query_metadata": {
                    "reranker_used": _rerank_meta["reranker_used"],
                    "reranker_provider": _rerank_meta["reranker_provider"],
                    "rerank_time_ms": _rerank_meta["rerank_time_ms"],
                },
            }
        )
    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error in git_file_history: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def _omni_git_log(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-git-log across multiple repositories."""
    repo_aliases = args.get("repository_alias", [])
    repo_aliases = _expand_wildcard_patterns(repo_aliases, user)
    limit = _coerce_int(args.get("limit"), 20)

    if not repo_aliases:
        return _mcp_response(
            {
                "success": True,
                "commits": [],
                "total_count": 0,
                "truncated": False,
                "repos_searched": 0,
                "errors": {},
            }
        )

    all_commits = []
    errors = {}
    repos_searched = 0
    truncated = False

    per_repo_limit = max(1, limit // len(repo_aliases)) if repo_aliases else limit

    for repo_alias in repo_aliases:
        try:
            single_args = dict(args)
            single_args["repository_alias"] = repo_alias
            single_args["limit"] = per_repo_limit

            single_result = handle_git_log(single_args, user)

            resp_content = single_result.get("content", [])
            if resp_content and resp_content[0].get("type") == "text":
                result_data = json_module.loads(resp_content[0]["text"])
                if result_data.get("success"):
                    repos_searched += 1
                    commits = result_data.get("commits", [])
                    for c in commits:
                        c["source_repo"] = repo_alias
                    all_commits.extend(commits)
                    if result_data.get("truncated"):
                        truncated = True
                else:
                    errors[repo_alias] = result_data.get("error", "Unknown error")
        except Exception as e:
            errors[repo_alias] = str(e)
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-058",
                    f"Omni-git-log failed for {repo_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    # Sort by date descending and apply limit
    all_commits.sort(key=lambda x: x.get("date", ""), reverse=True)
    final_commits = all_commits[:limit]

    # Story #331 AC7: Filter errors dict to hide unauthorized repo aliases
    _ac7_service = _get_access_filtering_service()
    if _ac7_service and not _ac7_service.is_admin_user(user.username):
        _ac7_accessible = _ac7_service.get_accessible_repos(user.username)
        errors = {
            k: v
            for k, v in errors.items()
            if k.removesuffix("-global") in _ac7_accessible or k in _ac7_accessible
        }

    response_format = args.get("response_format", "flat")
    formatted = _format_omni_response(
        all_results=final_commits,
        response_format=response_format,
        total_repos_searched=repos_searched,
        errors=errors,
    )
    formatted["truncated"] = truncated or len(all_commits) > limit
    if response_format == "flat":
        formatted["commits"] = formatted.pop("results")
        formatted["total_count"] = formatted.pop("total_results")
        formatted["repos_searched"] = formatted.pop("total_repos_searched")
    return _mcp_response(formatted)


def handle_git_log(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_log tool - retrieve commit history from a repository.

    Story #35: When log exceeds git_log_max_tokens, stores full log in
    PayloadCache and returns cache_handle for paginated retrieval.
    """
    from code_indexer.server.mcp.handlers import _utils as _handler_utils

    leg = _get_legacy()
    _resolve_git_repo_path = leg._resolve_git_repo_path

    repository_alias = args.get("repository_alias")
    repository_alias = _parse_json_string_array(repository_alias)
    args["repository_alias"] = repository_alias  # Update args for downstream

    # Route to omni-search when repository_alias is an array
    if isinstance(repository_alias, list):
        return _omni_git_log(args, user)

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        # Story #660: Extract reranking parameters and compute fetch limit
        requested_limit = max(1, _coerce_int(args.get("limit"), 50))
        rerank_query = args.get("rerank_query") or None
        fetch_limit = _compute_file_history_fetch_limit(requested_limit, rerank_query)

        # Create service and execute query
        from code_indexer.global_repos.git_operations import GitOperationsService

        service = GitOperationsService(Path(repo_path))
        result = service.get_log(
            limit=fetch_limit,
            path=args.get("path"),
            author=args.get("author"),
            since=args.get("since"),
            until=args.get("until"),
            branch=args.get("branch"),
        )

        # Convert dataclasses to dicts for JSON serialization
        commits = [
            {
                "hash": c.hash,
                "short_hash": c.short_hash,
                "author_name": c.author_name,
                "author_email": c.author_email,
                "author_date": c.author_date,
                "committer_name": c.committer_name,
                "committer_email": c.committer_email,
                "committer_date": c.committer_date,
                "subject": c.subject,
                "body": c.body,
            }
            for c in result.commits
        ]

        # Story #660: Apply cross-encoder reranking after retrieval, before caching.
        if rerank_query:
            commits, _rerank_meta = _mcp_reranking._apply_reranking_sync(
                results=commits,
                rerank_query=rerank_query,
                rerank_instruction=args.get("rerank_instruction"),
                content_extractor=lambda r: "\n".join(
                    p for p in [r.get("subject") or "", r.get("body") or ""] if p
                ),
                requested_limit=requested_limit,
                config_service=get_config_service(),
            )
        else:
            _rerank_meta = {
                "reranker_used": False,
                "reranker_provider": None,
                "rerank_time_ms": 0,
            }

        # Story #35: Build full log result for potential caching
        full_log_data = {
            "commits": commits,
            "total_count": result.total_count,
        }

        # Story #35: Apply token-based truncation with cache handle support
        payload_cache = getattr(
            _handler_utils.app_module.app.state, "payload_cache", None
        )
        config_service = get_config_service()
        content_limits = config_service.get_config().content_limits_config

        # Initialize truncation fields
        cache_handle = None
        truncated = False
        total_tokens = 0
        preview_tokens = 0
        total_pages = 0
        has_more = False

        # Serialize log result to JSON for token counting and caching
        log_json = json_module.dumps(full_log_data)

        if payload_cache is not None and log_json and content_limits is not None:
            from code_indexer.server.cache.truncation_helper import TruncationHelper

            truncation_helper = TruncationHelper(payload_cache, content_limits)
            truncation_result = truncation_helper.truncate_and_cache(
                content=log_json,
                content_type="log",
            )

            cache_handle = truncation_result.cache_handle
            truncated = truncation_result.truncated
            total_tokens = truncation_result.original_tokens
            preview_tokens = truncation_result.preview_tokens
            total_pages = truncation_result.total_pages
            has_more = truncation_result.has_more

        return _mcp_response(
            {
                "success": True,
                "commits": commits,
                "total_count": result.total_count,
                # Story #35: Truncation metadata fields
                "cache_handle": cache_handle,
                "truncated": truncated,
                "total_tokens": total_tokens,
                "preview_tokens": preview_tokens,
                "total_pages": total_pages,
                "has_more": has_more,
                # Story #660: Reranking telemetry
                "query_metadata": {
                    "reranker_used": _rerank_meta["reranker_used"],
                    "reranker_provider": _rerank_meta["reranker_provider"],
                    "rerank_time_ms": _rerank_meta["rerank_time_ms"],
                },
            }
        )

    except Exception as e:
        logger.exception(
            f"Error in git_log: {e}", extra={"correlation_id": get_correlation_id()}
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_git_show_commit(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_show_commit tool - get detailed commit information."""
    leg = _get_legacy()
    _resolve_git_repo_path = leg._resolve_git_repo_path

    repository_alias = args.get("repository_alias")
    commit_hash = args.get("commit_hash")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not commit_hash:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: commit_hash"}
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        # Create service and execute query
        from code_indexer.global_repos.git_operations import GitOperationsService

        service = GitOperationsService(Path(repo_path))
        result = service.show_commit(
            commit_hash=commit_hash,
            include_diff=args.get("include_diff", False),
            include_stats=args.get("include_stats", True),
        )

        # Convert dataclasses to dicts for JSON serialization
        commit_dict = {
            "hash": result.commit.hash,
            "short_hash": result.commit.short_hash,
            "author_name": result.commit.author_name,
            "author_email": result.commit.author_email,
            "author_date": result.commit.author_date,
            "committer_name": result.commit.committer_name,
            "committer_email": result.commit.committer_email,
            "committer_date": result.commit.committer_date,
            "subject": result.commit.subject,
            "body": result.commit.body,
        }

        stats_list = None
        if result.stats is not None:
            stats_list = [
                {
                    "path": s.path,
                    "insertions": s.insertions,
                    "deletions": s.deletions,
                    "status": s.status,
                }
                for s in result.stats
            ]

        return _mcp_response(
            {
                "success": True,
                "commit": commit_dict,
                "stats": stats_list,
                "diff": result.diff,
                "parents": result.parents,
            }
        )

    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error in git_show_commit: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_git_file_at_revision(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_file_at_revision tool - get file contents at specific revision."""
    leg = _get_legacy()
    _resolve_git_repo_path = leg._resolve_git_repo_path

    repository_alias = args.get("repository_alias")
    path = args.get("path")
    revision = args.get("revision")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not path:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: path"}
        )
    if not revision:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: revision"}
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        # Create service and execute query
        from code_indexer.global_repos.git_operations import GitOperationsService

        service = GitOperationsService(Path(repo_path))
        result = service.get_file_at_revision(path=path, revision=revision)

        return _mcp_response(
            {
                "success": True,
                "path": result.path,
                "revision": result.revision,
                "resolved_revision": result.resolved_revision,
                "content": result.content,
                "size_bytes": result.size_bytes,
            }
        )

    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error in git_file_at_revision: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_git_diff(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_diff tool - get diff between revisions.

    Story #34: When diff exceeds git_diff_max_tokens, stores full diff in
    PayloadCache and returns cache_handle for paginated retrieval.
    """
    from code_indexer.server.mcp.handlers import _utils as _handler_utils

    leg = _get_legacy()
    _resolve_git_repo_path = leg._resolve_git_repo_path

    repository_alias = args.get("repository_alias")
    from_revision = args.get("from_revision")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not from_revision:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: from_revision"}
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        # Create service and execute query
        from code_indexer.global_repos.git_operations import GitOperationsService

        service = GitOperationsService(Path(repo_path))
        result = service.get_diff(
            from_revision=from_revision,
            to_revision=args.get("to_revision"),
            path=args.get("path"),
            context_lines=args.get("context_lines", 3),
            stat_only=args.get("stat_only", False),
        )

        # Convert dataclasses to dicts for JSON serialization
        files = [
            {
                "path": f.path,
                "old_path": f.old_path,
                "status": f.status,
                "insertions": f.insertions,
                "deletions": f.deletions,
                "hunks": [
                    {
                        "old_start": h.old_start,
                        "old_count": h.old_count,
                        "new_start": h.new_start,
                        "new_count": h.new_count,
                        "content": h.content,
                    }
                    for h in f.hunks
                ],
            }
            for f in result.files
        ]

        # Story #34: Build full diff result for potential caching
        full_diff_data = {
            "from_revision": result.from_revision,
            "to_revision": result.to_revision,
            "files": files,
            "total_insertions": result.total_insertions,
            "total_deletions": result.total_deletions,
            "stat_summary": result.stat_summary,
        }

        # Story #34: Apply token-based truncation with cache handle support
        payload_cache = getattr(
            _handler_utils.app_module.app.state, "payload_cache", None
        )
        config_service = get_config_service()
        content_limits = config_service.get_config().content_limits_config

        # Initialize truncation fields
        cache_handle = None
        truncated = False
        total_tokens = 0
        preview_tokens = 0
        total_pages = 0
        has_more = False

        # Serialize diff result to JSON for token counting and caching
        diff_json = json_module.dumps(full_diff_data)

        if payload_cache is not None and diff_json and content_limits is not None:
            from code_indexer.server.cache.truncation_helper import TruncationHelper

            truncation_helper = TruncationHelper(payload_cache, content_limits)
            truncation_result = truncation_helper.truncate_and_cache(
                content=diff_json,
                content_type="diff",
            )

            cache_handle = truncation_result.cache_handle
            truncated = truncation_result.truncated
            total_tokens = truncation_result.original_tokens
            preview_tokens = truncation_result.preview_tokens
            total_pages = truncation_result.total_pages
            has_more = truncation_result.has_more

        return _mcp_response(
            {
                "success": True,
                "from_revision": result.from_revision,
                "to_revision": result.to_revision,
                "files": files,
                "total_insertions": result.total_insertions,
                "total_deletions": result.total_deletions,
                "stat_summary": result.stat_summary,
                # Story #34: Truncation metadata fields
                "cache_handle": cache_handle,
                "truncated": truncated,
                "total_tokens": total_tokens,
                "preview_tokens": preview_tokens,
                "total_pages": total_pages,
                "has_more": has_more,
            }
        )

    except Exception as e:
        logger.exception(
            f"Error in git_diff: {e}", extra={"correlation_id": get_correlation_id()}
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_git_blame(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_blame tool - get line-by-line blame annotations."""
    leg = _get_legacy()
    _resolve_git_repo_path = leg._resolve_git_repo_path

    repository_alias = args.get("repository_alias")
    path = args.get("path")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not path:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: path"}
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        # Create service and execute query
        from code_indexer.global_repos.git_operations import GitOperationsService

        service = GitOperationsService(Path(repo_path))
        result = service.get_blame(
            path=path,
            revision=args.get("revision"),
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
        )

        # Convert dataclasses to dicts for JSON serialization
        lines = [
            {
                "line_number": line.line_number,
                "commit_hash": line.commit_hash,
                "short_hash": line.short_hash,
                "author_name": line.author_name,
                "author_email": line.author_email,
                "author_date": line.author_date,
                "original_line_number": line.original_line_number,
                "content": line.content,
            }
            for line in result.lines
        ]

        return _mcp_response(
            {
                "success": True,
                "path": result.path,
                "revision": result.revision,
                "lines": lines,
                "unique_commits": result.unique_commits,
            }
        )

    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error in git_blame: {e}", extra={"correlation_id": get_correlation_id()}
        )
        return _mcp_response({"success": False, "error": str(e)})


def _omni_git_search_commits(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-git-search across multiple repositories."""
    repo_aliases = args.get("repository_alias", [])
    repo_aliases = _expand_wildcard_patterns(repo_aliases, user)
    query = args.get("query", "")
    is_regex = args.get("is_regex", False)

    if not repo_aliases:
        return _mcp_response(
            {
                "success": True,
                "query": query,
                "is_regex": is_regex,
                "matches": [],
                "total_matches": 0,
                "truncated": False,
                "search_time_ms": 0,
                "repos_searched": 0,
                "errors": {},
            }
        )

    start_time = time.time()
    all_matches = []
    errors = {}
    repos_searched = 0
    truncated = False

    for repo_alias in repo_aliases:
        try:
            single_args = dict(args)
            single_args["repository_alias"] = repo_alias

            single_result = handle_git_search_commits(single_args, user)

            resp_content = single_result.get("content", [])
            if resp_content and resp_content[0].get("type") == "text":
                result_data = json_module.loads(resp_content[0]["text"])
                if result_data.get("success"):
                    repos_searched += 1
                    matches = result_data.get("matches", [])
                    for m in matches:
                        m["source_repo"] = repo_alias
                    all_matches.extend(matches)
                    if result_data.get("truncated"):
                        truncated = True
                else:
                    errors[repo_alias] = result_data.get("error", "Unknown error")
        except Exception as e:
            errors[repo_alias] = str(e)
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-059",
                    f"Omni-git-search failed for {repo_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    elapsed_ms = int((time.time() - start_time) * 1000)

    # Story #653/#654: Apply cross-encoder reranking after collecting all matches
    _commit_limit = _coerce_int(args.get("limit"), len(all_matches))
    all_matches, _rerank_meta = _mcp_reranking._apply_reranking_sync(
        results=all_matches,
        rerank_query=args.get("rerank_query"),
        rerank_instruction=args.get("rerank_instruction"),
        content_extractor=lambda r: (
            ((r.get("subject") or "") + " " + (r.get("body") or "")).strip()
        ),
        requested_limit=_commit_limit,
        config_service=get_config_service(),
    )

    # Story #331 AC7: Filter errors dict to hide unauthorized repo aliases
    _ac7_service = _get_access_filtering_service()
    if _ac7_service and not _ac7_service.is_admin_user(user.username):
        _ac7_accessible = _ac7_service.get_accessible_repos(user.username)
        errors = {
            k: v
            for k, v in errors.items()
            if k.removesuffix("-global") in _ac7_accessible or k in _ac7_accessible
        }

    response_format = args.get("response_format", "flat")
    formatted = _format_omni_response(
        all_results=all_matches,
        response_format=response_format,
        total_repos_searched=repos_searched,
        errors=errors,
    )
    formatted["query"] = query
    formatted["is_regex"] = is_regex
    formatted["truncated"] = truncated
    formatted["search_time_ms"] = elapsed_ms
    # Story #654: reranker telemetry
    formatted["query_metadata"] = {
        "reranker_used": _rerank_meta["reranker_used"],
        "reranker_provider": _rerank_meta["reranker_provider"],
        "rerank_time_ms": _rerank_meta["rerank_time_ms"],
    }
    if response_format == "flat":
        formatted["matches"] = formatted.pop("results")
        formatted["total_matches"] = formatted.pop("total_results")
        formatted["repos_searched"] = formatted.pop("total_repos_searched")
    return _mcp_response(formatted)


def handle_git_search_commits(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_search_commits tool - search commit messages."""
    leg = _get_legacy()
    _resolve_git_repo_path = leg._resolve_git_repo_path

    repository_alias = args.get("repository_alias")
    repository_alias = _parse_json_string_array(repository_alias)
    args["repository_alias"] = repository_alias  # Update args for downstream
    query = args.get("query")

    # Route to omni-search when repository_alias is an array
    if isinstance(repository_alias, list):
        return _omni_git_search_commits(args, user)

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not query:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: query"}
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        # Create service and execute search
        from code_indexer.global_repos.git_operations import GitOperationsService

        service = GitOperationsService(Path(repo_path))
        result = service.search_commits(
            query=query,
            is_regex=args.get("is_regex", False),
            author=args.get("author"),
            since=args.get("since"),
            until=args.get("until"),
            limit=_coerce_int(args.get("limit"), 50),
        )

        # Convert dataclasses to dicts for JSON serialization
        matches = [
            {
                "hash": m.hash,
                "short_hash": m.short_hash,
                "author_name": m.author_name,
                "author_email": m.author_email,
                "author_date": m.author_date,
                "subject": m.subject,
                "body": m.body,
                "match_highlights": m.match_highlights,
            }
            for m in result.matches
        ]

        # Story #653/#654: Apply cross-encoder reranking after retrieval, before return
        _commit_limit = _coerce_int(args.get("limit"), len(matches))
        matches, _rerank_meta = _mcp_reranking._apply_reranking_sync(
            results=matches,
            rerank_query=args.get("rerank_query"),
            rerank_instruction=args.get("rerank_instruction"),
            content_extractor=lambda r: (
                ((r.get("subject") or "") + " " + (r.get("body") or "")).strip()
            ),
            requested_limit=_commit_limit,
            config_service=get_config_service(),
        )

        return _mcp_response(
            {
                "success": True,
                "query": result.query,
                "is_regex": result.is_regex,
                "matches": matches,
                "total_matches": len(matches),
                "truncated": result.truncated,
                "search_time_ms": result.search_time_ms,
                # Story #654: reranker telemetry
                "query_metadata": {
                    "reranker_used": _rerank_meta["reranker_used"],
                    "reranker_provider": _rerank_meta["reranker_provider"],
                    "rerank_time_ms": _rerank_meta["rerank_time_ms"],
                },
            }
        )

    except Exception as e:
        logger.exception(
            f"Error in git_search_commits: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_git_search_diffs(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_search_diffs tool - search for code changes (pickaxe search)."""
    leg = _get_legacy()
    _resolve_git_repo_path = leg._resolve_git_repo_path

    repository_alias = args.get("repository_alias")
    search_string = args.get("search_string")
    search_pattern = args.get("search_pattern")
    is_regex = args.get("is_regex", False)

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    # Determine which search parameter to use based on is_regex
    search_term = search_pattern if is_regex else search_string

    # Validate that at least one search parameter is provided
    if not search_term:
        return _mcp_response(
            {
                "success": False,
                "error": "Missing required parameter: search_string or search_pattern",
            }
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        # Create service and execute search
        service = GitOperationsService(Path(repo_path))
        is_regex = args.get("is_regex", False)
        if is_regex:
            result = service.search_diffs(
                search_pattern=search_term,
                is_regex=True,
                path=args.get("path"),
                since=args.get("since"),
                until=args.get("until"),
                limit=_coerce_int(args.get("limit"), 50),
            )
        else:
            result = service.search_diffs(
                search_string=search_term,
                is_regex=False,
                path=args.get("path"),
                since=args.get("since"),
                until=args.get("until"),
                limit=_coerce_int(args.get("limit"), 50),
            )

        # Convert dataclasses to dicts for JSON serialization
        matches = [
            {
                "hash": m.hash,
                "short_hash": m.short_hash,
                "author_name": m.author_name,
                "author_date": m.author_date,
                "subject": m.subject,
                "files_changed": m.files_changed,
                "diff_snippet": m.diff_snippet,
            }
            for m in result.matches
        ]

        # Story #653/#654: Apply cross-encoder reranking after retrieval, before return
        matches, _rerank_meta = _mcp_reranking._apply_reranking_sync(
            results=matches,
            rerank_query=args.get("rerank_query"),
            rerank_instruction=args.get("rerank_instruction"),
            content_extractor=lambda r: (
                r.get("diff_snippet") or r.get("subject") or ""
            ),
            requested_limit=_coerce_int(args.get("limit"), 50),
            config_service=get_config_service(),
        )

        return _mcp_response(
            {
                "success": True,
                "search_term": result.search_term,
                "is_regex": result.is_regex,
                "matches": matches,
                "total_matches": result.total_matches,
                "truncated": result.truncated,
                "search_time_ms": result.search_time_ms,
                # Story #654: reranker telemetry
                "query_metadata": {
                    "reranker_used": _rerank_meta["reranker_used"],
                    "reranker_provider": _rerank_meta["reranker_provider"],
                    "rerank_time_ms": _rerank_meta["rerank_time_ms"],
                },
            }
        )

    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error in git_search_diffs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_status(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_status tool - get repository working tree status."""
    leg = _get_legacy()
    _resolve_git_repo_path = leg._resolve_git_repo_path

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        result = git_operations_service.git_status(Path(repo_path))
        result["success"] = True
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-063",
                f"git_status failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_status: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_fetch(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_fetch tool - fetch refs from remote."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        # Bug #639: Call fetch_from_remote wrapper to trigger migration if needed
        remote = args.get("remote", "origin")
        result = git_operations_service.fetch_from_remote(
            repo_alias=repository_alias, username=user.username, remote=remote
        )
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-069",
                f"git_fetch failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_fetch: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_branch_list(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_branch_list tool - list all branches."""
    leg = _get_legacy()
    _resolve_git_repo_path = leg._resolve_git_repo_path

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        result = git_operations_service.git_branch_list(Path(repo_path))
        result["success"] = True
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-074",
                f"git_branch_list failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_branch_list: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_conflict_status(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_conflict_status tool - get detailed merge conflict status."""
    leg = _get_legacy()
    _resolve_git_repo_path = leg._resolve_git_repo_path

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        result = git_operations_service.git_conflict_status(Path(repo_path))
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-217",
                f"git_conflict_status failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_conflict_status: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Newer git_diff / git_log variants — these overwrite the HANDLER_REGISTRY
# entries for "git_diff" and "git_log" (same ordering as original _legacy.py).
# The older handle_git_diff / handle_git_log remain reachable for omni helpers.
# ---------------------------------------------------------------------------


def git_diff(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_diff tool - get diff of working tree changes with pagination."""
    leg = _get_legacy()
    _resolve_git_repo_path = leg._resolve_git_repo_path

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        # Bug #696: from_revision is required by the schema
        from_revision = args.get("from_revision")
        if not from_revision:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: from_revision"}
            )

        # Bug #696: Read all schema-advertised parameters and forward to service
        to_revision = args.get("to_revision")
        path = args.get("path")
        context_lines_raw = args.get("context_lines")
        context_lines = (
            _coerce_int(context_lines_raw, 3) if context_lines_raw is not None else None
        )
        stat_only = args.get("stat_only")

        # Story #686: Extract pagination parameters
        file_paths = args.get("file_paths")
        offset = _coerce_int(args.get("offset"), 0)
        limit = (
            _coerce_int(args.get("limit"), 500)
            if args.get("limit") is not None
            else None
        )  # None means use default (500)

        result = git_operations_service.git_diff(
            Path(repo_path),
            file_paths=file_paths,
            context_lines=context_lines,
            from_revision=from_revision,
            to_revision=to_revision,
            path=path,
            stat_only=stat_only,
            offset=offset,
            limit=limit,
        )
        result["success"] = True
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-078",
                f"git_diff failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_diff: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_log(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_log tool - get commit history with pagination."""
    leg = _get_legacy()
    _resolve_git_repo_path = leg._resolve_git_repo_path

    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})
        assert repo_path is not None  # narrowed by error_msg check above

        # Story #686: Updated default limit to 50, added offset parameter
        # Story #660: Extract reranking parameters and compute fetch limit
        requested_limit = max(1, _coerce_int(args.get("limit"), 50))
        rerank_query = args.get("rerank_query") or None
        fetch_limit = _compute_file_history_fetch_limit(requested_limit, rerank_query)
        offset = _coerce_int(args.get("offset"), 0)

        # Bug #697 Defect 2: schema param is "since", service param is "since_date"
        # — kept distinct to avoid touching service callers.
        since_date = args.get("since")

        # Bug #697 Defect 1: read and forward all schema-advertised filter params
        until = args.get("until")
        author = args.get("author")
        branch = args.get("branch")
        path = args.get("path")

        result = git_operations_service.git_log(
            Path(repo_path),
            limit=fetch_limit,
            offset=offset,
            since_date=since_date,
            until=until,
            author=author,
            branch=branch,
            path=path,
        )

        # Story #660: Apply reranking after retrieval, before response.
        if rerank_query:
            result["commits"], _rerank_meta = _mcp_reranking._apply_reranking_sync(
                results=result["commits"],
                rerank_query=rerank_query,
                rerank_instruction=args.get("rerank_instruction"),
                content_extractor=lambda r: r.get("message") or "",
                requested_limit=requested_limit,
                config_service=get_config_service(),
            )
        else:
            _rerank_meta = {
                "reranker_used": False,
                "reranker_provider": None,
                "rerank_time_ms": 0,
            }

        # Update commits_returned unconditionally (reranking may truncate)
        result["commits_returned"] = len(result["commits"])
        result["success"] = True
        result["query_metadata"] = {
            "reranker_used": _rerank_meta["reranker_used"],
            "reranker_provider": _rerank_meta["reranker_provider"],
            "rerank_time_ms": _rerank_meta["rerank_time_ms"],
        }
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-079",
                f"git_log failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_log: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _register(registry: dict) -> None:
    """Register all git read handlers into the provided HANDLER_REGISTRY.

    Registration order mirrors _legacy.py to preserve overwrite semantics:
    - git_log / git_diff registered first with older handle_* variants
    - Then overwritten with the newer git_log / git_diff variants
    """
    # Older variants (registered first, same order as _legacy.py)
    registry["git_log"] = handle_git_log
    registry["git_show_commit"] = handle_git_show_commit
    registry["git_file_at_revision"] = handle_git_file_at_revision
    registry["git_diff"] = handle_git_diff
    registry["git_blame"] = handle_git_blame
    registry["git_file_history"] = handle_git_file_history
    registry["git_search_commits"] = handle_git_search_commits
    registry["git_search_diffs"] = handle_git_search_diffs
    registry["git_status"] = git_status
    registry["git_fetch"] = git_fetch
    registry["git_branch_list"] = git_branch_list
    registry["git_conflict_status"] = git_conflict_status
    # Newer variants overwrite the registry entries (same as _legacy.py)
    registry["git_diff"] = git_diff
    registry["git_log"] = git_log
