"""Tests for sister-location temporal shard discovery (Story #1457 gap fix).

Story #1457 introduced a SISTER location for temporal shards:
``{golden_repos_dir}/.versioned/{repo_alias}-temporal-{embedder_slug}[-{quarter}]/v_<ts>/``
-- ONE LEVEL ABOVE the golden repo's own clone directory, reached only via
an alias pointer file under ``{golden_repos_dir}/aliases/``. The pre-existing
``enumerate_sweep_candidates`` walks ONLY ``repo_root/.code-indexer/index/``
so it never sees a published sister shard.

``enumerate_sister_temporal_candidates`` is the additive fix: it discovers
published sister temporal shards via a real ``TemporalShardResolver``, real
``AliasManager``, and real ``ChunkStore``/``HNSWIndexManager`` build
machinery -- no mocking of any of that infrastructure.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_consolidated_build import (
    build_fresh_consolidated_temporal_version,
)
from code_indexer.services.temporal.temporal_shard_publisher import (
    publish_temporal_shard_version,
)
from code_indexer.server.services.hnsw_orphan_sweep.discovery import (
    enumerate_sweep_candidates,
)

VECTOR_DIM = 16


def _make_records(n: int, dim: int, seed: int) -> List[Dict[str, Any]]:
    rng = np.random.RandomState(seed)
    records = []
    for i in range(n):
        vector = rng.randn(dim).astype(np.float32).tolist()
        records.append(
            {
                "id": f"proj:commit:{'a' * 40}{i}:0",
                "vector": vector,
                "payload": {"path": f"file_{i}.py", "chunk_text": f"chunk {i}"},
            }
        )
    return records


def _publish_sister_shard(
    golden_repos_dir: Path,
    repo_alias: str,
    embedder_slug: str,
    quarter: Optional[str],
    records: List[Dict[str, Any]],
    vector_dim: int = VECTOR_DIM,
) -> Path:
    aliases_dir = golden_repos_dir / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    suffix = f"-{quarter}" if quarter else ""
    pointer_namespace = f"{repo_alias}-temporal-{embedder_slug}{suffix}"
    version_path: Path = build_fresh_consolidated_temporal_version(
        golden_repos_dir,
        pointer_namespace,
        [records],
        vector_dim,
        embedder_slug=embedder_slug,
    )
    publish_temporal_shard_version(alias_manager, pointer_namespace, version_path)
    return version_path


class _FakeGoldenRepoManager:
    def __init__(self, repos: Dict[str, Path]):
        self._repos = repos

    def list_golden_repos(self) -> List[Dict[str, str]]:
        return [{"alias": alias} for alias in self._repos]

    def get_actual_repo_path(self, alias: str) -> str:
        return str(self._repos[alias])


class _EmptyActivatedRepoManager:
    def list_all_activated_repositories(self) -> List[Dict[str, Any]]:
        return []


class TestEnumerateSisterTemporalCandidates:
    def test_yields_published_sister_shard(self, tmp_path: Path) -> None:
        golden_repos_dir = tmp_path / "golden-repos"
        repo_root = golden_repos_dir / "myrepo"
        repo_root.mkdir(parents=True)

        records = _make_records(5, VECTOR_DIM, seed=1)
        version_path = _publish_sister_shard(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", records
        )

        golden_mgr = _FakeGoldenRepoManager({"myrepo": repo_root})

        # Import here so the RED phase fails with a clean AttributeError/
        # ImportError against the not-yet-implemented function.
        from code_indexer.server.services.hnsw_orphan_sweep.discovery import (
            SisterTemporalCandidate,
            enumerate_sister_temporal_candidates,
        )

        candidates = list(enumerate_sister_temporal_candidates(golden_mgr))

        assert len(candidates) == 1
        c = candidates[0]
        assert isinstance(c, SisterTemporalCandidate)
        assert c.kind == "sister_temporal"
        assert c.repo_alias == "myrepo"
        assert c.embedder_slug == "voyage_code_3"
        assert c.quarter == "2026Q1"
        assert c.version_path == version_path
        assert c.sort_key == "sister_temporal:myrepo:voyage_code_3:2026Q1"

    def test_original_sweep_does_not_see_sister_shard(self, tmp_path: Path) -> None:
        """Documentation/regression-guard: proves the two enumeration
        functions are genuinely complementary, not overlapping. The
        pre-existing enumerate_sweep_candidates walks ONLY
        repo_root/.code-indexer/index/ and must yield NOTHING for a repo
        whose only data is a published sister shard."""
        golden_repos_dir = tmp_path / "golden-repos"
        repo_root = golden_repos_dir / "myrepo"
        repo_root.mkdir(parents=True)

        records = _make_records(5, VECTOR_DIM, seed=2)
        _publish_sister_shard(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", records
        )

        golden_mgr = _FakeGoldenRepoManager({"myrepo": repo_root})
        activated_mgr = _EmptyActivatedRepoManager()

        candidates = list(enumerate_sweep_candidates(golden_mgr, activated_mgr))

        assert candidates == []

    def test_golden_repo_manager_none_yields_nothing_no_raise(self) -> None:
        from code_indexer.server.services.hnsw_orphan_sweep.discovery import (
            enumerate_sister_temporal_candidates,
        )

        assert list(enumerate_sister_temporal_candidates(None)) == []

    def test_no_temporal_data_yields_nothing(self, tmp_path: Path) -> None:
        golden_repos_dir = tmp_path / "golden-repos"
        repo_root = golden_repos_dir / "myrepo"
        repo_root.mkdir(parents=True)

        golden_mgr = _FakeGoldenRepoManager({"myrepo": repo_root})

        from code_indexer.server.services.hnsw_orphan_sweep.discovery import (
            enumerate_sister_temporal_candidates,
        )

        assert list(enumerate_sister_temporal_candidates(golden_mgr)) == []

    def test_tolerates_dangling_golden_registration(self, tmp_path: Path) -> None:
        class _DanglingGoldenRepoManager:
            def list_golden_repos(self):
                return [{"alias": "ghost"}]

            def get_actual_repo_path(self, alias: str) -> str:
                raise FileNotFoundError("no such repo")

        from code_indexer.server.services.hnsw_orphan_sweep.discovery import (
            enumerate_sister_temporal_candidates,
        )

        assert (
            list(enumerate_sister_temporal_candidates(_DanglingGoldenRepoManager()))
            == []
        )

    def test_not_yet_queryable_shard_is_excluded(self, tmp_path: Path) -> None:
        """A sister pointer published to a version dir with committed rows
        but NO hnsw_index.bin yet (crash-window) must not be yielded --
        this sweep repairs existing-but-broken indexes, it does not
        backfill missing ones."""
        golden_repos_dir = tmp_path / "golden-repos"
        repo_root = golden_repos_dir / "myrepo"
        repo_root.mkdir(parents=True)

        records = _make_records(5, VECTOR_DIM, seed=3)
        version_path = _publish_sister_shard(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", records
        )
        # Simulate a crash-window: hnsw_index.bin missing at the pointer's
        # target (queryability is verified per-read by the resolver).
        (version_path / "hnsw_index.bin").unlink()

        golden_mgr = _FakeGoldenRepoManager({"myrepo": repo_root})

        from code_indexer.server.services.hnsw_orphan_sweep.discovery import (
            enumerate_sister_temporal_candidates,
        )

        assert list(enumerate_sister_temporal_candidates(golden_mgr)) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
