"""A catalog directory tree reduced to what the caller may see.

Every file node is judged by its full catalog-relative path (the requested
subdirectory prefixed), at every depth. A directory survives only while it
still holds a visible descendant, so hidden names vanish from the nodes and
from the rendered text; counts are recomputed from what is kept.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set

from ....global_repos.directory_explorer import (
    DirectoryExplorerService,
    DirectoryTreeResult,
    TreeNode,
)
from .visibility import resolve_meta_visibility

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService

CATALOG_TOP_DIRS = frozenset({"dependency-map", "memories", "xray-patterns"})


def _full_path(base_path: str, node_path: str) -> str:
    base = base_path.strip("/")
    return f"{base}/{node_path}" if base and node_path else base or node_path


def catalog_tree_paths(result: DirectoryTreeResult, base_path: str) -> List[str]:
    """Full catalog-relative paths of every file node in *result*."""
    paths: List[str] = []
    stack = [result.root]
    while stack:
        node = stack.pop()
        for child in node.children or []:
            if child.is_directory:
                stack.append(child)
            else:
                paths.append(_full_path(base_path, child.path))
    return paths


def _prune(
    node: TreeNode, base_path: str, allowed: Set[str], counts: Dict[str, int]
) -> Optional[TreeNode]:
    full = _full_path(base_path, node.path)
    if not node.is_directory:
        if full not in allowed:
            return None
        counts["files"] += 1
        return node
    kept: List[TreeNode] = []
    for child in node.children or []:
        pruned = _prune(child, base_path, allowed, counts)
        if pruned is not None:
            kept.append(pruned)
    depth_limited = node.truncated and not node.children
    if not kept and not (depth_limited and full in CATALOG_TOP_DIRS):
        return None
    counts["dirs"] += 1
    return replace(node, children=kept)


def restrict_catalog_tree(
    result: DirectoryTreeResult,
    base_path: str,
    username: str,
    access_service: Optional["AccessFilteringService"],
    show_stats: bool = False,
) -> DirectoryTreeResult:
    """*result* reduced to the caller's view; unchanged for unrestricted
    callers. Without the access service no file is shown."""
    if (
        access_service is not None
        and resolve_meta_visibility(username, access_service).unrestricted
    ):
        return result
    allowed = (
        set(
            access_service.filter_cidx_meta_files(
                catalog_tree_paths(result, base_path), username
            )
        )
        if access_service is not None
        else set()
    )
    counts = {"files": 0, "dirs": 0}
    kept: List[TreeNode] = []
    for child in result.root.children or []:
        pruned = _prune(child, base_path, allowed, counts)
        if pruned is not None:
            kept.append(pruned)
    root = replace(result.root, children=kept)
    tree_string = DirectoryExplorerService(Path(result.root_path))._format_tree_string(
        root, show_stats, counts["dirs"], counts["files"]
    )
    return DirectoryTreeResult(
        root=root,
        tree_string=tree_string,
        total_directories=counts["dirs"],
        total_files=counts["files"],
        max_depth_reached=result.max_depth_reached,
        root_path=result.root_path,
    )
