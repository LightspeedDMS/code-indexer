"""Epic #1454 / Story #1461 salvage item 4 (MCP path).

Same defect as the REST path (test_semantic_query_manager_golden_temporal_
config_1461.py) but on the async-hybrid MCP worker: run_temporal_worker
calls reconstruct_temporal_backend(worker_input.repo_path=clone) with no
golden-repo-aware config correction at all, so an activated repo's stale
CoW-clone config.json silently selects the wrong temporal embedder after
the golden repo switches embedders.

Fix: _resolve_golden_repo_alias() (mirrors _search_single_repository's own
is_global/activated-repo distinction) + reusing
semantic_query_manager.load_golden_temporal_config() to swap config.temporal
before dispatch, exactly like the REST path.

Real infra: real Config/ConfigManager/GoldenRepoManager/ActivatedRepoManager,
real on-disk metadata + config.json files, real execute_temporal_query_with_
fusion dispatch. Only TemporalSearchService.query_temporal and the
coalesced_query_embedding reuse seam (genuine external-service boundaries)
are faked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.server.services.temporal_snapshot_store import (
    read_temporal_snapshot,
)
from code_indexer.server.services.temporal_worker import (
    _resolve_golden_repo_alias,
    run_temporal_worker,
)
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResults,
)
from code_indexer.services.temporal.temporal_worker_input import TemporalWorkerInput


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
    """Register both in-memory AND persist to the shared SQLite backend --
    run_temporal_worker constructs its OWN fresh ActivatedRepoManager()
    internally (a SEPARATE GoldenRepoManager instance), so only the
    persisted row is visible to it."""
    golden_repo_manager = activated_repo_manager.golden_repo_manager
    golden_repo = GoldenRepo(
        alias=alias,
        repo_url=f"local://{golden_dir}",
        default_branch="master",
        clone_path=str(golden_dir),
        created_at="2025-01-01T00:00:00Z",
        enable_temporal=True,
        temporal_options=None,
    )
    golden_repo_manager.golden_repos[alias] = golden_repo
    golden_repo_manager._sqlite_backend.add_repo(
        alias=golden_repo.alias,
        repo_url=golden_repo.repo_url,
        default_branch=golden_repo.default_branch,
        clone_path=golden_repo.clone_path,
        created_at=golden_repo.created_at,
        enable_temporal=golden_repo.enable_temporal,
        temporal_options=golden_repo.temporal_options,
    )


def _activate_repo(
    activated_repo_manager: ActivatedRepoManager,
    username: str,
    user_alias: str,
    golden_repo_alias: str,
    repo_path: Path,
) -> None:
    """Write real activated-repo metadata + a real clone dir on disk, the
    SAME on-disk shape ActivatedRepoManager.get_repository() reads."""
    repo_path.mkdir(parents=True, exist_ok=True)
    activated_repo_manager._save_metadata_file(
        username,
        user_alias,
        {
            "user_alias": user_alias,
            "golden_repo_alias": golden_repo_alias,
            "current_branch": "master",
            "activated_at": "2025-01-01T00:00:00Z",
            "last_accessed": "2025-01-01T00:00:00Z",
        },
    )


class TestResolveGoldenRepoAlias:
    def test_is_global_alias_returns_itself(self, tmp_path):
        activated_repo_manager = ActivatedRepoManager(data_dir=str(tmp_path))

        result = _resolve_golden_repo_alias(
            "alice", "my-repo-global", activated_repo_manager
        )

        assert result == "my-repo-global"

    def test_activated_repo_looks_up_golden_alias(self, tmp_path):
        activated_repo_manager = ActivatedRepoManager(data_dir=str(tmp_path))
        repo_path = Path(
            activated_repo_manager.get_activated_repo_path("alice", "my-clone")
        )
        _activate_repo(
            activated_repo_manager, "alice", "my-clone", "my-repo", repo_path
        )

        result = _resolve_golden_repo_alias("alice", "my-clone", activated_repo_manager)

        assert result == "my-repo"

    def test_repo_not_found_returns_none(self, tmp_path):
        activated_repo_manager = ActivatedRepoManager(data_dir=str(tmp_path))

        result = _resolve_golden_repo_alias(
            "alice", "nonexistent-clone", activated_repo_manager
        )

        assert result is None


@pytest.fixture
def payload_cache(tmp_path):
    db_path = tmp_path / "payload_cache.db"
    cache = PayloadCache(db_path=db_path, config=PayloadCacheConfig())
    cache.initialize()
    yield cache
    cache.close()


@pytest.fixture
def _capture_queried_collections(monkeypatch):
    """Real dispatch, never mocked. Fakes only the two genuine external-
    service boundaries: the up-front query-embedding reuse seam and the
    per-shard TemporalSearchService.query_temporal (real embedding + HNSW
    read) -- matching test_temporal_fusion_dispatch.py's own convention."""
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


def _make_worker_input(repo_path: Path, **overrides) -> TemporalWorkerInput:
    base = dict(
        repo_path=str(repo_path),
        repository_alias="my-clone",
        username="alice",
        query_text="auth logic",
        requested_limit=10,
        fusion_fetch_limit=30,
        time_range=("0001-01-01", "9999-12-31"),
        time_range_raw=None,
        time_range_all=True,
        file_path_filter=None,
        provider_filter=None,
        at_commit=None,
        language=None,
        exclude_language=None,
        exclude_path=None,
        diff_types=None,
        author=None,
        chunk_type=None,
        no_embedding_cache_shortcut=False,
        temporal_embedder=None,
        rerank_query=None,
        rerank_instruction=None,
        min_score_ignored_for_temporal=None,
        file_extensions_ignored_for_temporal=None,
    )
    base.update(overrides)
    return TemporalWorkerInput(**base)


class TestRunTemporalWorkerUsesGoldenConfig:
    def test_selects_golden_embedder_not_stale_clone_embedder(
        self, tmp_path, payload_cache, _capture_queried_collections, monkeypatch
    ):
        # run_temporal_worker constructs a bare ActivatedRepoManager()
        # internally (the SAME no-arg-construction convention this
        # codebase's own MCP handlers already use, e.g. files.py), which
        # resolves its default data_dir from Path.home(). Patch that so
        # the worker's internally-constructed manager and this test's
        # setup manager share the SAME on-disk data_dir.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        data_dir = tmp_path / ".cidx-server" / "data"
        golden_dir = data_dir / "golden-repos" / "my-repo"

        activated_repo_manager = ActivatedRepoManager()
        # Clone dir MUST match ActivatedRepoManager's own on-disk
        # convention -- get_repository()'s existence check looks for the
        # repo directory at exactly this path.
        clone_dir = Path(
            activated_repo_manager.get_activated_repo_path("alice", "my-clone")
        )

        _write_config(clone_dir, "voyage-code-3")  # stale clone: embedder A
        _write_config(golden_dir, "voyage-large-2")  # golden NOW: embedder B
        (
            clone_dir
            / ".code-indexer"
            / "index"
            / "code-indexer-temporal-voyage_large_2-2024Q1"
        ).mkdir(parents=True)

        _register_golden_repo(activated_repo_manager, "my-repo", golden_dir)
        _activate_repo(
            activated_repo_manager, "alice", "my-clone", "my-repo", clone_dir
        )

        worker_input = _make_worker_input(clone_dir)

        run_temporal_worker(worker_input, payload_cache, job_id="job-1461")

        assert len(_capture_queried_collections) == 1
        assert "voyage_large_2" in _capture_queried_collections[0]
        assert "voyage_code_3" not in _capture_queried_collections[0]

        snapshot = read_temporal_snapshot(payload_cache, "job-1461")
        assert snapshot["terminal"] is True
