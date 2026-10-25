"""Paths of the catalog (cidx-meta) readable by the owner and the service group only."""

from __future__ import annotations

import errno
import logging
import os
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, List, Sequence, Tuple

if TYPE_CHECKING:
    from .snapshot_manager import VersionedSnapshotManager  # pragma: no cover

logger = logging.getLogger(__name__)

_NOT_OWNED_SAMPLE_SIZE = 5


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
def _close_private_dir(directory: Path) -> bool:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid():
            return False
        os.fchmod(fd, stat.S_IMODE(info.st_mode) & ~stat.S_IRWXO)
        return True
    finally:
        os.close(fd)


def owned_by_service_user_test() -> List[str]:
    return ["-user", str(os.geteuid())]


def printed_entries(find_stdout: bytes) -> List[str]:
    entries = [os.fsdecode(raw) for raw in find_stdout.split(b"\0") if raw]
    return [entry[2:] if entry.startswith("./") else entry for entry in entries]


# ^ invariant evaluated before every other test; a directory owned by the service user is never pruned
# ^ external GNU find -readable/-executable = access(2) result for the current user
def unmanageable_directory_prune() -> List[str]:
    prune = ["(", "-type", "d", "!", *owned_by_service_user_test()]
    prune += ["(", "!", "-readable", "-o", "!", "-executable", ")"]
    return [*prune, "-prune", "-print0", ")", "-o"]


# ^ invariant one WARNING per pass, never ERROR, never raise; the catalog keeps publishing
def report_entries_not_owned(catalog_root: object, entries: Sequence[str]) -> None:
    if not entries:
        return
    root_name = os.path.basename(os.path.normpath(str(catalog_root)))
    shown = [root_name if entry == "." else entry for entry in entries]
    logger.warning(
        "%d catalog entries under '%s' are not owned by the service user or not "
        "readable by it and their mode cannot be managed by it; ownership should be "
        "the service user: %s",
        len(entries),
        catalog_root,
        ", ".join(shown[:_NOT_OWNED_SAMPLE_SIZE]),
    )


# ^ security private dir without other-class bits => no entry beneath it is reachable by other users
# ^ perf one chmod per private dir; no tree walk on the startup path
def close_catalog_private_dirs(catalog_root: Path) -> None:
    not_owned: List[str] = []
    for name in catalog_private_dirs():
        directory = catalog_root / name
        try:
            if not _close_private_dir(directory):
                not_owned.append(name)
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
    report_entries_not_owned(catalog_root, not_owned)


def report_symlinked_private_roots(catalog_root: str) -> None:
    for name in catalog_private_dirs():
        if os.path.islink(os.path.join(catalog_root, name)):
            _report_symlinked_private_root(catalog_root, name)


# ^ security catalog private paths => no other-class bits; test:test_local_snapshot_of_catalog_converges_private_paths_in_place
# ^ invariant symlinks under the private roots are never named to chmod; test:test_catalog_snapshot_skips_symlinks_and_restricts_real_entries
# ^ invariant only entries owned by the service user are named to chmod; test:test_not_owned_private_root_is_reported_and_owned_entries_closed
def close_catalog_private_tree(catalog_root: str) -> None:
    report_symlinked_private_roots(catalog_root)
    command = ["find", ".", *unmanageable_directory_prune()]
    command += catalog_private_find_test()
    command += ["(", "-type", "d", "-o", "-type", "f", ")"]
    command += ["(", *owned_by_service_user_test(), "-exec", "chmod", "o-rwx", "{}"]
    command += ["+", "-o", "-print0", ")"]
    try:
        result = subprocess.run(command, cwd=catalog_root, capture_output=True)
    except OSError as exc:
        raise CatalogPermissionError(
            f"Could not set catalog private modes under '{catalog_root}': {exc}"
        ) from exc
    report_entries_not_owned(catalog_root, printed_entries(result.stdout))
    if result.returncode != 0:
        raise CatalogPermissionError(
            f"Could not set catalog private modes under '{catalog_root}' "
            f"(exit {result.returncode}): {os.fsdecode(result.stderr).strip()}"
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
