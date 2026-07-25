"""Shared resolver-aware temporal-status inspection helper (GitHub Issue
#1459 AC4, Story #1457 follow-on).

Story #1457 introduced the ability for temporal (git-history) index data to
physically relocate OUT of a golden repo's cloned directory into a
golden-owned "sister location" (see this project's root CLAUDE.md, "Shared
Versioned-Snapshot Hardening + Server-Context Child-Env Marker (Story
#1457, partial)"). Five observability/inspection call sites previously
scanned ONLY the local clone's `.code-indexer/index/` directory for
`code-indexer-temporal-*` subdirectories -- once temporal data relocates,
every one of them would incorrectly report "not indexed" for a repo that
genuinely has queryable temporal data.

This module is the ONE shared, reusable answer to "does this repo have
temporal data, and is it queryable" -- built entirely on top of Story
#1457's `TemporalShardResolver` (`resolve()`/`catalog()`), never a parallel
sister-root scan. Every one of the five call sites calls
`get_temporal_repo_status()` instead of reimplementing the scan.

## The enumeration problem

`resolver.resolve()`/`resolver.catalog()` are scoped to ONE embedder_slug at
a time, but these status-reporting call sites don't know the configured
embedder slug(s) up front (they currently just ask "is there ANY temporal
data at all"). `_enumerate_candidate_embedder_slugs()` solves this by
scanning BOTH physical roots:
  - the in-repo legacy directory (base-name `code-indexer-temporal-*`
    dirs, parsed via `parse_physical_temporal_name`), and
  - the sister aliases directory (`{repo_alias}-temporal-*` pointer
    files), whose alias-prefixed name is rewritten into the base-name
    physical form and fed back through the SAME
    `parse_physical_temporal_name` parser -- never a second regex.

A filesystem scan failure (`OSError`) on either root is logged at WARNING
and treated as "no candidates found on that root this call" -- deliberately
fail-open, matching the established convention in this codebase's other
fleet-wide directory-scan helpers (e.g.
`fleet_migration/discovery.py::_discover_semantic_and_temporal`). This is a
read-only observability/status surface, not a correctness-critical write
path: a transient scan failure must not raise and break a status page, but
it IS logged so the condition is visible to operators.
"""

from __future__ import annotations

import glob
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_collection_naming import (
    TEMPORAL_COLLECTION_PREFIX,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    ResolvedTemporalShard,
    TemporalShardResolver,
    TemporalShardSource,
    parse_physical_temporal_name,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TemporalRepoStatus:
    """Repo-wide temporal-data status, resolved via TemporalShardResolver
    across BOTH physical roots (sister-published + in-repo legacy).

    Attributes:
        has_data: True if ANY temporal namespace (any embedder_slug/quarter)
            resolves to real data -- sister-published OR in-repo-legacy
            with committed rows. Does NOT imply queryability.
        is_queryable: True if AT LEAST ONE resolved namespace has a working
            HNSW (`ResolvedTemporalShard.is_queryable`). Distinct from
            `has_data` per the row-existence-not-queryability principle
            (Story #1457 AC8): a namespace can have real committed rows
            with no `hnsw_index.bin` yet (crash window) -- that namespace
            counts toward `has_data` but never toward `is_queryable`.
        resolved_path: Physical directory of the "best" resolved shard --
            preferring a queryable one; falling back to the first
            not-yet-queryable one when none is queryable. None when
            `has_data` is False. Callers that need to stat/read a marker
            file (`hnsw_index.bin`, `metadata.json`) inside the resolved
            shard use this path.
        resolved_source: Which physical root `resolved_path` came from
            (SISTER_POINTER or IN_REPO_LEGACY). None when `has_data` is
            False.
    """

    has_data: bool
    is_queryable: bool
    resolved_path: Optional[Path]
    resolved_source: Optional[TemporalShardSource]


_NO_TEMPORAL_DATA = TemporalRepoStatus(
    has_data=False, is_queryable=False, resolved_path=None, resolved_source=None
)


def _enumerate_candidate_embedder_slugs(
    legacy_index_path: Path, aliases_dir: Path, repo_alias: str
) -> Set[str]:
    """Enumerate candidate embedder_slugs from BOTH physical roots.

    Pure name-parsing, no queryability/existence judgement here --
    `resolve()`/`catalog()` decide correctness; this only discovers WHICH
    embedder slugs might be relevant to this repo so they can be handed to
    the resolver.
    """
    slugs: Set[str] = set()

    if legacy_index_path.is_dir():
        try:
            entries = list(legacy_index_path.iterdir())
        except OSError as exc:
            logger.warning(
                "get_temporal_repo_status: could not list legacy index path "
                "%s (%s); treating as no in-repo temporal candidates found "
                "this call",
                legacy_index_path,
                exc,
            )
            entries = []
        for entry in entries:
            if not entry.is_dir():
                continue
            parsed = parse_physical_temporal_name(entry.name)
            if parsed is not None:
                slugs.add(parsed[0])

    if aliases_dir.is_dir():
        alias_prefix = f"{repo_alias}-temporal-"
        try:
            escaped_prefix = glob.escape(alias_prefix)
            alias_files = list(aliases_dir.glob(f"{escaped_prefix}*.json"))
        except OSError as exc:
            logger.warning(
                "get_temporal_repo_status: could not glob aliases dir %s "
                "for repo_alias=%r (%s); treating as no sister-pointer "
                "temporal candidates found this call",
                aliases_dir,
                repo_alias,
                exc,
            )
            alias_files = []
        for alias_file in alias_files:
            # alias_file.stem always starts with alias_prefix here: the
            # glob pattern above is `{escaped_prefix}*.json`, so the full
            # filename (and therefore its .json-stripped stem) is
            # guaranteed to start with the literal alias_prefix -- no
            # defensive re-check needed.
            name = alias_file.stem
            remainder = name[len(alias_prefix) :]
            if not remainder:
                continue
            # Rewrite the alias-prefixed physical form into the base-name
            # physical form so the SAME quarter-suffix parser the resolver
            # module already exposes can recover (embedder_slug, quarter)
            # -- never a second regex.
            synthetic_physical_name = f"{TEMPORAL_COLLECTION_PREFIX}{remainder}"
            parsed = parse_physical_temporal_name(synthetic_physical_name)
            if parsed is not None:
                slugs.add(parsed[0])

    return slugs


def get_temporal_repo_status(
    golden_repos_dir: Path,
    repo_alias: str,
    legacy_index_path: Path,
) -> TemporalRepoStatus:
    """Resolve repo-wide temporal-data status via TemporalShardResolver.

    Args:
        golden_repos_dir: The golden-owned root housing `aliases/` and
            `.versioned/{namespace}/v_*/` (Story #1457's `sister_root`) --
            e.g. `Path(activated_repo_manager.activated_repos_dir).parent /
            "golden-repos"`.
        repo_alias: The golden repo's BARE alias (no `-global` suffix).
        legacy_index_path: The golden repo clone's own
            `.code-indexer/index/` directory (Story #1457's
            `legacy_index_path` -- in-repo fallback source).

    Returns:
        TemporalRepoStatus summarizing presence and queryability across
        every temporal namespace this repo has (any embedder, any
        quarter).
    """
    legacy_index_path = Path(legacy_index_path)
    golden_repos_dir = Path(golden_repos_dir)
    aliases_dir = golden_repos_dir / "aliases"

    embedder_slugs = _enumerate_candidate_embedder_slugs(
        legacy_index_path, aliases_dir, repo_alias
    )
    if not embedder_slugs:
        return _NO_TEMPORAL_DATA

    resolver = TemporalShardResolver(
        alias_manager=AliasManager(str(aliases_dir)),
        repo_alias=repo_alias,
        sister_root=golden_repos_dir,
        legacy_index_path=legacy_index_path,
    )

    has_data = False
    best_queryable: Optional[ResolvedTemporalShard] = None
    best_any: Optional[ResolvedTemporalShard] = None

    for embedder_slug in sorted(embedder_slugs):
        for quarter in resolver.catalog(embedder_slug):
            resolved = resolver.resolve(embedder_slug, quarter)
            if resolved is None:
                continue
            has_data = True
            if resolved.is_queryable:
                if best_queryable is None:
                    best_queryable = resolved
            else:
                if best_any is None:
                    best_any = resolved

    chosen = best_queryable or best_any
    return TemporalRepoStatus(
        has_data=has_data,
        is_queryable=best_queryable is not None,
        resolved_path=chosen.path if chosen else None,
        resolved_source=chosen.source if chosen else None,
    )
