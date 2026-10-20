"""
Composite repository file listing implementation.

Provides file listing functionality for composite repositories,
supporting Story 3.3 of the Server Composite Repository Activation epic.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import logging
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import Any, List, Optional

from ..models.activated_repository import ActivatedRepository
from ..models.composite_file_models import FileInfo
from ...proxy.config_manager import ProxyConfigManager
from ...utils.path_confinement import is_readable_within_root, resolve_confined_path
from ..services.cidx_meta_access.file_doors import (
    judge_app_request,
    visible_composite_entries,
)
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComponentListing:
    """One component's entries; ``base`` is the directory walked, relative
    to the component (``""`` for its root, symlinks resolved)."""

    component: str
    base: str
    entries: List[FileInfo]


def _walk_directory(
    directory: Path, repo_prefix: str, recursive: bool, root: Optional[Path] = None
) -> List[FileInfo]:
    """
    Walk directory and collect file information.

    Args:
        directory: Directory path to walk
        repo_prefix: Component repository prefix for paths
        recursive: Whether to walk recursively
        root: The component root; a symlink resolving outside it (or into
            its ``.git``) is not listed. Defaults to *directory*.

    Returns:
        List of FileInfo objects for files in directory
    """
    files = []
    resolved_root = (root or directory).resolve()

    def _escapes(item: Path) -> bool:
        return item.is_symlink() and not is_readable_within_root(item, resolved_root)

    if recursive:
        # Recursive walk
        for item in directory.rglob("*"):
            # Skip .git and .code-indexer directories
            if ".git" in item.parts or ".code-indexer" in item.parts:
                continue
            if _escapes(item):
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
                if item.name in [".git", ".code-indexer"] or _escapes(item):
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


def composite_activation(
    activated_repo_manager: Any, username: str, alias: str
) -> Optional[ActivatedRepository]:
    """The caller's composite activation *alias*, or None when it is not a
    composite or its record cannot be read (the door then serves it as a
    regular activation)."""
    try:
        record = activated_repo_manager.get_repository(username, alias)
        if record and record.get("is_composite", False):
            return ActivatedRepository.from_dict(record)
    except Exception as e:
        logger.debug(f"Could not check composite status for {alias}: {e}")
    return None


def _confined_target(component_root: Path, path: str) -> Optional[Path]:
    """The directory a listing of *path* walks inside *component_root*,
    resolved; None when it leaves the component (or enters its ``.git``)."""
    try:
        return resolve_confined_path(component_root, path)
    except PermissionError:
        return None


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
    listings = list_composite_components(repo, path, recursive)
    files = [entry for listing in listings for entry in listing.entries]
    return sorted(files, key=lambda f: f.full_path)


def visible_composite_files(
    state: Any,
    composite: ActivatedRepository,
    path: str,
    recursive: bool,
    username: str,
    activated_repo_manager: Any,
) -> List[FileInfo]:
    """The composite listing of *path* the caller may see: each component is
    walked confined to itself and judged on its own directory."""
    components = Path(composite.path)
    return visible_composite_entries(
        list_composite_components(composite, path, recursive),
        lambda name: judge_app_request(
            state, name, str(components / name), username, activated_repo_manager
        ),
    )


def list_composite_components(
    repo: ActivatedRepository, path: str = "", recursive: bool = False
) -> List[ComponentListing]:
    """The listing of *path* inside every component, confined to it: a path
    that leaves a component lists nothing from it, like a missing path."""
    listings: List[ComponentListing] = []
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

    for repo_name in discovered_repos:
        subrepo_path = Path(repo.path) / repo_name
        target_path = _confined_target(subrepo_path, path)

        if target_path is None or not target_path.is_dir():
            logger.debug(
                f"Path does not exist in component {repo_name}",
                extra={"correlation_id": get_correlation_id()},
            )
            continue

        base = target_path.relative_to(subrepo_path.resolve()).as_posix()
        entries = _walk_directory(target_path, repo_name, recursive, subrepo_path)
        listings.append(
            ComponentListing(repo_name, "" if base == "." else base, entries)
        )
    return listings
