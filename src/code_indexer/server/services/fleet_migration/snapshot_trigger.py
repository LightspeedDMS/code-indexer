"""trigger_post_consolidation_snapshot() -- Story #1458 AC10.

As the FINAL step of the per-repo migration job (fired EXACTLY ONCE per
repo, never per-collection), migration triggers one explicit snapshot-
creation + alias-swap for the target repo's alias by calling the LOW-LEVEL
publication primitives DIRECTLY:
``VersionedSnapshotManager.create_snapshot()`` + ``AliasManager.
swap_alias()`` (or ``create_alias()`` on a genuine first-ever publish) --
NEVER the scheduler-level ``trigger_refresh_for_repo()``/``_execute_
refresh()`` wrapper, which performs a non-blocking ``is_write_locked()``
check and SKIPS its cycle when the lock is held (``refresh_scheduler.py``,
``_execute_refresh``, returns "Skipped, write lock held"). Because
migration is ITSELF the lock holder throughout this call (AC2), invoking
that wrapper here would ALWAYS self-skip and never publish -- self-
defeating. That lock-check exists to stop a DIFFERENT refresh from
conflicting with migration; migration calling the low-level primitives
directly, while it already holds the lock, is exactly the safe, intended
case.

After the swap, this function invokes the SAME retention method the
scheduler path itself runs immediately after its own swap --
``RefreshScheduler._enforce_retention`` -- via the REAL ``RefreshScheduler``
reference migration already holds (AC2's ``RefreshScheduler.
acquire_write_lock``). Retention is never reimplemented here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

_GLOBAL_SUFFIX = "-global"


def trigger_post_consolidation_snapshot(
    refresh_scheduler: "RefreshScheduler", alias: str, source_path: str
) -> str:
    """Publish a consolidated post-migration snapshot for ``alias``.

    Args:
        refresh_scheduler: The REAL RefreshScheduler instance migration is
            already using to hold the write lock (AC2) -- its
            ``_snapshot_manager``, ``alias_manager``, and
            ``_enforce_retention`` are reused directly, never reimplemented.
        alias: The golden repo's alias, with or without the "-global"
            suffix (normalized internally -- ``VersionedSnapshotManager.
            create_snapshot`` is namespaced by the BARE alias, while
            ``AliasManager``'s pointer files use the "-global"-suffixed
            form, matching the existing scheduler convention exactly).
        source_path: The now-consolidated base clone path to snapshot.

    Returns:
        The new snapshot's absolute filesystem path (the new alias target).
    """
    bare_alias = (
        alias[: -len(_GLOBAL_SUFFIX)] if alias.endswith(_GLOBAL_SUFFIX) else alias
    )
    alias_name = f"{bare_alias}{_GLOBAL_SUFFIX}"

    new_target: str = refresh_scheduler._snapshot_manager.create_snapshot(
        bare_alias, source_path
    )

    if refresh_scheduler.alias_manager.alias_exists(alias_name):
        current_target = refresh_scheduler.alias_manager.read_alias(alias_name)
        refresh_scheduler.alias_manager.swap_alias(
            alias_name=alias_name,
            new_target=new_target,
            old_target=current_target,
        )

        # Bug #1775: evict stale HNSW + chunk-store cache entries for
        # the old versioned path -- this fleet-migration re-publish
        # previously invalidated NEITHER cache (a fifth real alias-swap
        # site). Shared helper -- never reimplement the two-cache
        # invalidation inline here.
        from code_indexer.server.cache.snapshot_cache_invalidation import (
            invalidate_snapshot_caches,
        )

        invalidate_snapshot_caches(
            current_target,
            log_context=f"[fleet-migration-{alias_name}]",
            is_versioned_snapshot_check=refresh_scheduler._is_versioned_snapshot,
        )
    else:
        refresh_scheduler.alias_manager.create_alias(alias_name, new_target)

    refresh_scheduler._enforce_retention(alias_name, new_target)

    return new_target
