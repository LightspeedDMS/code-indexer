"""A catalog directory tree reduced to what the caller may see.

Every file node is judged by its full catalog-relative path (the requested
subdirectory prefixed), at every depth. A directory survives only while it
still holds a visible descendant, so hidden names vanish from the nodes and
from the rendered text. The per-directory file limit is applied to visible
files only, so truncation markers and counts never include hidden entries;
the raw tree must therefore be generated without a file limit.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from ....global_repos.directory_explorer import (
    DirectoryExplorerService,
    DirectoryTreeResult,
    TreeNode,
)
from .visibility import resolve_meta_visibility

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService

CATALOG_TOP_DIRS = frozenset({"dependency-map", "memories", "xray-patterns"})
# Per-directory file limit for generating the raw catalog tree, so the
# caller's own limit can be applied to visible files only.
UNSCANNED_LIMIT = 2**31 - 1


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


class _Pruner:
    def __init__(
        self, base_path: str, allowed: Set[str], max_files_per_dir: Optional[int]
    ) -> None:
        self.base_path = base_path
        self.allowed = allowed
        self.limit = max_files_per_dir
        self.counts: Dict[str, int] = {"files": 0, "dirs": 0}

    def children(self, node: TreeNode) -> Tuple[List[TreeNode], int]:
        """Visible children of *node* (directories first) and how many
        visible files the per-directory limit leaves out."""
        dirs: List[TreeNode] = []
        files: List[TreeNode] = []
        for child in node.children or []:
            if child.is_directory:
                pruned = self.directory(child)
                if pruned is not None:
                    dirs.append(pruned)
            elif _full_path(self.base_path, child.path) in self.allowed:
                files.append(child)
        left_out = 0
        if self.limit is not None and len(files) > self.limit:
            left_out = len(files) - self.limit
            files = files[: self.limit]
        self.counts["files"] += len(files)
        return dirs + files, left_out

    def directory(self, node: TreeNode) -> Optional[TreeNode]:
        depth_limited = node.truncated and not node.children and not node.hidden_count
        kept, left_out = self.children(node)
        full = _full_path(self.base_path, node.path)
        if not kept and not (depth_limited and full in CATALOG_TOP_DIRS):
            return None
        self.counts["dirs"] += 1
        return replace(
            node,
            children=kept,
            truncated=depth_limited or left_out > 0,
            hidden_count=left_out,
        )


def restrict_catalog_tree(
    result: DirectoryTreeResult,
    base_path: str,
    username: str,
    access_service: Optional["AccessFilteringService"],
    show_stats: bool = False,
    max_files_per_dir: Optional[int] = None,
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
    pruner = _Pruner(base_path, allowed, max_files_per_dir)
    kept, left_out = pruner.children(result.root)
    root = replace(
        result.root, children=kept, truncated=left_out > 0, hidden_count=left_out
    )
    dirs, files = pruner.counts["dirs"], pruner.counts["files"]
    tree_string = DirectoryExplorerService(Path(result.root_path))._format_tree_string(
        root, show_stats, dirs, files
    )
    return DirectoryTreeResult(
        root=root,
        tree_string=tree_string,
        total_directories=dirs,
        total_files=files,
        max_depth_reached=result.max_depth_reached,
        root_path=result.root_path,
    )
