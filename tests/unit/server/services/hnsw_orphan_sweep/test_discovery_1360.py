"""Tests for HNSW fleet sweep discovery (Story #1360, Epic #1333 S3).

Component 1: iter_index_files_for_repo -- pure filesystem walk, no DB/cluster
dependency. Component 2: enumerate_sweep_candidates -- composes the existing
golden_repo_manager / activated_repo_manager enumeration primitives.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from code_indexer.server.services.hnsw_orphan_sweep.discovery import (
    SweepCandidate,
    enumerate_sweep_candidates,
    iter_index_files_for_repo,
)


def _make_collection(base: Path, *segments: str) -> Path:
    """Create a collection directory under base/segments with the two
    structural files (hnsw_index.bin + collection_meta.json) that define
    "a real HNSW collection" per the story's discovery mechanism."""
    coll = base.joinpath(*segments)
    coll.mkdir(parents=True, exist_ok=True)
    (coll / "hnsw_index.bin").write_bytes(b"fake-index-bytes")
    (coll / "collection_meta.json").write_text(json.dumps({"vector_dim": 1024}))
    return coll


class TestIterIndexFilesForRepo:
    def test_yields_single_regular_collection(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        _make_collection(repo_root, ".code-indexer", "index", "voyage-code-3")

        results = list(iter_index_files_for_repo(repo_root))

        assert results == [Path(".code-indexer/index/voyage-code-3/hnsw_index.bin")]

    def test_yields_nested_temporal_shard_same_walk(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        _make_collection(repo_root, ".code-indexer", "index", "voyage-code-3")
        _make_collection(
            repo_root,
            ".code-indexer",
            "index",
            "temporal",
            "voyage-context-4",
            "2026Q1",
        )

        results = {str(p) for p in iter_index_files_for_repo(repo_root)}

        assert ".code-indexer/index/voyage-code-3/hnsw_index.bin" in results
        assert (
            ".code-indexer/index/temporal/voyage-context-4/2026Q1/hnsw_index.bin"
            in results
        )

    def test_skips_bin_without_sibling_collection_meta(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        stray = repo_root / ".code-indexer" / "index" / "stray"
        stray.mkdir(parents=True)
        (stray / "hnsw_index.bin").write_bytes(b"x")
        # No collection_meta.json sibling -- not a real collection.

        assert list(iter_index_files_for_repo(repo_root)) == []

    def test_skips_versioned_snapshot_paths(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        _make_collection(repo_root, ".code-indexer", "index", "voyage-code-3")
        _make_collection(
            repo_root,
            ".code-indexer",
            "index",
            ".versioned",
            "voyage-code-3",
            "v_1720000000",
        )

        results = {str(p) for p in iter_index_files_for_repo(repo_root)}

        assert ".code-indexer/index/voyage-code-3/hnsw_index.bin" in results
        assert not any(".versioned" in r for r in results)

    def test_returns_relative_paths_not_absolute(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        _make_collection(repo_root, ".code-indexer", "index", "voyage-code-3")

        results = list(iter_index_files_for_repo(repo_root))

        assert len(results) == 1
        assert not results[0].is_absolute()

    def test_missing_index_root_yields_nothing(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        # No .code-indexer/index at all.

        assert list(iter_index_files_for_repo(repo_root)) == []

    def test_missing_repo_root_yields_nothing_no_raise(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "does-not-exist"

        assert list(iter_index_files_for_repo(repo_root)) == []


class TestEnumerateSweepCandidates:
    def test_enumerates_golden_repo_collections_with_stable_key(
        self, tmp_path: Path
    ) -> None:
        golden_root = tmp_path / "golden" / "myrepo"
        _make_collection(golden_root, ".code-indexer", "index", "voyage-code-3")

        golden_mgr = MagicMock()
        golden_mgr.list_golden_repos.return_value = [{"alias": "myrepo"}]
        golden_mgr.get_actual_repo_path.return_value = str(golden_root)

        activated_mgr = MagicMock()
        activated_mgr.list_all_activated_repositories.return_value = []

        candidates = list(enumerate_sweep_candidates(golden_mgr, activated_mgr))

        assert len(candidates) == 1
        c = candidates[0]
        assert isinstance(c, SweepCandidate)
        assert (
            c.sort_key
            == "golden:myrepo:.code-indexer/index/voyage-code-3/hnsw_index.bin"
        )
        assert c.kind == "golden"

    def test_enumerates_activated_repo_independently_no_dedup(
        self, tmp_path: Path
    ) -> None:
        golden_root = tmp_path / "golden" / "myrepo"
        _make_collection(golden_root, ".code-indexer", "index", "voyage-code-3")
        activated_root = tmp_path / "activated" / "alice" / "myrepo"
        _make_collection(activated_root, ".code-indexer", "index", "voyage-code-3")

        golden_mgr = MagicMock()
        golden_mgr.list_golden_repos.return_value = [{"alias": "myrepo"}]
        golden_mgr.get_actual_repo_path.return_value = str(golden_root)

        activated_mgr = MagicMock()
        activated_mgr.list_all_activated_repositories.return_value = [
            {"username": "alice", "user_alias": "myrepo"}
        ]
        activated_mgr.get_activated_repo_path.return_value = str(activated_root)

        candidates = list(enumerate_sweep_candidates(golden_mgr, activated_mgr))

        assert len(candidates) == 2
        keys = {c.sort_key for c in candidates}
        assert "golden:myrepo:.code-indexer/index/voyage-code-3/hnsw_index.bin" in keys
        assert (
            "activated:alice/myrepo:.code-indexer/index/voyage-code-3/hnsw_index.bin"
            in keys
        )

    def test_tolerates_dangling_golden_registration(self, tmp_path: Path) -> None:
        golden_mgr = MagicMock()
        golden_mgr.list_golden_repos.return_value = [{"alias": "ghost"}]
        golden_mgr.get_actual_repo_path.side_effect = FileNotFoundError("no such repo")

        activated_mgr = MagicMock()
        activated_mgr.list_all_activated_repositories.return_value = []

        # Must not raise.
        candidates = list(enumerate_sweep_candidates(golden_mgr, activated_mgr))
        assert candidates == []

    def test_tolerates_missing_activated_repo_path(self, tmp_path: Path) -> None:
        golden_mgr = MagicMock()
        golden_mgr.list_golden_repos.return_value = []

        activated_mgr = MagicMock()
        activated_mgr.list_all_activated_repositories.return_value = [
            {"username": "bob", "user_alias": "vanished"}
        ]
        activated_mgr.get_activated_repo_path.return_value = str(
            tmp_path / "does-not-exist"
        )

        candidates = list(enumerate_sweep_candidates(golden_mgr, activated_mgr))
        assert candidates == []

    def test_sort_key_produces_stable_lexicographic_order(self, tmp_path: Path) -> None:
        root_b = tmp_path / "golden" / "bravo"
        root_a = tmp_path / "golden" / "alpha"
        _make_collection(root_b, ".code-indexer", "index", "voyage-code-3")
        _make_collection(root_a, ".code-indexer", "index", "voyage-code-3")

        golden_mgr = MagicMock()
        golden_mgr.list_golden_repos.return_value = [
            {"alias": "bravo"},
            {"alias": "alpha"},
        ]
        golden_mgr.get_actual_repo_path.side_effect = lambda alias: str(
            tmp_path / "golden" / alias
        )

        activated_mgr = MagicMock()
        activated_mgr.list_all_activated_repositories.return_value = []

        candidates = sorted(
            enumerate_sweep_candidates(golden_mgr, activated_mgr),
            key=lambda c: c.sort_key,
        )
        assert candidates[0].sort_key.startswith("golden:alpha:")
        assert candidates[1].sort_key.startswith("golden:bravo:")
