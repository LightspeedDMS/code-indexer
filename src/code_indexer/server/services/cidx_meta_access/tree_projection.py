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
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from ....global_repos.directory_explorer import (
    DirectoryExplorerService,
    DirectoryTreeResult,
    TreeNode,
    path_not_found_error,
)
from .path_policy import XRAY_ANY_SCOPE, XRAY_PATTERNS_DIR, normalize_meta_path
from .visibility import unrestricted_caller

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService

CATALOG_TOP_DIRS = frozenset({"dependency-map", "memories", "xray-patterns"})
# Per-directory file limit for generating the raw catalog tree, so the
# caller's own limit can be applied to visible files only.
UNSCANNED_LIMIT = 2**31 - 1


def _full_path(base_path: str, node_path: str) -> str:
    base = base_path.strip("/")
    return f"{base}/{node_path}" if base and node_path else base or node_path


def _listable_directory(full_path: str) -> bool:
    """A catalog directory every caller may know about, whatever it holds
    for them: a top-level area or the shared X-Ray pattern scope."""
    parts = normalize_meta_path(full_path.strip("/"))
    if not parts or any(seg.startswith(".") for seg in parts):
        return False
    if len(parts) == 1:
        return parts[0] in CATALOG_TOP_DIRS
    return parts[0] == XRAY_PATTERNS_DIR and parts[1] == XRAY_ANY_SCOPE


def catalog_tree_paths(result: DirectoryTreeResult, base_path: str) -> List[str]:
    """Full catalog-relative paths of every file node in *result* (the
    requested path itself when it names a file)."""
    if not result.root.is_directory:
        return [_full_path(base_path, "")]
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
        self.depth_limited_kept = False

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
        if not kept and not (depth_limited and _listable_directory(full)):
            return None
        self.counts["dirs"] += 1
        self.depth_limited_kept = self.depth_limited_kept or depth_limited
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
    callers. Without the access service no file is shown.

    Raises:
        ValueError: the tree door's not-found error, for a requested
            *base_path* the caller may not see.
    """
    if unrestricted_caller(username, access_service):
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
    if not result.root.is_directory:
        if _full_path(base_path, "") not in allowed:
            raise path_not_found_error(base_path)
        return result
    pruner = _Pruner(base_path, allowed, max_files_per_dir)
    kept, left_out = pruner.children(result.root)
    if base_path.strip("/") and not kept and not _listable_directory(base_path):
        raise path_not_found_error(base_path)
    root = replace(
        result.root, children=kept, truncated=left_out > 0, hidden_count=left_out
    )
    dirs, files = pruner.counts["dirs"], pruner.counts["files"]
    tree_string = DirectoryExplorerService.format_tree_string(
        root, show_stats, dirs, files
    )
    return DirectoryTreeResult(
        root=root,
        tree_string=tree_string,
        total_directories=dirs,
        total_files=files,
        max_depth_reached=pruner.depth_limited_kept,
        root_path=result.root_path,
    )
