"""Shared keep-last-N versioned-snapshot retention primitive.

Extracted from `RefreshScheduler._enforce_retention` (Bug #1084 Phase A6)
as a standalone, alias-agnostic function so it can be reused by callers
other than `RefreshScheduler`'s own per-repo semantic-refresh loop.

Story #1457 MEDIUM #14 (2026-07-23 code review): temporal sister-location
aliases (`{repo_alias}-temporal-{embedder_slug}[-{quarter}]`, published
directly via `AliasManager.create_alias`/`swap_alias` in
`temporal_shard_publisher.py` -- NOT a `golden_repos` registry row) are
structurally invisible to `RefreshScheduler`'s per-repo enumeration loop
(`registry.list_due_repos()`/`list_global_repos()`), so they never reached
`_enforce_retention` before this module existed, leaking one superseded
`.versioned/{ns}/v_*` directory per temporal refresh cycle forever.
`discover_and_enforce_temporal_retention` closes that gap by discovering
temporal aliases for one golden repo directly from the alias directory
(the same on-disk convention `AliasManager` itself uses: one
`{alias_name}.json` file per alias) and reusing `enforce_snapshot_retention`
per discovered alias -- "reuse, don't reinvent" per the review finding.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from code_indexer.server.services.config_service import get_config_service

if TYPE_CHECKING:
    from .alias_manager import AliasManager
    from .cleanup_manager import CleanupManager
    from code_indexer.server.storage.shared.snapshot_manager import (
        VersionedSnapshotManager,
    )

logger = logging.getLogger(__name__)

#: Fallback keep-last-N when the configured value is missing or invalid.
DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST = 3


def resolve_retention_keep_last() -> int:
    """Return the configured keep-last-N, falling back to the safe default.

    A value < 1 would schedule EVERY snapshot for deletion (including the
    live one once it ages out), so non-positive / unreadable values fall
    back to DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST. Any read/parse failure is
    logged (not silently swallowed) before falling back.
    """
    try:
        keep = int(get_config_service().get_config().snapshot_retention_keep_last)
    except Exception as exc:
        logger.warning(
            "[retention] failed to read snapshot_retention_keep_last "
            "(falling back to default=%d): %s: %s",
            DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST,
            type(exc).__name__,
            exc,
        )
        return DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST
    if keep < 1:
        logger.warning(
            "[retention] snapshot_retention_keep_last=%r is non-positive "
            "(falling back to default=%d)",
            keep,
            DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST,
        )
        return DEFAULT_SNAPSHOT_RETENTION_KEEP_LAST
    return keep


def enforce_snapshot_retention(
    alias_name: str,
    current_target: str,
    *,
    snapshot_manager: Optional["VersionedSnapshotManager"],
    alias_manager: "AliasManager",
    cleanup_manager: "CleanupManager",
    retention_keep_last: Optional[int] = None,
) -> None:
    """Schedule deletion of superseded snapshots beyond keep-last-N.

    Lists snapshots via the discovery API (`snapshot_manager.list_snapshots`)
    and schedules (through the refcount-gated CleanupManager) every snapshot
    EXCEPT: the N newest, the alias's current `target_path`, and its
    `previous_path`. Naturally inert when `snapshot_manager` is None or
    discovery returns `[]` (e.g. ONTAP). Non-fatal: any failure is logged
    and swallowed so a caller's refresh/publish never fails on retention.

    Args:
        retention_keep_last: Pre-resolved keep-last-N. Callers that already
            have their OWN config-resolution path (e.g. RefreshScheduler's
            `_retention_keep_last()`, preserved for its existing test
            patch target) should pass it explicitly; omitted, this falls
            back to `resolve_retention_keep_last()`'s own config read.
    """
    if snapshot_manager is None:
        return
    try:
        keep_last = (
            retention_keep_last
            if retention_keep_last is not None
            else resolve_retention_keep_last()
        )
        snapshots = snapshot_manager.list_snapshots(alias_name)
        if len(snapshots) <= keep_last:
            return

        # Force-keep set: current target + previous_path (rollback) + N newest.
        protected: set = set()
        if current_target:
            protected.add(current_target)
        previous_path = alias_manager.get_previous_path(alias_name)
        if previous_path:
            protected.add(previous_path)
        # snapshots are sorted ascending by ts; the last keep_last are newest.
        for path, _ts in snapshots[-keep_last:]:
            protected.add(path)

        for path, _ts in snapshots:
            if path not in protected:
                logger.info(
                    f"[retention] Scheduling cleanup of superseded snapshot "
                    f"{path} (keep_last={keep_last}) for {alias_name}"
                )
                cleanup_manager.schedule_cleanup(path)
    except Exception as exc:
        logger.warning(
            f"[retention] keep-last-N enforcement failed for {alias_name} "
            f"(non-fatal): {type(exc).__name__}: {exc}"
        )


def discover_and_enforce_temporal_retention(
    repo_alias: str,
    *,
    snapshot_manager: Optional["VersionedSnapshotManager"],
    alias_manager: "AliasManager",
    cleanup_manager: "CleanupManager",
) -> None:
    """Discover and retention-sweep every temporal sister alias for ONE
    golden repo.

    Args:
        repo_alias: The golden repo's BARE alias (no "-global" suffix --
            callers must normalize, matching the same convention
            `temporal_relocation_trigger.py` uses).
        snapshot_manager: VersionedSnapshotManager rooted at the sister
            location. None is a no-op (mirrors enforce_snapshot_retention).
        alias_manager: AliasManager whose `aliases_dir` is globbed for
            `{repo_alias}-temporal-*.json` alias files.
        cleanup_manager: CleanupManager to schedule deletions through.
    """
    if snapshot_manager is None:
        return
    prefix = f"{repo_alias}-temporal-"
    try:
        alias_files = sorted(alias_manager.aliases_dir.glob(f"{prefix}*.json"))
    except OSError as exc:
        logger.warning(
            "[temporal-retention] alias directory scan failed for %s "
            "(non-fatal): %s: %s",
            repo_alias,
            type(exc).__name__,
            exc,
        )
        return

    for alias_file in alias_files:
        temporal_alias_name = alias_file.stem
        current_target = alias_manager.read_alias(temporal_alias_name)
        if not current_target:
            continue
        enforce_snapshot_retention(
            temporal_alias_name,
            current_target,
            snapshot_manager=snapshot_manager,
            alias_manager=alias_manager,
            cleanup_manager=cleanup_manager,
        )
