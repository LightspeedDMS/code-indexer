"""Shared temporal-status inspection helper (Issue #1459, rewritten by Bug
#1529).

Status-reporting call sites need one repo-wide answer to "does this golden
repo have temporal data, and is any of it queryable?" -- across BOTH physical
roots temporal data can occupy:

  1. the FIXED server-owned root, ``{golden_repos_dir}/.temporal/{alias}/``
     (Bug #1529 -- where server-context temporal indexing writes); and
  2. the golden repo clone's own ``.code-indexer/index/`` (the standalone-CLI
     location, and where pre-#1529 server data still sits until relocated).

Bug #1529 replaced Story #1457's versioned-snapshot + alias-pointer +
``TemporalShardResolver`` design with a fixed, deterministic path per
(alias, embedder, quarter). There is consequently nothing to "resolve": both
roots are scanned directly, and the fixed root wins when the same namespace
appears in both (it is the location the current write path targets).

``has_data`` and ``is_queryable`` remain DELIBERATELY DISTINCT (the
row-existence-is-not-queryability principle): a shard can hold real committed
rows with no ``hnsw_index.bin`` yet (a crash window between the row write and
the index finalize). Such a shard counts toward ``has_data`` but NEVER toward
``is_queryable``. Callers must not conflate the two.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Literal, Optional, Set, Tuple

from code_indexer.services.temporal.temporal_collection_naming import (
    parse_physical_temporal_name,
)
from code_indexer.services.temporal.temporal_row_existence import (
    temporal_shard_has_committed_rows,
)
from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)

logger = logging.getLogger(__name__)

_HNSW_INDEX_FILENAME = "hnsw_index.bin"


class TemporalDataLocation(Enum):
    """Which physical root a temporal shard was found in.

    Replaces Story #1457's ``TemporalShardSource`` (SISTER_POINTER /
    IN_REPO_LEGACY), which described alias-pointer resolution that no longer
    exists.
    """

    #: The fixed server-owned root, {golden_repos_dir}/.temporal/{alias}/.
    FIXED_SERVER_ROOT = "fixed_server_root"
    #: The golden repo clone's own .code-indexer/index/ directory.
    IN_REPO = "in_repo"


@dataclass(frozen=True)
class TemporalRepoStatus:
    """Repo-wide temporal-data status across both physical roots.

    Attributes:
        has_data: True if ANY temporal shard (any embedder_slug/quarter, in
            either root) holds real committed rows. Does NOT imply
            queryability.
        is_queryable: True if AT LEAST ONE such shard also has a working
            ``hnsw_index.bin``. Distinct from ``has_data`` -- see the module
            docstring.
        resolved_path: Physical directory of the "best" shard found --
            preferring a queryable one, falling back to the first
            row-bearing-but-not-yet-queryable one. None when ``has_data``
            is False. Callers that stat/read a marker file inside the shard
            use this path.
        resolved_source: Which root ``resolved_path`` came from. None when
            ``has_data`` is False.
    """

    has_data: bool
    is_queryable: bool
    resolved_path: Optional[Path]
    resolved_source: Optional[TemporalDataLocation]


_NO_TEMPORAL_DATA = TemporalRepoStatus(
    has_data=False, is_queryable=False, resolved_path=None, resolved_source=None
)


def _scan_root(
    root: Path, location: TemporalDataLocation
) -> Dict[Tuple[str, Optional[str]], Tuple[Path, TemporalDataLocation]]:
    """Map every parseable temporal shard directly under ``root``.

    Keyed by (embedder_slug, quarter) so the caller can prefer one root over
    the other for the same namespace. Never raises: a missing/unreadable root
    contributes nothing (this is an inspection helper feeding status surfaces,
    which must stay resilient).
    """
    found: Dict[Tuple[str, Optional[str]], Tuple[Path, TemporalDataLocation]] = {}
    if not root.is_dir():
        return found
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        logger.warning(
            "temporal status: could not list %s (%s); treating as no temporal "
            "shards found this call",
            root,
            exc,
        )
        return found

    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        parsed = parse_physical_temporal_name(entry.name)
        if parsed is None:
            continue
        found[parsed] = (entry, location)
    return found


def _discover_shards(
    golden_repos_dir: Path,
    repo_alias: str,
    legacy_index_path: Path,
    *,
    on_error: Literal["treat_absent", "raise"] = "treat_absent",
) -> List[Tuple[Path, TemporalDataLocation]]:
    """Every ROW-BEARING temporal shard for this repo, across both roots.

    The fixed server root wins for a namespace present in both -- it is where
    the current write path targets, so an older in-repo copy of the same
    (embedder, quarter) is stale by construction.

    ``on_error`` is forwarded verbatim to the row-existence predicate for each
    candidate shard; see ``get_temporal_repo_status``.
    """
    in_repo = _scan_root(Path(legacy_index_path), TemporalDataLocation.IN_REPO)
    fixed = _scan_root(
        server_temporal_index_root(golden_repos_dir, repo_alias),
        TemporalDataLocation.FIXED_SERVER_ROOT,
    )

    merged = dict(in_repo)
    merged.update(fixed)  # fixed root takes precedence

    shards: List[Tuple[Path, TemporalDataLocation]] = []
    for key in sorted(merged, key=lambda k: (k[0], k[1] or "")):
        path, location = merged[key]
        if temporal_shard_has_committed_rows(path, on_error=on_error):
            shards.append((path, location))
    return shards


def get_temporal_repo_status(
    golden_repos_dir: Path,
    repo_alias: str,
    legacy_index_path: Path,
    *,
    on_error: Literal["treat_absent", "raise"] = "treat_absent",
) -> TemporalRepoStatus:
    """Resolve repo-wide temporal-data status across both physical roots.

    Args:
        golden_repos_dir: The golden-owned root (e.g.
            ``Path(activated_repo_manager.activated_repos_dir).parent /
            "golden-repos"``). The fixed temporal root is derived from it.
        repo_alias: The golden repo's alias (a trailing ``-global`` is
            normalized away by the path helper).
        legacy_index_path: The golden repo clone's own
            ``.code-indexer/index/`` directory.
        on_error: What to do when a shard's ``chunks.db`` cannot be read
            (corrupt, locked, permission denied). ``"treat_absent"`` (the
            default, and correct for every STATUS/health surface) reports it
            as no data so reporting stays resilient. ``"raise"`` propagates
            and MUST be used by any caller whose "no data" answer authorizes
            a DESTRUCTIVE action -- see ``temporal_reindex_needs_clear``,
            where a silent False wipes real shards and forces a full
            re-embed of the entire git history (Bug #1529 finding #5).

    Returns:
        TemporalRepoStatus summarizing presence and queryability across every
        temporal namespace this repo has (any embedder, any quarter).

    Raises:
        Propagates the underlying store-read error when ``on_error="raise"``.
    """
    shards = _discover_shards(
        Path(golden_repos_dir),
        repo_alias,
        Path(legacy_index_path),
        on_error=on_error,
    )
    if not shards:
        return _NO_TEMPORAL_DATA

    best_queryable: Optional[Tuple[Path, TemporalDataLocation]] = None
    best_any: Optional[Tuple[Path, TemporalDataLocation]] = None
    for path, location in shards:
        if (path / _HNSW_INDEX_FILENAME).exists():
            if best_queryable is None:
                best_queryable = (path, location)
        elif best_any is None:
            best_any = (path, location)

    chosen = best_queryable or best_any
    return TemporalRepoStatus(
        has_data=True,
        is_queryable=best_queryable is not None,
        resolved_path=chosen[0] if chosen else None,
        resolved_source=chosen[1] if chosen else None,
    )


def _read_shard_completed_commit_hashes(progress_path: Path) -> Optional[list]:
    """Read one shard's ``temporal_progress.json`` completed_commits list.

    Returns None (meaning "could not be reliably read") when the file is
    missing, unreadable, malformed, or does not carry a list -- the caller
    treats that as "repo-wide total unknowable" and fails open rather than
    undercounting.
    """
    try:
        if not progress_path.is_file():
            return None
        with open(str(progress_path), "r") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug(
            "temporal status: could not read %s (%s)",
            progress_path,
            exc,
        )
        return None

    if not isinstance(data, dict):
        return None
    completed = data.get("completed_commits")
    if not isinstance(completed, list):
        return None
    return completed


def get_temporal_repo_max_commits(
    golden_repos_dir: Path,
    repo_alias: str,
    legacy_index_path: Path,
) -> Optional[int]:
    """Bug #1461-6: repo-wide max_commits fallback, unioned across EVERY
    row-bearing quarter shard of EVERY discoverable embedder -- never just the
    single "best" shard get_temporal_repo_status() picks.

    The legacy ``temporal_meta.json.total_commits`` field this replaces was a
    REPO-WIDE total. Reading completed_commits from only one quarter shard
    undercounts any repo with more than one quarter -- setting --max-commits
    LOWER than the true total and truncating historical coverage. That is
    worse than omitting the bound entirely (the by-design-cheap unbounded full
    walk), so this fails OPEN to None whenever the total cannot be reliably
    determined, rather than silently undercounting.

    Returns:
        The repo-wide completed-commit count (union of hashes across every
        shard -- a hash appearing in more than one quarter/embedder counted
        once), or None if no temporal data was found, any shard's progress
        data could not be reliably read, or the aggregate union is empty. An
        all-empty aggregate is treated as "unknown": emitting
        ``--max-commits 0`` would cap the run to zero new commits, silently
        defeating the indexing operation -- never a safe automatic value.
    """
    shards = _discover_shards(
        Path(golden_repos_dir), repo_alias, Path(legacy_index_path)
    )
    if not shards:
        return None

    commit_hashes: Set[str] = set()
    for path, _location in shards:
        shard_commits = _read_shard_completed_commit_hashes(
            path / "temporal_progress.json"
        )
        if shard_commits is None:
            logger.debug(
                "get_temporal_repo_max_commits: %s has real data but its "
                "temporal_progress.json could not be reliably read -- "
                "repo-wide total for %s is unknowable, omitting "
                "--max-commits rather than under-capping",
                path,
                repo_alias,
            )
            return None
        commit_hashes.update(shard_commits)

    if not commit_hashes:
        return None
    return len(commit_hashes)
