"""xray_dump_ast MCP handler (Issue #1935 Part 2).

Moved verbatim out of the monolithic xray.py -- pure relocation, zero
behaviour change. `handle_xray_dump_ast` below is an intact, unmodified
copy of HEAD's single function (git show HEAD:.../xray.py:1825-2001).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from code_indexer.server.auth.user_manager import User
from code_indexer.xray.search_engine import XRaySearchEngine

from ._infra import (
    _DUMP_AST_MAX_NODES_DEFAULT,
    _DUMP_AST_MAX_NODES_MIN,
    _DUMP_AST_MAX_NODES_MAX,
    _DUMP_AST_MAX_FILE_SIZE_BYTES,
    _resolve_repo_path,
    logger,  # shared logger name -- see _infra.py's own comment
)

from .. import _utils
from .._utils import _mcp_response


def handle_xray_dump_ast(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """MCP handler for the xray_dump_ast tool (Issue #19).

    Synchronous single-file AST dump — no background job.  Returns the
    parse tree of a single file within a repository snapshot inline.

    Auth: query_repos permission required.

    Inputs:
        repository_alias (str): Global repository alias.
        file_path (str): Relative path within the repository.

    Output:
        {ast_tree: <BFS-serialised root node>} on success, or an error dict.

    Error codes:
        auth_required              — unauthenticated or missing query_repos.
        repository_not_found       — alias cannot be resolved.
        invalid_file_path          — file_path is empty or absolute.
        path_traversal_rejected    — file_path escapes the repository root.
        file_not_found             — resolved path does not exist.
        unsupported_language       — no tree-sitter grammar for the extension.
        xray_extras_not_installed  — tree-sitter extras not installed.
        parse_error                — unexpected failure during AST parsing.
    """
    # 1. Auth + permission check
    if user is None or not user.has_permission("query_repos"):
        return _mcp_response({"error": "auth_required"})

    # 2. Parameter extraction
    repo_alias: str = params.get("repository_alias", "")
    file_path_raw: str = params.get("file_path", "")

    # max_nodes: optional int, default 500, range [1, 2000] (Finding 3.2, v10.4.4)
    max_nodes_raw = params.get("max_nodes", _DUMP_AST_MAX_NODES_DEFAULT)
    try:
        max_nodes = int(max_nodes_raw)
    except (TypeError, ValueError):
        return _mcp_response(
            {
                "error": "max_nodes_invalid",
                "message": f"max_nodes must be int, got {max_nodes_raw!r}",
            }
        )
    if not (_DUMP_AST_MAX_NODES_MIN <= max_nodes <= _DUMP_AST_MAX_NODES_MAX):
        return _mcp_response(
            {
                "error": "max_nodes_out_of_range",
                "message": (
                    f"max_nodes must be in "
                    f"[{_DUMP_AST_MAX_NODES_MIN}, {_DUMP_AST_MAX_NODES_MAX}]"
                ),
            }
        )

    if not file_path_raw:
        return _mcp_response(
            {"error": "invalid_file_path", "message": "file_path must not be empty"}
        )

    # Story #1039: bare-to-global alias fallback (read-only handler).
    if isinstance(repo_alias, str) and not repo_alias.endswith("-global"):
        # Bug #1709: probes via _utils._lazy_module_attr_or_none() (the
        # generalized form of Bug #1693's _lazy_singleton_app_or_none())
        # instead of a bare getattr(_utils.app_module, name, None), which
        # would otherwise permanently construct the process-wide app
        # singleton as a side effect of merely reading it.
        _arm = _utils._lazy_module_attr_or_none("activated_repo_manager")
        _grm = _utils._lazy_module_attr_or_none("golden_repo_manager")
        if _arm is not None and _grm is not None:
            if not _arm.user_has_activated_repo(user.username, repo_alias):
                from .._global_fallback import try_global_fallback

                _promoted = try_global_fallback(repo_alias, _grm)
                if _promoted is not None:
                    logger.info(
                        "bare-alias fallback: %r -> %r for user %r",
                        repo_alias,
                        _promoted,
                        user.username,
                    )
                    repo_alias = _promoted
                    params["repository_alias"] = _promoted

    # 3. Repository alias resolution
    repo_path_str = _resolve_repo_path(repo_alias)
    if repo_path_str is None:
        return _mcp_response(
            {
                "error": "repository_not_found",
                "message": f"Repository alias {repo_alias!r} not found",
            }
        )

    repo_root = Path(repo_path_str)

    # 4. Path traversal protection — resolve and verify the path stays within repo root.
    target = (repo_root / file_path_raw).resolve()
    try:
        target.relative_to(repo_root.resolve())
    except ValueError:
        return _mcp_response(
            {
                "error": "path_traversal_rejected",
                "message": (
                    f"file_path {file_path_raw!r} resolves outside the repository root"
                ),
            }
        )

    # 5. File existence check
    if not target.is_file():
        return _mcp_response(
            {
                "error": "file_not_found",
                "message": f"File not found: {file_path_raw!r}",
            }
        )

    # 5.5. File-size cap (Story #1494 AC1, Finding A2): refuse BEFORE
    # touching tree-sitter at all -- os.stat() is a cheap syscall, so a
    # file above the cap is rejected near-instantly instead of triggering
    # an unbounded, GIL-held in-process parse (159ms measured on a 759KB
    # file per the report; scales with file size, with no upper bound).
    file_size = target.stat().st_size
    if file_size > _DUMP_AST_MAX_FILE_SIZE_BYTES:
        return _mcp_response(
            {
                "error": "file_too_large",
                "message": (
                    f"AST dump refused: file exceeds the in-process parse "
                    f"size limit of {_DUMP_AST_MAX_FILE_SIZE_BYTES} bytes "
                    f"(file is {file_size} bytes). Narrow to a smaller "
                    f"file, or use xray_search/xray_explore (the Rust "
                    f"subprocess scan path) which has no such limit."
                ),
            }
        )

    # 6. Parse and serialise
    try:
        engine = XRaySearchEngine()
        lang = engine.ast_engine.detect_language(target)
        if lang is None:
            return _mcp_response(
                {
                    "error": "unsupported_language",
                    "message": (
                        f"No tree-sitter grammar for extension {target.suffix!r}"
                    ),
                }
            )
        source_bytes = target.read_bytes()
        root = engine.ast_engine.parse(source_bytes, lang)
        ast_tree = XRaySearchEngine._serialize_ast(root, max_nodes=max_nodes)
        return _mcp_response(
            {
                "ast_tree": ast_tree,
                "file_path": file_path_raw,
                "language": lang,
            }
        )
    except ImportError as exc:
        return _mcp_response(
            {
                "error": "xray_extras_not_installed",
                "message": str(exc),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("xray_dump_ast parse error for %s: %s", file_path_raw, exc)
        return _mcp_response(
            {
                "error": "parse_error",
                "message": str(exc),
            }
        )
