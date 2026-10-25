"""Paths of the catalog (cidx-meta) readable by the owner and the service group only."""

from __future__ import annotations

import errno
import logging
import os
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    from .snapshot_manager import VersionedSnapshotManager  # pragma: no cover

logger = logging.getLogger(__name__)


class CatalogPermissionError(RuntimeError):
    pass


def catalog_private_dirs() -> Tuple[str, ...]:
    from code_indexer.server.services.cidx_meta_access.path_policy import (
        MEMORIES_DIR,
    )
    from code_indexer.utils.path_confinement import INDEX_DIRECTORY_NAME

    return (MEMORIES_DIR, INDEX_DIRECTORY_NAME)


def catalog_private_find_test() -> List[str]:
    # ^ invariant paths are relative to the catalog root; find must run with cwd = that root
    expression: List[str] = ["("]
    for name in catalog_private_dirs():
        if len(expression) > 1:
            expression.append("-o")
        expression += ["-path", f"./{name}", "-o", "-path", f"./{name}/*"]
    expression.append(")")
    return expression


# ^ why report, not refuse; a catalog snapshot always proceeds; test:test_symlinked_private_root_is_reported_not_followed
def _report_symlinked_private_root(catalog_root: object, name: str) -> None:
    logger.error(
        "Catalog private root '%s' under '%s' is a symbolic link; its mode is not managed",
        name,
        catalog_root,
    )


# ^ invariant mode is set through an O_NOFOLLOW descriptor, never by path; test:test_private_root_swapped_for_symlink_after_check_is_not_followed
def _close_private_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, stat.S_IMODE(os.fstat(fd).st_mode) & ~stat.S_IRWXO)
    finally:
        os.close(fd)


# ^ security private dir without other-class bits => no entry beneath it is reachable by other users
# ^ perf one chmod per private dir; no tree walk on the startup path
def close_catalog_private_dirs(catalog_root: Path) -> None:
    for name in catalog_private_dirs():
        directory = catalog_root / name
        try:
            _close_private_dir(directory)
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno == errno.ELOOP or (
                exc.errno == errno.ENOTDIR and os.path.islink(directory)
            ):
                _report_symlinked_private_root(catalog_root, name)
                continue
            if exc.errno == errno.ENOTDIR:
                continue
            # ^ why log, not raise; startup stays available and the next catalog snapshot raises CatalogPermissionError
            logger.error(
                "Could not close catalog directory '%s' to other users: %s",
                directory,
                type(exc).__name__,
            )


# ^ security catalog private paths => no other-class bits; test:test_local_snapshot_of_catalog_converges_private_paths_in_place
def report_symlinked_private_roots(catalog_root: str) -> None:
    for name in catalog_private_dirs():
        if os.path.islink(os.path.join(catalog_root, name)):
            _report_symlinked_private_root(catalog_root, name)


# ^ invariant symlinks under the private roots are never named to chmod; test:test_catalog_snapshot_skips_symlinks_and_restricts_real_entries
def close_catalog_private_tree(catalog_root: str) -> None:
    report_symlinked_private_roots(catalog_root)
    command = ["find", ".", *catalog_private_find_test()]
    command += ["(", "-type", "d", "-o", "-type", "f", ")"]
    command += ["-exec", "chmod", "o-rwx", "{}", "+"]
    try:
        result = subprocess.run(
            command, cwd=catalog_root, capture_output=True, text=True
        )
    except OSError as exc:
        raise CatalogPermissionError(
            f"Could not set catalog private modes under '{catalog_root}': {exc}"
        ) from exc
    if result.returncode != 0:
        raise CatalogPermissionError(
            f"Could not set catalog private modes under '{catalog_root}' "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )


# ^ invariant only snapshots owned by this process's user are changed; others leave at retention
# ^ perf catalog snapshots only = keep-last-N directories; one find each
def repair_retained_catalog_snapshots(
    snapshot_manager: "VersionedSnapshotManager",
) -> int:
    from code_indexer.server.services.constants import CIDX_META_REPO

    owner = os.geteuid()
    repaired = 0
    other_owner = 0
    for snapshot_path, _timestamp in snapshot_manager.list_snapshots(CIDX_META_REPO):
        try:
            if os.lstat(snapshot_path).st_uid != owner:
                other_owner += 1
                continue
            close_catalog_private_tree(snapshot_path)
            repaired += 1
        except Exception as exc:
            logger.warning(
                "Could not set catalog private modes on retained snapshot '%s': %s",
                snapshot_path,
                type(exc).__name__,
            )
    if other_owner:
        logger.info(
            "%d retained catalog snapshot(s) are owned by another user and "
            "converge when retention rotates them out",
            other_owner,
        )
    return repaired
