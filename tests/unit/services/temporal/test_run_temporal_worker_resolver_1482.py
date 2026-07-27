"""GitHub Issue #1482: run_temporal_worker must construct a
TemporalShardResolver and (a) attach it to the vector_store used for
search AND (b) forward it into execute_temporal_query_with_fusion --
otherwise the LIVE MCP temporal front door (Story #1400's
temporal_live_dispatch -> run_temporal_worker path) can only ever read
the in-repo legacy location, which Story #1457's AC1 relocation trigger
empties once it succeeds (true on every local-disk/solo server, i.e.
production).

The resolver wiring was previously added ONLY to the retired
SemanticQueryManager._execute_temporal_query path (semantic_query_
manager.py:2649-2691) -- Story #1400 replaced that path with this live
worker, which never received the equivalent wiring. This test proves the
bug (RED: no resolver constructed/forwarded, sister-only data invisible)
and the fix (GREEN: resolver constructed, attached, forwarded, sister
data resolvable).

Real infra throughout: real AliasManager, real QueryTracker, a real
on-disk sister-location layout (alias pointer + versioned dir + a real
hnsw_index.bin marker file), real ActivatedRepoManager (no-arg
construction, matching test_run_temporal_worker_golden_config_1461.py's
established Path.home() patching convention) and real config.json/
ConfigManager-driven backend reconstruction. Only the actual per-shard
fusion dispatch (execute_temporal_query_with_fusion) is faked, matching
this project's established boundary for these tests (real embedding/HNSW
reads are a separate, already-covered concern -- see
test_temporal_fusion_dispatch_resolver_e2e_1457.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.services.temporal_snapshot_store import (
    read_temporal_snapshot,
)
from code_indexer.server.services.temporal_worker import run_temporal_worker
from code_indexer.services.temporal.temporal_search_service import (
    ALL_TIME_RANGE,
    TemporalSearchResult,
    TemporalSearchResults,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
)
from code_indexer.services.temporal.temporal_worker_input import TemporalWorkerInput

POINTER_NAMESPACE = "mock-test-temporal-voyage_code_3-2024Q1"


@pytest.fixture
def payload_cache(tmp_path):
    db_path = tmp_path / "payload_cache.db"
    cache = PayloadCache(db_path=db_path, config=PayloadCacheConfig())
    cache.initialize()
    yield cache
    cache.close()


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


def _make_worker_input(repo_path: Path, **overrides) -> TemporalWorkerInput:
    base = dict(
        repo_path=str(repo_path),
        repository_alias="mock-test-global",
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


@dataclass
class _SisterFixture:
    repo_dir: Path
    version_dir: Path


def _build_sister_only_repo(tmp_path: Path, monkeypatch) -> _SisterFixture:
    """Set up a repo whose temporal shard data lives ONLY at the golden-
    owned sister location -- the in-repo legacy index dir is bare, exactly
    the production symptom Story #1457's relocation trigger produces.

    Patches Path.home() (run_temporal_worker's internal, no-arg
    ActivatedRepoManager() construction resolves data_dir from it) so the
    sister location lands under tmp_path, matching
    test_run_temporal_worker_golden_config_1461.py's own convention.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    golden_repos_dir = tmp_path / ".cidx-server" / "data" / "golden-repos"

    repo_dir = tmp_path / "clone"
    _write_clone_config(repo_dir)
    (repo_dir / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

    version_dir = golden_repos_dir / ".versioned" / POINTER_NAMESPACE / "v_1785164318"
    version_dir.mkdir(parents=True)
    (version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
    alias_manager = AliasManager(str(golden_repos_dir / "aliases"))
    alias_manager.create_alias(POINTER_NAMESPACE, str(version_dir))

    return _SisterFixture(repo_dir=repo_dir, version_dir=version_dir)


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


def _run_worker_capturing_fusion_call(
    worker_input, payload_cache, job_id, monkeypatch, final_results, **worker_kwargs
):
    captured_args: list = []
    captured_kwargs: dict = {}

    def _fake_fusion(*args, **kwargs):
        captured_args.extend(args)
        captured_kwargs.update(kwargs)
        return final_results

    monkeypatch.setattr(
        "code_indexer.server.services.temporal_worker."
        "execute_temporal_query_with_fusion",
        _fake_fusion,
    )

    run_temporal_worker(worker_input, payload_cache, job_id=job_id, **worker_kwargs)
    return captured_args, captured_kwargs


class TestRunTemporalWorkerSisterLocationWiring:
    """Bug #1482: the live worker must consult the golden-owned sister
    location -- previously it only ever read the in-repo legacy location,
    which relocation (Story #1457 AC1) empties."""

    def test_resolver_is_real_and_resolves_sister_data(
        self, tmp_path, payload_cache, monkeypatch
    ):
        fixture = _build_sister_only_repo(tmp_path, monkeypatch)
        query_tracker = QueryTracker()
        worker_input = _make_worker_input(fixture.repo_dir)

        _, captured_kwargs = _run_worker_capturing_fusion_call(
            worker_input,
            payload_cache,
            "job-1482",
            monkeypatch,
            _make_sentinel_final_results(),
            query_tracker=query_tracker,
        )

        resolver = captured_kwargs.get("resolver")
        assert isinstance(resolver, TemporalShardResolver), (
            "run_temporal_worker must construct a REAL TemporalShardResolver "
            "and forward it via resolver= to "
            "execute_temporal_query_with_fusion (Bug #1482), got: "
            f"{resolver!r}"
        )
        resolved = resolver.resolve("voyage_code_3", "2024Q1")
        assert resolved is not None
        assert resolved.path.resolve() == fixture.version_dir.resolve()

    def test_resolver_attached_to_vector_store_used_for_search(
        self, tmp_path, payload_cache, monkeypatch
    ):
        """The 'disconnected reader' lesson: a resolver threaded only into
        fusion dispatch's own bookkeeping is not enough --
        _get_collection_path() on the store instance itself must see it
        too."""
        fixture = _build_sister_only_repo(tmp_path, monkeypatch)
        query_tracker = QueryTracker()
        worker_input = _make_worker_input(fixture.repo_dir)

        captured_args, captured_kwargs = _run_worker_capturing_fusion_call(
            worker_input,
            payload_cache,
            "job-1482b",
            monkeypatch,
            _make_sentinel_final_results(),
            query_tracker=query_tracker,
        )

        # vector_store is the 3rd positional arg to
        # execute_temporal_query_with_fusion(config, index_path,
        # vector_store, ...).
        assert len(captured_args) >= 3
        vector_store_arg = captured_args[2]
        resolver = captured_kwargs.get("resolver")
        assert getattr(vector_store_arg, "_temporal_shard_resolver", None) is (resolver)

    def test_final_snapshot_contains_sister_resolved_row(
        self, tmp_path, payload_cache, monkeypatch
    ):
        fixture = _build_sister_only_repo(tmp_path, monkeypatch)
        query_tracker = QueryTracker()
        worker_input = _make_worker_input(fixture.repo_dir)

        _run_worker_capturing_fusion_call(
            worker_input,
            payload_cache,
            "job-1482c",
            monkeypatch,
            _make_sentinel_final_results(),
            query_tracker=query_tracker,
        )

        snapshot = read_temporal_snapshot(payload_cache, "job-1482c")
        assert snapshot is not None
        assert snapshot["terminal"] is True
        assert len(snapshot["results"]) == 1
        assert snapshot["results"][0]["file_path"] == "a.py"

    def test_no_query_tracker_means_no_resolver_byte_identical_to_today(
        self, tmp_path, payload_cache, monkeypatch
    ):
        """Without a query_tracker, resolver construction must stay a
        no-op -- pin() would be a silent no-op anyway, and constructing a
        resolver regardless would reintroduce the mid-read deletion
        hazard AC8 Step 6 guards against."""
        fixture = _build_sister_only_repo(tmp_path, monkeypatch)
        worker_input = _make_worker_input(fixture.repo_dir)

        _, captured_kwargs = _run_worker_capturing_fusion_call(
            worker_input,
            payload_cache,
            "job-1482-no-tracker",
            monkeypatch,
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
