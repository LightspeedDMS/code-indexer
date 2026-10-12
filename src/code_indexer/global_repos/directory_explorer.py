"""
Directory exploration service for hierarchical tree view generation.

Provides the DirectoryExplorerService that generates tree views of repository
directory structure, similar to the 'tree' command.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Protocol, Tuple

from code_indexer.utils.path_confinement import (
    confine_relative_path,
    resolve_confined_path,
)

#: Characters that make an exclude pattern a glob rather than a literal name.
_GLOB_CHARS = frozenset("*?[")


class _TreeEntry(Protocol):
    """What the tree walk asks of an entry: the subset of ``pathlib.Path`` it uses.

    ``Path`` satisfies it for the local walk; ``_ManifestNode`` satisfies it for
    a repository described by a content-seam manifest, so ONE walk serves both.
    """

    @property
    def name(self) -> str: ...

    def is_file(self) -> bool: ...

    def is_dir(self) -> bool: ...

    def is_symlink(self) -> bool: ...

    def iterdir(self) -> Iterable["_TreeEntry"]: ...


@dataclass
class TreeNode:
    """A node in the directory tree."""

    name: str
    path: str  # Relative path from repo root
    is_directory: bool
    children: Optional[List["TreeNode"]] = None  # None for files
    truncated: bool = False  # True if max_files exceeded
    hidden_count: int = 0  # Number of hidden children
    size_bytes: Optional[int] = None  # File size if show_stats


@dataclass
class DirectoryTreeResult:
    """Result of directory tree generation."""

    root: TreeNode
    tree_string: str  # Pre-formatted tree output
    total_directories: int
    total_files: int
    max_depth_reached: bool
    root_path: str


class DirectoryExplorerService:
    """Service for directory exploration and tree generation."""

    DEFAULT_EXCLUDE_PATTERNS = [
        ".git",
        ".code-indexer",
        ".svn",
        ".hg",
        "node_modules",
        "vendor",
        "__pycache__",
        ".pytest_cache",
        ".tox",
        ".venv",
        "venv",
        ".idea",
        ".vscode",
        "*.pyc",
        "*.pyo",
    ]

    def __init__(self, repo_path: Path):
        """Initialize the directory explorer service.

        Args:
            repo_path: Path to the repository root
        """
        self.repo_path = repo_path

    def generate_tree(
        self,
        path: Optional[str] = None,
        max_depth: int = 3,
        max_files_per_dir: int = 50,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        show_stats: bool = False,
        include_hidden: bool = False,
    ) -> DirectoryTreeResult:
        """Generate hierarchical directory tree.

        Args:
            path: Subdirectory to use as tree root (relative to repo root)
            max_depth: Maximum depth of tree to display (default: 3, range: 1-10)
            max_files_per_dir: Maximum files to show per directory (default: 50)
            include_patterns: Glob patterns for files to include
            exclude_patterns: Glob patterns for files/directories to exclude
            show_stats: Show statistics summary at end (default: False)
            include_hidden: Include hidden files/directories (default: False)

        Returns:
            DirectoryTreeResult with tree structure and formatted string

        Raises:
            ValueError: If path doesn't exist
        """
        start_path, root_path = self._resolve_start(path)

        # Merge exclude patterns with defaults
        all_excludes = list(self.DEFAULT_EXCLUDE_PATTERNS)
        if exclude_patterns:
            all_excludes.extend(exclude_patterns)

        # Track metrics
        total_dirs = 0
        total_files = 0
        max_depth_reached = False

        def should_exclude(name: str, is_dir: bool, rel_path: str = "") -> bool:
            """Check if entry should be excluded."""
            import pathspec

            # Always exclude .git and .code-indexer
            if name == ".git":
                return True
            if name == ".code-indexer":
                return True

            # Check hidden files
            if not include_hidden and name.startswith("."):
                return True

            # Use rel_path if available (more accurate for ** patterns), otherwise use name
            match_path = rel_path if rel_path else name

            # Check exclude patterns
            for pattern in all_excludes:
                spec = pathspec.PathSpec.from_lines("gitwildmatch", [pattern])
                if spec.match_file(match_path):
                    return True

            return False

        def should_include_file(name: str, rel_path: str = "") -> bool:
            """Check if file matches include patterns."""
            import pathspec

            if not include_patterns:
                return True

            # Use rel_path if available (more accurate for ** patterns), otherwise use name
            match_path = rel_path if rel_path else name

            for pattern in include_patterns:
                spec = pathspec.PathSpec.from_lines("gitwildmatch", [pattern])
                if spec.match_file(match_path):
                    return True

            return False

        def build_tree(
            current_path: _TreeEntry, relative_path: str, current_depth: int
        ) -> Optional[TreeNode]:
            """Recursively build tree structure."""
            nonlocal total_dirs, total_files, max_depth_reached

            name = current_path.name or relative_path.split("/")[-1] or start_path.name

            if current_path.is_file():
                # Check include patterns for files
                if not should_include_file(name, relative_path):
                    return None

                total_files += 1
                return TreeNode(
                    name=name,
                    path=relative_path,
                    is_directory=False,
                    children=None,
                )

            # It's a directory
            if current_depth > 0:
                total_dirs += 1

            # Check depth limit
            if current_depth >= max_depth:
                max_depth_reached = True
                return TreeNode(
                    name=name,
                    path=relative_path,
                    is_directory=True,
                    children=[],  # Empty children indicates truncated by depth
                    truncated=True,
                )

            # Get directory contents
            try:
                entries = list(current_path.iterdir())
            except (PermissionError, OSError):
                return TreeNode(
                    name=name,
                    path=relative_path,
                    is_directory=True,
                    children=[],
                )

            # Sort entries: directories first, then files, both alphabetically
            dirs: List[_TreeEntry] = []
            files: List[_TreeEntry] = []

            for entry in entries:
                # Calculate entry's relative path
                entry_rel_path = (
                    f"{relative_path}/{entry.name}" if relative_path else entry.name
                )

                try:
                    # Skip excluded entries
                    if should_exclude(entry.name, entry.is_dir(), entry_rel_path):
                        continue

                    # Skip symlinks to prevent loops
                    if entry.is_symlink():
                        # Include as a file but don't follow
                        if entry.is_file() or not entry.is_dir():
                            if should_include_file(entry.name, entry_rel_path):
                                files.append(entry)
                        continue

                    if entry.is_dir():
                        dirs.append(entry)
                    elif entry.is_file():
                        if should_include_file(entry.name, entry_rel_path):
                            files.append(entry)
                except (PermissionError, OSError):
                    # Skip entries we cannot stat (e.g., symlinks to restricted files,
                    # device files, or other inaccessible filesystem entries)
                    continue

            # Sort alphabetically (case-insensitive)
            dirs.sort(key=lambda e: e.name.lower())
            files.sort(key=lambda e: e.name.lower())

            # Build children nodes
            children: List[TreeNode] = []
            truncated = False
            hidden_count = 0

            # Add directories first
            for entry in dirs:
                child_relative = (
                    f"{relative_path}/{entry.name}" if relative_path else entry.name
                )
                child_node = build_tree(entry, child_relative, current_depth + 1)
                if child_node:
                    # If include_patterns specified, only include dirs with matching descendants
                    if include_patterns:
                        if child_node.is_directory:
                            if child_node.children or not include_patterns:
                                children.append(child_node)
                    else:
                        children.append(child_node)

            # Add files with truncation
            file_count = 0
            for entry in files:
                if file_count >= max_files_per_dir:
                    hidden_count = len(files) - max_files_per_dir
                    truncated = True
                    break

                child_relative = (
                    f"{relative_path}/{entry.name}" if relative_path else entry.name
                )
                child_node = build_tree(entry, child_relative, current_depth + 1)
                if child_node:
                    children.append(child_node)
                    file_count += 1

            return TreeNode(
                name=name,
                path=relative_path,
                is_directory=True,
                children=children,
                truncated=truncated,
                hidden_count=hidden_count,
            )

        # Build the tree starting from start_path
        root_name = start_path.name
        root = build_tree(start_path, "", 0)

        if root is None:
            root = TreeNode(
                name=root_name,
                path="",
                is_directory=True,
                children=[],
            )

        root.name = root_name

        # Generate tree string
        tree_string = self._format_tree_string(
            root, show_stats, total_dirs, total_files
        )

        return DirectoryTreeResult(
            root=root,
            tree_string=tree_string,
            total_directories=total_dirs,
            total_files=total_files,
            max_depth_reached=max_depth_reached,
            root_path=root_path,
        )

    @classmethod
    def prunable_dir_names(cls) -> Tuple[str, ...]:
        """The default excludes that are literal NAMES, not globs.

        Each excludes a directory of that name at any depth together with
        everything under it, so a manifest may skip such directories outright
        without changing the tree.
        """
        return tuple(
            p for p in cls.DEFAULT_EXCLUDE_PATTERNS if not _GLOB_CHARS & set(p)
        )

    def _resolve_start(self, path: Optional[str]) -> Tuple[_TreeEntry, str]:
        """The entry the tree starts at, and the root path to report for it.

        Raises:
            ValueError: if ``path`` does not exist or escapes the repository.
        """
        if not path:
            return self.repo_path, str(self.repo_path)
        # Bug #1891 round 2 (S2): confine `path` to self.repo_path
        # BEFORE using it as the tree-walk root. The previous
        # `self.repo_path / path` was unconfined -- a parent-directory
        # traversal or a same-repo symlink pointing outside the repo
        # would enumerate whatever it resolved to. A PermissionError
        # (escape attempt) is mapped to the SAME ValueError message the
        # pre-existing nonexistent-path check already raises, so an
        # escape attempt cannot be distinguished from an ordinary
        # missing path.
        try:
            start_path = resolve_confined_path(self.repo_path, path)
        except PermissionError:
            raise ValueError(f"Path does not exist: {path}")
        if not start_path.exists():
            raise ValueError(f"Path does not exist: {path}")
        return start_path, str(start_path)

    def _format_tree_string(
        self,
        root: TreeNode,
        show_stats: bool,
        total_dirs: int,
        total_files: int,
    ) -> str:
        """Format the tree as a string.

        Args:
            root: Root TreeNode
            show_stats: Whether to include summary statistics
            total_dirs: Total directory count
            total_files: Total file count

        Returns:
            Formatted tree string
        """
        lines: List[str] = []

        # Root line
        if root.is_directory:
            lines.append(f"{root.name}/")
        else:
            lines.append(root.name)

        def format_children(
            node: TreeNode, prefix: str = "", is_last: bool = True
        ) -> None:
            """Recursively format children."""
            if not node.children:
                return

            children = node.children
            num_children = len(children)

            for i, child in enumerate(children):
                is_last_child = i == num_children - 1 and not node.truncated
                connector = "+--" if is_last_child else "|--"
                extension = "    " if is_last_child else "|   "

                if child.is_directory:
                    # Check if directory was depth-limited
                    if child.truncated and not child.children:
                        lines.append(f"{prefix}{connector} {child.name}/ [...]")
                    else:
                        lines.append(f"{prefix}{connector} {child.name}/")
                        format_children(child, prefix + extension, is_last_child)
                else:
                    lines.append(f"{prefix}{connector} {child.name}")

            # Add truncation indicator if files were hidden
            if node.truncated and node.hidden_count > 0:
                connector = "+--"
                lines.append(f"{prefix}{connector} [+{node.hidden_count} more files]")

        format_children(root)

        # Add statistics if requested
        if show_stats:
            lines.append("")
            lines.append(f"{total_dirs} directories, {total_files} files")

        return "\n".join(lines)


class _ManifestNode:
    """One entry of a manifest-described repository, shaped like a ``Path``.

    A directory is implied by the file paths below it. A symlink is a leaf: the
    manifest reports it and never follows it, so it is a file to the walk
    whatever it points at.
    """

    def __init__(self, name: str, *, is_dir: bool, is_symlink: bool = False) -> None:
        self._name = name
        self._symlink = is_symlink
        self.children: Optional[Dict[str, "_ManifestNode"]] = {} if is_dir else None

    @property
    def name(self) -> str:
        return self._name

    def is_dir(self) -> bool:
        return self.children is not None

    def is_file(self) -> bool:
        return self.children is None

    def is_symlink(self) -> bool:
        return self._symlink

    def iterdir(self) -> Iterable["_ManifestNode"]:
        if self.children is None:
            raise NotADirectoryError(self._name)
        return list(self.children.values())

    def find(self, rel: str) -> Optional["_ManifestNode"]:
        """The node at repo-relative *rel* (``""`` is this node), or None."""
        node: Optional[_ManifestNode] = self
        for part in rel.split("/") if rel else ():
            if node is None or node.children is None:
                return None
            node = node.children.get(part)
        return node


class ManifestDirectoryExplorerService(DirectoryExplorerService):
    """The directory tree of a repository known only by its manifest.

    For a repository whose files are not on this machine (a delegated content
    backend). The walk, the excludes, the hidden-file rule and the depth
    semantics are :class:`DirectoryExplorerService`'s own; only the entries
    come from ``(path, is_symlink)`` pairs instead of the disk. Two documented
    differences follow from what a manifest is: a symlink is always a leaf
    (locally a link to a directory is skipped), and a directory holding no
    listed file does not exist.
    """

    def __init__(self, repo_path: Path, entries: Iterable[Tuple[str, bool]]) -> None:
        super().__init__(repo_path)
        self._root = _ManifestNode(repo_path.name, is_dir=True)
        for rel, is_symlink in entries:
            self._add(rel, is_symlink)

    def _add(self, rel: str, is_symlink: bool) -> None:
        *dirs, leaf = rel.split("/")
        node = self._root
        for part in dirs:
            if node.children is None:
                return  # a file stands where a directory would be
            node = node.children.setdefault(part, _ManifestNode(part, is_dir=True))
        if node.children is not None:
            node.children[leaf] = _ManifestNode(
                leaf, is_dir=False, is_symlink=is_symlink
            )

    def _resolve_start(self, path: Optional[str]) -> Tuple[_TreeEntry, str]:
        if not path:
            return self._root, str(self.repo_path)
        try:
            rel = confine_relative_path(path)
        except PermissionError:
            raise ValueError(f"Path does not exist: {path}")
        node = self._root.find(rel)
        if node is None:
            raise ValueError(f"Path does not exist: {path}")
        return node, str(self.repo_path / rel) if rel else str(self.repo_path)
