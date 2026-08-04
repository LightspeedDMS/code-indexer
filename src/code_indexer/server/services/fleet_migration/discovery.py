"""Fleet-wide golden-repo migration candidate discovery (Story #1458,
Technical Implementation Details -- "Migration job integrates with
BackgroundJobManager + JobTracker" checklist item).

``run_fleet_migration_for_repo()`` (orchestrator.py) is the real, tested
per-repo migration sequence, but it takes EXPLICIT arguments
(``semantic_collection_dirs``, ``temporal_namespaces``, ``sister_root``,
``sister_alias_manager``) rather than discovering them itself. This module
is the missing link a fleet-wide scheduler needs: it enumerates every golden
repo's MUTABLE BASE CLONE (the same Priority-1 path the orchestrator itself
consolidates -- ``GoldenRepoManager.get_actual_repo_path``) and computes
those arguments from real on-disk structure.

Reuses ``golden_repo_manager.list_golden_repos()``/``get_actual_repo_path()``
-- the SAME primitives ``hnsw_orphan_sweep/discovery.py`` already reuses --
rather than inventing a fourth repo-enumeration mechanism. Temporal
namespace parsing reuses ``parse_physical_temporal_name`` and the SAME
``pointer_namespace`` construction formula Story #1457's own production
``maybe_relocate_shard_to_sister_location`` trigger uses
(``temporal_relocation_trigger.py``): ``sister_root`` is the base clone's
own parent directory (``golden_repos_dir``), and ``sister_alias_manager`` is
``AliasManager(golden_repos_dir / "aliases")`` -- byte-identical to that
trigger's own construction.

``is_repo_already_migrated`` deliberately does NOT read from a separate
durable-cursor table. It re-derives completeness FRESH from disk every
call, using the SAME two authorities already governing correctness
elsewhere in this story: ``repo_temporal_dirs_fully_consolidated``
(completion_gate.py, AC1/AC10's own completion predicate) and
``resolve_chunk_layout`` (AC12's canonical, uncached layout resolver). This
keeps a fleet-wide scheduler naturally idempotent and crash-safe without
introducing a second source of truth that could drift from the actual
filesystem state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.services.fleet_migration.completion_gate import (
    repo_has_published_post_consolidation_snapshot,
    repo_temporal_dirs_fully_consolidated,
)
from code_indexer.server.services.query_path_cache import (
    is_immutable_versioned_snapshot,
)
from code_indexer.server.services.fleet_migration.orchestrator import (
    TemporalNamespaceSpec,
)
from code_indexer.services.temporal.temporal_collection_naming import (
    TEMPORAL_COLLECTION_PREFIX,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    parse_physical_temporal_name,
)
from code_indexer.storage.shared.collection_migration import (
    verify_collection_fully_migrated,
)

logger = logging.getLogger(__name__)

_INDEX_ROOT_SEGMENTS = (".code-indexer", "index")
_META_FILENAME = "collection_meta.json"


@dataclass(frozen=True)
class FleetMigrationCandidate:
    """One golden repo's fully-resolved fleet-migration inputs, ready to
    pass directly to ``run_fleet_migration_for_repo``."""

    sort_key: str
    golden_alias: str
    base_clone_path: Path
    index_path: Path
    semantic_collection_dirs: List[Path]
    temporal_namespaces: List[TemporalNamespaceSpec]
    sister_root: Path
    sister_alias_manager: AliasManager


def _pointer_namespace(
    repo_alias: str, embedder_slug: str, quarter: Optional[str]
) -> str:
    """Byte-identical to temporal_relocation_trigger.py's own formula."""
    suffix = f"-{quarter}" if quarter else ""
    return f"{repo_alias}-temporal-{embedder_slug}{suffix}"


def _discover_semantic_and_temporal(
    repo_alias: str, index_path: Path
) -> Tuple[List[Path], List[TemporalNamespaceSpec]]:
    semantic_dirs: List[Path] = []
    temporal_namespaces: List[TemporalNamespaceSpec] = []

    try:
        if not index_path.is_dir():
            return semantic_dirs, temporal_namespaces
        entries = sorted(index_path.iterdir())
    except OSError as exc:
        logger.debug(
            "enumerate_fleet_migration_candidates: could not list %s (%s); "
            "treating as no collections found this pass",
            index_path,
            exc,
        )
        return semantic_dirs, temporal_namespaces

    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.is_symlink():
            # Codex CRITICAL finding (round 5): a symlinked child
            # directory under index_path must never be accepted here --
            # consolidate_collection_in_place() (and the temporal
            # legacy-shard bootstrap path) would write straight THROUGH
            # the symlink into whatever it resolves to. The candidate-
            # level is_immutable_versioned_snapshot() check above only
            # inspects the repo's OWN base_clone_path; it has no
            # visibility into an individual nested collection/shard
            # directory that happens to be a symlink into an unrelated
            # (possibly immutable .versioned/) location.
            logger.warning(
                "enumerate_fleet_migration_candidates: '%s' is a SYMLINK "
                "-- refusing to treat it as a real collection/temporal "
                "directory (would bypass in-place-consolidation safety)",
                entry,
            )
            continue
        if entry.name.startswith(TEMPORAL_COLLECTION_PREFIX):
            parsed = parse_physical_temporal_name(entry.name)
            if parsed is None:
                logger.debug(
                    "enumerate_fleet_migration_candidates: '%s' has the "
                    "temporal prefix but did not parse; skipping",
                    entry.name,
                )
                continue
            embedder_slug, quarter = parsed
            temporal_namespaces.append(
                TemporalNamespaceSpec(
                    pointer_namespace=_pointer_namespace(
                        repo_alias, embedder_slug, quarter
                    ),
                    legacy_shard_dir=entry,
                    embedder_slug=embedder_slug,
                )
            )
        elif (entry / _META_FILENAME).is_file():
            semantic_dirs.append(entry)

    return semantic_dirs, temporal_namespaces


def enumerate_fleet_migration_candidates(
    golden_repo_manager: Any,
) -> Iterator[FleetMigrationCandidate]:
    """Enumerate every golden repo's fleet-migration inputs, in stable
    alias-sorted order (a deterministic scan order a scheduler can page
    through).

    Tolerates dangling registrations (no resolvable/existing clone on disk)
    -- skips them rather than raising, matching the fleet-sweep convention
    already established by ``hnsw_orphan_sweep/discovery.py``.

    Args:
        golden_repo_manager: Object with ``list_golden_repos()`` and
            ``get_actual_repo_path(alias)``.
    """
    entries = golden_repo_manager.list_golden_repos()
    sorted_entries = sorted(
        entries, key=lambda e: e.get("alias") or e.get("alias_name") or ""
    )

    for entry in sorted_entries:
        alias = entry.get("alias") or entry.get("alias_name")
        if not alias:
            logger.debug(
                "enumerate_fleet_migration_candidates: golden repo entry "
                "missing alias: %s",
                entry,
            )
            continue

        try:
            base_clone_path = Path(golden_repo_manager.get_actual_repo_path(alias))
        except Exception as exc:
            logger.debug(
                "enumerate_fleet_migration_candidates: could not resolve "
                "golden repo '%s' path (%s); skipping (dangling registration)",
                alias,
                exc,
            )
            continue

        # Codex Finding #1 (CRITICAL): get_actual_repo_path() falls back to
        # the IMMUTABLE .versioned/{alias}/v_<ts>/ snapshot path when the
        # mutable base-clone metadata path is absent (project CLAUDE.md
        # "Golden Repo Versioned Path" invariant -- NEVER modify/checkout/
        # index inside .versioned/). Migration only ever targets the
        # MUTABLE base clone; a dangling registration that only resolves to
        # the immutable snapshot MUST be skipped entirely, never fed to the
        # destructive migration engine.
        #
        # Follow-up hardening: get_actual_repo_path() can return an
        # UNRESOLVED symlink whose real target lands inside .versioned/ --
        # the string alone would not structurally match the canonical
        # predicate's shape, so resolve() BEFORE testing (checking the
        # resolved path only, base_clone_path itself is left untouched for
        # all downstream use).
        if is_immutable_versioned_snapshot(str(base_clone_path.resolve())):
            logger.warning(
                "enumerate_fleet_migration_candidates: golden repo '%s' "
                "resolved to an IMMUTABLE .versioned/ snapshot (%s) -- no "
                "mutable base clone exists; skipping (never migrate an "
                "immutable snapshot)",
                alias,
                base_clone_path,
            )
            continue

        if not base_clone_path.exists():
            logger.debug(
                "enumerate_fleet_migration_candidates: golden repo '%s' "
                "root %s does not exist; skipping",
                alias,
                base_clone_path,
            )
            continue

        index_path = base_clone_path.joinpath(*_INDEX_ROOT_SEGMENTS)
        semantic_dirs, temporal_namespaces = _discover_semantic_and_temporal(
            alias, index_path
        )

        golden_repos_dir = base_clone_path.parent
        sister_alias_manager = AliasManager(str(golden_repos_dir / "aliases"))

        yield FleetMigrationCandidate(
            sort_key=alias,
            golden_alias=alias,
            base_clone_path=base_clone_path,
            index_path=index_path,
            semantic_collection_dirs=semantic_dirs,
            temporal_namespaces=temporal_namespaces,
            sister_root=golden_repos_dir,
            sister_alias_manager=sister_alias_manager,
        )


def is_repo_already_migrated(candidate: FleetMigrationCandidate) -> bool:
    """AC1/AC10's binding completeness gate, evaluated fresh from disk.

    True iff (a) the repo has ZERO residual in-repo temporal directories of
    either shape, (b) every semantic collection is GENUINELY, VERIFIED
    fully migrated, AND (c) the AC10 post-consolidation snapshot has
    actually been published.

    Codex CRITICAL Finding #2 (round 2): (b) is deliberately NOT a bare
    ``resolve_chunk_layout(...) == ChunkLayout.CHUNKS_DB`` discriminator
    check -- that alone cannot distinguish a genuinely complete collection
    from one where a crash happened between the durable flip and cleanup
    completing (in which case the real resume/cleanup verifier in
    ``consolidate_collection_in_place`` would never be reached again, since
    the scheduler skips any candidate this predicate calls "migrated").
    Delegates to :func:`verify_collection_fully_migrated`, the SAME
    fresh-reopen verification the resume path itself uses -- never
    reinvented here.

    New CRITICAL finding: (c) closes a distinct gap -- (a) and (b) alone
    only prove DATA consolidation finished; a crash before/during the
    AC10 snapshot trigger (``orchestrator.py``'s
    ``trigger_post_consolidation_snapshot`` call) would otherwise leave
    the snapshot permanently unpublished, since this predicate is exactly
    what the scheduler uses to decide whether to skip the repo. A repo
    with NO semantic collections and NO temporal namespaces is therefore
    NOT "trivially migrated" until it, too, carries the marker -- the
    orchestrator fires the snapshot unconditionally on every completed
    pass, collections or not, so the marker's presence is the one true
    signal that this repo has been through a real, complete pass.
    """
    if not repo_temporal_dirs_fully_consolidated(candidate.index_path):
        return False
    if not all(
        verify_collection_fully_migrated(collection_dir)
        for collection_dir in candidate.semantic_collection_dirs
    ):
        return False
    # bool(...) wrap works around a pre-existing project mypy module-
    # identity quirk (this file resolves under a src.-prefixed module
    # identity when checked from the repo root, which otherwise infers
    # this cross-module return as Any despite the callee's correctly-
    # annotated bool return type -- see collection_migration.py's own
    # analogous workaround).
    return bool(repo_has_published_post_consolidation_snapshot(candidate.index_path))
