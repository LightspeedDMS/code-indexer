"""A catalog directory tree reduced to what the caller may see.

Every file node is judged by its full catalog-relative path (the requested
subdirectory prefixed), at every depth. A directory survives while it still
holds a visible descendant or the path policy could show the caller
something under it (a top-level area, a visible X-Ray scope), so hidden
names vanish from the nodes and from the rendered text. The per-directory
file limit is applied to visible files only, so truncation markers and
counts never include hidden entries; the raw tree must therefore be
generated without a file limit.

The requested path is resolved first (dot segments and in-repository
symlinks) and the tree is walked from that canonical root: a root the caller
may not see is answered exactly like a path that does not exist, before
anything is read; a visible document is a valid root.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set, Tuple

from ....global_repos.directory_explorer import (
    DirectoryExplorerService,
    DirectoryTreeResult,
    TreeNode,
    path_not_found_error,
)
from .path_policy import listable_paths, may_list_under
from .visibility import MetaVisibility, memory_visibility_fn

if TYPE_CHECKING:
    from ..access_filtering_service import AccessFilteringService
    from .file_doors import CatalogFileView

# Per-directory file limit for generating the raw catalog tree, so the
# caller's own limit can be applied to visible files only.
UNSCANNED_LIMIT = 2**31 - 1


def _full_path(base_path: str, node_path: str) -> str:
    base = base_path.strip("/")
    return f"{base}/{node_path}" if base and node_path else base or node_path


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
        self,
        base_path: str,
        allowed: Set[str],
        may_list: Callable[[str], bool],
        max_files_per_dir: Optional[int],
    ) -> None:
        self.base_path = base_path
        self.allowed = allowed
        self.may_list = may_list
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
        if not kept and not self.may_list(_full_path(self.base_path, node.path)):
            return None
        self.counts["dirs"] += 1
        self.depth_limited_kept = self.depth_limited_kept or depth_limited
        return replace(
            node,
            children=kept,
            truncated=depth_limited or left_out > 0,
            hidden_count=left_out,
        )


def catalog_tree_root(view: "CatalogFileView", requested: str) -> str:
    """The canonical catalog-relative root a restricted tree request walks
    (``""`` for the catalog itself), decided before anything is read.

    Raises:
        ValueError: the not-found error a missing path gets, for a root the
            caller may not see.
    """
    root = view.tree_root(requested)
    if root is None:
        raise path_not_found_error(requested)
    return root


def restrict_catalog_tree(
    result: DirectoryTreeResult,
    base_path: str,
    visibility: MetaVisibility,
    access_service: Optional["AccessFilteringService"],
    show_stats: bool = False,
    max_files_per_dir: Optional[int] = None,
    listable: Optional[Callable[[str], bool]] = None,
) -> DirectoryTreeResult:
    """*result* reduced to the caller's *visibility* (resolved once for the
    request); unchanged when it is unrestricted. Without the access service
    no memory is shown. *listable* (a file door's view) decides file nodes
    when given, dependency-map documents included.

    Raises:
        ValueError: the tree door's not-found error, for a requested
            *base_path* the caller may not see.
    """
    if visibility.unrestricted:
        return result
    memory_fn = memory_visibility_fn(access_service, visibility)
    paths = catalog_tree_paths(result, base_path)
    if listable is not None:
        allowed = {path for path in paths if listable(path)}
    else:
        allowed = set(listable_paths(paths, visibility, memory_fn))
    if not result.root.is_directory:
        if _full_path(base_path, "") not in allowed:
            raise path_not_found_error(base_path)
        return result

    def may_list(full_path: str) -> bool:
        return may_list_under(full_path, visibility, memory_fn)

    pruner = _Pruner(base_path, allowed, may_list, max_files_per_dir)
    kept, left_out = pruner.children(result.root)
    base = base_path.strip("/")
    if base and not kept and not may_list(base):
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
