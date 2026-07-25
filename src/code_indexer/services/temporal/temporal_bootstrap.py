"""bootstrap_temporal_namespace_to_sister() -- completes Story #1457's AC11
as bound by Story #1458 AC1/AC1a.

Story #1457's AC11 (one-time proactive bootstrap of pre-existing in-repo
temporal shards to the sister location) was explicitly left structurally
blocked on Story #1458's per-repo fleet-migration job, held write lock, and
in-process reclamation context -- see `temporal_bootstrap_disposition.py`'s
module docstring. This module IS that completion: invoked as literal step 2
of Story #1458's per-repo fleet-migration job (AC1), inside the SAME held
write lock, for every in-repo temporal quarter-shard (or quarter-less
monolith) of the target repo.

This module owns ONLY the per-namespace orchestration -- classify, then act
on the disposition. It reuses, and never reimplements:
  - `classify_bootstrap_disposition` (Story #1457 AC11 Finding 6) for the
    three-way per-namespace decision.
  - `read_legacy_shard_rows` (Story #1457 AC1) for the side-effect-free
    full-row scan.
  - `build_fresh_consolidated_temporal_version` /
    `publish_temporal_shard_version` (Story #1457 AC6) for the actual
    build+publish mechanics -- the SAME primitives AC6's own bootstrap
    branch uses, so a bootstrapped namespace and an AC6-refreshed one are
    built identically.

Reclamation (deleting the in-repo tree) is SYNCHRONOUS and IN-PROCESS here
-- never deferred via CleanupManager -- per Story #1457 AC11's round-21
finding: deferring would leave the tree physically present past Story
#1458's completion gate for the duration of AC13's minimum-retention-age
floor, contradicting that gate.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_bootstrap_disposition import (
    BootstrapDisposition,
    classify_bootstrap_disposition,
)
from code_indexer.services.temporal.temporal_consolidated_build import (
    build_fresh_consolidated_temporal_version,
)
from code_indexer.services.temporal.temporal_row_reader import read_legacy_shard_rows
from code_indexer.services.temporal.temporal_shard_publisher import (
    publish_temporal_shard_version,
)
from code_indexer.server.services.query_path_cache import (
    is_immutable_versioned_snapshot,
)
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout
from code_indexer.storage.shared.collection_migration import (
    ConsolidationVerificationError,
    _verify_record_field_for_field,
)
from code_indexer.storage.sqlite_chunk_store import open_chunk_store_for_path

logger = logging.getLogger(__name__)


def _pointer_target_is_valid_and_queryable(
    alias_manager: AliasManager,
    sister_root: Path,
    pointer_namespace: str,
    legacy_shard_dir: Path,
) -> bool:
    """Codex Finding #5 (CRITICAL, hardened round 2): ``alias_exists()``
    alone (a bare file-existence check on the pointer FILE) is NOT
    sufficient grounds to treat legacy in-repo temporal data as safely
    reclaimable. A dangling, stale, corrupt, or MIS-POINTED alias pointer
    must never trigger ``shutil.rmtree`` on the only intact copy of the
    data.

    Resolves the alias's actual TARGET and confirms ALL of:
      (a) CONFINEMENT: the target resolves to EXACTLY
          ``sister_root/.versioned/{pointer_namespace}/<version>`` -- the
          one and only location a real publish
          (``build_fresh_consolidated_temporal_version``) can ever write
          to. A pointer that happens to resolve to a content-valid,
          queryable database SOMEWHERE ELSE must still be refused: content
          validity alone does not prove the pointer is genuinely this
          namespace's own published version.
      (b) the target directory exists and resolves to
          ``ChunkLayout.CHUNKS_DB`` via the canonical (AC12) resolver.
      (c) its ``chunks.db`` opens cleanly with at least one record --
          via :func:`open_chunk_store_for_path`, the SAME
          ``is_immutable_versioned_snapshot()``-gated pattern already
          established for opening published versioned snapshots
          elsewhere in this codebase (never a raw mutable ``ChunkStore``,
          which would violate this project's absolute "NEVER modify/
          checkout/index inside .versioned/" invariant).
    """
    target_path_str = alias_manager.read_alias(pointer_namespace)
    if not target_path_str:
        logger.warning(
            "_pointer_target_is_valid_and_queryable: alias %r has no "
            "readable target (dangling or unreadable pointer)",
            pointer_namespace,
        )
        return False

    target_path = Path(target_path_str).resolve()

    # Codex CRITICAL finding (round 4), bypass #2: reject -- never
    # silently resolve through -- a SYMLINKED namespace directory. If the
    # raw (unresolved) namespace dir is itself a symlink, .resolve() would
    # transparently follow it to an unrelated location, letting an
    # unrelated-but-coincidentally-valid database pass confinement.
    raw_ns_dir = Path(sister_root) / ".versioned" / pointer_namespace
    if raw_ns_dir.is_symlink():
        logger.warning(
            "_pointer_target_is_valid_and_queryable: namespace directory "
            "%s for alias %r is itself a SYMLINK -- refusing to resolve "
            "through it (would let an unrelated location pass confinement)",
            raw_ns_dir,
            pointer_namespace,
        )
        return False

    expected_ns_dir = raw_ns_dir.resolve()
    if target_path.parent != expected_ns_dir:
        logger.warning(
            "_pointer_target_is_valid_and_queryable: alias %r target %s "
            "is not confined to the expected namespace directory %s -- "
            "refusing to trust a pointer that resolves outside its own "
            "namespace, regardless of the target's content",
            pointer_namespace,
            target_path,
            expected_ns_dir,
        )
        return False

    # Codex CRITICAL finding (round 4), bypass #1: confinement under the
    # right namespace directory is NOT sufficient by itself -- the target
    # must ALSO match the canonical v_<digits> immutable-snapshot shape
    # this codebase's real publishes always produce (a target like
    # ".../current" would otherwise pass confinement+content checks but
    # get opened MUTABLE by open_chunk_store_for_path's own fallback,
    # since is_immutable_versioned_snapshot() rejects non-canonical
    # leaves). Reject outright here, explicitly, rather than relying on
    # that fallback.
    if not is_immutable_versioned_snapshot(str(target_path)):
        logger.warning(
            "_pointer_target_is_valid_and_queryable: alias %r target %s "
            "does not match the canonical immutable versioned-snapshot "
            "shape (.versioned/{namespace}/v_<digits>) -- refusing to "
            "trust it, regardless of confinement or content",
            pointer_namespace,
            target_path,
        )
        return False

    if not target_path.is_dir():
        logger.warning(
            "_pointer_target_is_valid_and_queryable: alias %r target %s "
            "does not exist on disk (dangling pointer)",
            pointer_namespace,
            target_path,
        )
        return False

    if resolve_chunk_layout(target_path) != ChunkLayout.CHUNKS_DB:
        logger.warning(
            "_pointer_target_is_valid_and_queryable: alias %r target %s "
            "does not resolve to a committed chunks_db layout",
            pointer_namespace,
            target_path,
        )
        return False

    # Read the full legacy row content ONCE, up front -- fail_on_corrupt=True
    # preserved exactly as before (a corrupt legacy row raises loudly, is
    # NEVER silently converted into a "return False"). Needed as full row
    # dicts (not just ids) for the round-8 content comparison below.
    legacy_rows = list(read_legacy_shard_rows(legacy_shard_dir, fail_on_corrupt=True))
    legacy_ids = {row["id"] for row in legacy_rows if "id" in row}

    try:
        with open_chunk_store_for_path(
            target_path / "chunks.db", str(target_path)
        ) as store:
            record_count = store.count()
            published_ids = store.all_point_ids()

            if record_count == 0:
                logger.warning(
                    "_pointer_target_is_valid_and_queryable: alias %r "
                    "target %s chunks.db opened but contains ZERO records",
                    pointer_namespace,
                    target_path,
                )
                return False

            # Codex round-6 CRITICAL finding #2: content validity,
            # confinement, and count() > 0 are NOT proof the published
            # target actually CONTAINS the specific legacy records about
            # to be deleted -- an unrelated (but otherwise valid)
            # published version at this pointer would previously pass
            # every check above. Require every legacy row id to already
            # be present in the published set (a subset check, not exact
            # equality -- the published version legitimately accumulates
            # MORE history than a single local legacy tree over time).
            missing_ids = legacy_ids - published_ids
            if missing_ids:
                sample = sorted(missing_ids)[:10]
                logger.warning(
                    "_pointer_target_is_valid_and_queryable: alias %r "
                    "target %s does NOT contain %d legacy record id(s) "
                    "about to be deleted (sample: %s) -- refusing to "
                    "trust this pointer as covering the legacy data",
                    pointer_namespace,
                    target_path,
                    len(missing_ids),
                    sample,
                )
                return False

            # Round-8 CRITICAL finding (Codex empirical reproduction):
            # "ID exists in both places" is NECESSARY but NOT SUFFICIENT
            # -- a published row sharing the SAME id as a legacy row but
            # with completely different field content (a corrupted or
            # mismatched published row) previously passed validation,
            # authorizing deletion of the only correct copy. Reuse the
            # SAME exact-equality field/value comparison the temporal
            # build verifier already established (round-6 CRITICAL #3,
            # collection_migration.py's _verify_record_field_for_field)
            # -- never invent a second comparison mechanism.
            for row in legacy_rows:
                point_id = row.get("id")
                if not point_id:
                    continue
                stored = store.read(point_id)
                try:
                    _verify_record_field_for_field(point_id, row, stored)
                except ConsolidationVerificationError as exc:
                    logger.warning(
                        "_pointer_target_is_valid_and_queryable: alias "
                        "%r target %s record %r content does NOT match "
                        "the legacy source (%s) -- refusing to trust "
                        "this pointer as covering the legacy data",
                        pointer_namespace,
                        target_path,
                        point_id,
                        exc,
                    )
                    return False
    except Exception as exc:
        logger.warning(
            "_pointer_target_is_valid_and_queryable: alias %r target %s "
            "chunks.db failed to open/query cleanly: %s",
            pointer_namespace,
            target_path,
            exc,
        )
        return False

    return True


@dataclass
class BootstrapOutcome:
    """Outcome of one :func:`bootstrap_temporal_namespace_to_sister` call."""

    disposition: BootstrapDisposition
    reclaimed: bool
    version_path: Optional[Path] = None
    records_migrated: int = 0


def _reclaim_in_repo_tree(legacy_shard_dir: Path) -> bool:
    """Delete the in-repo legacy shard tree if it still physically exists.

    Returns True if the tree is now (or already was) physically absent.
    """
    if legacy_shard_dir.exists():
        shutil.rmtree(legacy_shard_dir)
    return not legacy_shard_dir.exists()


def bootstrap_temporal_namespace_to_sister(
    *,
    alias_manager: AliasManager,
    sister_root: Path,
    pointer_namespace: str,
    legacy_shard_dir: Path,
    embedder_slug: str,
) -> BootstrapOutcome:
    """Bootstrap ONE (embedder, quarter) in-repo temporal namespace to the
    sister location, per its classified disposition.

    Args:
        alias_manager: AliasManager scoped to the sister location's aliases
            directory (the SAME instance AC6/AC8 use).
        sister_root: Root directory under which `.versioned/{ns}/v_*/`
            published versions live.
        pointer_namespace: The alias-prefixed pointer name, e.g.
            "{repo_alias}-temporal-{embedder_slug}[-{quarter}]".
        legacy_shard_dir: The in-repo legacy shard directory for this
            namespace (may not exist on disk -- valid when already
            ALREADY_PUBLISHED).
        embedder_slug: Sanitized embedder model slug, threaded into the
            v2 temporal_structure.json marker (see
            `build_fresh_consolidated_temporal_version`).

    Returns:
        A :class:`BootstrapOutcome` describing what happened. ``reclaimed``
        is True iff the in-repo tree is physically absent from disk when
        this call returns -- the state this story's completion gate checks.
    """
    disposition = classify_bootstrap_disposition(
        alias_manager, pointer_namespace, legacy_shard_dir
    )

    if disposition == BootstrapDisposition.ALREADY_PUBLISHED:
        # Codex Finding #5 (CRITICAL): alias_exists() alone is NOT
        # sufficient grounds to delete the only intact legacy copy --
        # resolve and validate the pointer's actual TARGET first.
        if not _pointer_target_is_valid_and_queryable(
            alias_manager, sister_root, pointer_namespace, legacy_shard_dir
        ):
            raise RuntimeError(
                f"ALREADY_PUBLISHED disposition for {pointer_namespace!r} "
                f"but the alias pointer's target does not resolve to a "
                f"valid, queryable chunks_db sister version -- refusing "
                f"to delete the only intact legacy temporal data at "
                f"{legacy_shard_dir} (see WARNING log above for the "
                f"specific validation failure)"
            )
        reclaimed = _reclaim_in_repo_tree(legacy_shard_dir)
        return BootstrapOutcome(disposition=disposition, reclaimed=reclaimed)

    if disposition == BootstrapDisposition.EMPTY_ARTIFACT:
        reclaimed = _reclaim_in_repo_tree(legacy_shard_dir)
        return BootstrapOutcome(disposition=disposition, reclaimed=reclaimed)

    # NEEDS_BOOTSTRAP: build fresh, read-back verify (inside the build
    # primitive itself), publish, THEN reclaim -- never reclaim before the
    # sister version is durably published (a crash between build and
    # publish simply leaves the in-repo tree as the still-authoritative
    # legacy representation; retry redoes the build, which is a pure
    # addition at a fresh version-id, safe to redo).
    rows = list(read_legacy_shard_rows(legacy_shard_dir, fail_on_corrupt=True))
    if not rows:
        # classify_bootstrap_disposition's NEEDS_BOOTSTRAP already
        # guarantees at least one committed row via the same row-existence
        # scan -- this is a defensive invariant check, not a reachable
        # production path.
        raise RuntimeError(
            f"NEEDS_BOOTSTRAP disposition for {pointer_namespace!r} but "
            f"read_legacy_shard_rows found zero rows in {legacy_shard_dir}"
        )

    vector_dim = len(rows[0]["vector"])
    version_path = build_fresh_consolidated_temporal_version(
        sister_root,
        pointer_namespace,
        [rows],
        vector_dim,
        embedder_slug=embedder_slug,
    )

    publish_temporal_shard_version(alias_manager, pointer_namespace, version_path)

    reclaimed = _reclaim_in_repo_tree(legacy_shard_dir)
    if not reclaimed:
        logger.warning(
            "bootstrap_temporal_namespace_to_sister: published %s but "
            "failed to fully reclaim in-repo tree %s -- will be swept on "
            "a later migration pass",
            pointer_namespace,
            legacy_shard_dir,
        )

    return BootstrapOutcome(
        disposition=disposition,
        reclaimed=reclaimed,
        version_path=version_path,
        records_migrated=len(rows),
    )
