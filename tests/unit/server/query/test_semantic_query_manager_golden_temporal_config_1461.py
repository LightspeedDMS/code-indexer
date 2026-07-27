"""Epic #1454 / Story #1461 salvage item 4 (REST path).

For an activated (non-global) repo, _execute_temporal_query resolves the
CoW clone's config via reconstruct_temporal_backend(repo_path=clone) ->
ConfigManager.create_with_backtrack(clone). The clone's config.json is a
CoW-snapshot taken at activation time and is NEVER live-synced with the
golden repo it was cloned from. If the golden repo's active_embedder (and
its `embedders` registry) changes after activation, the clone's stale
config would silently select the WRONG embedder -- both for shard
discovery (config.temporal.active_embedder) and for the query-embedding
provider construction that matches the resolved collection back against
config.temporal.embedders (temporal_fusion_dispatch._create_embedding_
provider_for_collection).

Fix: when a golden_repo_alias is known, resolve the GOLDEN repo's OWN,
CURRENT config via load_golden_temporal_config() and swap the ENTIRE
config.temporal sub-object (never just the active_embedder scalar --
swapping only the scalar while leaving a stale `embedders` list would
still silently mismatch-and-fallback inside
_create_embedding_provider_for_collection) before calling
execute_temporal_query_with_fusion.

Real infra throughout: real Config/ConfigManager/GoldenRepoManager/
ActivatedRepoManager, real config.json files on disk, real
execute_temporal_query_with_fusion dispatch (never mocked). Only the
innermost per-shard TemporalSearchService and the coalesced_query_
embedding reuse-seam (both genuine external-service/network boundaries)
are faked, matching this codebase's own established
test_temporal_fusion_dispatch.py convention.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_indexer.server.query.semantic_query_manager import (
    SemanticQueryManager,
    load_golden_temporal_config,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResults,
)


def _write_config(repo_dir: Path, active_embedder: str) -> None:
    (repo_dir / ".code-indexer").mkdir(parents=True, exist_ok=True)
    cfg = {
        "codebase_dir": str(repo_dir),
        "embedding_provider": "voyage-ai",
        "voyage_ai": {"model": active_embedder},
        "temporal": {
            "embedders": [active_embedder],
            "active_embedder": active_embedder,
        },
    }
    (repo_dir / ".code-indexer" / "config.json").write_text(json.dumps(cfg))


def _register_golden_repo(activated_repo_manager, alias: str, golden_dir: Path):
    golden_repo_manager = activated_repo_manager.golden_repo_manager
    golden_repo_manager.golden_repos[alias] = GoldenRepo(
        alias=alias,
        repo_url=f"local://{golden_dir}",
        default_branch="master",
        clone_path=str(golden_dir),
        created_at="2025-01-01T00:00:00Z",
        enable_temporal=True,
        temporal_options=None,
    )


class TestLoadGoldenTemporalConfigHelper:
    def test_resolves_golden_repos_own_current_config(self, tmp_path):
        data_dir = tmp_path / "server-data"
        golden_dir = data_dir / "golden-repos" / "my-repo"
        _write_config(golden_dir, "voyage-large-2")

        from code_indexer.server.repositories.activated_repo_manager import (
            ActivatedRepoManager,
        )

        activated_repo_manager = ActivatedRepoManager(data_dir=str(data_dir))
        _register_golden_repo(activated_repo_manager, "my-repo", golden_dir)

        config = load_golden_temporal_config("my-repo", activated_repo_manager)

        assert config is not None
        assert config.temporal.active_embedder == "voyage-large-2"
        assert config.temporal.embedders == ["voyage-large-2"]

    def test_returns_none_when_golden_repo_cannot_be_resolved(self, tmp_path):
        data_dir = tmp_path / "server-data"
        from code_indexer.server.repositories.activated_repo_manager import (
            ActivatedRepoManager,
        )

        activated_repo_manager = ActivatedRepoManager(data_dir=str(data_dir))

        config = load_golden_temporal_config("nonexistent-repo", activated_repo_manager)

        assert config is None


@pytest.fixture
def _capture_queried_collections(monkeypatch):
    """Real dispatch (execute_temporal_query_with_fusion), never mocked.
    Fakes only the two genuine external-service boundaries: the up-front
    query-embedding reuse seam (coalesced_query_embedding -> real network
    call) and the per-shard TemporalSearchService.query_temporal (real
    embedding + HNSW read) -- matching test_temporal_fusion_dispatch.py's
    own established convention. Returns the list of collection_name values
    TemporalSearchService was actually constructed+queried with, proving
    which embedder's shard was selected."""
    captured: list = []

    def _fake_query_temporal(self, **kwargs):
        captured.append(self.collection_name)
        return TemporalSearchResults(
            results=[], query="auth", filter_type="none", filter_value=None
        )

    monkeypatch.setattr(
        "code_indexer.services.temporal.temporal_fusion_dispatch.coalesced_query_embedding",
        None,
    )
    monkeypatch.setattr(
        "code_indexer.services.temporal.temporal_search_service."
        "TemporalSearchService.query_temporal",
        _fake_query_temporal,
    )
    return captured


class TestExecuteTemporalQueryEmbedderSelection:
    def test_uses_golden_config_not_stale_clone_config(
        self, tmp_path, _capture_queried_collections
    ):
        """The activated CLONE's own config.json says embedder A (stale);
        the GOLDEN repo's CURRENT config.json says embedder B. The real
        dispatch must select+construct embedder B, never A."""
        data_dir = tmp_path / "server-data"
        clone_dir = tmp_path / "activated-clone"
        golden_dir = data_dir / "golden-repos" / "my-repo"

        _write_config(clone_dir, "voyage-code-3")  # stale clone: embedder A
        _write_config(golden_dir, "voyage-large-2")  # golden NOW: embedder B

        # A real quarter shard directory for embedder B only -- proves
        # discovery picked B (an A-only clone would find nothing).
        (
            clone_dir
            / ".code-indexer"
            / "index"
            / "code-indexer-temporal-voyage_large_2-2024Q1"
        ).mkdir(parents=True)

        manager = SemanticQueryManager(data_dir=str(data_dir))
        _register_golden_repo(manager.activated_repo_manager, "my-repo", golden_dir)

        manager._execute_temporal_query(
            repo_path=clone_dir,
            repository_alias="my-repo-activated",
            query_text="auth logic",
            limit=10,
            min_score=None,
            time_range=None,
            time_range_all=True,
            golden_repo_alias="my-repo",
        )

        assert len(_capture_queried_collections) == 1
        assert "voyage_large_2" in _capture_queried_collections[0]
        assert "voyage_code_3" not in _capture_queried_collections[0]

    def test_without_golden_repo_alias_uses_clone_config_unchanged(
        self, tmp_path, _capture_queried_collections
    ):
        """Regression guard: golden_repo_alias=None (every pre-#1461
        caller: is_global-without-tracker, CLI, solo) must stay
        byte-identical -- the clone's OWN config still governs."""
        data_dir = tmp_path / "server-data"
        clone_dir = tmp_path / "activated-clone"
        _write_config(clone_dir, "voyage-code-3")
        (
            clone_dir
            / ".code-indexer"
            / "index"
            / "code-indexer-temporal-voyage_code_3-2024Q1"
        ).mkdir(parents=True)

        manager = SemanticQueryManager(data_dir=str(data_dir))

        manager._execute_temporal_query(
            repo_path=clone_dir,
            repository_alias="my-repo-activated",
            query_text="auth logic",
            limit=10,
            min_score=None,
            time_range=None,
            time_range_all=True,
            golden_repo_alias=None,
        )

        assert len(_capture_queried_collections) == 1
        assert "voyage_code_3" in _capture_queried_collections[0]
