"""
Composite repository file listing implementation.

Provides file listing functionality for composite repositories,
supporting Story 3.3 of the Server Composite Repository Activation epic.

Under ``content_backend='delegated'`` the members are working copies on
workspace-mcp: they are listed from the content seam's manifest
(``_list_delegated_member``), never from this pod's disk. Known differences
from a local walk, both because the manifest describes files only and never
follows a symlink: a directory in a single-level listing reports the newest
modification time of the files beneath it (locally: the directory's own),
and a symlink is listed with its own size and time (locally: its target's).
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple

from ..models.activated_repository import ActivatedRepository
from ..models.composite_file_models import FileInfo
from ..services.composite_content import (
    SKIPPED_DIRS,
    composite_seam_source,
    member_entries,
    modified_from_ns,
    plain_member_names,
)
from ..services.file_service import ContentSeamSource
from ..storage.shared.content_backend import ContentEntry
from ...proxy.config_manager import ProxyConfigManager
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)


def _walk_directory(
    directory: Path, repo_prefix: str, recursive: bool
) -> List[FileInfo]:
    """
    Walk directory and collect file information.

    Args:
        directory: Directory path to walk
        repo_prefix: Component repository prefix for paths
        recursive: Whether to walk recursively

    Returns:
        List of FileInfo objects for files in directory
    """
    files = []

    if recursive:
        # Recursive walk
        for item in directory.rglob("*"):
            # Skip .git and .code-indexer directories
            if ".git" in item.parts or ".code-indexer" in item.parts:
                continue

            if item.is_file():
                try:
                    relative_path = item.relative_to(directory)
                    stat_info = item.stat()

                    files.append(
                        FileInfo(
                            full_path=f"{repo_prefix}/{relative_path}",
                            name=item.name,
                            size=stat_info.st_size,
                            modified=datetime.fromtimestamp(stat_info.st_mtime),
                            is_directory=False,
                            component_repo=repo_prefix,
                        )
                    )
                except (OSError, ValueError) as e:
                    logger.warning(
                        format_error_log(
                            "MCP-GENERAL-019",
                            f"Cannot access file {item}: {e}",
                        )
                    )
                    continue
    else:
        # Single level listing
        try:
            for item in directory.iterdir():
                # Skip .git and .code-indexer directories
                if item.name in [".git", ".code-indexer"]:
                    continue

                try:
                    relative_path = item.relative_to(directory)
                    stat_info = item.stat()

                    files.append(
                        FileInfo(
                            full_path=f"{repo_prefix}/{relative_path}",
                            name=item.name,
                            size=stat_info.st_size if item.is_file() else 0,
                            modified=datetime.fromtimestamp(stat_info.st_mtime),
                            is_directory=item.is_dir(),
                            component_repo=repo_prefix,
                        )
                    )
                except (OSError, ValueError) as e:
                    logger.warning(
                        format_error_log(
                            "MCP-GENERAL-020",
                            f"Cannot access item {item}: {e}",
                        )
                    )
                    continue
        except OSError as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-021",
                    f"Cannot access directory {directory}: {e}",
                )
            )

    return files


def _list_composite_files(
    repo: ActivatedRepository, path: str = "", recursive: bool = False
) -> List[FileInfo]:
    """
    List files across all component repositories.

    Args:
        repo: Activated composite repository
        path: Optional path filter (subdirectory within components)
        recursive: Whether to walk recursively

    Returns:
        List of FileInfo objects sorted by full_path
    """
    source = composite_seam_source(repo.path)
    if source is not None:
        return _list_delegated_composite_files(repo, source, path, recursive)

    files = []

    # Get component repos using ProxyConfigManager
    try:
        proxy_config = ProxyConfigManager(repo.path)
        discovered_repos = proxy_config.get_repositories()
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-022",
                f"Failed to get discovered repos from {repo.path}: {e}",
            )
        )
        return []

    # Walk each component repository
    for repo_name in discovered_repos:
        subrepo_path = repo.path / repo_name
        target_path = subrepo_path / path if path else subrepo_path

        if not target_path.exists():
            logger.debug(
                f"Path does not exist: {target_path}",
                extra={"correlation_id": get_correlation_id()},
            )
            continue

        # Walk the component repository
        repo_files = _walk_directory(target_path, repo_name, recursive)
        files.extend(repo_files)

    # Sort by path for consistent output
    return sorted(files, key=lambda f: f.full_path)


def _relative_filter(path: str) -> Tuple[str, ...]:
    """*path* (a subdirectory inside every member) as plain components.

    Refuses an absolute path or a ``..`` component: joined onto a member it
    would list outside the composite -- on workspace-mcp, possibly another
    user's copy.
    """
    if not path:
        return ()
    if path.startswith("/") or path.startswith("\\"):
        raise ValueError(f"Composite listing path must be relative: {path!r}")
    parts = tuple(p for p in path.split("/") if p not in ("", "."))
    if ".." in parts:
        raise ValueError(f"Composite listing path may not contain '..': {path!r}")
    return parts


def _list_delegated_composite_files(
    repo: ActivatedRepository,
    source: ContentSeamSource,
    path: str,
    recursive: bool,
) -> List[FileInfo]:
    """``_list_composite_files`` for members on workspace-mcp.

    The member list is the registry's (``repo.discovered_repos``), so it does
    not depend on this pod holding the composite's index state. Raises on any
    backend failure (a member missing on workspace-mcp included).
    """
    prefix = _relative_filter(path)
    files: List[FileInfo] = []
    for repo_name in plain_member_names(repo.discovered_repos, repo.path):
        entries = member_entries(source, repo.path / repo_name)
        files.extend(_list_delegated_member(entries, repo_name, prefix, recursive))
    return sorted(files, key=lambda f: f.full_path)


def _list_delegated_member(
    entries: Tuple[ContentEntry, ...],
    repo_prefix: str,
    prefix: Tuple[str, ...],
    recursive: bool,
) -> List[FileInfo]:
    """One member's listing from its manifest, shaped as ``_walk_directory``
    shapes a local walk of ``<member>/<prefix>``."""
    files: List[FileInfo] = []
    children: Dict[str, datetime] = {}
    for entry in entries:
        parts = tuple(entry.path.split("/"))
        if parts[: len(prefix)] != prefix or len(parts) == len(prefix):
            continue
        rel = parts[len(prefix) :]
        if any(part in SKIPPED_DIRS for part in parts):
            continue
        modified = modified_from_ns(entry.mtime_ns, entry.path)
        if recursive or len(rel) == 1:
            files.append(
                FileInfo(
                    full_path=f"{repo_prefix}/{'/'.join(rel)}",
                    name=rel[-1],
                    size=entry.size,
                    modified=modified,
                    is_directory=False,
                    component_repo=repo_prefix,
                )
            )
            continue
        newest = children.get(rel[0])
        children[rel[0]] = modified if newest is None else max(newest, modified)
    for name, newest_beneath in children.items():
        files.append(
            FileInfo(
                full_path=f"{repo_prefix}/{name}",
                name=name,
                size=0,
                modified=newest_beneath,
                is_directory=True,
                component_repo=repo_prefix,
            )
        )
    return files
