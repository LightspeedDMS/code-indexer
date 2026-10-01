"""Tests for HNSWOrphanRepairSweepScheduler (Story #1360 AC1/AC3/AC4).

Real components throughout:
  - Real hnswlib fork indexes (via HNSWIndexManager.build_index) as sweep
    targets -- no mocking of check_integrity()/repair_orphans().
  - Real HNSWOrphanSweepStateSqliteBackend over a real SQLite file.
  - Real JobTracker + its actual idx_active_job_per_repo partial unique index
    for the cross-worker single-flight dedup test (feedback_faithful_db_mocks:
    the DB gate under test must be the real driver, not an unfaithful mock).

AC1 focus: paced batch per tick, durable stable-key resume, and the
resume-correctness property under candidate-set mutation between ticks
(new items are neither silently skipped nor double-processed within the
same pass).
"""

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import numpy as np
import pytest

from code_indexer.server.cache.hnsw_index_cache import HNSWIndexCacheEntry
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.server.services.job_tracker import (
    DuplicateJobError as TrackerDuplicateJobError,
    JobTracker,
)
from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import (
    HNSWOrphanSweepStateSqliteBackend,
)
from code_indexer.server.services.hnsw_orphan_sweep.discovery import SweepCandidate
from code_indexer.server.services.hnsw_orphan_sweep.scheduler import (
    HNSWOrphanRepairSweepScheduler,
)
from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
    SweepOutcome,
)
from tests.utils.hnsw_orphan_corpus import build_hnsw_index, near_tie_corpus


CORPUS_DIM = 8


def _make_clean_collection(collection_path: Path, seed: int, n: int = 6) -> None:
    """Build a real, self-healed (S2) clean HNSW collection via the
    production build path -- check_integrity() will show 0 orphans."""
    collection_path.mkdir(parents=True, exist_ok=True)
    rng = np.random.RandomState(seed)
    vectors = rng.randn(n, CORPUS_DIM).astype(np.float32)
    ids = [f"vec_{i}" for i in range(n)]
    manager = HNSWIndexManager(vector_dim=CORPUS_DIM)
    manager.build_index(collection_path, vectors, ids)


class _FakeGoldenRepoManager:
    """Test double (not a Mock) -- a real, controlled stand-in for the
    minimal golden_repo_manager surface enumerate_sweep_candidates() needs."""

    def __init__(self, repos: Dict[str, Path]):
        self._repos = repos

    def list_golden_repos(self) -> List[Dict[str, str]]:
        return [{"alias": alias} for alias in self._repos]

    def get_actual_repo_path(self, alias: str) -> str:
        return str(self._repos[alias])

    def add_repo(self, alias: str, root: Path) -> None:
        self._repos[alias] = root


class _EmptyActivatedRepoManager:
    def list_all_activated_repositories(self) -> List[Dict[str, Any]]:
        return []


@pytest.fixture
def state_backend(tmp_path: Path):
    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    return HNSWOrphanSweepStateSqliteBackend(db_path)


class _RecordingConfigService:
    def __init__(self, *, enabled: bool = True, batch_size: int = 2):
        self.enabled = enabled
        self.batch_size = batch_size

    def get_config(self):
        cfg = self

        class _Wrapper:
            hnsw_orphan_repair_sweep_config = cfg

        return _Wrapper()


class _RealGateBackgroundJobManager:
    """submit_job() delegates straight into a REAL JobTracker instance --
    exercising the actual idx_active_job_per_repo DB-level gate, not a mock
    of it (feedback_faithful_db_mocks)."""

    def __init__(self, job_tracker: JobTracker):
        self._job_tracker = job_tracker

    def submit_job(
        self,
        operation_type: str,
        func,
        *args,
        submitter_username: str,
        is_admin: bool = False,
        repo_alias=None,
        **kwargs,
    ) -> str:
        job_id = str(uuid.uuid4())
        try:
            self._job_tracker.register_job_if_no_conflict(
                job_id=job_id,
                operation_type=operation_type,
                username=submitter_username,
                repo_alias=repo_alias,
                is_admin=is_admin,
            )
        except TrackerDuplicateJobError as exc:
            raise DuplicateJobError(
                exc.operation_type, exc.repo_alias, exc.existing_job_id
            ) from exc
        # Execute synchronously for test determinism (no real thread pool).
        func(*args, **kwargs)
        return job_id


def _make_scheduler(
    tmp_path: Path,
    golden_repo_manager,
    state_backend,
    *,
    batch_size: int = 2,
    process_fn=None,
) -> HNSWOrphanRepairSweepScheduler:
    kwargs: Dict[str, Any] = dict(
        golden_repo_manager=golden_repo_manager,
        activated_repo_manager=_EmptyActivatedRepoManager(),
        state_backend=state_backend,
        background_job_manager=None,
        config_service=_RecordingConfigService(batch_size=batch_size),
    )
    if process_fn is not None:
        kwargs["process_fn"] = process_fn
    return HNSWOrphanRepairSweepScheduler(**kwargs)


class TestPacedBatchPerTick:
    def test_processes_at_most_batch_size_items(
        self, tmp_path: Path, state_backend
    ) -> None:
        repo_root = tmp_path / "repo"
        for name in ("a", "b", "c", "d"):
            _make_clean_collection(
                repo_root / ".code-indexer" / "index" / name, seed=ord(name)
            )
        golden = _FakeGoldenRepoManager({"myrepo": repo_root})
        scheduler = _make_scheduler(tmp_path, golden, state_backend, batch_size=2)

        result = scheduler._run_tick()

        assert result["processed"] == 2
        state = state_backend.get_state()
        assert state["pass_indexes_checked"] == 2


class TestResumeAcrossCandidateSetMutation:
    def test_no_item_silently_skipped_or_double_processed(
        self, tmp_path: Path, state_backend
    ) -> None:
        repo_root = tmp_path / "repo"
        for name in ("a", "b", "c", "d", "e"):
            _make_clean_collection(
                repo_root / ".code-indexer" / "index" / name, seed=ord(name)
            )
        golden = _FakeGoldenRepoManager({"myrepo": repo_root})

        processed_log: List[str] = []
        from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
            process_candidate as real_process_candidate,
        )

        def spy_process(candidate):
            processed_log.append(candidate.sort_key)
            return real_process_candidate(candidate)

        scheduler = _make_scheduler(
            tmp_path, golden, state_backend, batch_size=2, process_fn=spy_process
        )

        # Tick 1: processes a, b.
        scheduler._run_tick()
        assert [k.split(":")[-1].split("/")[-2] for k in processed_log] == [
            "a",
            "b",
        ]

        # Mutate the candidate set between ticks:
        #  - "f" sorts AFTER everything already known -> must be processed
        #    later in THIS pass (proves new items aren't lost).
        #  - "aa" sorts BEFORE the current cursor ("b") -> must be DEFERRED
        #    to the NEXT pass, not silently dropped nor double-processed
        #    this pass.
        _make_clean_collection(
            repo_root / ".code-indexer" / "index" / "f", seed=ord("f")
        )
        _make_clean_collection(repo_root / ".code-indexer" / "index" / "aa", seed=999)

        # Tick 2: pending (key > cursor "...b...") = c, d, e, f -> processes c, d.
        scheduler._run_tick()
        # Tick 3: pending = e, f -> processes e, f. Nothing left beyond
        # cursor ("aa" always sorts before it) -> pass completes.
        scheduler._run_tick()

        pass1_names = [k.split(":")[-1].split("/")[-2] for k in processed_log]
        assert pass1_names == ["a", "b", "c", "d", "e", "f"]
        assert len(pass1_names) == len(set(pass1_names)), "no double-processing"

        state = state_backend.get_state()
        assert state["pass_id"] == 2, "pass completed and a new pass started"
        assert state["last_completed_key"] is None

        # Next pass picks up "aa" -- proves it was deferred, not lost.
        processed_log.clear()
        scheduler._run_tick()
        pass2_names = [k.split(":")[-1].split("/")[-2] for k in processed_log]
        assert "aa" in pass2_names


class TestPassCompletion:
    def test_complete_pass_called_when_nothing_beyond_cursor(
        self, tmp_path: Path, state_backend
    ) -> None:
        repo_root = tmp_path / "repo"
        _make_clean_collection(repo_root / ".code-indexer" / "index" / "only", seed=1)
        golden = _FakeGoldenRepoManager({"myrepo": repo_root})
        scheduler = _make_scheduler(tmp_path, golden, state_backend, batch_size=5)

        scheduler._run_tick()

        state = state_backend.get_state()
        assert state["pass_id"] == 2
        assert state["last_completed_key"] is None
        assert state["last_full_pass_completed_at"] is not None

    def test_empty_candidate_set_does_not_complete_pass(
        self, tmp_path: Path, state_backend
    ) -> None:
        """Code review finding: a tick that finds NOTHING to check (empty
        fleet -- no golden repos, no activated repos, no collections at all)
        must NOT be treated as a completed pass. Otherwise pass_id/
        last_full_pass_completed_at churn meaninglessly on every idle tick
        instead of only advancing when an actual sweep of real candidates
        completes."""
        golden = _FakeGoldenRepoManager({})
        scheduler = _make_scheduler(tmp_path, golden, state_backend, batch_size=5)

        scheduler._run_tick()
        scheduler._run_tick()
        scheduler._run_tick()

        state = state_backend.get_state()
        assert state["pass_id"] == 1
        assert state["last_full_pass_completed_at"] is None


class TestFailSoftPerItem:
    def test_one_item_raising_does_not_abort_tick(
        self, tmp_path: Path, state_backend
    ) -> None:
        repo_root = tmp_path / "repo"
        # 3 collections with batch_size=2: the pass does NOT complete within
        # this tick, so the pass-scoped counters are still readable
        # afterward (complete_pass() resets them, so this must be verified
        # BEFORE the pass finishes).
        for name in ("a", "b", "c"):
            _make_clean_collection(
                repo_root / ".code-indexer" / "index" / name, seed=ord(name)
            )
        golden = _FakeGoldenRepoManager({"myrepo": repo_root})

        calls = {"n": 0}

        def flaky_process(candidate):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return SweepOutcome.CLEAN

        scheduler = _make_scheduler(
            tmp_path, golden, state_backend, batch_size=2, process_fn=flaky_process
        )

        result = scheduler._run_tick()

        assert result["processed"] == 2
        assert result["error"] == 1
        assert result["clean"] == 1
        state = state_backend.get_state()
        assert state["pass_errors"] == 1
        assert state["pass_id"] == 1, "pass not yet complete (1 item remains)"


class TestSingleFlightAcrossWorkers:
    @pytest.fixture
    def atomic_db_path(self, tmp_path: Path) -> str:
        db = tmp_path / "test_atomic.db"
        with closing(sqlite3.connect(str(db))) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS background_jobs (
                job_id TEXT PRIMARY KEY NOT NULL,
                operation_type TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                result TEXT,
                error TEXT,
                progress INTEGER NOT NULL DEFAULT 0,
                username TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                cancelled INTEGER NOT NULL DEFAULT 0,
                repo_alias TEXT,
                resolution_attempts INTEGER NOT NULL DEFAULT 0,
                claude_actions TEXT,
                failure_reason TEXT,
                extended_error TEXT,
                language_resolution_status TEXT,
                progress_info TEXT,
                metadata TEXT,
                actor_username TEXT
            )"""
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
                ON background_jobs(operation_type, repo_alias)
                WHERE status IN ('pending', 'running')
                  AND repo_alias IS NOT NULL
                """
            )
            conn.commit()
        return str(db)

    def test_second_worker_tick_is_skipped_via_real_db_gate(
        self, tmp_path: Path, state_backend, atomic_db_path: str
    ) -> None:
        """Simulates a second cluster worker's tick firing while the first
        worker's tick job is still 'pending' in the REAL background_jobs
        table -- the second call must be rejected by the actual
        idx_active_job_per_repo unique index (not a mocked check), and
        trigger_now() must return None rather than raising."""
        tracker = JobTracker(atomic_db_path)

        # Simulate "worker 1" already holding the active tick job.
        tracker.register_job_if_no_conflict(
            job_id="worker-1-job",
            operation_type=HNSWOrphanRepairSweepScheduler.OPERATION_TYPE,
            username="system",
            repo_alias="server",
        )

        repo_root = tmp_path / "repo"
        _make_clean_collection(repo_root / ".code-indexer" / "index" / "only", seed=1)
        golden = _FakeGoldenRepoManager({"myrepo": repo_root})

        scheduler = HNSWOrphanRepairSweepScheduler(
            golden_repo_manager=golden,
            activated_repo_manager=_EmptyActivatedRepoManager(),
            state_backend=state_backend,
            background_job_manager=_RealGateBackgroundJobManager(tracker),
            config_service=_RecordingConfigService(batch_size=5),
        )

        result = scheduler.trigger_now()

        assert result is None
        # The sweep must NOT have advanced -- worker 2's tick never ran.
        assert state_backend.get_state()["pass_indexes_checked"] == 0

    def test_trigger_now_runs_when_no_conflict(
        self, tmp_path: Path, state_backend, atomic_db_path: str
    ) -> None:
        tracker = JobTracker(atomic_db_path)
        repo_root = tmp_path / "repo"
        _make_clean_collection(repo_root / ".code-indexer" / "index" / "only", seed=1)
        golden = _FakeGoldenRepoManager({"myrepo": repo_root})

        scheduler = HNSWOrphanRepairSweepScheduler(
            golden_repo_manager=golden,
            activated_repo_manager=_EmptyActivatedRepoManager(),
            state_backend=state_backend,
            background_job_manager=_RealGateBackgroundJobManager(tracker),
            config_service=_RecordingConfigService(batch_size=5),
        )

        result = scheduler.trigger_now()

        assert result is not None
        # With 1 collection and batch_size=5 the pass completes within this
        # same tick, which resets pass_indexes_checked by design -- pass_id
        # advancing to 2 is the durable evidence the tick actually ran.
        assert state_backend.get_state()["pass_id"] == 2


class TestGetStats:
    def test_get_stats_exposes_durable_state(
        self, tmp_path: Path, state_backend
    ) -> None:
        golden = _FakeGoldenRepoManager({})
        scheduler = _make_scheduler(tmp_path, golden, state_backend)

        stats = scheduler.get_stats()

        assert stats["pass_id"] == 1
        assert stats["total_orphans_repaired_lifetime"] == 0


# Bug #1542 Codex-review follow-up fixtures -- module-level helpers so the
# test method itself stays focused on scheduler dispatch + assertions.
_ACTIVATED_FIXTURE_DIM = 1024
_ACTIVATED_FIXTURE_SIZE = 270


def _plant_activated_prebroken_fixture(collection_path: Path) -> None:
    """Plant a genuinely pre-broken, saved-then-loaded .bin fixture at
    *collection_path* -- mirrors test_repair_executor_1360.py's own AC5
    shape-matrix recipe (NOT built via HNSWIndexManager, which self-heals
    per S2)."""
    collection_path.mkdir(parents=True, exist_ok=True)
    vectors = near_tie_corpus(
        size=_ACTIVATED_FIXTURE_SIZE,
        dim=_ACTIVATED_FIXTURE_DIM,
        noise_scale=0.01,
        pocket_fraction=1.0,
        seed=42,
    )
    broken_index = build_hnsw_index(vectors, num_threads=1)
    orphans_before = sum(
        1 for e in broken_index.check_integrity()["errors"] if "orphan" in e
    )
    assert orphans_before > 0, "fixture recipe must start broken"
    broken_index.save_index(str(collection_path / HNSWIndexManager.INDEX_FILENAME))
    id_mapping = {str(i): f"vec_{i}" for i in range(_ACTIVATED_FIXTURE_SIZE)}
    (collection_path / "collection_meta.json").write_text(
        json.dumps(
            {
                "vector_dim": _ACTIVATED_FIXTURE_DIM,
                "hnsw_index": {
                    "vector_count": _ACTIVATED_FIXTURE_SIZE,
                    "vector_dim": _ACTIVATED_FIXTURE_DIM,
                    "space": "cosine",
                    "M": 16,
                    "ef_construction": 200,
                    "id_mapping": id_mapping,
                },
            }
        )
    )


def _seed_cache_entry(cache: Any, key: str) -> None:
    """Directly insert a stub entry under *key* into the REAL global
    HNSWIndexCache, bypassing the loader -- same pattern
    test_invalidate_prefix.py uses."""
    with cache._cache_lock:
        cache._cache[key] = MagicMock(
            spec=HNSWIndexCacheEntry, repo_path=key, is_expired=lambda: False
        )


class _FakeActivatedRepoManagerWithActivationId:
    """Test double exposing exactly the two methods the scheduler +
    ``_resolve_activation_id`` need: enumeration (unused here, empty since
    this test drives ``_process_one`` directly) and ``get_activation_id``."""

    def __init__(self, username: str, user_alias: str, activation_id: str):
        self._username = username
        self._user_alias = user_alias
        self._activation_id = activation_id

    def list_all_activated_repositories(self) -> List[Dict[str, Any]]:
        return []

    def get_activation_id(self, username: str, user_alias: str) -> str:
        assert username == self._username
        assert user_alias == self._user_alias
        return self._activation_id


def _make_activated_candidate(
    repo_root: Path, username: str, user_alias: str
) -> SweepCandidate:
    relpath = ".code-indexer/index/voyage-code-3/hnsw_index.bin"
    return SweepCandidate(
        sort_key=f"activated:{username}/{user_alias}:{relpath}",
        repo_root=repo_root,
        index_relpath=Path(relpath),
        kind="activated",
        alias=f"{username}/{user_alias}",
    )


def _seed_activation_scoped_entry(
    repo_root: Path, collection_path: Path, activation_id: str
) -> str:
    """Seed the REAL global cache under the activation-scoped canonical
    key an activated-repo ``search()`` actually stores under, returning
    that key for later assertion."""
    from code_indexer.server.cache import get_global_cache
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    store = FilesystemVectorStore(base_path=repo_root, activation_id=activation_id)
    canonical_key: str = store.hnsw_cache_key_for_collection(collection_path)
    _seed_cache_entry(get_global_cache(), canonical_key)
    return canonical_key


def _make_activated_scheduler(
    state_backend: Any,
    username: str,
    user_alias: str,
    activation_id: str,
    process_fn: Any = None,
) -> HNSWOrphanRepairSweepScheduler:
    kwargs: Dict[str, Any] = dict(
        golden_repo_manager=_FakeGoldenRepoManager({}),
        activated_repo_manager=_FakeActivatedRepoManagerWithActivationId(
            username, user_alias, activation_id
        ),
        state_backend=state_backend,
        background_job_manager=None,
        config_service=_RecordingConfigService(),
    )
    if process_fn is not None:
        kwargs["process_fn"] = process_fn
    return HNSWOrphanRepairSweepScheduler(**kwargs)


def _make_wrapped_process_candidate() -> Any:
    """A ``functools.wraps``-wrapped ``process_candidate``, deliberately
    NOT identical to the bare function, proving Codex-review Q2's
    marker-based (not identity-based) dispatch fix. ``functools.wraps``
    copies ``__dict__`` (``WRAPPER_UPDATES``), so the plain function
    attribute ``supports_activated_repo_manager`` is inherited."""
    import functools

    from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
        process_candidate,
    )

    @functools.wraps(process_candidate)
    def wrapped(candidate: Any, **kwargs: Any) -> Any:
        return process_candidate(candidate, **kwargs)

    assert wrapped is not process_candidate
    assert getattr(wrapped, "supports_activated_repo_manager", False)
    return wrapped


class TestActivatedRepoCacheInvalidationWiring:
    """Bug #1542 Codex-review follow-up: the scheduler's default dispatch
    (``self._process_fn is`` the real ``process_candidate``) must thread
    ``activated_repo_manager`` through so an activated-repo candidate's
    activation-scoped cache entry (Story #1458 AC11) is correctly evicted
    after repair -- proving the wiring holds end-to-end through the
    scheduler, not just when ``process_candidate()`` is called directly
    (as test_repair_executor_1360.py already proves)."""

    def test_process_one_resolves_activation_id_for_activated_candidate(
        self, tmp_path: Path, state_backend
    ) -> None:
        from code_indexer.server.cache import get_global_cache, reset_global_cache
        from code_indexer.storage.filesystem_vector_store import (
            FilesystemVectorStore,
        )

        reset_global_cache()
        try:
            username = "alice"
            user_alias = "myrepo"
            activation_id = "11111111-2222-3333-4444-555555555555"
            repo_root = tmp_path / "activated" / username / user_alias
            collection_path = repo_root / ".code-indexer" / "index" / "voyage-code-3"
            _plant_activated_prebroken_fixture(collection_path)

            store = FilesystemVectorStore(
                base_path=repo_root, activation_id=activation_id
            )
            canonical_key = store.hnsw_cache_key_for_collection(collection_path)
            cache = get_global_cache()
            _seed_cache_entry(cache, canonical_key)

            scheduler = HNSWOrphanRepairSweepScheduler(
                golden_repo_manager=_FakeGoldenRepoManager({}),
                activated_repo_manager=_FakeActivatedRepoManagerWithActivationId(
                    username, user_alias, activation_id
                ),
                state_backend=state_backend,
                background_job_manager=None,
                config_service=_RecordingConfigService(),
            )
            candidate = _make_activated_candidate(repo_root, username, user_alias)

            outcome = scheduler._process_one(candidate)

            assert outcome == SweepOutcome.REPAIRED
            assert canonical_key not in cache._cache, (
                "scheduler's default dispatch did not thread "
                "activated_repo_manager through to process_candidate() -- "
                "the activation-scoped cache entry was not evicted"
            )
        finally:
            reset_global_cache()

    def test_wrapped_process_candidate_still_gets_activation_id_via_capability_marker(
        self, tmp_path: Path, state_backend
    ) -> None:
        """Codex-review Q2: a functools.wraps-wrapped, non-identical
        callable must still get activated_repo_manager threaded through
        via the inherited capability marker, not an identity check."""
        from code_indexer.server.cache import get_global_cache, reset_global_cache

        wrapped_process_candidate = _make_wrapped_process_candidate()
        reset_global_cache()
        try:
            username, user_alias = "alice", "myrepo"
            activation_id = "22222222-3333-4444-5555-666666666666"
            repo_root = tmp_path / "activated" / username / user_alias
            collection_path = repo_root / ".code-indexer" / "index" / "voyage-code-3"
            _plant_activated_prebroken_fixture(collection_path)
            canonical_key = _seed_activation_scoped_entry(
                repo_root, collection_path, activation_id
            )

            scheduler = _make_activated_scheduler(
                state_backend,
                username,
                user_alias,
                activation_id,
                wrapped_process_candidate,
            )
            candidate = _make_activated_candidate(repo_root, username, user_alias)
            outcome = scheduler._process_one(candidate)

            assert outcome == SweepOutcome.REPAIRED
            assert canonical_key not in get_global_cache()._cache, (
                "wrapped process_candidate did not get activated_repo_manager "
                "threaded through -- dispatch is still identity-based"
            )
        finally:
            reset_global_cache()
