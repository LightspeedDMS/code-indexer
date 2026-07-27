"""Tests for scheduler-level wiring of sister-location temporal candidates
into the HNSW orphan repair sweep (Story #1457 gap fix).

Real components: a real published sister temporal shard (via
build_fresh_consolidated_temporal_version + publish_temporal_shard_version)
and a real HNSWOrphanSweepStateSqliteBackend. The per-item sister processor
is injected (mirroring the existing process_fn injection pattern) so this
test verifies DISPATCH wiring, not the repair mechanics themselves (already
covered by test_repair_executor_sister_temporal_1457.py).

Regression coverage: the pre-existing golden AND activated dispatch path
(enumerate_sweep_candidates + the REAL default process_candidate) must keep
working unchanged, both with and without the new process_sister_fn param,
and in a tick that mixes both kinds of candidates together.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_consolidated_build import (
    build_fresh_consolidated_temporal_version,
)
from code_indexer.services.temporal.temporal_shard_publisher import (
    publish_temporal_shard_version,
)
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import (
    HNSWOrphanSweepStateSqliteBackend,
)
from code_indexer.server.services.hnsw_orphan_sweep.scheduler import (
    HNSWOrphanRepairSweepScheduler,
)
from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
    SweepOutcome,
)

VECTOR_DIM = 8


def _make_records(n: int, dim: int, seed: int) -> List[Dict[str, Any]]:
    rng = np.random.RandomState(seed)
    records = []
    for i in range(n):
        vector = rng.randn(dim).astype(np.float32).tolist()
        records.append({"id": f"proj:commit:{'a' * 40}{i}:0", "vector": vector})
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


def _make_clean_in_repo_collection(
    collection_path: Path, seed: int, n: int = 6
) -> None:
    """Build a real, self-healed (S2) clean in-repo HNSW collection via the
    production build path -- exercises the PRE-EXISTING golden/activated
    dispatch path (enumerate_sweep_candidates + process_candidate)."""
    collection_path.mkdir(parents=True, exist_ok=True)
    rng = np.random.RandomState(seed)
    vectors = rng.randn(n, VECTOR_DIM).astype(np.float32)
    ids = [f"vec_{i}" for i in range(n)]
    manager = HNSWIndexManager(vector_dim=VECTOR_DIM)
    manager.build_index(collection_path, vectors, ids)


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


class _FakeActivatedRepoManager:
    """Test double (not a Mock) -- real, controlled stand-in for the
    minimal activated_repo_manager surface enumerate_sweep_candidates()
    needs."""

    def __init__(self, repos: Dict[Tuple[str, str], Path]):
        self._repos = repos

    def list_all_activated_repositories(self) -> List[Dict[str, Any]]:
        return [
            {"username": username, "user_alias": user_alias}
            for (username, user_alias) in self._repos
        ]

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        return str(self._repos[(username, user_alias)])


class _RecordingConfigService:
    def __init__(self, *, batch_size: int = 10):
        self.batch_size = batch_size

    def get_config(self):
        cfg = self

        class _Wrapper:
            hnsw_orphan_repair_sweep_config = cfg

        return _Wrapper()


@pytest.fixture
def state_backend(tmp_path: Path):
    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    return HNSWOrphanSweepStateSqliteBackend(db_path)


class TestSchedulerDispatchesSisterTemporalCandidates:
    def test_run_tick_discovers_and_dispatches_sister_candidate(
        self, tmp_path: Path, state_backend
    ) -> None:
        golden_repos_dir = tmp_path / "golden-repos"
        repo_root = golden_repos_dir / "myrepo"
        repo_root.mkdir(parents=True)

        records = _make_records(4, VECTOR_DIM, seed=1)
        _publish_sister_shard(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", records
        )

        golden = _FakeGoldenRepoManager({"myrepo": repo_root})

        dispatched_sort_keys: List[str] = []

        def fake_sister_processor(candidate):
            dispatched_sort_keys.append(candidate.sort_key)
            return SweepOutcome.SISTER_TEMPORAL_REPAIRED

        scheduler = HNSWOrphanRepairSweepScheduler(
            golden_repo_manager=golden,
            activated_repo_manager=_EmptyActivatedRepoManager(),
            state_backend=state_backend,
            background_job_manager=None,
            config_service=_RecordingConfigService(batch_size=10),
            process_sister_fn=fake_sister_processor,
        )

        result = scheduler._run_tick()

        assert dispatched_sort_keys == ["sister_temporal:myrepo:voyage_code_3:2026Q1"]
        assert result["sister_temporal_repaired"] == 1
        assert result["processed"] == 1

    def test_mixed_golden_activated_and_sister_candidates_all_dispatched_correctly(
        self, tmp_path: Path, state_backend
    ) -> None:
        """A single tick containing an in-repo GOLDEN collection, an in-repo
        ACTIVATED collection, AND a sister temporal shard must dispatch
        each to the CORRECT processor -- proves the new sister branch is
        additive and does not disturb the pre-existing golden/activated
        dispatch path (real process_candidate/process_fn default, not
        injected for those two)."""
        golden_repos_dir = tmp_path / "golden-repos"
        repo_root = golden_repos_dir / "myrepo"
        repo_root.mkdir(parents=True)
        _make_clean_in_repo_collection(
            repo_root / ".code-indexer" / "index" / "voyage-code-3", seed=7
        )

        activated_root = tmp_path / "activated" / "alice" / "myrepo"
        _make_clean_in_repo_collection(
            activated_root / ".code-indexer" / "index" / "voyage-code-3", seed=8
        )

        records = _make_records(4, VECTOR_DIM, seed=2)
        _publish_sister_shard(
            golden_repos_dir, "myrepo", "voyage_code_3", "2026Q1", records
        )

        golden = _FakeGoldenRepoManager({"myrepo": repo_root})
        activated = _FakeActivatedRepoManager({("alice", "myrepo"): activated_root})

        sister_calls: List[str] = []

        def fake_sister_processor(candidate):
            sister_calls.append(candidate.sort_key)
            return SweepOutcome.SISTER_TEMPORAL_REPAIRED

        # process_fn is OMITTED -- the real, default process_candidate
        # handles both in-repo collections exactly as it always has.
        scheduler = HNSWOrphanRepairSweepScheduler(
            golden_repo_manager=golden,
            activated_repo_manager=activated,
            state_backend=state_backend,
            background_job_manager=None,
            config_service=_RecordingConfigService(batch_size=10),
            process_sister_fn=fake_sister_processor,
        )

        result = scheduler._run_tick()

        # Both real in-repo collections were built clean via the
        # production build path -- process_candidate (the untouched
        # default) reports them CLEAN. The sister candidate is dispatched
        # to the injected fake, distinctly counted.
        assert result["processed"] == 3
        assert result["clean"] == 2
        assert result["sister_temporal_repaired"] == 1
        assert sister_calls == ["sister_temporal:myrepo:voyage_code_3:2026Q1"]

    def test_existing_golden_and_activated_dispatch_unaffected_by_new_param(
        self, tmp_path: Path, state_backend
    ) -> None:
        """Regression: constructing the scheduler WITHOUT process_sister_fn
        (every pre-existing call site) must still work, AND the
        pre-existing golden AND activated dispatch paths must still process
        real in-repo collections through the real default process_candidate."""
        golden_repos_dir = tmp_path / "golden-repos"
        repo_root = golden_repos_dir / "myrepo"
        _make_clean_in_repo_collection(
            repo_root / ".code-indexer" / "index" / "voyage-code-3", seed=9
        )

        activated_root = tmp_path / "activated" / "bob" / "myrepo"
        _make_clean_in_repo_collection(
            activated_root / ".code-indexer" / "index" / "voyage-code-3", seed=10
        )

        golden = _FakeGoldenRepoManager({"myrepo": repo_root})
        activated = _FakeActivatedRepoManager({("bob", "myrepo"): activated_root})
        scheduler = HNSWOrphanRepairSweepScheduler(
            golden_repo_manager=golden,
            activated_repo_manager=activated,
            state_backend=state_backend,
            background_job_manager=None,
            config_service=_RecordingConfigService(batch_size=10),
        )

        result = scheduler._run_tick()

        assert result["processed"] == 2
        assert result["clean"] == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
