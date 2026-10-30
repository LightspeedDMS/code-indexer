"""What a server auto-watch could leave at a removed activated repository
(#2323): the workspace directory holding only the watch's own files."""

import logging
import os
import stat
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_METADATA_DIR = ".code-indexer"
_PROOF_FILE = "watch_metadata.json"
# ^ coupled with services/watch_metadata.py:WatchMetadata.save_to_disk and services/indexing_lock.py:IndexingLock; the files a watch writes outside a cycle
_LEFTOVER_FILES = frozenset({_PROOF_FILE, "indexing_heartbeat.json"})
_SAVE_TEMP_PREFIX = ".watch_metadata.json."
_SAVE_TEMP_SUFFIX = ".tmp"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _is_leftover_file(entry: os.DirEntry) -> bool:
    name = entry.name
    return entry.is_file(follow_symlinks=False) and (
        name in _LEFTOVER_FILES
        or (name.startswith(_SAVE_TEMP_PREFIX) and name.endswith(_SAVE_TEMP_SUFFIX))
    )


def _open_child_dir(parent_fd: int, name: str) -> Optional[int]:
    """The child directory opened without following a symlink, verified to
    be the directory a no-follow lstat saw; None for anything else."""
    try:
        seen = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(seen.st_mode):
        return None
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        opened = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    if (opened.st_dev, opened.st_ino) != (seen.st_dev, seen.st_ino):
        os.close(fd)
        return None
    return fd


def _scan(fd: int) -> List[os.DirEntry]:
    with os.scandir(fd) as entries:
        return list(entries)


def _leftover_files(metadata_fd: int) -> Optional[List[str]]:
    files = _scan(metadata_fd)
    names = [e.name for e in files]
    if _PROOF_FILE in names and all(_is_leftover_file(e) for e in files):
        return names
    return None


def _remove_leftover_inside(root_fd: int) -> bool:
    if [e.name for e in _scan(root_fd)] != [_METADATA_DIR]:
        return False
    metadata_fd = _open_child_dir(root_fd, _METADATA_DIR)
    if metadata_fd is None:
        return False
    try:
        names = _leftover_files(metadata_fd)
        if names is None:
            return False
        for name in names:
            os.unlink(name, dir_fd=metadata_fd)
    finally:
        os.close(metadata_fd)
    os.rmdir(_METADATA_DIR, dir_fd=root_fd)
    return True


def remove_watch_leftover(workspace: Path, alias: str) -> bool:
    """Remove ``workspace`` when it holds nothing but watch leftovers.

    The caller proves no activated-repo registry row exists for it. Returns
    True when removed, False (nothing touched) when the path is absent or
    holds anything else.

    Raises:
        OSError: a leftover file or directory could not be removed (rmdir
            never removes content that appeared meanwhile).
    """
    # ^ invariant one activation per (operation, alias) while pending/running: activated_repo_manager.py:ActivatedRepoManager._activate_single_repository
    # ^ see background_jobs.py:BackgroundJobManager.submit_job and database_manager.py:DatabaseSchema; the atomic claim and its partial unique index
    # ^ why watch_metadata.json is required, not an empty directory; a peer's orphan sweep can end a live claim, and its reservation never holds that file
    try:
        parent_fd = os.open(workspace.parent, _DIR_FLAGS)
    except (FileNotFoundError, NotADirectoryError):
        return False
    try:
        root_fd = _open_child_dir(parent_fd, workspace.name)
        if root_fd is None:
            return False
        try:
            removed_inside = _remove_leftover_inside(root_fd)
        finally:
            os.close(root_fd)
        if not removed_inside:
            return False
        os.rmdir(workspace.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    logger.info(
        f"Removed the auto-watch leftover of activated repository '{alias}' "
        f"before activating it"
    )
    return True
