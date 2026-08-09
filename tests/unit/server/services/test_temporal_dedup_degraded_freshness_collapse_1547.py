"""Bug #1547 Finding 2 (RED): a DEGRADED/unknown freshness signal must
never be able to join a dedup entry created under a DIFFERENT freshness
state, while two CONCURRENT identical degraded queries must still dedup
with each other.

Codex's confirmed sequence: `list(_stat_index_fingerprint(...) or [])`
mapped EVERY unstat-able shard to the same `[]`. Query A runs while a
shard stat fails -> signature contains `[shard, []]`; the index is then
refreshed; query B (identical inputs) runs while the stat fails again ->
same `[]` -> same signature -> joins query A's terminal entry -> the
PRE-REFRESH snapshot is served. The whole-signal `return None` fallback
paths (resolution failure, activated_repo_manager is None, a generic
exception) have the same defect.

Required behavior: fail toward recompute, never toward stale re-serve --
but two genuinely concurrent identical degraded requests must still
collapse into ONE job (never trivially-unique, or an outage would
stampede duplicate work).

Written BEFORE the fix -- execute_live_temporal_search has no
freshness_cache parameter yet, so both tests in this module must
genuinely fail against the current (unmodified) code.
"""

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.maintenance_service import (
    _reset_maintenance_state,
    get_maintenance_state,
)
from code_indexer.server.services.temporal_dedup_cache import TemporalDedupCache
from code_indexer.server.services.temporal_snapshot_store import (
    store_temporal_snapshot,
)
from code_indexer.services.temporal.temporal_worker_input import TemporalWorkerInput

#: Realistic physical shard directory name -- deliberately created WITHOUT
#: an hnsw_index.bin file, so _stat_index_fingerprint always fails for it
#: (a persistent, recurring stat failure, matching Codex's "the stat fails
#: again" scenario).
_SHARD_DIR_NAME = "code-indexer-temporal-voyage_code_3-2024Q1"
_GOLDEN_REPO_BARE_ALIAS = "my-repo"
_GOLDEN_REPO_GLOBAL_ALIAS = f"{_GOLDEN_REPO_BARE_ALIAS}-global"

#: A generous inline-wait budget so the fast fake worker's terminal
#: snapshot is always observed within a single dispatch call.
_INLINE_WAIT_SECONDS = 5.0

#: Reserve budget subtracted from any handler deadline (unused here since
#: handler_deadline_monotonic is None, but required by the dispatch
#: contract).
_RESPONSE_RESERVE_SECONDS = 1.0

#: BackgroundJobManager's temporal lane concurrency for these tests.
_TEMPORAL_LANE_CONCURRENCY = 2

#: Property (i) test: forces a FRESH recompute (and a new generation) on
#: every call, isolating Finding 2's generation-tagging behavior from
#: Finding 1's rate-limiting.
_ALWAYS_RECOMPUTE_INTERVAL_SECONDS = 0.0

#: Expected worker-run counts: property (i) must resubmit (2 separate
#: jobs); property (ii) must dedup (1 shared job).
_EXPECTED_RUNS_WHEN_RESUBMITTED = 2
_EXPECTED_RUNS_WHEN_DEDUPED = 1


@pytest.fixture(autouse=True)
def _clean_maintenance_state():
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
        background_jobs_config=BackgroundJobsConfig(
            temporal_lane_concurrency=_TEMPORAL_LANE_CONCURRENCY
        ),
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


class _FakeActivatedRepoManagerForDegraded:
    """Mirrors the identical helper class in
    test_temporal_dedup_freshness_1547.py: exposes exactly what
    _resolve_golden_temporal_context needs (activated_repos_dir) to
    resolve a -global alias's fixed temporal root."""

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
        query_text="degraded freshness query 1547",
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
                "ctx": {"requested_limit": worker_input.requested_limit},
            },
            terminal=True,
        )
        return {"result_ready": True}

    return _worker


def _make_degraded_shard(tmp_path):
    """A shard directory that exists (discoverable by
    list_temporal_shard_dirs_under_fixed_root) but has NO hnsw_index.bin --
    _stat_index_fingerprint always fails for it (FileNotFoundError),
    reproducing Codex's persistent-stat-failure scenario. Returns the
    activated_repos_dir to construct the fake manager from."""
    data_dir = tmp_path / "data"
    activated_repos_dir = data_dir / "activated-repos"
    golden_repos_dir = data_dir / "golden-repos"
    activated_repos_dir.mkdir(parents=True)
    shard_dir = (
        golden_repos_dir / ".temporal" / _GOLDEN_REPO_BARE_ALIAS / _SHARD_DIR_NAME
    )
    shard_dir.mkdir(parents=True)
    return activated_repos_dir


class TestDegradedFreshnessSignalNeverJoinsPreRefreshEntry:
    """Property (i): a degraded query must never join a dedup entry
    created under a DIFFERENT (earlier) freshness state -- proven here by
    forcing a stat failure that PERSISTS across recompute passes
    (min_recheck_interval_seconds=0.0) and confirming the two dispatches
    submit TWO SEPARATE jobs rather than being silently joined via a
    collapsed `[]`/None marker."""

    def test_persistent_stat_failure_across_recompute_passes_does_not_dedup(
        self, tmp_path, payload_cache, bgm
    ):
        from code_indexer.server.services.temporal_live_dispatch import (
            execute_live_temporal_search,
        )
        from code_indexer.server.services.temporal_freshness_cache import (
            TemporalFreshnessSignalCache,
        )

        activated_repos_dir = _make_degraded_shard(tmp_path)
        manager = _FakeActivatedRepoManagerForDegraded(str(activated_repos_dir))

        dedup_cache = TemporalDedupCache()
        freshness_cache = TemporalFreshnessSignalCache(
            min_recheck_interval_seconds=_ALWAYS_RECOMPUTE_INTERVAL_SECONDS
        )
        worker_input = _make_worker_input(tmp_path)
        submit_count = {"n": 0}

        kwargs = dict(
            background_job_manager=bgm,
            payload_cache=payload_cache,
            access_filtering_service=_FakeAccessFilteringService(),
            is_admin=False,
            inline_wait_seconds=_INLINE_WAIT_SECONDS,
            handler_deadline_monotonic=None,
            response_reserve_seconds=_RESPONSE_RESERVE_SECONDS,
            dedup_cache=dedup_cache,
            freshness_cache=freshness_cache,
            worker_fn=_make_counting_worker(payload_cache, submit_count),
            activated_repo_manager=manager,
        )

        result1 = execute_live_temporal_search(worker_input=worker_input, **kwargs)
        assert result1["status"] == "completed"
        assert submit_count["n"] == 1

        result2 = execute_live_temporal_search(worker_input=worker_input, **kwargs)

        assert submit_count["n"] == _EXPECTED_RUNS_WHEN_RESUBMITTED, (
            "Bug #1547 Finding 2: a degraded (unstat-able) freshness "
            "signal must never collapse to a constant sentinel that lets "
            "a LATER recompute pass join an EARLIER one's dedup entry -- "
            f"worker ran {submit_count['n']} time(s), job_ids "
            f"{result1['job_id']!r} / {result2['job_id']!r}"
        )
        assert result1["job_id"] != result2["job_id"]


class TestConcurrentDegradedQueriesStillDedup:
    """Property (ii): two CONCURRENT (here: rapid, sequential-but-within-
    the-recheck-interval) identical degraded queries must still join the
    SAME job -- the generation-tagging fix must not defeat dedup for
    genuinely simultaneous identical requests."""

    def test_two_rapid_degraded_queries_join_the_same_job(
        self, tmp_path, payload_cache, bgm
    ):
        from code_indexer.server.services.temporal_live_dispatch import (
            execute_live_temporal_search,
        )
        from code_indexer.server.services.temporal_freshness_cache import (
            TemporalFreshnessSignalCache,
        )

        activated_repos_dir = _make_degraded_shard(tmp_path)
        manager = _FakeActivatedRepoManagerForDegraded(str(activated_repos_dir))

        dedup_cache = TemporalDedupCache()
        # Default (nonzero) interval: both calls happen well within it, so
        # they must share the SAME cached (degraded) freshness signal.
        freshness_cache = TemporalFreshnessSignalCache()
        worker_input = _make_worker_input(tmp_path, query_text="concurrent degraded")
        submit_count = {"n": 0}

        kwargs = dict(
            background_job_manager=bgm,
            payload_cache=payload_cache,
            access_filtering_service=_FakeAccessFilteringService(),
            is_admin=False,
            inline_wait_seconds=_INLINE_WAIT_SECONDS,
            handler_deadline_monotonic=None,
            response_reserve_seconds=_RESPONSE_RESERVE_SECONDS,
            dedup_cache=dedup_cache,
            freshness_cache=freshness_cache,
            worker_fn=_make_counting_worker(payload_cache, submit_count),
            activated_repo_manager=manager,
        )

        result1 = execute_live_temporal_search(worker_input=worker_input, **kwargs)
        result2 = execute_live_temporal_search(worker_input=worker_input, **kwargs)

        assert result1["job_id"] == result2["job_id"], (
            "Bug #1547 Finding 2: two rapid identical degraded queries "
            "must still dedup into the SAME job -- generation-tagging "
            "must not defeat dedup for genuinely concurrent requests"
        )
        assert submit_count["n"] == _EXPECTED_RUNS_WHEN_DEDUPED
