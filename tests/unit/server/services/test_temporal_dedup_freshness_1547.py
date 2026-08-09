"""Bug #1547 defect 1 (RED): TemporalDedupCache re-serves a COMPLETED prior
job's stored result snapshot even after the golden repo's temporal index has
been refreshed (a new commit indexed), because its signature key
(_worker_input_signature_dict) carries no index-freshness information.

Reproduces the confirmed clustered-staging symptom locally (single node):
a never-before-used query text returns fresh data because the HNSW index on
disk IS current, but a query text WARMED before a refresh keeps returning the
pre-refresh dedup'd result for as long as the dedup entry's terminal TTL has
not lapsed -- because the SAME dedup signature is computed both before and
after the refresh.

Written BEFORE the fix -- this must genuinely fail against the unmodified
execute_live_temporal_search (the dedup signature is a pure function of
TemporalWorkerInput alone; nothing about the on-disk index state is
consulted, so a refresh cannot change the signature and the terminal entry is
simply re-served for the whole terminal TTL window).
"""

import os

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.maintenance_service import (
    _reset_maintenance_state,
    get_maintenance_state,
)
from code_indexer.server.services.temporal_dedup_cache import TemporalDedupCache
from code_indexer.server.services.temporal_freshness_cache import (
    TemporalFreshnessSignalCache,
)
from code_indexer.server.services.temporal_snapshot_store import (
    store_temporal_snapshot,
)
from code_indexer.services.temporal.temporal_worker_input import TemporalWorkerInput

#: Realistic physical shard directory name (embedder slug + quarter suffix) --
#: see temporal_collection_naming.parse_physical_temporal_name.
_SHARD_DIR_NAME = "code-indexer-temporal-voyage_code_3-2024Q1"
_GOLDEN_REPO_BARE_ALIAS = "my-repo"
_GOLDEN_REPO_GLOBAL_ALIAS = f"{_GOLDEN_REPO_BARE_ALIAS}-global"

_PRE_REFRESH_HNSW_BYTES = b"pre-refresh-graph-bytes"
_POST_REFRESH_HNSW_BYTES = b"post-refresh-graph-bytes-with-new-commit"


@pytest.fixture(autouse=True)
def _clean_maintenance_state():
    """Mirrors test_temporal_live_dispatch_1400.py's fixture -- guarantees
    the process-wide MaintenanceState singleton never leaks across tests in
    the same pytest process."""
    _reset_maintenance_state()
    yield
    get_maintenance_state().exit_maintenance_mode()
    _reset_maintenance_state()


@pytest.fixture
def payload_cache(tmp_path):
    db_path = tmp_path / "payload_cache.db"
    cache = PayloadCache(db_path=db_path, config=PayloadCacheConfig())
    cache.initialize()
    yield cache
    cache.close()


@pytest.fixture
def bgm(tmp_path):
    from code_indexer.server.utils.config_manager import BackgroundJobsConfig

    manager = BackgroundJobManager(
        storage_path=str(tmp_path / "jobs.json"),
        background_jobs_config=BackgroundJobsConfig(temporal_lane_concurrency=2),
    )
    yield manager
    manager.shutdown()


class _FakeAccessFilteringService:
    def __init__(self, admins=None):
        self._admins = admins or {"admin"}

    def is_admin_user(self, user_id):
        return user_id in self._admins

    def filter_query_results(self, results, user_id):
        return results


class _FakeActivatedRepoManagerForFreshness:
    """Minimal real-shape stand-in exposing exactly what
    _resolve_golden_temporal_context needs to resolve a -global alias's
    fixed temporal root: activated_repos_dir alone (get_repository must
    NEVER be called for a -global alias -- it is its own golden alias)."""

    def __init__(self, activated_repos_dir: str) -> None:
        self.activated_repos_dir = activated_repos_dir

    def get_repository(self, username, repository_alias, touch=False):
        raise AssertionError(
            "get_repository must not be called for a -global repository_alias"
        )

    def uses_shared_metadata_stores(self) -> bool:
        return True


def _make_worker_input(tmp_path, **overrides) -> TemporalWorkerInput:
    base = dict(
        repo_path=str(tmp_path / "repo"),
        repository_alias=_GOLDEN_REPO_GLOBAL_ALIAS,
        username="alice",
        query_text="never before used query 1547",
        requested_limit=10,
        fusion_fetch_limit=30,
        time_range=("2024-01-01", "2024-12-31"),
        time_range_raw=None,
        time_range_all=False,
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


def _make_counting_worker(payload_cache: PayloadCache, submit_count: dict):
    """A fast fake worker (matches run_temporal_worker's call contract)
    that records how many times it actually ran and writes a real, verified
    terminal snapshot via store_temporal_snapshot -- distinguishing a fresh
    submission from a dedup join."""

    def _worker(
        worker_input,
        payload_cache,
        job_id,
        progress_callback=None,
        cancel_check=None,
        query_tracker=None,
        activated_repo_manager=None,
    ):
        submit_count["n"] += 1
        store_temporal_snapshot(
            payload_cache,
            job_id,
            {
                "results": [{"file_path": f"result-{submit_count['n']}.py"}],
                "shards_completed": 1,
                "shards_total": 1,
                "ctx": {
                    "requested_limit": worker_input.requested_limit,
                    "rerank_query": worker_input.rerank_query,
                    "rerank_instruction": worker_input.rerank_instruction,
                },
            },
            terminal=True,
        )
        return {"result_ready": True}

    return _worker


class TestFreshnessInvalidatesStaleTemporalDedupEntry:
    def test_repo_refresh_between_queries_does_not_reserve_stale_dedup_snapshot(
        self, tmp_path, payload_cache, bgm
    ):
        from code_indexer.server.services.temporal_live_dispatch import (
            execute_live_temporal_search,
        )

        data_dir = tmp_path / "data"
        activated_repos_dir = data_dir / "activated-repos"
        golden_repos_dir = data_dir / "golden-repos"
        activated_repos_dir.mkdir(parents=True)

        shard_dir = (
            golden_repos_dir / ".temporal" / _GOLDEN_REPO_BARE_ALIAS / _SHARD_DIR_NAME
        )
        shard_dir.mkdir(parents=True)
        hnsw_file = shard_dir / "hnsw_index.bin"
        hnsw_file.write_bytes(_PRE_REFRESH_HNSW_BYTES)

        manager = _FakeActivatedRepoManagerForFreshness(str(activated_repos_dir))

        dedup_cache = TemporalDedupCache()
        # Bug #1547 Finding 1: the default freshness_cache singleton now
        # rate-limits recomputation (min_recheck_interval_seconds=2.0 by
        # default), which would otherwise serve the PRE-refresh cached
        # signal for the second call below (issued with no sleep in
        # between). This test's purpose is to prove the underlying
        # fingerprint-based signal reacts to a real file replacement, a
        # concern INDEPENDENT of Finding 1's caching -- so recomputation is
        # forced on every call here via an interval of 0.0.
        freshness_cache = TemporalFreshnessSignalCache(min_recheck_interval_seconds=0.0)
        worker_input = _make_worker_input(tmp_path)
        submit_count = {"n": 0}

        kwargs = dict(
            background_job_manager=bgm,
            payload_cache=payload_cache,
            access_filtering_service=_FakeAccessFilteringService(),
            is_admin=False,
            inline_wait_seconds=5.0,
            handler_deadline_monotonic=None,
            response_reserve_seconds=1.0,
            dedup_cache=dedup_cache,
            freshness_cache=freshness_cache,
            worker_fn=_make_counting_worker(payload_cache, submit_count),
            activated_repo_manager=manager,
        )

        result1 = execute_live_temporal_search(worker_input=worker_input, **kwargs)
        assert result1["status"] == "completed"
        assert submit_count["n"] == 1

        # Simulate a temporal refresh: hnsw_index.bin is atomically replaced
        # with a NEW file (different inode) via the same os.replace-based
        # atomic-rename publish pattern HNSWIndexManager/
        # BackgroundIndexRebuilder use in production (Bug #1538).
        new_hnsw_file = shard_dir / "hnsw_index.bin.new"
        new_hnsw_file.write_bytes(_POST_REFRESH_HNSW_BYTES)
        os.replace(str(new_hnsw_file), str(hnsw_file))

        result2 = execute_live_temporal_search(worker_input=worker_input, **kwargs)

        assert submit_count["n"] == 2, (
            "Bug #1547: an identical query issued after a temporal refresh "
            "changed the shard's hnsw_index.bin must NOT re-serve the "
            f"pre-refresh terminal dedup entry -- got job_id "
            f"{result1['job_id']!r} then {result2['job_id']!r}, worker ran "
            f"{submit_count['n']} time(s)"
        )
        assert result1["job_id"] != result2["job_id"]
        assert result2["results"] == [{"file_path": "result-2.py"}]
