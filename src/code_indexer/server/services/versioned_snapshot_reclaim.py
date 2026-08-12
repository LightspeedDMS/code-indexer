"""
Bug #1570 Half 2: reclaim `.versioned/{alias}/` namespaces already leaked
by golden-repo removal.

Removing a golden repo deletes its `-global` alias pointer as part of the
removal cascade (see golden_repo_manager.py). Bug #1567's orphan sweep
(versioned_snapshot_reconciler.py) fail-closed-skips any namespace whose
alias pointer is missing/unreadable -- correct when the repo still exists
(a transient pointer-read failure must never authorize deleting live
data), but that same skip is exactly what permanently strands a namespace
whose owning golden repo was actually removed: once its pointer is gone,
nothing else ever reclaims the space. Bug #1570's write-path fix (golden
repo removal now deletes its own `.versioned/{alias}/` tree) stops FUTURE
leaks, but -- the same lesson Bug #1567 itself teaches for its own
pre-existing snapshot backlog -- cannot heal installations that already
leaked before that fix shipped.

A namespace whose alias pointer is unreadable/missing is reclaimed
(rather than skipped forever) only when ALL THREE hold:

  1. No readable alias pointer for this namespace (checked by the caller,
     versioned_snapshot_reconciler.py, before consulting this module).
  2. No base-clone directory at `{golden_repos_dir}/{bare_namespace}`.
  3. The bare namespace is NOT a `golden_repos` registry row (requires an
     explicit `golden_repo_manager`; omitted/unavailable means this
     conjunct can never be proven, so reclaim never fires).

This conjunction is deliberately conservative: a repo that still exists
but happens to have a transiently unreadable pointer right now is caught
by conjunct 3 (still registered) or conjunct 2 (its clone still exists)
and continues to be skipped exactly as before. Only a namespace with NO
live target by ANY of these three signals is reclaimed -- there is no
"which snapshot is live" question left to answer, so every genuine
versioned-snapshot entry found for the namespace is scheduled for
deletion (not merely the superseded ones), through the SAME refcount+
min-age-gated `cleanup_manager.schedule_cleanup` the supersession path
already uses.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Set

if TYPE_CHECKING:
    from code_indexer.global_repos.cleanup_manager import CleanupManager
    from code_indexer.server.storage.shared.snapshot_manager import (
        VersionedSnapshotManager,
    )

logger = logging.getLogger(__name__)

#: golden_repo_manager is typed Any deliberately: callers pass either the
#: real GoldenRepoManager or a duck-typed test double exposing
#: list_golden_repos() -> List[Dict[str, Any]] with an "alias" key.
GoldenRepoManagerLike = Any


def _is_unsafe_namespace(bare_namespace: object) -> bool:
    """Defensive invariant (Messi Rule #15): reject a non-string, empty,
    or path-traversal-containing namespace BEFORE it is used to build a
    filesystem path -- mirrors the identical check golden_repo_manager
    already applies to aliases at registration time."""
    if not isinstance(bare_namespace, str):
        return True
    return (
        not bare_namespace
        or ".." in bare_namespace
        or "/" in bare_namespace
        or "\\" in bare_namespace
    )


def resolve_registered_aliases(
    golden_repo_manager: Optional[GoldenRepoManagerLike],
) -> Optional[Set[str]]:
    """Bare aliases currently registered in `golden_repos`, or None when
    no registry signal is available (no manager supplied, or the read
    itself failed) -- fail-closed, never guessed as "empty registry"."""
    if golden_repo_manager is None:
        return None
    try:
        return {repo["alias"] for repo in golden_repo_manager.list_golden_repos()}
    except Exception as registry_error:
        logger.warning(
            "Bug #1570 reconcile: failed to read golden-repo registry "
            "(namespace reclaim disabled for this sweep, existing "
            "pointer-missing skips are unaffected): %s",
            registry_error,
        )
        return None


def _base_clone_is_confirmed_absent(base_clone: Path) -> bool:
    """Explicit os.stat()-based existence check (never Path.exists(),
    whose internal errno-suppression list is an implementation detail we
    do not want to depend on). FileNotFoundError/NotADirectoryError mean
    genuinely absent. Any OTHER OSError (permission denied, a stale NFS
    handle, an I/O error) means "cannot confirm absence" -- returns False
    so the caller stays fail-closed rather than risking a false-positive
    reclaim of a live repo's data under a flaky/stale mount."""
    try:
        os.stat(base_clone)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError as stat_error:
        logger.warning(
            "Bug #1570 reconcile: failed to check base-clone presence "
            "at '%s' (treating as present -- blocking reclaim, "
            "fail-closed): %s",
            base_clone,
            stat_error,
        )
        return False
    return False


def namespace_is_genuinely_orphaned(
    bare_namespace: str,
    golden_repos_path: Path,
    registered_aliases: Optional[Set[str]],
) -> bool:
    """True only when the caller has ALREADY confirmed there is no
    readable alias pointer for *bare_namespace*, AND both of the
    following also hold: *registered_aliases* is not None (a registry
    signal was actually available) and does not contain
    *bare_namespace*; and no base-clone directory exists at
    `golden_repos_path/bare_namespace`.

    Fail-closed in every direction: an unsafe *bare_namespace* (non-
    string, empty, or containing path-traversal characters -- Messi Rule
    #15) always returns False; a non-Path *golden_repos_path* always
    returns False (cannot safely build a path to check); *
    registered_aliases is None* always returns False (a still-registered
    repo with a temporarily unreadable pointer can never be ruled out
    without that signal); see `_base_clone_is_confirmed_absent` for the
    base-clone check's own fail-closed handling of filesystem errors.
    """
    if _is_unsafe_namespace(bare_namespace):
        logger.error(
            "Bug #1570 reconcile: refusing to evaluate reclaim for "
            "namespace '%r': unsafe/empty namespace name.",
            bare_namespace,
        )
        return False
    if not isinstance(golden_repos_path, Path):
        logger.error(
            "Bug #1570 reconcile: refusing to evaluate reclaim for "
            "namespace '%s': golden_repos_path is not a Path (%r).",
            bare_namespace,
            golden_repos_path,
        )
        return False
    if registered_aliases is None:
        return False
    if bare_namespace in registered_aliases:
        return False
    return _base_clone_is_confirmed_absent(golden_repos_path / bare_namespace)


def reclaim_orphaned_namespace(
    bare_namespace: str,
    *,
    snapshot_manager: Optional["VersionedSnapshotManager"],
    cleanup_manager: Optional["CleanupManager"],
) -> List[str]:
    """Schedule every genuine versioned-snapshot entry found for a
    namespace already proven genuinely orphaned
    (`namespace_is_genuinely_orphaned`) for deletion.

    Returns only the paths that were ACTUALLY scheduled successfully --
    never speculatively recorded before `cleanup_manager.schedule_cleanup`
    confirms it did not raise. Both snapshot enumeration and per-path
    scheduling failures are logged and skipped, never fatal to the rest
    of the namespace (or the sweep). Defensive invariant (Messi Rule
    #15): an unsafe *bare_namespace*, or either collaborator missing,
    returns an empty list rather than dereferencing None or building a
    path from an unsafe name.
    """
    if _is_unsafe_namespace(bare_namespace):
        logger.error(
            "Bug #1570 reconcile: refusing to reclaim namespace '%r': "
            "unsafe/empty namespace name.",
            bare_namespace,
        )
        return []
    if snapshot_manager is None or cleanup_manager is None:
        logger.error(
            "Bug #1570 reconcile: refusing to reclaim namespace '%s': "
            "snapshot_manager/cleanup_manager not wired.",
            bare_namespace,
        )
        return []

    try:
        snapshots = snapshot_manager.list_snapshots(bare_namespace)
    except Exception as list_error:
        logger.error(
            "Bug #1570 reconcile: failed to list snapshots for "
            "namespace '%s' (zero deletions for this namespace): %s",
            bare_namespace,
            list_error,
        )
        return []

    scheduled: List[str] = []
    for path, _ts in snapshots:
        # Structural re-confirmation: must be a genuine versioned
        # snapshot, never the master clone or a foreign entry -- the SAME
        # defensive re-check the supersession path already performs
        # immediately before scheduling.
        if not snapshot_manager.is_versioned_snapshot(path):
            continue
        try:
            cleanup_manager.schedule_cleanup(path)
        except Exception as schedule_error:
            logger.error(
                "Bug #1570 reconcile: failed to schedule cleanup for "
                "'%s' in reclaimed namespace '%s': %s",
                path,
                bare_namespace,
                schedule_error,
            )
            continue
        scheduled.append(path)
    logger.info(
        "Bug #1570 reconcile: namespace '%s' has no alias pointer, no "
        "base clone, and is not a golden-repo registry row -- reclaimed "
        "%d snapshot(s) (no live target exists).",
        bare_namespace,
        len(scheduled),
    )
    return scheduled
