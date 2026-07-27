"""GitHub Issue #1482 (extension): MultiSearchService._search_temporal_sync
must construct a TemporalShardResolver and (a) attach it to the vector
store used for search AND (b) forward it into
execute_temporal_query_with_fusion -- otherwise the omni/multi-repo
temporal front door can only ever read the in-repo legacy location, which
Story #1457's AC1 relocation trigger empties once it succeeds.

This mirrors the CANONICAL TEMPLATE fix already applied to
run_temporal_worker (tests/unit/services/temporal/
test_run_temporal_worker_resolver_1482.py) and to
SemanticQueryManager._execute_temporal_query
(semantic_query_manager.py:2649-2691).

Real infra throughout: real AliasManager, real QueryTracker, a real
on-disk sister-location layout (alias pointer + versioned dir + a real
hnsw_index.bin marker file). Only execute_temporal_query_with_fusion
itself is faked (captured), matching the established boundary in the
worker-resolver test (real embedding/HNSW reads are a separate, already-
covered concern).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.multi.multi_search_config import MultiSearchConfig
from code_indexer.server.multi.multi_search_service import MultiSearchService
from code_indexer.server.multi.models import MultiSearchRequest
from code_indexer.services.temporal.temporal_search_service import (
    ALL_TIME_RANGE,
    TemporalSearchResult,
    TemporalSearchResults,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
)

REPO_ALIAS = "myrepo-global"
NORMALIZED_ALIAS = "myrepo"
POINTER_NAMESPACE = "myrepo-temporal-voyage_code_3-2024Q1"


@dataclass
class _SisterFixture:
    repo_dir: Path
    golden_repos_dir: Path
    version_dir: Path


def _write_clone_config(repo_dir: Path) -> None:
    (repo_dir / ".code-indexer").mkdir(parents=True, exist_ok=True)
    cfg = {
        "codebase_dir": str(repo_dir),
        "embedding_provider": "voyage-ai",
        "voyage_ai": {"model": "voyage-code-3"},
        "temporal": {
            "embedders": ["voyage-code-3"],
            "active_embedder": "voyage-code-3",
        },
    }
    (repo_dir / ".code-indexer" / "config.json").write_text(json.dumps(cfg))


def _build_sister_only_repo(tmp_path: Path) -> _SisterFixture:
    """Sets up a repo whose temporal shard data lives ONLY at the golden-
    owned sister location -- the in-repo legacy index dir is bare, exactly
    the production symptom Story #1457's relocation trigger produces."""
    golden_repos_dir = tmp_path / "golden-repos"

    repo_dir = tmp_path / "clone"
    _write_clone_config(repo_dir)
    (repo_dir / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

    version_dir = golden_repos_dir / ".versioned" / POINTER_NAMESPACE / "v_1785164318"
    version_dir.mkdir(parents=True)
    (version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
    alias_manager = AliasManager(str(golden_repos_dir / "aliases"))
    alias_manager.create_alias(POINTER_NAMESPACE, str(version_dir))

    return _SisterFixture(
        repo_dir=repo_dir, golden_repos_dir=golden_repos_dir, version_dir=version_dir
    )


def _make_request() -> MultiSearchRequest:
    return MultiSearchRequest(
        repositories=[REPO_ALIAS],
        query="auth logic",
        search_type="temporal",
        limit=10,
    )


def _make_sentinel_final_results() -> TemporalSearchResults:
    sentinel = TemporalSearchResult(
        file_path="a.py",
        chunk_index=0,
        content="content",
        score=0.9,
        metadata={"commit_hash": "sentinel"},
        temporal_context={"commit_hash": "sentinel", "commit_timestamp": 100},
    )
    return TemporalSearchResults(
        results=[sentinel],
        query="auth logic",
        filter_type="time_range",
        filter_value=ALL_TIME_RANGE,
        total_found=1,
        shards_total=1,
        shards_attempted=1,
        shards_succeeded=1,
    )


def _run_temporal_sync_capturing_fusion_call(
    service, repo_id, request, fixture, query_tracker, final_results
):
    captured_args: list = []
    captured_kwargs: dict = {}

    def _fake_fusion(*args, **kwargs):
        captured_args.extend(args)
        captured_kwargs.update(kwargs)
        return final_results

    mock_backend_registry = MagicMock()
    mock_backend_registry.global_repos.get_repo.return_value = {
        "index_path": str(fixture.repo_dir)
    }
    mock_app_state = MagicMock()
    mock_app_state.backend_registry = mock_backend_registry
    mock_app_state.query_tracker = query_tracker

    with (
        patch(
            "code_indexer.server.multi.multi_search_service._get_golden_repos_dir",
            return_value=str(fixture.golden_repos_dir),
        ),
        patch("code_indexer.server.app.app") as mock_fastapi_app,
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch."
            "execute_temporal_query_with_fusion",
            _fake_fusion,
        ),
    ):
        mock_fastapi_app.state = mock_app_state
        result = service._search_temporal_sync(repo_id, request)

    return result, captured_args, captured_kwargs


class TestMultiSearchServiceTemporalResolverWiring:
    """Bug #1482 extension: the omni/multi-repo temporal path must consult
    the golden-owned sister location, exactly like run_temporal_worker and
    SemanticQueryManager._execute_temporal_query already do."""

    def test_resolver_is_real_and_resolves_sister_data(self, tmp_path):
        fixture = _build_sister_only_repo(tmp_path)
        query_tracker = QueryTracker()
        service = MultiSearchService(MultiSearchConfig(max_workers=2))
        request = _make_request()

        _, _, captured_kwargs = _run_temporal_sync_capturing_fusion_call(
            service,
            REPO_ALIAS,
            request,
            fixture,
            query_tracker,
            _make_sentinel_final_results(),
        )

        resolver = captured_kwargs.get("resolver")
        assert isinstance(resolver, TemporalShardResolver), (
            "_search_temporal_sync must construct a REAL TemporalShardResolver "
            "and forward it via resolver= to execute_temporal_query_with_fusion "
            f"(Bug #1482 extension), got: {resolver!r}"
        )
        resolved = resolver.resolve("voyage_code_3", "2024Q1")
        assert resolved is not None
        assert resolved.path.resolve() == fixture.version_dir.resolve()

    def test_resolver_attached_to_vector_store_used_for_search(self, tmp_path):
        """'Disconnected reader' lesson: a resolver threaded only into
        fusion dispatch's own bookkeeping is not enough --
        _get_collection_path() on the store instance itself must see it
        too."""
        fixture = _build_sister_only_repo(tmp_path)
        query_tracker = QueryTracker()
        service = MultiSearchService(MultiSearchConfig(max_workers=2))
        request = _make_request()

        _, captured_args, captured_kwargs = _run_temporal_sync_capturing_fusion_call(
            service,
            REPO_ALIAS,
            request,
            fixture,
            query_tracker,
            _make_sentinel_final_results(),
        )

        # vector_store is the 3rd kwarg/positional to
        # execute_temporal_query_with_fusion(config=..., index_path=...,
        # vector_store=..., ...) -- the production call site uses kwargs.
        vector_store_arg = captured_kwargs.get("vector_store")
        if vector_store_arg is None and len(captured_args) >= 3:
            vector_store_arg = captured_args[2]
        resolver = captured_kwargs.get("resolver")
        assert vector_store_arg is not None
        assert getattr(vector_store_arg, "_temporal_shard_resolver", None) is (resolver)

    def test_no_query_tracker_means_no_resolver_byte_identical_to_today(self, tmp_path):
        """Without a query_tracker, resolver construction must stay a
        no-op -- pin() would be a silent no-op anyway, and constructing a
        resolver regardless would reintroduce the mid-read deletion
        hazard AC8 Step 6 guards against."""
        fixture = _build_sister_only_repo(tmp_path)
        service = MultiSearchService(MultiSearchConfig(max_workers=2))
        request = _make_request()

        _, _, captured_kwargs = _run_temporal_sync_capturing_fusion_call(
            service,
            REPO_ALIAS,
            request,
            fixture,
            None,
            TemporalSearchResults(
                results=[],
                query="q",
                filter_type="time_range",
                filter_value=None,
                shards_total=0,
                shards_attempted=0,
                shards_succeeded=0,
            ),
        )

        assert captured_kwargs.get("resolver") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
