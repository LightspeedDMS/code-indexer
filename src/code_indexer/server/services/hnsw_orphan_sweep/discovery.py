"""HNSW fleet sweep discovery (Story #1360, Epic #1333 S3).

Two components (design settled in the issue's "Discovery mechanism" section):

Component 1 -- ``iter_index_files_for_repo``: a pure filesystem walk under
``.code-indexer/index/`` for one repo root. No DB/cluster dependency.
Trivially unit-testable with a temp-dir fixture.

Component 2 -- ``enumerate_sweep_candidates``: composes the SAME repo
enumeration primitives ``data_retention_scheduler.py`` and
``description_refresh_scheduler.py`` already reuse
(``golden_repo_manager.list_golden_repos()`` /
``activated_repo_manager.list_all_activated_repositories()``), rather than
inventing a third enumeration mechanism.

Explicitly NOT implemented here: any filtering by ``ShardOwnership.owns()``.
That primitive exists for query-serving cache locality (deliberately
fail-open); reusing it for maintenance-work distribution would create a
real coverage gap under this story's single-flight model (see the issue's
"REJECTED alternative" section). Cross-worker dedup for this story is
``register_job_if_no_conflict`` ONLY (wired in scheduler.py), never a
per-node ownership filter on the candidate set itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.server.storage.shared.snapshot_paths import is_versioned_snapshot

logger = logging.getLogger(__name__)

_INDEX_ROOT_SEGMENTS = (".code-indexer", "index")
_META_FILENAME = "collection_meta.json"


def iter_index_files_for_repo(repo_root: Path) -> Iterator[Path]:
    """Walk ``repo_root/.code-indexer/index/`` yielding real HNSW collections.

    Yields paths to ``hnsw_index.bin`` (relative to *repo_root*) whose parent
    directory ALSO contains ``collection_meta.json`` -- the pair is the
    structural definition of "a real HNSW collection", not just a stray
    file. Regular collections and temporal quarterly shards are structurally
    identical (same two files, different nesting depth), so this single walk
    finds both without needing to know temporal-shard naming conventions.

    Skips any path recognized by the project's canonical immutable-snapshot
    predicate (``is_versioned_snapshot``) -- S2 guarantees freshly-rebuilt
    replacements of versioned snapshots are already clean, and writing inside
    ``.versioned/`` is forbidden project-wide.

    Tolerates ENOENT at every step (missing repo root, missing index root,
    directory removed mid-walk by a concurrent deactivation) -- logs at DEBUG
    and stops iteration; never raises (this is a maintenance-sweep discovery
    generator, not a caller-facing filesystem API -- a vanished repo mid-walk
    is an expected, benign race with activation/deactivation, not a bug to
    surface loudly).

    Args:
        repo_root: Repository root (golden repo clone root or activated repo
            root). Paths yielded are RELATIVE to this root (required for
            stable cursor keys in Component 2 / Component 3).

    Returns:
        Iterator of relative Paths, each pointing at an ``hnsw_index.bin``.
    """
    index_root = repo_root.joinpath(*_INDEX_ROOT_SEGMENTS)

    try:
        if not index_root.is_dir():
            return
        bin_paths = sorted(index_root.rglob(HNSWIndexManager.INDEX_FILENAME))
    except OSError as exc:
        # Repo root / index root vanished mid-walk (concurrent deactivation,
        # NFS blip) -- transient and expected, tolerate without raising.
        logger.debug(
            "iter_index_files_for_repo: transient walk error under %s (%s); "
            "treating as no collections found this pass",
            index_root,
            exc,
        )
        return

    for bin_path in bin_paths:
        parent = bin_path.parent
        try:
            meta_path = parent / _META_FILENAME
            has_meta = meta_path.is_file()
        except OSError as exc:
            logger.debug(
                "iter_index_files_for_repo: could not stat %s (%s); skipping",
                parent / _META_FILENAME,
                exc,
            )
            continue
        if not has_meta:
            continue

        if is_versioned_snapshot(str(parent)):
            continue

        try:
            yield bin_path.relative_to(repo_root)
        except ValueError:
            # Unreachable in practice (bin_path always under repo_root via
            # rglob), but a discovery generator must never raise.
            logger.debug(
                "iter_index_files_for_repo: %s is not relative to %s; skipping",
                bin_path,
                repo_root,
            )
            continue


@dataclass(frozen=True)
class SweepCandidate:
    """One HNSW collection discovered by the fleet sweep, with a stable
    lexicographic sort key used as the durable resume cursor value.

    The sort key is a STRING (never a numeric offset) so that a mid-pass
    mutation of the candidate set (new temporal shard created, repo
    activated/deactivated) cannot silently mean "a different item" the way a
    numeric position would -- see the issue's "Why a stable lexicographic key
    cursor" rationale.
    """

    sort_key: str
    repo_root: Path
    index_relpath: Path
    kind: str  # "golden" | "activated"
    alias: str  # golden alias, or "username/user_alias" for activated repos


def _golden_candidates(golden_repo_manager: Any) -> Iterator[SweepCandidate]:
    for entry in golden_repo_manager.list_golden_repos():
        alias = entry.get("alias") or entry.get("alias_name")
        if not alias:
            logger.debug(
                "enumerate_sweep_candidates: golden repo entry missing alias: %s",
                entry,
            )
            continue
        try:
            repo_root = Path(golden_repo_manager.get_actual_repo_path(alias))
        except Exception as exc:
            # Dangling registration (no clone on disk, registry-orphan, etc.)
            # -- tolerate per the story's "must not raise" requirement, but
            # log so an operator can correlate against the golden-repo
            # registry-orphan reconciler (Bug #1317).
            logger.debug(
                "enumerate_sweep_candidates: could not resolve golden repo "
                "'%s' path (%s); skipping (dangling registration)",
                alias,
                exc,
            )
            continue
        if not repo_root.exists():
            logger.debug(
                "enumerate_sweep_candidates: golden repo '%s' root %s does "
                "not exist; skipping",
                alias,
                repo_root,
            )
            continue
        for relpath in iter_index_files_for_repo(repo_root):
            yield SweepCandidate(
                sort_key=f"golden:{alias}:{relpath}",
                repo_root=repo_root,
                index_relpath=relpath,
                kind="golden",
                alias=alias,
            )


def _activated_candidates(activated_repo_manager: Any) -> Iterator[SweepCandidate]:
    for entry in activated_repo_manager.list_all_activated_repositories():
        username = entry.get("username")
        user_alias = entry.get("user_alias")
        if not username or not user_alias:
            logger.debug(
                "enumerate_sweep_candidates: activated repo entry missing "
                "username/user_alias: %s",
                entry,
            )
            continue
        try:
            repo_root = Path(
                activated_repo_manager.get_activated_repo_path(username, user_alias)
            )
        except Exception as exc:
            logger.debug(
                "enumerate_sweep_candidates: could not resolve activated repo "
                "'%s/%s' path (%s); skipping (dangling registration)",
                username,
                user_alias,
                exc,
            )
            continue
        if not repo_root.exists():
            logger.debug(
                "enumerate_sweep_candidates: activated repo '%s/%s' root %s "
                "does not exist; skipping",
                username,
                user_alias,
                repo_root,
            )
            continue
        for relpath in iter_index_files_for_repo(repo_root):
            yield SweepCandidate(
                sort_key=f"activated:{username}/{user_alias}:{relpath}",
                repo_root=repo_root,
                index_relpath=relpath,
                kind="activated",
                alias=f"{username}/{user_alias}",
            )


def enumerate_sweep_candidates(
    golden_repo_manager: Any, activated_repo_manager: Any
) -> Iterator[SweepCandidate]:
    """Enumerate every HNSW collection across golden + activated repos.

    Reuses ``golden_repo_manager.list_golden_repos()`` and
    ``activated_repo_manager.list_all_activated_repositories()`` -- the same
    primitives ``data_retention_scheduler.py`` / ``description_refresh_scheduler.py``
    already reuse. Activated repos are swept independently of golden (NOT
    deduplicated): CoW/reflink cloning gives each activated repo its own
    physical ``hnsw_index.bin`` once activation completes, and S2's build-path
    fix does nothing for repos cloned before S2 existed.

    Tolerates dangling registrations (missing root path) -- skips them rather
    than raising, so one bad registration never aborts the whole sweep.

    Args:
        golden_repo_manager: Object with ``list_golden_repos()`` and
            ``get_actual_repo_path(alias)``.
        activated_repo_manager: Object with
            ``list_all_activated_repositories()`` and
            ``get_activated_repo_path(username, user_alias)``.

    Returns:
        Iterator of SweepCandidate (NOT pre-sorted -- callers that need
        stable-key order, e.g. the scheduler, must sort by ``sort_key``
        themselves).
    """
    yield from _golden_candidates(golden_repo_manager)
    yield from _activated_candidates(activated_repo_manager)
