"""Temporal status inspection across both physical roots.

Supersedes the resolver-based Issue #1459 suite: Bug #1529 replaced Story
#1457's alias-pointer resolver with ONE fixed, deterministic root per golden
repo. The behavioral contract pinned here is unchanged in spirit:

  - status is repo-wide, across BOTH the fixed server-owned root and the
    golden clone's own in-repo index;
  - ``has_data`` (real committed rows) and ``is_queryable`` (a working
    ``hnsw_index.bin``) are DELIBERATELY DISTINCT -- a crash-window shard with
    rows but no HNSW counts toward the former and never the latter;
  - ``get_temporal_repo_max_commits`` unions completed commits across EVERY
    shard and fails OPEN to None rather than undercounting (an undercount
    would truncate historical coverage, worse than omitting the bound).

Real files, real chunk stores, real HNSW builds -- nothing mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)
from code_indexer.services.temporal.temporal_status import (
    TemporalDataLocation,
    get_temporal_repo_max_commits,
    get_temporal_repo_status,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

REPO_ALIAS = "evolution"
EMBEDDER = "voyage_code_3"
VECTOR_SIZE = 8


def _shard_name(quarter: str) -> str:
    return f"code-indexer-temporal-{EMBEDDER}-{quarter}"


def _rows(commit: str) -> List[Dict[str, Any]]:
    rng = np.random.default_rng(abs(hash(commit)) % (2**31))
    return [
        {
            "id": f"proj:commit:{commit}:0",
            "vector": rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist(),
            "payload": {"path": "src/a.py", "commit_hash": commit},
            "chunk_text": "x",
        }
    ]


def _build_shard(index_root: Path, quarter: str, commit: str) -> Path:
    store = FilesystemVectorStore(
        base_path=index_root, use_chunks_db_for_new_collections=True
    )
    name = _shard_name(quarter)
    store.create_collection(name, vector_size=VECTOR_SIZE)
    store.begin_indexing(name)
    store.upsert_points(name, _rows(commit))
    store.end_indexing(name)
    return index_root / name


def _write_progress(shard_dir: Path, commits: List[str]) -> None:
    (shard_dir / "temporal_progress.json").write_text(
        json.dumps({"completed_commits": commits})
    )


def _paths(tmp_path: Path):
    golden_repos_dir = tmp_path / "golden-repos"
    legacy_index = golden_repos_dir / REPO_ALIAS / ".code-indexer" / "index"
    fixed_root = server_temporal_index_root(golden_repos_dir, REPO_ALIAS)
    return golden_repos_dir, legacy_index, fixed_root


def test_no_temporal_data_anywhere(tmp_path: Path) -> None:
    golden_repos_dir, legacy_index, _ = _paths(tmp_path)
    status = get_temporal_repo_status(golden_repos_dir, REPO_ALIAS, legacy_index)
    assert status.has_data is False
    assert status.is_queryable is False
    assert status.resolved_path is None
    assert status.resolved_source is None


def test_data_in_the_fixed_server_root_is_found(tmp_path: Path) -> None:
    golden_repos_dir, legacy_index, fixed_root = _paths(tmp_path)
    shard = _build_shard(fixed_root, "2024Q1", "aaaaaaaa")

    status = get_temporal_repo_status(golden_repos_dir, REPO_ALIAS, legacy_index)

    assert status.has_data is True
    assert status.is_queryable is True
    assert status.resolved_path == shard
    assert status.resolved_source is TemporalDataLocation.FIXED_SERVER_ROOT


def test_data_only_in_repo_is_found(tmp_path: Path) -> None:
    """Standalone-CLI / pre-#1529 location must still report correctly."""
    golden_repos_dir, legacy_index, _ = _paths(tmp_path)
    shard = _build_shard(legacy_index, "2024Q1", "bbbbbbbb")

    status = get_temporal_repo_status(golden_repos_dir, REPO_ALIAS, legacy_index)

    assert status.has_data is True
    assert status.resolved_path == shard
    assert status.resolved_source is TemporalDataLocation.IN_REPO


def test_fixed_root_wins_over_in_repo_for_the_same_namespace(tmp_path: Path) -> None:
    """The fixed root is where the current write path targets, so an older
    in-repo copy of the SAME (embedder, quarter) is stale by construction."""
    golden_repos_dir, legacy_index, fixed_root = _paths(tmp_path)
    _build_shard(legacy_index, "2024Q1", "cccccccc")
    fixed_shard = _build_shard(fixed_root, "2024Q1", "dddddddd")

    status = get_temporal_repo_status(golden_repos_dir, REPO_ALIAS, legacy_index)

    assert status.resolved_path == fixed_shard
    assert status.resolved_source is TemporalDataLocation.FIXED_SERVER_ROOT


def test_rows_without_hnsw_are_data_but_not_queryable(tmp_path: Path) -> None:
    """The row-existence-is-not-queryability distinction (crash window)."""
    golden_repos_dir, legacy_index, fixed_root = _paths(tmp_path)
    shard = _build_shard(fixed_root, "2024Q1", "eeeeeeee")
    (shard / "hnsw_index.bin").unlink()

    status = get_temporal_repo_status(golden_repos_dir, REPO_ALIAS, legacy_index)

    assert status.has_data is True
    assert status.is_queryable is False
    assert status.resolved_path == shard


def test_a_queryable_shard_is_preferred_over_a_broken_one(tmp_path: Path) -> None:
    golden_repos_dir, legacy_index, fixed_root = _paths(tmp_path)
    broken = _build_shard(fixed_root, "2024Q1", "ffffffff")
    (broken / "hnsw_index.bin").unlink()
    good = _build_shard(fixed_root, "2024Q2", "99999999")

    status = get_temporal_repo_status(golden_repos_dir, REPO_ALIAS, legacy_index)

    assert status.is_queryable is True
    assert status.resolved_path == good


def test_empty_collection_is_not_reported_as_data(tmp_path: Path) -> None:
    golden_repos_dir, legacy_index, fixed_root = _paths(tmp_path)
    store = FilesystemVectorStore(
        base_path=fixed_root, use_chunks_db_for_new_collections=True
    )
    store.create_collection(_shard_name("2024Q1"), vector_size=VECTOR_SIZE)

    status = get_temporal_repo_status(golden_repos_dir, REPO_ALIAS, legacy_index)
    assert status.has_data is False


def test_global_suffixed_alias_resolves_the_same_root(tmp_path: Path) -> None:
    golden_repos_dir, legacy_index, fixed_root = _paths(tmp_path)
    shard = _build_shard(fixed_root, "2024Q1", "aaaaaaaa")

    status = get_temporal_repo_status(
        golden_repos_dir, f"{REPO_ALIAS}-global", legacy_index
    )
    assert status.resolved_path == shard


# ---------------------------------------------------------------------------
# get_temporal_repo_max_commits
# ---------------------------------------------------------------------------


def test_max_commits_unions_across_quarters(tmp_path: Path) -> None:
    golden_repos_dir, legacy_index, fixed_root = _paths(tmp_path)
    q1 = _build_shard(fixed_root, "2024Q1", "aaaaaaaa")
    q2 = _build_shard(fixed_root, "2024Q2", "bbbbbbbb")
    _write_progress(q1, ["c1", "c2"])
    _write_progress(q2, ["c2", "c3"])  # c2 overlaps -- counted once

    assert (
        get_temporal_repo_max_commits(golden_repos_dir, REPO_ALIAS, legacy_index) == 3
    )


def test_max_commits_is_none_when_no_data(tmp_path: Path) -> None:
    golden_repos_dir, legacy_index, _ = _paths(tmp_path)
    assert (
        get_temporal_repo_max_commits(golden_repos_dir, REPO_ALIAS, legacy_index)
        is None
    )


def test_max_commits_fails_open_when_progress_unreadable(tmp_path: Path) -> None:
    """Undercounting would truncate history -- worse than omitting the bound."""
    golden_repos_dir, legacy_index, fixed_root = _paths(tmp_path)
    q1 = _build_shard(fixed_root, "2024Q1", "aaaaaaaa")
    q2 = _build_shard(fixed_root, "2024Q2", "bbbbbbbb")
    _write_progress(q1, ["c1", "c2"])
    (q2 / "temporal_progress.json").write_text("{not json")

    assert (
        get_temporal_repo_max_commits(golden_repos_dir, REPO_ALIAS, legacy_index)
        is None
    )


def test_max_commits_is_none_for_empty_aggregate(tmp_path: Path) -> None:
    """--max-commits 0 would cap the run to zero new commits."""
    golden_repos_dir, legacy_index, fixed_root = _paths(tmp_path)
    q1 = _build_shard(fixed_root, "2024Q1", "aaaaaaaa")
    _write_progress(q1, [])

    assert (
        get_temporal_repo_max_commits(golden_repos_dir, REPO_ALIAS, legacy_index)
        is None
    )
