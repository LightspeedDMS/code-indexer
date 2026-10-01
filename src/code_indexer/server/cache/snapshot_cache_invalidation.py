"""Shared helper: invalidate BOTH the HNSW index cache and the chunk-store
cache for a superseded versioned-snapshot path (GitHub Bug #1775).

Every real alias-swap/publish site in this codebase MUST call
:func:`invalidate_snapshot_caches` exactly once, immediately after the
swap succeeds, with the OLD (about-to-be-superseded) snapshot path. As of
this module's introduction there are five such sites:

- ``GoldenRepoManager._cb_swap_alias`` (operator-initiated branch switch)
- ``RefreshScheduler._execute_refresh_impl`` (the periodic, hourly,
  fleet-wide golden-repo refresh -- the ACTUAL production leak driver
  Bug #1775 was filed against; the original fix wired only the first site
  above, leaving this one -- and the two below -- unwired)
- ``GoldenRepoManager``'s add-index post-loop publish
- ``server/mcp/handlers/repos.py``'s ``_post_provider_index_snapshot``
- ``server/services/fleet_migration/snapshot_trigger.py``'s
  ``trigger_post_consolidation_snapshot``

Factored out of what was a single inline try/except block (duplicated a
second time for the chunk-store cache during the #1775 fix) so the
two-cache invalidation contract cannot silently drift as new snapshot
publish sites are added -- a bare "add invalidate_prefix() to the HNSW
cache" review comment on a NEW site would otherwise be an easy way to
reintroduce exactly this bug's shape (a primitive that exists but isn't
called everywhere it needs to be, matching the "registered but unwired"
trap this project's CLAUDE.md documents for Bug #1665).

Each cache is invalidated independently and non-fatally: a failure
evicting one cache must never prevent the other from being attempted, and
neither failure should ever propagate to the caller -- the swap/publish
itself has already succeeded by the time this runs, and a stale entry
still self-heals eventually (HNSW: TTL; chunk-store: LRU eviction), so a
logged WARNING is the correct severity here, not a raised exception.

Round-5 addition: the chunk-store cache's LOCAL invalidation only ever
reaches the CALLING process's own ChunkStoreThreadCache singleton -- live
staging validation found a multi-worker deployment where a DIFFERENT
worker's independently-cached handle for the SAME superseded snapshot
leaked forever, because the stale-prefix signal never crossed the OS
process boundary. ``_evict_chunk_store_cache()`` now ALSO publishes
``old_target`` to a shared, PayloadCache-backed cross-process registry
(see ``storage/shared/chunk_store_cache_cross_process.py``) so every
OTHER worker/node's background poller can discover and apply it -- this
publish step is its own separately non-fatal try/except, isolated from
the local invalidation above.

Round-2 code review remediation (HIGH #2, both an independent Claude
review and an independent Codex review): a golden repo's FIRST refresh
has ``old_target`` equal to the MASTER base clone path
(``golden_repos_dir/{alias}``), not yet any ``.versioned/`` snapshot --
this is the EXACT scenario ``refresh_scheduler.py``'s own pre-existing
comment, written for the physical-cleanup-scheduling decision a few lines
below its ``swap_alias()`` call, already warns about: "Only schedule
cleanup for versioned snapshots, never for the master golden repo... On
first refresh current_target IS the master -- scheduling it would
permanently destroy it." The chunk-store cache's stale-prefix registry
has the SAME hazard in a different, more insidious form: unlike the
HNSWIndexCache's ONE-SHOT ``invalidate_prefix()`` (evicts once, forgets),
a chunk-store stale registration is a STANDING rule nothing ever
reverses -- if the master clone were ever registered, that repo's
chunk-store cache would be silently, permanently disabled the moment it
first onboards. This module now applies the SAME guard the physical
cleanup code already relies on, ONCE, for BOTH caches, so a future 6th
call site cannot reintroduce this mistake by forgetting to repeat it
locally.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _resolve_correlation_id() -> Optional[str]:
    """Best-effort correlation id for log lines below. Isolated from the
    HNSW eviction try/except so a correlation-module import/call failure
    can never silently skip the HNSW eviction it would otherwise be
    bundled with -- logs a WARNING (does not swallow silently) and falls
    back to ``None``.
    """
    try:
        from code_indexer.server.middleware.correlation import get_correlation_id

        return get_correlation_id()
    except Exception as _corr_err:
        logger.warning("Failed to resolve correlation id: %s", _corr_err)
        return None


def _evict_hnsw_cache(
    old_target: str, log_context: str, correlation_id: Optional[str]
) -> None:
    """Evict stale HNSW cache entries for ``old_target``. Non-fatal --
    logs a WARNING and returns on any failure, never raises.
    """
    try:
        from code_indexer.server.cache import get_global_cache

        _hnsw_cache = get_global_cache()
        if _hnsw_cache is not None:
            _evicted = _hnsw_cache.invalidate_prefix(old_target)
            logger.info(
                "%s Evicted %d HNSW cache entries for old snapshot %s",
                log_context,
                _evicted,
                old_target,
                extra={"correlation_id": correlation_id},
            )
    except Exception as _hnsw_err:
        logger.warning(
            "%s Failed to evict HNSW cache for old snapshot %s: %s",
            log_context,
            old_target,
            _hnsw_err,
        )


def _publish_chunk_store_stale_prefix_cross_process(
    old_target: str, log_context: str
) -> None:
    """Round 5: publish ``old_target`` to the shared cross-process
    registry, so every OTHER worker/node's ChunkStoreCrossProcessPoller
    can discover and apply it. Non-fatal -- a failure here must never
    undo or block the (already-succeeded) LOCAL invalidation. A no-op
    (no warning) when no ``PayloadCache`` is registered (CLI/solo-import
    or pre-startup contexts).
    """
    try:
        from code_indexer.storage.shared.chunk_store_cache_cross_process import (
            get_registered_payload_cache,
            publish_stale_prefix,
            record_registry_publish_failure,
            record_registry_publish_success,
        )

        payload_cache = get_registered_payload_cache()
        if payload_cache is not None:
            # Round 6 (Codex): check the real outcome -- publish_stale_
            # prefix() already logs its own WARNING on failure; logging
            # "Published..." here unconditionally would silently lie
            # about whether propagation actually happened.
            published = publish_stale_prefix(payload_cache, old_target)
            if published:
                record_registry_publish_success()
                logger.info(
                    "%s Published chunk store cache stale prefix to "
                    "cross-process registry for old snapshot %s",
                    log_context,
                    old_target,
                )
            else:
                # Round 8 (Claude): publish_stale_prefix() correctly
                # returns False on a genuine write-verification failure
                # (round-7 fix), but until now that produced ZERO log
                # output at any level -- streak-tracked so a sustained
                # outage gets exactly one WARNING at start/recovery, not
                # one per refresh.
                record_registry_publish_failure(old_target)
    except Exception as _publish_err:
        logger.warning(
            "%s Failed to publish chunk store cache stale prefix for "
            "old snapshot %s: %s",
            log_context,
            old_target,
            _publish_err,
        )


def _evict_chunk_store_cache(old_target: str, log_context: str) -> None:
    """Register ``old_target`` as a stale chunk-store prefix, LOCALLY and
    cross-process. Non-fatal -- logs a WARNING and returns on any
    failure, never raises.
    """
    try:
        from code_indexer.storage.shared.chunk_store_cache import (
            get_global_chunk_store_cache,
        )

        # Bug #1775: ChunkStoreThreadCache.invalidate_prefix() is
        # deliberately void -- it only REGISTERS old_target as a stale
        # prefix (fast, safe from any thread); actual eviction happens
        # lazily, later, per-key, inside get_or_open(). There is no
        # synchronous "evicted count" for this call site to log (unlike
        # the HNSW cache, whose invalidate_prefix() evicts synchronously
        # and does return one).
        get_global_chunk_store_cache().invalidate_prefix(old_target)
        logger.info(
            "%s Registered chunk store cache stale prefix for old snapshot %s",
            log_context,
            old_target,
        )
    except Exception as _chunk_err:
        logger.warning(
            "%s Failed to invalidate chunk store cache for old snapshot %s: %s",
            log_context,
            old_target,
            _chunk_err,
        )

    _publish_chunk_store_stale_prefix_cross_process(old_target, log_context)


def invalidate_snapshot_caches(
    old_target: Optional[str],
    *,
    log_context: str,
    is_versioned_snapshot_check: Callable[[str], bool],
) -> None:
    """Evict stale HNSW + chunk-store cache entries for a superseded
    versioned-snapshot path.

    Args:
        old_target: The alias's PREVIOUS target path (before the swap). A
            falsy value (e.g. a first-ever publish, where there is no
            prior snapshot) is a no-op. A value that is not itself a
            genuine versioned snapshot (e.g. the master base clone on a
            repo's first refresh) is ALSO a no-op -- see the module
            docstring's HIGH #2 remediation note.
        log_context: Short prefix identifying the caller for log
            correlation (e.g. ``"[change_branch]"``,
            ``"[REFRESH-my-repo-global]"``, ``"[add_index]"``).
        is_versioned_snapshot_check: REQUIRED mount-aware resolver
            (round-3 MEDIUM remediation; round-4: made non-optional --
            see module docstring). Every one of the 5 real call sites
            passes a ``GoldenRepoManager``/``RefreshScheduler`` instance's
            own ``_is_versioned_snapshot`` bound method (which delegates
            to its ``VersionedSnapshotManager`` when configured), so
            legacy cow-daemon/ONTAP snapshot shapes (no ``.versioned/``
            segment, only recognized when ``mount_point`` is known) are
            correctly classified. Required (not optional) so a future 6th
            call site cannot silently regress into the mount-unaware bare
            predicate by simply forgetting to pass it -- mypy now catches
            a missing argument at CI time. A caller that genuinely wants
            the bare canonical-only behavior may still pass
            ``code_indexer.server.storage.shared.snapshot_paths.
            is_versioned_snapshot`` explicitly.
    """
    if not old_target:
        return

    try:
        is_versioned = is_versioned_snapshot_check(old_target)
    except Exception as _check_err:
        logger.warning(
            "%s is_versioned_snapshot_check failed for %s: %s -- skipping "
            "cache invalidation (fail-safe: treat as non-versioned rather "
            "than raise, matching this function's own non-fatal contract)",
            log_context,
            old_target,
            _check_err,
        )
        return

    if not is_versioned:
        logger.info(
            "%s Skipping cache invalidation for non-versioned-snapshot "
            "path %s (most likely the master base clone on a repo's "
            "first refresh -- never eligible for stale-prefix "
            "invalidation)",
            log_context,
            old_target,
        )
        return

    correlation_id = _resolve_correlation_id()
    _evict_hnsw_cache(old_target, log_context, correlation_id)
    _evict_chunk_store_cache(old_target, log_context)
